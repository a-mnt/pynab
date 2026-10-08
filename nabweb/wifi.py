"""
Wi-Fi of the rabbit.

Two systems are found on rabbits:
- NetworkManager (nmcli), used with comitup and on recent Raspberry Pi OS;
- wpa_supplicant (wpa_cli) with dhcpcd, the classic Raspberry Pi OS setup.
The one actually running is used. When joining a new network fails, the
previous one is brought back at once.
"""

import binascii
import hashlib
import re

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
    return bool(shutil.which("nmcli") or shutil.which("wpa_cli"))


def _backend():
    """'nm' (NetworkManager), 'wpa' (wpa_supplicant) or None."""
    if shutil.which("nmcli"):
        _device, state, _name = _wifi_device()
        if state not in ("no-networkmanager", "unmanaged"):
            return "nm"
    if shutil.which("wpa_cli") and _wpa("ping")[1].strip() == "PONG":
        return "wpa"
    return None


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
    backend = _backend()
    if backend == "nm":
        return _list_networks(rescan=True)
    if backend == "wpa":
        return _wpa_list_networks(rescan=True)
    return None


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
    backend = _backend()
    if backend == "wpa":
        return _wpa_status()
    if backend is None:
        # Neither NetworkManager nor wpa_supplicant answers.
        return {
            "connected": False,
            "ssid": None,
            "bars": 0,
            "address": None,
            "hotspot": False,
            "managed": False,
            "state": "no-wifi-manager",
        }
    return _nm_status()


def _nm_status():
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
    target = _wpa_connect if _backend() == "wpa" else _connect
    thread = threading.Thread(
        target=target, args=(ssid, password, hidden, security), daemon=True
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


# --------------------------------------------------------------------------
# wpa_supplicant (wpa_cli), with dhcpcd giving the address.
# --------------------------------------------------------------------------

WPA_WAIT = 40  # seconds given to wpa_supplicant to join a network


def _wpa_iface():
    if IFACE:
        return IFACE
    try:
        for name in sorted(os.listdir("/sys/class/net")):
            if os.path.isdir(os.path.join("/sys/class/net", name, "wireless")):
                return name
    except OSError:
        pass
    return "wlan0"


def _wpa(*args, timeout=10):
    try:
        proc = subprocess.run(
            ["wpa_cli", "-i", _wpa_iface(), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=dict(os.environ, LC_ALL="C"),
            check=False,
        )
    except subprocess.TimeoutExpired:
        return 124, ""
    except OSError:
        return 127, ""
    return proc.returncode, proc.stdout


def _wpa_ok(*args):
    return _wpa(*args)[1].strip() == "OK"


def decode_wpa_ssid(text):
    r"""Undo the escaping of wpa_cli: \\, \" and \xNN."""
    raw = bytearray()
    index = 0
    while index < len(text):
        char = text[index]
        if char == "\\" and index + 1 < len(text):
            following = text[index + 1]
            if following == "x" and index + 3 < len(text):
                try:
                    raw.append(int(text[index + 2 : index + 4], 16))
                    index += 4
                    continue
                except ValueError:
                    pass
            raw += following.encode("utf8")
            index += 2
            continue
        raw += char.encode("utf8")
        index += 1
    return raw.decode("utf8", errors="replace")


def _dbm_bars(dbm):
    if dbm >= -55:
        return 4
    if dbm >= -67:
        return 3
    if dbm >= -75:
        return 2
    return 1


def _wpa_state():
    _code, out = _wpa("status")
    values = {}
    for line in out.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    return values


def _wpa_security(flags):
    if "EAP" in flags:
        return "WPA2-Enterprise"
    if "SAE" in flags and "PSK" not in flags:
        return "WPA3"
    if "WPA2" in flags or "RSN" in flags:
        return "WPA2"
    if "WPA" in flags:
        return "WPA1"
    if "WEP" in flags:
        return "WEP"
    return ""


def _wpa_list_networks(rescan):
    if rescan:
        _wpa("scan")
        time.sleep(4)  # scanning takes a few seconds
    code, out = _wpa("scan_results")
    if code != 0:
        return None
    current = decode_wpa_ssid(_wpa_state().get("ssid", ""))
    best = {}
    for line in out.splitlines()[1:]:
        fields = line.split("\t")
        if len(fields) < 5:
            continue
        ssid = decode_wpa_ssid(fields[4])
        if not ssid or ssid.strip("\x00") == "":
            continue
        try:
            dbm = int(fields[2])
        except ValueError:
            dbm = -90
        security = _wpa_security(fields[3])
        network = {
            "ssid": ssid,
            "signal": max(0, min(100, 2 * (dbm + 100))),
            "bars": _dbm_bars(dbm),
            "secure": security != "",
            "security": security,
            "current": ssid == current,
        }
        known = best.get(ssid)
        if known is None or network["signal"] > known["signal"]:
            best[ssid] = network
    return sorted(
        best.values(), key=lambda n: (not n["current"], -n["signal"])
    )


def _ip_address(iface):
    try:
        proc = subprocess.run(
            ["ip", "-4", "-o", "addr", "show", "dev", iface],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", proc.stdout)
    return match.group(1) if match else None


def _wpa_status():
    state = _wpa_state()
    result = {
        "connected": False,
        "ssid": None,
        "bars": 0,
        "address": None,
        "hotspot": False,
        "managed": True,
    }
    if state.get("wpa_state") != "COMPLETED":
        return result
    result["connected"] = True
    result["ssid"] = decode_wpa_ssid(state.get("ssid", ""))
    result["address"] = state.get("ip_address") or _ip_address(_wpa_iface())
    _code, out = _wpa("signal_poll")
    match = re.search(r"RSSI=(-?\d+)", out)
    result["bars"] = _dbm_bars(int(match.group(1))) if match else 2
    return result


def _wpa_networks():
    """Saved networks: [(id, ssid, current)]."""
    _code, out = _wpa("list_networks")
    saved = []
    for line in out.splitlines()[1:]:
        fields = line.split("\t")
        if len(fields) >= 2 and fields[0].isdigit():
            flags = fields[3] if len(fields) > 3 else ""
            saved.append((fields[0], decode_wpa_ssid(fields[1]), "CURRENT" in flags))
    return saved


def _wpa_connect(ssid, password, hidden, security):
    new_id = None
    try:
        previous = next((nid for nid, _s, current in _wpa_networks() if current), None)
        _code, out = _wpa("add_network")
        new_id = out.strip().splitlines()[-1] if out.strip() else ""
        if not new_id.isdigit():
            new_id = None
            raise RuntimeError("add_network")
        ssid_hex = binascii.hexlify(ssid.encode("utf8")).decode("ascii")
        settings_list = [("ssid", ssid_hex), ("priority", PRIORITY)]
        if hidden:
            settings_list.append(("scan_ssid", "1"))
        if not password:
            settings_list.append(("key_mgmt", "NONE"))
        elif _key_mgmt(security) == "sae":
            settings_list += [
                ("key_mgmt", "SAE"),
                ("ieee80211w", "2"),
                ("sae_password", '"' + password + '"'),
            ]
        else:
            # The key itself, not the password: nothing readable is saved.
            psk = hashlib.pbkdf2_hmac(
                "sha1", password.encode("utf8"), ssid.encode("utf8"), 4096, 32
            ).hex()
            settings_list += [("key_mgmt", "WPA-PSK"), ("psk", psk)]
        for key, value in settings_list:
            if not _wpa_ok("set_network", new_id, key, value):
                raise RuntimeError(key)
        _wpa("select_network", new_id)

        seen = set()
        deadline = time.monotonic() + WPA_WAIT
        joined = False
        while time.monotonic() < deadline:
            time.sleep(1)
            state = _wpa_state()
            seen.add(state.get("wpa_state"))
            if (
                state.get("wpa_state") == "COMPLETED"
                and state.get("id") == new_id
            ):
                joined = True
                break

        if joined:
            # Keep the other saved networks as fallbacks, forget older
            # entries of the same network, and save for the next start.
            for nid, saved_ssid, _current in _wpa_networks():
                if saved_ssid == ssid and nid != new_id:
                    _wpa("remove_network", nid)
            _wpa("enable_network", "all")
            saved = _wpa_ok("save_config")
            for _second in range(15):  # address given by dhcpcd
                if _ip_address(_wpa_iface()):
                    break
                time.sleep(1)
            _job.update({"state": "connected", "saved": saved})
            return

        if "4WAY_HANDSHAKE" in seen:
            reason = "wrong-password"
        elif not any(
            n["ssid"] == ssid for n in (_wpa_list_networks(False) or [])
        ) and not hidden:
            reason = "not-found"
        else:
            reason = "failed"
        _wpa_restore(new_id, previous)
        _job.update({"state": "failed", "reason": reason})
    except Exception:
        _wpa_restore(new_id, None)
        _job.update({"state": "failed", "reason": "failed"})
    finally:
        _lock.release()


def _wpa_restore(new_id, previous):
    if new_id is not None:
        _wpa("remove_network", new_id)
    if previous is not None:
        _wpa("select_network", previous)
    _wpa("enable_network", "all")
