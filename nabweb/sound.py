"""
Volume of the rear wheel.

The wheel has three positions (mute, medium, loud). A small program
installed with the sound card driver, tagtagtag-mixerd, watches it and
applies the level of each position. It reads these levels from its
configuration file and reloads it on SIGUSR1:
https://github.com/pguyot/wm8960/tree/tagtagtag-sound

The web page only changes the "medium" and "loud" levels in that file.
"""

import os
import re
import signal
import subprocess

from django.conf import settings

MIXER_CONF = getattr(
    settings, "NABWEB_MIXER_CONF", "/var/lib/tagtagtag-sound/mixer.conf"
)
MIXER_PID = getattr(
    settings, "NABWEB_MIXER_PID", "/run/tagtagtag-mixerd.pid"
)
SOUND_CARD = "tagtagtagsound"

# Output volume of the codec: 1 dB per step, 127 is the loudest (+6 dB).
# 0 % is kept audible (about -54 dB), not silent: silence is the job of
# the "mute" position of the wheel.
LEVEL_MIN = 67
LEVEL_MAX = 127

# Keys of the configuration file, for (medium, loud), by rabbit model.
KEYS = {
    "2019_TAG": ("tag-speaker-low", "tag-speaker-high"),
    "2019_TAGTAG": ("tagtag-speaker-low", "tagtag-speaker-high"),
}
# Values the driver uses when the file does not set them.
DEFAULTS = {
    "tag-speaker-low": 121,
    "tag-speaker-high": 127,
    "tagtag-speaker-low": 110,
    "tagtag-speaker-high": 120,
}

# Wheel position from the two switches (GPIO 27, GPIO 22), by model.
# Wiring differs between the two rabbits (see tagtagtag-mixerd.c).
WHEEL = {
    "2019_TAG": {(0, 0): "mute", (0, 1): "high", (1, 0): "low"},
    "2019_TAGTAG": {(0, 0): "low", (0, 1): "mute", (1, 0): "high"},
}


def available():
    """True when the rabbit has the 2019 sound card and its mixer."""
    return os.path.isfile(MIXER_CONF)


def keys_for(model):
    """Configuration keys for (medium, loud). Unknown model: Nabaztag:tag."""
    return KEYS.get(model, KEYS["2019_TAGTAG"])


def to_percent(level):
    level = max(LEVEL_MIN, min(LEVEL_MAX, int(level)))
    return round((level - LEVEL_MIN) * 100 / (LEVEL_MAX - LEVEL_MIN))


def to_level(percent):
    percent = max(0, min(100, int(percent)))
    return LEVEL_MIN + round(percent * (LEVEL_MAX - LEVEL_MIN) / 100)


_LINE = re.compile(r"^\s*([A-Za-z0-9_-]+)\s*=\s*(.*?)\s*$")


def read_conf():
    """Values of the configuration file, as {key: text}."""
    values = {}
    with open(MIXER_CONF, encoding="utf8") as conf:
        for line in conf:
            if line.lstrip().startswith(";"):
                continue
            match = _LINE.match(line)
            if match:
                values[match.group(1)] = match.group(2)
    return values


def write_conf(changes):
    """
    Set some keys of the configuration file, keeping everything else
    (comments included). The file is replaced in one go.
    """
    with open(MIXER_CONF, encoding="utf8") as conf:
        lines = conf.read().splitlines()
    remaining = dict(changes)
    for index, line in enumerate(lines):
        if line.lstrip().startswith(";"):
            continue
        match = _LINE.match(line)
        if match and match.group(1) in remaining:
            key = match.group(1)
            lines[index] = f"{key}={remaining.pop(key)}"
    for key, value in remaining.items():
        lines.append(f"{key}={value}")
    temporary = MIXER_CONF + ".tmp"
    with open(temporary, "w", encoding="utf8") as conf:
        conf.write("\n".join(lines) + "\n")
    try:
        os.chmod(temporary, os.stat(MIXER_CONF).st_mode & 0o777)
    except OSError:
        pass
    os.replace(temporary, MIXER_CONF)


def reload_mixer():
    """Ask tagtagtag-mixerd to apply the file again. False if not running."""
    try:
        with open(MIXER_PID, encoding="utf8") as pid_file:
            pid = int(pid_file.read().strip())
        os.kill(pid, signal.SIGUSR1)
        return True
    except (OSError, ValueError):
        return False


def get_levels(model):
    """Current (medium, loud) levels of the file, in percent."""
    values = read_conf()
    low_key, high_key = keys_for(model)
    levels = []
    for key in (low_key, high_key):
        try:
            levels.append(to_percent(values.get(key, DEFAULTS[key])))
        except ValueError:
            levels.append(to_percent(DEFAULTS[key]))
    return tuple(levels)


def default_levels(model):
    low_key, high_key = keys_for(model)
    return to_percent(DEFAULTS[low_key]), to_percent(DEFAULTS[high_key])


def set_levels(model, low, high):
    """Write (medium, loud) in percent; medium is never above loud."""
    low = max(0, min(100, int(low)))
    high = max(0, min(100, int(high)))
    low = min(low, high)
    low_key, high_key = keys_for(model)
    changes = {low_key: to_level(low), high_key: to_level(high)}
    if model not in KEYS:
        # Unknown model: set both rabbits, only one is wired anyway.
        for other_low, other_high in KEYS.values():
            changes[other_low] = to_level(low)
            changes[other_high] = to_level(high)
    write_conf(changes)
    return reload_mixer()


def wheel_position(model):
    """'mute', 'low', 'high', or None when it cannot be read."""
    try:
        proc = subprocess.run(
            [
                "amixer",
                "-c",
                SOUND_CARD,
                "cget",
                "iface=CARD,name=Volume Button",
            ],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = re.search(r":\s*values=(on|off),(on|off)", proc.stdout)
    if not match:
        return None
    state = (int(match.group(1) == "on"), int(match.group(2) == "on"))
    return WHEEL.get(model, WHEEL["2019_TAGTAG"]).get(state)
