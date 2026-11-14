import os
import stat
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

from nabttsd import tts

FAKE_PICO = """#!/bin/sh
# Fake pico2wave: writes 0.1 s of silence to the file given with -w.
while [ $# -gt 0 ]; do
  if [ "$1" = "-w" ]; then out="$2"; shift; fi
  shift
done
python3 -c "
import wave, sys
f = wave.open(sys.argv[1], 'wb')
f.setnchannels(1); f.setsampwidth(2); f.setframerate(16000)
f.writeframes(b'\\x10\\x00' * 1600); f.close()
" "$out"
"""


class TestNormalize(unittest.TestCase):
    def test_html_urls_and_symbols(self):
        text = "<b>Météo</b> : 20&nbsp;°C &amp; 50% https://x.fr/a ☀️"
        self.assertEqual(
            tts.normalize(text, "fr_FR"),
            "Météo : 20 degrés et 50 pour cent",
        )

    def test_english_symbols(self):
        self.assertEqual(tts.normalize("A & B", "en_US"), "A and B")

    def test_leading_dash_and_markup(self):
        self.assertEqual(tts.normalize("- a < b", "fr_FR"), "a b")

    def test_length_cut_at_sentence(self):
        text = ("Phrase courte. " * 100).strip()
        result = tts.normalize(text, "fr_FR")
        self.assertLessEqual(len(result), tts.MAX_TEXT_LENGTH)
        self.assertTrue(result.endswith("."))


class TestCommand(unittest.TestCase):
    def test_pico_markup(self):
        cmd, stdin = tts.build_command("pico", "Salut", "fr_FR", 120, 90, "o")
        self.assertEqual(cmd[:5], ["pico2wave", "-l", "fr-FR", "-w", "o"])
        self.assertEqual(
            cmd[5],
            "<speed level='120'><pitch level='90'>Salut</pitch></speed>",
        )
        self.assertIsNone(stdin)

    def test_pico_default_no_markup(self):
        cmd, _ = tts.build_command("pico", "Salut", "fr_FR", 100, 100, "o")
        self.assertEqual(cmd[5], "Salut")

    def test_espeak(self):
        cmd, stdin = tts.build_command("espeak", "Hi", "en_GB", 200, 300, "o")
        self.assertEqual(cmd[:3], ["espeak-ng", "-v", "en-gb"])
        self.assertIn("350", cmd)  # 175 wpm * 200 %
        self.assertIn("99", cmd)  # pitch clamped
        self.assertEqual(stdin, "Hi")

    def test_mbrola_voice(self):
        cmd, _ = tts.build_command("mbrola", "x", "fr_FR", 100, 100, "o")
        self.assertEqual(cmd[2], "mb-fr1")


class TestSynthesize(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        bin_dir = Path(self.tmp.name, "bin")
        bin_dir.mkdir()
        pico = bin_dir / "pico2wave"
        pico.write_text(FAKE_PICO)
        pico.chmod(pico.stat().st_mode | stat.S_IEXEC)
        self.env = mock.patch.dict(
            os.environ, {"PATH": f"{bin_dir}:{os.environ['PATH']}"}
        )
        self.env.start()
        self.cache = mock.patch.object(
            tts, "CACHE_DIR", Path(self.tmp.name, "cache")
        )
        self.cache.start()

    def tearDown(self):
        self.cache.stop()
        self.env.stop()
        self.tmp.cleanup()

    def test_synthesize_and_cache(self):
        path = tts.synthesize("Bonjour", volume=200)
        self.assertTrue(path.is_absolute() and path.is_file())
        with wave.open(str(path), "rb") as f:
            self.assertEqual(f.readframes(1), b"\x20\x00")  # volume x2
        self.assertEqual(tts.synthesize("Bonjour", volume=200), path)
        self.assertEqual(len(list(tts.CACHE_DIR.glob("*.wav"))), 1)

    def test_falls_back_to_installed_engine(self):
        self.assertEqual(tts.resolve_engine("espeak"), "pico")

    def test_empty_text(self):
        with self.assertRaises(tts.TTSError):
            tts.synthesize("☀️ https://x.fr")


if __name__ == "__main__":
    unittest.main()
