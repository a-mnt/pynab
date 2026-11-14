"""
Mode analyse : relevés périodiques de l'état du lapin (nab-monitor.sh),
pour comprendre ce qui s'accumule avant un freeze.

Le relevé est fait par cron, indépendamment de nabweb et de nabd, pour
continuer à enregistrer même si l'un d'eux se bloque. Activer le mode
analyse crée /etc/cron.d/nab-monitor ; le désactiver le supprime (les
mesures déjà prises sont conservées).
"""
import os
import subprocess
from datetime import datetime

SCRIPT = os.path.join(os.path.dirname(__file__), "monitor", "nab-monitor.sh")
CRON_FILE = "/etc/cron.d/nab-monitor"
DATA_FILE = "/var/lib/nab-monitor/nab-monitor.csv"
JOURNAL_DIR = "/var/log/journal"
INTERVAL_MINUTES = 10
MAX_POINTS = 300

# Bits de `vcgencmd get_throttled` (survenus depuis le démarrage)
THROTTLED_FLAGS = (
    (0x10000, "under_voltage"),
    (0x20000, "freq_capped"),
    (0x40000, "throttled"),
    (0x80000, "soft_temp_limit"),
)


def is_enabled():
    return os.path.isfile(CRON_FILE)


def journal_persistent():
    return os.path.isdir(JOURNAL_DIR)


def _run(cmd, timeout=30):
    try:
        subprocess.run(
            cmd, capture_output=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.SubprocessError):
        pass


def take_sample():
    _run(["/bin/bash", SCRIPT])


def enable():
    if not os.path.isdir(os.path.dirname(CRON_FILE)):
        raise RuntimeError("cron n'est pas installé (/etc/cron.d absent)")
    with open(CRON_FILE, "w") as f:
        f.write(
            "# Mode analyse pynab (page Système de nabweb)\n"
            f"*/{INTERVAL_MINUTES} * * * * root /bin/bash {SCRIPT}\n"
        )
    os.chmod(CRON_FILE, 0o644)
    # Journal système conservé d'un démarrage à l'autre, pour lire
    # ce qui s'est passé juste avant un freeze (journalctl -b -1).
    if not journal_persistent():
        try:
            os.makedirs(JOURNAL_DIR, exist_ok=True)
            _run(["systemd-tmpfiles", "--create", "--prefix", JOURNAL_DIR])
            _run(["systemctl", "restart", "systemd-journald"])
        except OSError:
            pass
    take_sample()


def disable():
    try:
        os.remove(CRON_FILE)
    except FileNotFoundError:
        pass


def clear():
    try:
        os.remove(DATA_FILE)
    except FileNotFoundError:
        pass


def _int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_processes(field):
    """
    "nabd=41000,12,5 postgres=9000,8,1 postgres=7000,7,1" ->
    {"nabd": {"rss": 41000, "fds": 12, "threads": 5, "count": 1},
     "postgres": {"rss": 16000, "fds": 15, "threads": 2, "count": 2}}
    (rss en ko)
    """
    procs = {}
    for item in field.split():
        name, _, values = item.partition("=")
        parts = values.split(",")
        if not name or len(parts) != 3:
            continue
        proc = procs.setdefault(
            name, {"rss": 0, "fds": 0, "threads": 0, "count": 0}
        )
        proc["rss"] += _int(parts[0])
        proc["fds"] += _int(parts[1])
        proc["threads"] += _int(parts[2])
        proc["count"] += 1
    return procs


def _parse_line(line):
    fields = line.rstrip("\n").split(";")
    if len(fields) < 12:
        return None
    try:
        date = datetime.strptime(fields[0], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None
    try:
        throttled = int(fields[6], 0) if fields[6] else 0
    except ValueError:
        throttled = 0
    return {
        "date": date,
        "uptime": _int(fields[1]),
        "mem_avail": _int(fields[2]),
        "swap_used": _int(fields[3]),
        "load": _float(fields[4]),
        "temp": _int(fields[5]) / 1000 if fields[5] else None,
        "throttled": throttled,
        "root_pct": _int(fields[7]),
        "tmp": _int(fields[8]),
        "nb_proc": _int(fields[9]),
        "wifi": fields[10],
        "processes": _parse_processes(fields[11]),
    }


def read_samples(path=None):
    samples = []
    try:
        with open(path or DATA_FILE, "r") as f:
            for line in f:
                if line.startswith("date;"):
                    continue
                sample = _parse_line(line)
                if sample is not None:
                    samples.append(sample)
    except FileNotFoundError:
        pass
    return samples


def split_boots(samples):
    """
    Découpe les mesures par démarrage : l'uptime qui diminue signale un
    redémarrage (après un freeze, ou volontaire).
    """
    boots = []
    current = []
    for sample in samples:
        if current and sample["uptime"] < current[-1]["uptime"]:
            boots.append(current)
            current = []
        current.append(sample)
    if current:
        boots.append(current)
    return boots


def _process_table(boot):
    first, last = boot[0], boot[-1]
    days = max((last["date"] - first["date"]).total_seconds() / 86400, 0)
    names = set()
    for sample in boot:
        names.update(sample["processes"].keys())
    table = []
    for name in sorted(names):
        series = [
            s["processes"][name] for s in boot if name in s["processes"]
        ]
        start, end = series[0], series[-1]
        rss_growth = (end["rss"] - start["rss"]) / 1024
        fds_growth = end["fds"] - start["fds"]
        threads_growth = end["threads"] - start["threads"]
        per_day = rss_growth / days if days >= 0.25 else None
        table.append(
            {
                "name": name,
                "count": end["count"],
                "rss_start": round(start["rss"] / 1024, 1),
                "rss_end": round(end["rss"] / 1024, 1),
                "rss_max": round(max(p["rss"] for p in series) / 1024, 1),
                "rss_per_day": (
                    round(per_day, 1) if per_day is not None else None
                ),
                "fds_start": start["fds"],
                "fds_end": end["fds"],
                "threads_start": start["threads"],
                "threads_end": end["threads"],
                "suspect": (
                    (per_day is not None and per_day >= 2)
                    or fds_growth >= 20
                    or threads_growth >= 10
                ),
            }
        )
    table.sort(key=lambda p: (not p["suspect"], -p["rss_end"]))
    return table


def _alerts(boot):
    alerts = []
    flags = 0
    for sample in boot:
        flags |= sample["throttled"]
    for bit, name in THROTTLED_FLAGS:
        if flags & bit:
            alerts.append(name)
    last = boot[-1]
    if last["mem_avail"] < 40 * 1024:
        alerts.append("low_memory")
    if last["swap_used"] > 100 * 1024:
        alerts.append("swap")
    if last["root_pct"] >= 90:
        alerts.append("disk_full")
    if last["tmp"] > 50 * 1024:
        alerts.append("tmp_full")
    if any(s["wifi"] not in ("up", "") for s in boot):
        alerts.append("wifi_down")
    return alerts


def _downsample(boot, max_points=MAX_POINTS):
    step = max(1, -(-len(boot) // max_points))
    points = boot[::step]
    if points[-1] is not boot[-1]:
        points.append(boot[-1])
    return points


def summary(boot_index=None):
    samples = read_samples()
    boots = split_boots(samples)
    result = {
        "enabled": is_enabled(),
        "journal_persistent": journal_persistent(),
        "interval_minutes": INTERVAL_MINUTES,
        "count": len(samples),
        "boots": [
            {
                "start": b[0]["date"].isoformat(),
                "end": b[-1]["date"].isoformat(),
                "count": len(b),
            }
            for b in boots
        ],
        "boot": None,
    }
    if not boots:
        return result
    if boot_index is None or not 0 <= boot_index < len(boots):
        boot_index = len(boots) - 1
    boot = boots[boot_index]
    last = boot[-1]
    result["boot"] = {
        "index": boot_index,
        "points": [
            {
                "t": s["date"].isoformat(),
                "mem": round(s["mem_avail"] / 1024, 1),
                "swap": round(s["swap_used"] / 1024, 1),
                "load": s["load"],
            }
            for s in _downsample(boot)
        ],
        "last": {
            "date": last["date"].isoformat(),
            "uptime": last["uptime"],
            "mem": round(last["mem_avail"] / 1024, 1),
            "swap": round(last["swap_used"] / 1024, 1),
            "temp": last["temp"],
            "load": last["load"],
            "root_pct": last["root_pct"],
            "tmp": round(last["tmp"] / 1024, 1),
            "nb_proc": last["nb_proc"],
            "nb_proc_start": boot[0]["nb_proc"],
            "wifi": last["wifi"],
        },
        "alerts": _alerts(boot),
        "processes": _process_table(boot),
        # le redémarrage suivant a eu lieu après la dernière mesure :
        # c'est l'état juste avant un éventuel freeze
        "ended_by_reboot": boot_index < len(boots) - 1,
    }
    return result
