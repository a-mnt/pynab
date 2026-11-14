"""
Text-to-speech for pynab: turns a text into a WAV file that nabd can play.

Everything runs locally, with a command-line engine called on demand (no
engine stays in memory next to speech recognition). Results are cached by
a hash of the text and of the settings, so recurring sentences are only
synthesized once.

This module has no Django dependency: it can be used by any service.
Engines are installed with apt:
  - pico:   sudo apt-get install libttspico-utils   (non-free)
  - espeak: sudo apt-get install espeak-ng
  - mbrola: sudo apt-get install espeak-ng mbrola mbrola-fr1
"""

import audioop
import hashlib
import html
import logging
import os
import re
import shutil
import subprocess  # nosec B404
import tempfile
import time
import unicodedata
import wave
from pathlib import Path
from typing import List, Optional, Tuple

CACHE_DIR = Path(os.getenv("NABTTSD_CACHE_DIR", "/var/cache/nabttsd"))
CACHE_MAX_BYTES = int(os.getenv("NABTTSD_CACHE_MAX_MB", "50")) * 1024 * 1024
# Files younger than this are kept even if the cache is full: nabd may
# not have played them yet (the rabbit could be asleep).
CACHE_MIN_AGE_S = 24 * 3600
MAX_TEXT_LENGTH = 1000
ENGINE_TIMEOUT_S = 120

# Engine id -> label shown on the website.
ENGINES = {
    "pico": "SVOX Pico",
    "mbrola": "eSpeak NG + MBROLA",
    "espeak": "eSpeak NG",
}
# Default order when no engine is chosen: best quality first.
ENGINE_PREFERENCE = ["pico", "mbrola", "espeak"]

# pynab locale -> (pico language, espeak voice, mbrola voice)
VOICES = {
    "fr_FR": ("fr-FR", "fr", "mb-fr1"),
    "en_US": ("en-US", "en-us", "mb-us1"),
    "en_GB": ("en-GB", "en-gb", "mb-en1"),
    "de_DE": ("de-DE", "de", "mb-de1"),
    "es_ES": ("es-ES", "es", "mb-es1"),
    "it_IT": ("it-IT", "it", "mb-it1"),
}
MBROLA_DIR = Path("/usr/share/mbrola")

_SYMBOLS = {
    "fr": {"&": " et ", "%": " pour cent", "€": " euros", "°C": " degrés",
           "°": " degrés", "+": " plus ", "@": " arobase "},
    "en": {"&": " and ", "%": " percent", "€": " euros", "°C": " degrees",
           "°": " degrees", "+": " plus ", "@": " at "},
}


class TTSError(Exception):
    pass


def _voices(locale: str) -> Tuple[str, str, str]:
    return VOICES.get(locale, VOICES["fr_FR"])


def _mbrola_installed(locale: str) -> bool:
    mb_voice = _voices(locale)[2][3:]  # "mb-fr1" -> "fr1"
    return (MBROLA_DIR / mb_voice).exists() or (
        MBROLA_DIR / mb_voice / mb_voice
    ).exists()


def available_engines(locale: str = "fr_FR") -> List[Tuple[str, str]]:
    """Engines installed on this system, best first: [(id, label)]."""
    result = []
    for engine in ENGINE_PREFERENCE:
        if engine == "pico":
            ok = shutil.which("pico2wave") is not None
        elif engine == "mbrola":
            ok = (
                shutil.which("espeak-ng") is not None
                and shutil.which("mbrola") is not None
                and _mbrola_installed(locale)
            )
        else:
            ok = shutil.which("espeak-ng") is not None
        if ok:
            result.append((engine, ENGINES[engine]))
    return result


def resolve_engine(engine: str, locale: str = "fr_FR") -> str:
    """The engine to use: the one chosen if installed, else the best one."""
    installed = [e for e, _ in available_engines(locale)]
    if not installed:
        raise TTSError(
            "no TTS engine installed "
            "(sudo apt-get install libttspico-utils or espeak-ng)"
        )
    if engine in installed:
        return engine
    return installed[0]


def normalize(text: str, locale: str = "fr_FR") -> str:
    """
    Make a text speakable: no HTML, URLs, emoji or symbols the engines
    read badly, single spaces, bounded length.
    """
    text = html.unescape(text or "")
    text = re.sub(r"<[^>]*>", " ", text)
    text = re.sub(r"(https?://|www\.)\S+", " ", text)
    text = re.sub(r"[#*_~`|\\^]+", " ", text)
    symbols = _SYMBOLS["fr" if locale.startswith("fr") else "en"]
    for symbol in sorted(symbols, key=len, reverse=True):
        text = text.replace(symbol, symbols[symbol])
    # Keep letters, digits, punctuation and spaces; drop emoji & co.
    text = "".join(
        ch
        for ch in text
        if unicodedata.category(ch)[0] in ("L", "N", "P", "Z")
    )
    text = text.replace("<", " ").replace(">", " ")
    text = re.sub(r"\s+", " ", text).strip().lstrip("-").strip()
    if len(text) > MAX_TEXT_LENGTH:
        cut = text[:MAX_TEXT_LENGTH]
        last_stop = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
        text = cut[: last_stop + 1] if last_stop > 0 else cut
    return text


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, int(value)))


def build_command(
    engine: str, text: str, locale: str, speed: int, pitch: int, out: str
) -> Tuple[List[str], Optional[str]]:
    """Command line and stdin to synthesize text into out (WAV)."""
    pico_lang, espeak_voice, mbrola_voice = _voices(locale)
    speed = _clamp(speed, 50, 200)
    pitch = _clamp(pitch, 50, 200)
    if engine == "pico":
        markup = text
        if pitch != 100:
            markup = f"<pitch level='{pitch}'>{markup}</pitch>"
        if speed != 100:
            markup = f"<speed level='{speed}'>{markup}</speed>"
        return ["pico2wave", "-l", pico_lang, "-w", out, markup], None
    voice = mbrola_voice if engine == "mbrola" else espeak_voice
    wpm = 175 * speed // 100
    espeak_pitch = _clamp(50 * pitch // 100, 0, 99)
    command = [
        "espeak-ng", "-v", voice, "-s", str(wpm), "-p", str(espeak_pitch),
        "-w", out, "--stdin",
    ]
    return command, text


def cache_key(
    text: str, engine: str, locale: str, speed: int, pitch: int, volume: int
) -> str:
    raw = f"{engine}|{locale}|{speed}|{pitch}|{volume}|{text}"
    return hashlib.sha1(raw.encode("utf8")).hexdigest()  # nosec B324


def _apply_volume(path: str, volume: int) -> None:
    if volume == 100:
        return
    factor = _clamp(volume, 10, 300) / 100
    with wave.open(path, "rb") as f:
        params = f.getparams()
        frames = f.readframes(f.getnframes())
    frames = audioop.mul(frames, params.sampwidth, factor)
    with wave.open(path, "wb") as f:
        f.setparams(params)
        f.writeframes(frames)


def _prune_cache() -> None:
    try:
        files = [(p, p.stat()) for p in CACHE_DIR.glob("*.wav")]
    except OSError:
        return
    total = sum(st.st_size for _, st in files)
    if total <= CACHE_MAX_BYTES:
        return
    now = time.time()
    for path, st in sorted(files, key=lambda item: item[1].st_mtime):
        if total <= CACHE_MAX_BYTES or now - st.st_mtime < CACHE_MIN_AGE_S:
            break
        try:
            path.unlink()
            total -= st.st_size
        except OSError:
            pass


def synthesize(
    text: str,
    engine: str = "",
    locale: str = "fr_FR",
    speed: int = 100,
    pitch: int = 100,
    volume: int = 100,
) -> Path:
    """
    Synthesize text and return the path of the WAV file (absolute, so it
    can be given as is to nabd in a command or message packet).
    Blocking: call it from an executor in asyncio code.
    """
    text = normalize(text, locale)
    if not text:
        raise TTSError("nothing to say")
    engine = resolve_engine(engine, locale)
    key = cache_key(text, engine, locale, speed, pitch, volume)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    target = CACHE_DIR / f"{key}.wav"
    if target.is_file():
        os.utime(target)  # most recently used
        return target

    fd, tmp = tempfile.mkstemp(suffix=".wav", dir=CACHE_DIR)
    os.close(fd)
    try:
        command, stdin = build_command(
            engine, text, locale, speed, pitch, tmp
        )
        started = time.monotonic()
        result = subprocess.run(  # nosec B603
            command,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=ENGINE_TIMEOUT_S,
        )
        if result.returncode != 0 or os.path.getsize(tmp) == 0:
            raise TTSError(
                f"{engine} failed ({result.returncode}): "
                f"{result.stderr.strip()[:200]}"
            )
        _apply_volume(tmp, volume)
        os.chmod(tmp, 0o644)
        os.replace(tmp, target)
        logging.info(
            f"{engine}: {len(text)} chars in "
            f"{time.monotonic() - started:.1f}s"
        )
    except subprocess.TimeoutExpired:
        raise TTSError(f"{engine} timed out")
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    _prune_cache()
    return target
