"""
Wi-Fi of the rabbit, through NetworkManager (nmcli).

Comitup already relies on NetworkManager: it shows the red configuration
hotspot when no known network answers. Using the same tool keeps both in
agreement. When joining a new network fails, the previous one is brought
back at once; comitup remains the last resort.
"""

import os
import shutil
import socket
import subprocess
import threading
import time
from urllib.request import Request, urlopen

from django.conf import settings

# Wi-Fi interface: found automatically (usually wlan0) unless set here.
IFACE = getattr(settings, "NABWEB_WIFI_IFACE", None)
INTERNET_TEST_URL = getattr(
    settings,
    "NABWEB_INTERNET_TEST_URL",
    "http://connectivitycheck.gstatic.com/generate_204",
)
CONNECT_WAIT = 45  # seconds given to NetworkManager to join a network
PRIORITY = "10"  # networks chosen here are preferred at the next start

_lock = threading.Lock()
_job = {"state": "idle"}


def available():
    return shutil.which("nmcli") is not None


WIFI_TYPES = ("wifi", "802-11-wireless")


def _wifi_device():
    """
    (interface, state, connection) of the Wi-Fi interface, as NetworkManager
    reports it, e.g. ("wlan0", "connected", "preconfigured").
    """
    code, out, _err = _nmcli(
        "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device", "status"
    )
    if code != 0:
        # Typically "NetworkManager is not running".
        return IFACE or "wlan0", "no-networkmanager", ""
    if code == 0:
        for line in out.splitlines():
            fields = split_terse(line)
            if len(fields) < 4 or fields[1] not in WIFI_TYPES:
                continue
            if IFACE and fields[0] != IFACE:
                continue
            return fields[0], fields[2], fields[3]
    return IFACE or "wlan0", "unavailable", ""


def _iface():
    return _wifi_device()[0]


def _nmcli(*args, timeout=20):
    env = dict(os.environ, LC_ALL="C")
    try:
        proc = subprocess.run(
            ["nmcli", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except OSError as err:
        return 127, "", str(err)
    return proc.returncode, proc.stdout, proc.stderr.strip()


def split_terse(line):
    """Split a line of `nmcli -t` output: ':' separates, '\\' escapes."""
    fields, current, escaped = [], [], False
    for char in line:
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == ":":
            fields.append("".join(current))
            current = []
        else:
            current.append(char)
    fields.append("".join(current))
    return fields


def _bars(signal):
    """Signal 0-100 as 1 to 4 bars."""
    if signal >= 75:
        return 4
    if signal >= 50:
        return 3
    if signal >= 25:
        return 2
    return 1


def _list_networks(rescan):
    code, out, _err = _nmcli(
        "-t",
        "-f",
        "IN-USE,SSID,SIGNAL,SECURITY",
        "device",
        "wifi",
        "list",
        "ifname",
        _iface(),
        "--rescan",
        "yes" if rescan else "no",
        timeout=30,
    )
    if code != 0:
        return None
    best = {}
    for line in out.splitlines():
        fields = split_terse(line)
        if len(fields) < 4:
            continue
        in_use, ssid, signal, security = fields[:4]
        if not ssid:
            continue  # hidden network: joined by name
        try:
            signal = int(signal)
        except ValueError:
            signal = 0
        network = {
            "ssid": ssid,
            "signal": signal,
            "bars": _bars(signal),
            "secure": bool(security.strip() and security.strip() != "--"),
            "security": security.strip(),
            "current": in_use.strip() == "*",
        }
        known = best.get(ssid)
        if (
            known is None
            or network["current"]
            or (signal > known["signal"] and not known["current"])
        ):
            best[ssid] = network
    return sorted(
        best.values(), key=lambda n: (not n["current"], -n["signal"])
    )


def scan():
    return _list_networks(rescan=True)


def _active_connection():
    """Name and mode ("infrastructure", "ap") of the Wi-Fi connection in use."""
    _device, state, name = _wifi_device()
    # "connected", "connected (externally)", "connected (site only)"...
    if not state.startswith("connected") or not name or name == "--":
        return None, None
    _code, mode, _err = _nmcli(
        "-t", "-g", "802-11-wireless.mode", "connection", "show", name
    )
    return name, mode.strip()


def status():
    """Network in use, its signal and the address of the rabbit."""
    result = {
        "connected": False,
        "ssid": None,
        "bars": 0,
        "address": None,
        "hotspot": False,
        "managed": True,
    }
    _device, state, _name = _wifi_device()
    if state in ("no-networkmanager", "unmanaged", "unavailable"):
        # Wi-Fi not handled by NetworkManager: this page cannot change it.
        result["managed"] = False
        result["state"] = state
        return result
    name, mode = _active_connection()
    if name is None:
        return result
    result["hotspot"] = mode == "ap"
    code, out, _err = _nmcli(
        "-t", "-f", "IP4.ADDRESS", "device", "show", _iface()
    )
    if code == 0:
        for line in out.splitlines():
            fields = split_terse(line)
            if len(fields) >= 2 and fields[0].startswith("IP4.ADDRESS"):
                result["address"] = fields[1].split("/")[0]
                break
    result["connected"] = True
    networks = _list_networks(rescan=False) or []
    current = next((n for n in networks if n["current"]), None)
    if current:
        result["ssid"] = current["ssid"]
        result["bars"] = current["bars"]
    else:
        _code, ssid, _err = _nmcli(
            "-t", "-g", "802-11-wireless.ssid", "connection", "show", name
        )
        result["ssid"] = ssid.strip() or name
    return result


def internet_test():
    """Is Internet reachable from the rabbit? Time of the answer in ms."""
    started = time.monotonic()
    try:
        request = Request(INTERNET_TEST_URL, headers={"User-Agent": "pynab"})
        with urlopen(request, timeout=6) as response:
            ok = response.status in (200, 204)
    except Exception:
        ok = False
    elapsed = round((time.monotonic() - started) * 1000)
    if ok:
        return {"ok": True, "ms": elapsed}
    try:
        socket.getaddrinfo("pool.ntp.org", 80)
        reason = "no-answer"
    except OSError:
        reason = "no-dns"
    return {"ok": False, "reason": reason}


def job():
    return dict(_job)


def _key_mgmt(security):
    security = security or ""
    if "WPA3" in security and "WPA2" not in security and "WPA1" not in security:
        return "sae"
    return "wpa-psk"


def _profile_exists(name):
    code, out, _err = _nmcli("-t", "-f", "NAME", "connection", "show")
    if code != 0:
        return False
    return any(split_terse(line)[0] == name for line in out.splitlines())


def _failure_message(err):
    text = err.lower()
    if "secrets were required" in text or "802-1x" in text or "psk" in text:
        return "wrong-password"
    if "no network with ssid" in text or "not found" in text:
        return "not-found"
    if "timeout" in text:
        return "timeout"
    return "failed"


def connect(ssid, password, hidden=False, security=""):
    """
    Join a network in the background. Returns False if a change is
    already in progress. Follow it with job().
    """
    if not _lock.acquire(blocking=False):
        return False
    _job.clear()
    _job.update({"state": "connecting", "ssid": ssid})
    thread = threading.Thread(
        target=_connect, args=(ssid, password, hidden, security), daemon=True
    )
    thread.start()
    return True


def _connect(ssid, password, hidden, security):
    try:
        iface = _iface()
        previous, _mode = _active_connection()
        name = ssid
        existed = _profile_exists(name)
        old_secret = None
        settings_args = [
            "connection.autoconnect",
            "yes",
            "connection.autoconnect-priority",
            PRIORITY,
            "802-11-wireless.hidden",
            "yes" if hidden else "no",
        ]
        if password:
            settings_args += [
                "wifi-sec.key-mgmt",
                _key_mgmt(security),
                "wifi-sec.psk",
                password,
            ]
        if existed:
            _code, old_secret, _err = _nmcli(
                "-s",
                "-t",
                "-g",
                "802-11-wireless-security.psk",
                "connection",
                "show",
                name,
            )
            old_secret = old_secret.strip()
            code, _out, err = _nmcli(
                "connection", "modify", name, "802-11-wireless.ssid", ssid,
                *settings_args
            )
        else:
            code, _out, err = _nmcli(
                "connection", "add", "type", "wifi", "ifname", iface,
                "con-name", name, "ssid", ssid, *settings_args
            )
        if code == 0:
            code, _out, err = _nmcli(
                "--wait",
                str(CONNECT_WAIT),
                "connection",
                "up",
                "id",
                name,
                "ifname",
                iface,
                timeout=CONNECT_WAIT + 15,
            )
        if code == 0:
            _job.update({"state": "connected"})
            return

        # Failed: forget what was changed and go back to the previous network.
        if not existed:
            _nmcli("connection", "delete", "id", name)
        elif old_secret:
            _nmcli("connection", "modify", name, "wifi-sec.psk", old_secret)
        if previous and previous != name:
            _nmcli(
                "--wait",
                str(CONNECT_WAIT),
                "connection",
                "up",
                "id",
                previous,
                timeout=CONNECT_WAIT + 15,
            )
        _job.update({"state": "failed", "reason": _failure_message(err)})
    except Exception:
        _job.update({"state": "failed", "reason": "failed"})
    finally:
        _lock.release()
