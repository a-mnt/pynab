import os
import signal
import subprocess
import tempfile
from unittest import mock

from django.core.cache import cache
from django.test import Client, TestCase

from nabweb import sound, views, wifi

CONF = """; tagtagtag-mixerd configuration file

debug=false
tag-speaker-low=121
tag-speaker-high=127
tagtag-speaker-low=110
tagtag-speaker-high=120
; lineout-mode=headphone
speaker-base=255
"""


class SoundTestCase(TestCase):
    def setUp(self):
        directory = tempfile.mkdtemp()
        self.conf = os.path.join(directory, "mixer.conf")
        self.pid = os.path.join(directory, "mixerd.pid")
        with open(self.conf, "w") as conf:
            conf.write(CONF)
        with open(self.pid, "w") as pid:
            pid.write("4321\n")
        patches = [
            mock.patch.object(sound, "MIXER_CONF", self.conf),
            mock.patch.object(sound, "MIXER_PID", self.pid),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        cache.clear()

    def text(self):
        with open(self.conf) as conf:
            return conf.read()


class TestSoundFile(SoundTestCase):
    def test_percent_and_level_round_trip(self):
        for level in range(sound.LEVEL_MIN, sound.LEVEL_MAX + 1):
            self.assertEqual(sound.to_level(sound.to_percent(level)), level)
        self.assertEqual(sound.to_level(0), sound.LEVEL_MIN)
        self.assertEqual(sound.to_level(100), 127)
        self.assertEqual(sound.to_level(150), 127)

    def test_read_levels_by_model(self):
        self.assertEqual(sound.get_levels("2019_TAGTAG"), (72, 88))
        self.assertEqual(sound.get_levels("2019_TAG"), (90, 100))
        # Unknown model: Nabaztag:tag keys.
        self.assertEqual(sound.get_levels(""), (72, 88))

    def test_write_keeps_the_rest_of_the_file(self):
        with mock.patch("os.kill") as kill:
            self.assertTrue(sound.set_levels("2019_TAGTAG", 50, 95))
        kill.assert_called_once_with(4321, signal.SIGUSR1)
        text = self.text()
        self.assertIn("tagtag-speaker-low=97\n", text)
        self.assertIn("tagtag-speaker-high=124\n", text)
        # Untouched: the other rabbit, comments, other settings.
        self.assertIn("tag-speaker-low=121\n", text)
        self.assertIn("; lineout-mode=headphone\n", text)
        self.assertIn("speaker-base=255\n", text)
        self.assertEqual(sound.get_levels("2019_TAGTAG"), (50, 95))

    def test_medium_never_above_loud(self):
        with mock.patch("os.kill"):
            sound.set_levels("2019_TAG", 80, 40)
        self.assertEqual(sound.get_levels("2019_TAG"), (40, 40))

    def test_unknown_model_sets_both_rabbits(self):
        with mock.patch("os.kill"):
            sound.set_levels(None, 0, 100)
        self.assertEqual(sound.get_levels("2019_TAG"), (0, 100))
        self.assertEqual(sound.get_levels("2019_TAGTAG"), (0, 100))

    def test_missing_keys_are_added(self):
        with open(self.conf, "w") as conf:
            conf.write("debug=false\n")
        self.assertEqual(sound.get_levels("2019_TAGTAG"), (72, 88))
        with mock.patch("os.kill"):
            sound.set_levels("2019_TAGTAG", 10, 20)
        self.assertEqual(self.text(), "debug=false\ntagtag-speaker-low=73\ntagtag-speaker-high=79\n")

    def test_mixer_not_running(self):
        os.remove(self.pid)
        self.assertFalse(sound.set_levels("2019_TAGTAG", 10, 20))
        self.assertEqual(sound.get_levels("2019_TAGTAG"), (10, 20))

    def test_wheel_position(self):
        def answer(values):
            return subprocess.CompletedProcess(
                [], 0, f"numid=12,iface=CARD,name='Volume Button'\n  ; type=BOOLEAN,access=r--v----,values=2\n  : values={values}\n", ""
            )

        with mock.patch("subprocess.run", return_value=answer("off,off")):
            self.assertEqual(sound.wheel_position("2019_TAGTAG"), "low")
            self.assertEqual(sound.wheel_position("2019_TAG"), "mute")
        with mock.patch("subprocess.run", return_value=answer("on,off")):
            self.assertEqual(sound.wheel_position("2019_TAGTAG"), "high")
        with mock.patch("subprocess.run", return_value=answer("off,on")):
            self.assertEqual(sound.wheel_position("2019_TAGTAG"), "mute")
        with mock.patch("subprocess.run", side_effect=OSError):
            self.assertIsNone(sound.wheel_position("2019_TAGTAG"))


class TestSoundView(SoundTestCase):
    def setUp(self):
        super().setUp()
        cache.set("rabbit_model", "2019_TAGTAG", 60)
        patch = mock.patch.object(sound, "wheel_position", return_value="low")
        patch.start()
        self.addCleanup(patch.stop)

    def test_unavailable_without_mixer(self):
        os.remove(self.conf)
        self.assertEqual(Client().get("/settings/sound").json(), {"status": "unavailable"})
        response = Client().get("/settings/")
        self.assertNotContains(response, "js-volume")

    def test_settings_page_shows_the_card(self):
        response = Client().get("/settings/")
        self.assertContains(response, "js-volume")

    def test_status(self):
        data = Client().get("/settings/sound").json()
        self.assertEqual(
            data,
            {"status": "ok", "low": 72, "high": 88, "default_low": 72, "default_high": 88, "wheel": "low"},
        )

    def test_save(self):
        with mock.patch("os.kill") as kill:
            data = Client().post("/settings/sound", {"action": "save", "low": "30", "high": "60"}).json()
        self.assertEqual((data["status"], data["low"], data["high"], data["applied"]), ("ok", 30, 60, True))
        kill.assert_called_once()
        self.assertIn("tagtag-speaker-low=85\n", self.text())

    def test_invalid_values(self):
        response = Client().post("/settings/sound", {"action": "save", "low": "x", "high": "60"})
        self.assertEqual(response.status_code, 400)
        response = Client().post("/settings/sound", {"action": "save", "low": "10", "high": "101"})
        self.assertEqual(response.status_code, 400)
        response = Client().post("/settings/sound", {"action": "nope", "low": "10", "high": "20"})
        self.assertEqual(response.status_code, 400)

    def test_test_plays_at_the_level_then_restores(self):
        levels_during_test = []

        def play(function, *args):
            levels_during_test.append(sound.get_levels("2019_TAGTAG"))
            return {"status": "ok"}

        before = self.text()
        with mock.patch("os.kill"), mock.patch.object(views.NabdConnection, "transaction", side_effect=play):
            data = Client().post(
                "/settings/sound", {"action": "test", "which": "low", "low": "40", "high": "90"}
            ).json()
        self.assertEqual(data, {"status": "ok", "wheel": "low"})
        self.assertEqual(levels_during_test, [(40, 40)])
        self.assertEqual(sound.get_levels("2019_TAGTAG"), (72, 88))
        for line in before.splitlines():
            self.assertIn(line, self.text())

    def test_test_refused_when_rabbit_busy(self):
        with mock.patch("os.kill"), mock.patch.object(
            views.NabdConnection, "transaction", return_value={"status": "busy", "state": "asleep"}
        ):
            data = Client().post(
                "/settings/sound", {"action": "test", "which": "high", "low": "40", "high": "90"}
            ).json()
        self.assertEqual(data["status"], "busy")
        self.assertEqual(data["state"], "asleep")
        self.assertEqual(sound.get_levels("2019_TAGTAG"), (72, 88))


class FakeNmcli:
    """Records nmcli calls and answers from a table of prefixes."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def __call__(self, *args, timeout=20):
        self.calls.append(args)
        for prefix, answer in self.answers:
            if args[: len(prefix)] == prefix:
                return answer(args) if callable(answer) else answer
        return 0, "", ""


LIST = (
    "*:Maison:70:WPA2\n"
    ":Maison_5G:90:WPA2\n"
    ":Maison_5G:40:WPA2\n"
    ":Box\\:Invités:30:\n"
    ":Neuf:10:WPA3\n"
    "::80:WPA2\n"
)


class TestWifi(TestCase):
    def test_split_terse(self):
        self.assertEqual(wifi.split_terse("a\\:b:c\\\\:d"), ["a:b", "c\\", "d"])

    def test_scan(self):
        fake = FakeNmcli([(("-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY"), (0, LIST, ""))])
        with mock.patch.object(wifi, "_nmcli", fake):
            networks = wifi.scan()
        self.assertEqual(
            [(n["ssid"], n["bars"], n["secure"], n["current"]) for n in networks],
            [("Maison", 3, True, True), ("Maison_5G", 4, True, False), ("Box:Invités", 2, False, False), ("Neuf", 1, True, False)],
        )
        self.assertIn("yes", fake.calls[-1])

    def test_status(self):
        fake = FakeNmcli(
            [
                (("-t", "-f", "DEVICE,TYPE,STATE,CONNECTION"), (0, "eth0:ethernet:unavailable:\nlo:loopback:connected (externally):lo\nwlan0:wifi:connected:preconfigured\n", "")),
                (("-t", "-g", "802-11-wireless.mode"), (0, "infrastructure\n", "")),
                (("-t", "-f", "IP4.ADDRESS"), (0, "IP4.ADDRESS[1]:192.168.1.42/24\n", "")),
                (("-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY"), (0, LIST, "")),
            ]
        )
        with mock.patch.object(wifi, "_nmcli", fake):
            self.assertEqual(
                wifi.status(),
                {"connected": True, "ssid": "Maison", "bars": 3, "address": "192.168.1.42", "hotspot": False, "managed": True},
            )

    def test_status_hotspot_and_disconnected(self):
        fake = FakeNmcli(
            [
                (("-t", "-f", "DEVICE,TYPE,STATE,CONNECTION"), (0, "wlan0:802-11-wireless:connected:comitup-123\n", "")),
                (("-t", "-g", "802-11-wireless.mode"), (0, "ap\n", "")),
                (("-t", "-f", "IP4.ADDRESS"), (0, "IP4.ADDRESS[1]:10.41.0.1/24\n", "")),
                (("-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY"), (0, "", "")),
                (("-t", "-g", "802-11-wireless.ssid"), (0, "comitup-123\n", "")),
            ]
        )
        with mock.patch.object(wifi, "_nmcli", fake):
            current = wifi.status()
        self.assertTrue(current["hotspot"])
        self.assertEqual(current["address"], "10.41.0.1")
        disconnected = FakeNmcli([(("-t", "-f", "DEVICE,TYPE,STATE,CONNECTION"), (0, "wlan0:wifi:disconnected:--\n", ""))])
        with mock.patch.object(wifi, "_nmcli", disconnected):
            current = wifi.status()
        self.assertFalse(current["connected"])
        self.assertTrue(current["managed"])

    def test_status_without_networkmanager(self):
        not_running = FakeNmcli([(("-t",), (8, "", "Error: NetworkManager is not running."))])
        with mock.patch.object(wifi, "_nmcli", not_running):
            current = wifi.status()
        self.assertEqual((current["managed"], current["state"], current["connected"]), (False, "no-networkmanager", False))
        unmanaged = FakeNmcli([(("-t", "-f", "DEVICE,TYPE,STATE,CONNECTION"), (0, "wlan0:wifi:unmanaged:--\n", ""))])
        with mock.patch.object(wifi, "_nmcli", unmanaged):
            self.assertFalse(wifi.status()["managed"])

    def test_interface_found_by_type(self):
        fake = FakeNmcli([(("-t", "-f", "DEVICE,TYPE,STATE,CONNECTION"), (0, "p2p-dev-wlan1:wifi-p2p:disconnected:--\nwlan1:wifi:connected:Maison\n", ""))])
        with mock.patch.object(wifi, "_nmcli", fake):
            self.assertEqual(wifi._iface(), "wlan1")

    def connect(self, answers, *args):
        fake = FakeNmcli(answers)
        with mock.patch.object(wifi, "_nmcli", fake):
            self.assertTrue(wifi._lock.acquire(blocking=False))
            wifi._connect(*args)
        self.assertTrue(wifi._lock.acquire(blocking=False))
        wifi._lock.release()
        return fake.calls

    ACTIVE = (("-t", "-f", "DEVICE,TYPE,STATE,CONNECTION"), (0, "wlan0:wifi:connected:Maison\n", ""))
    PROFILES = (("-t", "-f", "NAME"), (0, "Maison\nlo\n", ""))

    def test_connect_new_network(self):
        calls = self.connect([self.ACTIVE, self.PROFILES], "Voisin", "motdepasse", False, "WPA2")
        self.assertEqual(wifi.job()["state"], "connected")
        add = next(c for c in calls if c[:2] == ("connection", "add"))
        self.assertIn("Voisin", add)
        self.assertEqual(add[add.index("wifi-sec.psk") + 1], "motdepasse")
        self.assertEqual(add[add.index("wifi-sec.key-mgmt") + 1], "wpa-psk")
        self.assertEqual(add[add.index("802-11-wireless.hidden") + 1], "no")
        self.assertTrue(any(c[2:4] == ("connection", "up") and "Voisin" in c for c in calls))

    def test_connect_hidden_wpa3_network(self):
        calls = self.connect([self.ACTIVE, self.PROFILES], "Cache", "motdepasse", True, "WPA3")
        add = next(c for c in calls if c[:2] == ("connection", "add"))
        self.assertEqual(add[add.index("802-11-wireless.hidden") + 1], "yes")
        self.assertEqual(add[add.index("wifi-sec.key-mgmt") + 1], "sae")

    def test_failure_goes_back_to_previous_network(self):
        up = lambda args: (4, "", "Error: Connection activation failed: Secrets were required, but not provided.") if "Voisin" in args else (0, "", "")
        calls = self.connect([self.ACTIVE, self.PROFILES, (("--wait",), up)], "Voisin", "mauvais!", False, "WPA2")
        self.assertEqual((wifi.job()["state"], wifi.job()["reason"]), ("failed", "wrong-password"))
        self.assertIn(("connection", "delete", "id", "Voisin"), calls)
        back = [c for c in calls if c[:1] == ("--wait",) and "Maison" in c]
        self.assertEqual(len(back), 1)

    def test_failure_on_known_network_restores_its_password(self):
        answers = [
            self.ACTIVE,
            (("-t", "-f", "NAME"), (0, "Maison\nVoisin\n", "")),
            (("-s",), (0, "ancien-secret\n", "")),
            (("--wait",), lambda args: (10, "", "Error: No network with SSID 'Voisin' found.") if "Voisin" in args else (0, "", "")),
        ]
        wifi._job.update({"ssid": "Voisin"})
        calls = self.connect(answers, "Voisin", "nouveau-secret", False, "WPA2")
        self.assertEqual(wifi.job()["reason"], "not-found")
        self.assertIn(("connection", "modify", "Voisin", "wifi-sec.psk", "ancien-secret"), calls)
        self.assertNotIn(("connection", "delete", "id", "Voisin"), calls)

    def test_internet_test(self):
        class Answer:
            status = 204

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        with mock.patch.object(wifi, "urlopen", return_value=Answer()):
            self.assertTrue(wifi.internet_test()["ok"])
        with mock.patch.object(wifi, "urlopen", side_effect=OSError), mock.patch("socket.getaddrinfo", side_effect=OSError):
            self.assertEqual(wifi.internet_test(), {"ok": False, "reason": "no-dns"})
        with mock.patch.object(wifi, "urlopen", side_effect=OSError), mock.patch("socket.getaddrinfo", return_value=[]):
            self.assertEqual(wifi.internet_test(), {"ok": False, "reason": "no-answer"})


class TestWifiView(TestCase):
    def test_unavailable_without_nmcli(self):
        with mock.patch.object(wifi, "available", return_value=False):
            self.assertEqual(Client().get("/settings/wifi").json(), {"status": "unavailable"})
            self.assertNotContains(Client().get("/settings/"), "js-wifi")

    def test_settings_page_shows_the_card(self):
        with mock.patch.object(wifi, "available", return_value=True):
            self.assertContains(Client().get("/settings/"), "js-wifi")

    def test_connect_checks_the_password(self):
        with mock.patch.object(wifi, "available", return_value=True), mock.patch.object(wifi, "connect") as connect:
            response = Client().post("/settings/wifi", {"action": "connect", "ssid": "Voisin", "password": "court"})
            self.assertEqual(response.status_code, 400)
            response = Client().post("/settings/wifi", {"action": "connect", "ssid": "", "password": "assez-long"})
            self.assertEqual(response.status_code, 400)
            connect.assert_not_called()
            connect.return_value = True
            data = Client().post(
                "/settings/wifi", {"action": "connect", "ssid": "Voisin", "password": "assez-long", "hidden": "1", "security": "WPA2"}
            ).json()
        self.assertEqual(data["status"], "ok")
        connect.assert_called_once_with("Voisin", "assez-long", hidden=True, security="WPA2")

    def test_connect_while_busy(self):
        with mock.patch.object(wifi, "available", return_value=True), mock.patch.object(wifi, "connect", return_value=False):
            data = Client().post("/settings/wifi", {"action": "connect", "ssid": "Voisin", "password": ""}).json()
        self.assertEqual(data, {"status": "busy"})
