import datetime
import asyncio
import fcntl
import json
import os
import tempfile
import threading
import time
from unittest import mock

from django.core.cache import cache
from django.http import JsonResponse
from django.test import Client, TestCase

from nabcommon import nabservice
from nabweb import views


class TestView(TestCase):
    def test_get_home(self):
        c = Client()
        response = c.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.templates[0].name, "nabweb/index.html")
        self.assertTrue("services" in response.context)
        self.assertTrue("nabmastodond" in response.context["services"])
        self.assertTrue("current_locale" in response.context)
        self.assertEqual(response.context["current_locale"], "fr_FR")

    def test_post_home_empty(self):
        c = Client()
        response = c.post("/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.templates[0].name, "nabweb/index.html")
        self.assertTrue("services" in response.context)
        self.assertTrue("nabmastodond" in response.context["services"])
        self.assertTrue("current_locale" in response.context)
        self.assertEqual(response.context["current_locale"], "fr_FR")

    def test_post_home_set_locale(self):
        c = Client()
        response = c.post("/", {"locale": "en_US"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.templates[0].name, "nabweb/index.html")
        self.assertTrue("services" in response.context)
        self.assertTrue("nabmastodond" in response.context["services"])
        self.assertTrue("current_locale" in response.context)
        self.assertEqual(response.context["current_locale"], "en_US")

    def test_get_settings(self):
        c = Client()
        response = c.get("/settings/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.templates[0].name, "nabweb/settings/index.html"
        )
        self.assertEqual(response.context["current_locale"], "fr_FR")
        self.assertTrue("locales" in response.context)
        self.assertFalse(response.context["locale_saved"])
        # nabd is not running in this test
        self.assertEqual(response.context["rfid_support"]["status"], "error")

    def test_post_settings_set_locale(self):
        c = Client()
        response = c.post("/settings/", {"locale": "en_US"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.templates[0].name, "nabweb/settings/index.html"
        )
        self.assertEqual(response.context["current_locale"], "en_US")
        self.assertTrue(response.context["locale_saved"])

    def test_get_help_pages(self):
        c = Client()
        for url in ("/help/", "/help/weather", "/help/airquality"):
            response = c.get(url)
            self.assertEqual(response.status_code, 200)
            template_names = [t.name for t in response.templates]
            self.assertTrue("nabweb/_base.html" in template_names)

    def test_home_state(self):
        cache.clear()
        response = Client().get("/")
        self.assertEqual(response.status_code, 200)
        # nabd is not running in this test
        self.assertIsNone(response.context["rabbit_state"])
        self.assertContains(response, "Injoignable")
        self.assertTrue("ssh" in response.context)
        self.assertEqual(response.context["alerts"], [])
        self.assertNotContains(response, "Alertes importantes")
        self.assertContains(response, "js-quick-action")

    def test_home_update_alert(self):
        cache.set("git/info/pynab", {"status": "ok", "commits_count": 2}, 60)
        try:
            response = Client().get("/")
        finally:
            cache.clear()
        self.assertEqual(response.context["alerts"], ["update"])
        self.assertContains(response, "Alertes importantes")

    def test_home_no_alert_when_up_to_date(self):
        cache.set("git/info/pynab", {"status": "ok", "commits_count": 0}, 60)
        try:
            response = Client().get("/")
        finally:
            cache.clear()
        self.assertEqual(response.context["alerts"], [])

    def test_services_cards(self):
        response = Client().get("/services/")
        cards = response.context["service_cards"]
        names = [card["name"] for card in cards]
        self.assertTrue("nabradio" in names)
        # No settings page: not listed, instead of a card that fails to load.
        self.assertFalse("nabwebhook" in names)
        for card in cards:
            self.assertEqual(Client().get(card["url"]).status_code, 200)
            self.assertNotEqual(str(card["title"]), "")

    def test_system_page_has_quick_actions(self):
        response = Client().get("/system-info/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "js-quick-action")
        self.assertContains(response, "/system-info/shutdown/reboot")
        self.assertContains(response, "/system-info/shutdown/shutdown")

    def test_missing_program_does_not_crash(self):
        returncode, stdout, stderr = views._run_command(
            ["this-program-does-not-exist"]
        )
        self.assertEqual((returncode, stdout), (127, ""))

    def test_get_rfid(self):
        c = Client()
        response = c.get("/rfid/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.templates[0].name, "nabweb/rfid/index.html")
        self.assertTrue("rfid_services" in response.context)
        rfid_services = response.context["rfid_services"]
        for item in rfid_services:
            self.assertTrue("app" in item)
            self.assertTrue("name" in item)
        self.assertTrue("rfid_support" in response.context)
        self.assertEqual(response.context["rfid_support"]["status"], "error")

    def test_get_services(self):
        c = Client()
        response = c.get("/services/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.templates[0].name, "nabweb/services/index.html"
        )
        self.assertTrue("services" in response.context)
        self.assertFalse("nabmastodond" in response.context["services"])
        self.assertTrue("current_locale" in response.context)
        self.assertEqual(response.context["current_locale"], "fr_FR")


class TestGitInfo(TestCase):
    def test_git_runs_as_repository_owner(self):
        """
        The web server runs as root and repositories belong to another
        user: git refuses to read them ("dubious ownership") unless it is
        run as their owner.
        """
        other_uid = str(os.geteuid() + 1)
        with mock.patch(
            "nabweb.views._run_command", return_value=(0, "abc", "")
        ) as run_mock:
            views.GitInfo._git("/repo", "rev-parse", "HEAD", sudo_uid=other_uid)
            views.GitInfo._git(
                "/repo", "rev-parse", "HEAD", sudo_uid=str(os.geteuid())
            )
        self.assertEqual(
            run_mock.call_args_list[0][0][0],
            ["sudo", "-u", f"#{other_uid}"]
            + ["git", "-C", "/repo", "rev-parse", "HEAD"],
        )
        # No sudo needed when the repository is ours.
        self.assertEqual(
            run_mock.call_args_list[1][0][0],
            ["git", "-C", "/repo", "rev-parse", "HEAD"],
        )

    def test_repository_info_uses_owner_for_every_command(self):
        commands = []

        def fake_run(cmd, cwd=None):
            commands.append(cmd)
            return (0, "1", "")

        other_uid = os.geteuid() + 1
        with mock.patch("nabweb.views._run_command", side_effect=fake_run):
            with mock.patch("nabweb.views.os.stat") as stat_mock:
                stat_mock.return_value.st_uid = other_uid
                info = views.GitInfo.do_get_repository_info(
                    "pynab", ".", force=True
                )
        self.assertEqual(info["status"], "ok")
        self.assertTrue(len(commands) >= 8)
        for cmd in commands:
            self.assertEqual(cmd[:3], ["sudo", "-u", f"#{other_uid}"])

    def test_error_message_includes_git_error(self):
        with mock.patch(
            "nabweb.views._run_command",
            return_value=(128, "", "fatal: detected dubious ownership"),
        ):
            info = views.GitInfo.do_get_repository_info("pynab", ".")
        self.assertEqual(info["status"], "error")
        self.assertTrue("dubious ownership" in info["message"])


class TestUpgradeProgress(TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp()
        os.close(handle)
        patcher = mock.patch.object(
            views.NabWebUpgradeNowView, "UPGRADE_FILE", self.path
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(lambda: os.path.exists(self.path) and os.remove(self.path))

    def test_no_upgrade_file(self):
        os.remove(self.path)
        self.assertIsNone(views.NabWebUpgradeNowView.current_step())
        response = Client().get("/upgrade/now")
        self.assertEqual(response.json(), {"status": "done", "skipped": []})

    def test_file_not_locked_means_no_upgrade(self):
        with open(self.path, "w") as upgrade_f:
            upgrade_f.write("Updating data models - 10/14\n")
        self.assertIsNone(views.NabWebUpgradeNowView.current_step())

    def test_running_upgrade_reports_its_step(self):
        """
        The upgrade keeps the file locked while it runs and writes its
        current step in it: that step must reach the web page.
        """
        with open(self.path, "w") as upgrade_f:
            fcntl.flock(upgrade_f, fcntl.LOCK_EX)
            upgrade_f.write("Updating data models - 10/14\n")
            upgrade_f.flush()
            response = Client().get("/upgrade/now")
            self.assertEqual(
                response.json(),
                {"status": "ok", "message": "Updating data models - 10/14", "skipped": []},
            )
        # Lock released: the upgrade is over.
        self.assertEqual(
            Client().get("/upgrade/now").json(), {"status": "done", "skipped": []}
        )

    def test_page_lists_the_steps(self):
        response = Client().get("/upgrade/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content.decode().count("data-step="), 15)


class TestEars(TestCase):
    def gestalt(self, left, right):
        return {
            "status": "ok",
            "result": {
                "hardware": {
                    "left_ear_status": left,
                    "right_ear_status": right,
                }
            },
        }

    def test_parse_ears(self):
        ears = views.parse_ears(
            self.gestalt("ok (position=5)", "ok (position unknown)")
        )
        self.assertEqual(ears["left"], {"working": True, "position": 5})
        self.assertEqual(ears["right"], {"working": True, "position": None})

    def test_parse_broken_and_virtual_ears(self):
        ears = views.parse_ears(self.gestalt("broken", "virtual (position=16)"))
        self.assertEqual(ears["left"], {"working": False, "position": None})
        self.assertEqual(ears["right"], {"working": True, "position": 16})

    def test_parse_ears_without_nabd(self):
        self.assertIsNone(
            views.parse_ears({"status": "error", "message": "no nabd"})
        )

    def test_home_shows_ears(self):
        response = Client().get("/")
        # nabd is not running in this test: state unknown, not "broken".
        self.assertIsNone(response.context["ears"])
        self.assertContains(response, "js-ears")
        self.assertNotContains(response, "en panne")

    def test_ears_status_without_nabd(self):
        response = Client().get("/ears")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "error")

    def test_move_rejects_invalid_positions(self):
        for data in ({}, {"left": "17"}, {"right": "-1"}, {"left": "abc"}):
            response = Client().post("/ears", data)
            self.assertEqual(response.status_code, 400, data)


class TestSleepOverride(TestCase):
    def test_override_is_saved_for_nabclockd(self):
        """
        nabclockd wakes the rabbit up (or sends it back to sleep) unless it
        is told the user decided otherwise: the override must be saved in
        its own configuration.
        """
        from nabclockd.models import Config as ClockConfig

        self.assertIsNone(ClockConfig.load().sleep_wakeup_override)
        with mock.patch("nabweb.views.os.kill") as kill_mock:
            with mock.patch(
                "builtins.open", mock.mock_open(read_data="4242\n")
            ):
                views._set_sleep_override(True)
        self.assertTrue(ClockConfig.load().sleep_wakeup_override)
        # nabclockd is told to reload its configuration.
        kill_mock.assert_called_once_with(4242, views.signal.SIGUSR1)

        views._set_sleep_override(False)
        self.assertFalse(ClockConfig.load().sleep_wakeup_override)

    def test_clock_daemon_not_running(self):
        with mock.patch.object(views, "CLOCK_PIDFILE", "/nonexistent/pid"):
            self.assertFalse(views._clock_daemon_active())

    def test_clock_daemon_running_but_clock_not_synchronized(self):
        handle, pidfile = tempfile.mkstemp()
        os.write(handle, str(os.getpid()).encode())
        os.close(handle)
        self.addCleanup(os.remove, pidfile)
        with mock.patch.object(views, "CLOCK_PIDFILE", pidfile):
            with mock.patch.object(
                views, "CLOCK_SYNCHRONIZED_FILE", "/nonexistent/synchronized"
            ):
                self.assertFalse(views._clock_daemon_active())
            with mock.patch.object(views, "CLOCK_SYNCHRONIZED_FILE", pidfile):
                self.assertTrue(views._clock_daemon_active())

    def test_stop_radio_before_sleep(self):
        from nabradio import control

        control.status_payload()
        with mock.patch(
            "nabradio.views._signal_radio_daemon", return_value=True
        ) as signal_mock:
            views._stop_radio()
            signal_mock.assert_not_called()
            control.set_playing(True)
            views._stop_radio()
            signal_mock.assert_called_once()
        self.assertFalse(control.get_desired()[0])


class TestNabdClientBase(TestCase):
    async def mock_nabd_service_handler(self, reader, writer):
        self.service_writer = writer
        if hasattr(self, "state_packet"):
            writer.write(self.state_packet)
        else:
            writer.write(b'{"type":"state","state":"idle"}\r\n')
        await writer.drain()
        while not reader.at_eof():
            line = await reader.readline()
            if line != b"":
                packet = json.loads(line.decode("utf8"))
                if packet["type"] == "gestalt" and hasattr(
                    self, "gestalt_answer"
                ):
                    response_packet = self.gestalt_answer.copy()
                    if "request_id" in packet:
                        response_packet["request_id"] = packet["request_id"]
                    response_json = json.JSONEncoder().encode(response_packet)
                    response_json += "\r\n"
                    writer.write(response_json.encode("utf8"))
                    self.gestalt_answered += 1
                elif hasattr(self, "packet_handler"):
                    self.packet_handler(packet, writer)

    def mock_nabd_thread_entry_point(self, kwargs):
        self.mock_nabd_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.mock_nabd_loop)
        server_task = self.mock_nabd_loop.create_task(
            asyncio.start_server(
                self.mock_nabd_service_handler,
                "localhost",
                nabservice.NabService.PORT_NUMBER,
            )
        )
        try:
            self.mock_nabd_loop.run_forever()
        finally:
            server = server_task.result()
            server.close()
            if self.service_writer:
                self.service_writer.close()
            self.mock_nabd_loop.close()

    def setUp(self):
        self.service_writer = None
        self.mock_nabd_loop = None
        self.mock_nabd_thread = threading.Thread(
            target=self.mock_nabd_thread_entry_point, args=[self]
        )
        self.mock_nabd_thread.start()
        time.sleep(1)

    def tearDown(self):
        self.mock_nabd_loop.call_soon_threadsafe(
            lambda: self.mock_nabd_loop.stop()
        )
        self.mock_nabd_thread.join(3)


class TestRfidView(TestNabdClientBase):
    def test_get_rfid_unsupported(self):
        self.gestalt_answer = {"type": "response", "hardware": {"rfid": False}}
        self.gestalt_answered = 0
        c = Client()
        response = c.get("/rfid/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.gestalt_answered, 1)
        self.assertEqual(response.templates[0].name, "nabweb/rfid/index.html")
        self.assertTrue("rfid_services" in response.context)
        rfid_services = response.context["rfid_services"]
        for item in rfid_services:
            self.assertTrue("app" in item)
            self.assertTrue("name" in item)
        self.assertTrue("rfid_support" in response.context)
        self.assertEqual(response.context["rfid_support"]["status"], "ok")
        self.assertEqual(response.context["rfid_support"]["available"], False)

    def test_get_rfid_supported(self):
        self.gestalt_answer = {"type": "response", "hardware": {"rfid": True}}
        self.gestalt_answered = 0
        c = Client()
        response = c.get("/rfid/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.gestalt_answered, 1)
        self.assertEqual(response.templates[0].name, "nabweb/rfid/index.html")
        self.assertTrue("rfid_services" in response.context)
        rfid_services = response.context["rfid_services"]
        for item in rfid_services:
            self.assertTrue("app" in item)
            self.assertTrue("name" in item)
        self.assertTrue("rfid_support" in response.context)
        self.assertEqual(response.context["rfid_support"]["status"], "ok")
        self.assertEqual(response.context["rfid_support"]["available"], True)


class TestRfidReadView(TestNabdClientBase):
    def read_timeout_handler(self, packet, writer):
        self.packets.append(packet)
        response_packet = None
        if packet["type"] == "mode":
            response_packet = {"type": "response"}
            if "request_id" in packet:
                response_packet["request_id"] = packet["request_id"]
        elif packet["type"] == "command":
            response_packet = {"type": "response"}
            if "request_id" in packet:
                response_packet["request_id"] = packet["request_id"]
        if response_packet:
            response_json = json.JSONEncoder().encode(response_packet)
            response_json += "\r\n"
            writer.write(response_json.encode("utf8"))

    def test_read_timeout(self):
        self.packet_handler = self.read_timeout_handler
        self.packets = []
        c = Client()
        response = c.post("/rfid/read")
        self.assertEqual(response.status_code, 200)
        print(self.packets)
        self.assertEqual(len(self.packets), 2)
        self.assertTrue(isinstance(response, JsonResponse))
        json_response = json.loads(response.content.decode("utf8"))
        self.assertTrue("status" in json_response)
        self.assertEqual(json_response["status"], "timeout")

    def read_rfid_handler(self, packet, writer):
        self.packets.append(packet)
        response_packet = None
        if packet["type"] == "mode":
            response_packet = {"type": "response"}
            if "request_id" in packet:
                response_packet["request_id"] = packet["request_id"]
        elif packet["type"] == "command":
            response_packet = {"type": "response"}
            if "request_id" in packet:
                response_packet["request_id"] = packet["request_id"]
        if response_packet:
            response_json = json.JSONEncoder().encode(response_packet)
            response_json += "\r\n"
            writer.write(response_json.encode("utf8"))
        if packet["type"] == "command":
            event_json = json.JSONEncoder().encode(self.rfid_event)
            event_json += "\r\n"
            writer.write(event_json.encode("utf8"))

    def test_read_clear(self):
        self.packet_handler = self.read_rfid_handler
        self.rfid_event = {
            "type": "rfid_event",
            "event": "detected",
            "tech": "st25tb",
            "uid": "d0:02:18:01:02:03:04:05",
            "support": "empty",
        }
        self.packets = []
        c = Client()
        response = c.post("/rfid/read")
        self.assertEqual(response.status_code, 200)
        print(self.packets)
        self.assertEqual(len(self.packets), 2)
        self.assertEqual(self.packets[0]["type"], "mode")
        self.assertEqual(self.packets[1]["type"], "command")
        self.assertTrue("sequence" in self.packets[1])
        self.assertTrue(isinstance(response, JsonResponse))
        json_response = json.loads(response.content.decode("utf8"))
        self.assertTrue("status" in json_response)
        self.assertEqual(json_response["status"], "ok")
        self.assertTrue("event" in json_response)
        self.assertTrue("support" in json_response["event"])
        self.assertEqual(json_response["event"]["support"], "empty")

    def test_read_formatted(self):
        self.packet_handler = self.read_rfid_handler
        self.rfid_event = {
            "type": "rfid_event",
            "event": "detected",
            "tech": "st25tb",
            "uid": "d0:02:18:01:02:03:04:05",
            "app": "nabtaichid",
            "picture": 8,
            "data": "",
            "support": "formatted",
        }
        self.packets = []
        c = Client()
        response = c.post("/rfid/read")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.packets), 2)
        self.assertEqual(self.packets[0]["type"], "mode")
        self.assertEqual(self.packets[1]["type"], "command")
        self.assertTrue("sequence" in self.packets[1])
        self.assertTrue(isinstance(response, JsonResponse))
        json_response = json.loads(response.content.decode("utf8"))
        self.assertTrue("status" in json_response)
        self.assertEqual(json_response["status"], "ok")
        self.assertTrue("event" in json_response)
        self.assertTrue("support" in json_response["event"])
        self.assertEqual(json_response["event"]["support"], "formatted")
        self.assertTrue("app" in json_response["event"])
        self.assertEqual(json_response["event"]["app"], "nabtaichid")
        self.assertTrue("picture" in json_response["event"])
        self.assertEqual(json_response["event"]["picture"], 8)


class TestRfidWriteView(TestNabdClientBase):
    def write_rfid_handler(self, packet, writer):
        self.packets.append(packet)
        response_packet = None
        if packet["type"] == "rfid_write":
            response_packet = self.write_response
            if "request_id" in packet:
                response_packet["request_id"] = packet["request_id"]
        if response_packet:
            response_json = json.JSONEncoder().encode(response_packet)
            response_json += "\r\n"
            writer.write(response_json.encode("utf8"))

    def test_write_ok(self):
        self.packet_handler = self.write_rfid_handler
        self.write_response = {"type": "response", "status": "ok"}
        self.packets = []
        c = Client()
        response = c.post(
            "/rfid/write",
            {
                "tech": "st25tb",
                "uid": "d0:02:18:01:02:03:04:05",
                "app": "nabweatherd",
                "picture": 8,
                "data": "\x02",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.packets), 1)
        self.assertTrue(isinstance(response, JsonResponse))
        json_response = json.loads(response.content.decode("utf8"))
        self.assertTrue("status" in json_response)
        self.assertEqual(json_response["status"], "ok")
        self.assertTrue("rfid" in json_response)
        self.assertTrue("uid" in json_response["rfid"])
        self.assertEqual(
            json_response["rfid"]["uid"], "d0:02:18:01:02:03:04:05"
        )

    def test_write_missing_uid(self):
        self.packet_handler = self.write_rfid_handler
        self.write_response = {"type": "response", "status": "ok"}
        self.packets = []
        c = Client()
        response = c.post(
            "/rfid/write", {"app": "nabweatherd", "picture": 8, "data": "\x02"}
        )
        self.assertEqual(response.status_code, 400)

    def test_write_timeout(self):
        self.packet_handler = self.write_rfid_handler
        self.write_response = {"type": "response", "status": "timeout"}
        self.packets = []
        c = Client()
        response = c.post(
            "/rfid/write",
            {
                "tech": "st25tb",
                "uid": "d0:02:18:01:02:03:04:05",
                "app": "nabweatherd",
                "picture": 8,
                "data": "\x02",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.packets), 1)
        self.assertTrue(isinstance(response, JsonResponse))
        json_response = json.loads(response.content.decode("utf8"))
        self.assertTrue("status" in json_response)
        self.assertEqual(json_response["status"], "timeout")


class TestShutdownView(TestNabdClientBase):
    def shutdown_view_handler(self, packet, writer):
        self.packets.append(packet)
        response_packet = None
        if packet["type"] == "shutdown":
            response_packet = self.write_response
            if "request_id" in packet:
                response_packet["request_id"] = packet["request_id"]
        if response_packet:
            response_json = json.JSONEncoder().encode(response_packet)
            response_json += "\r\n"
            writer.write(response_json.encode("utf8"))

    def test_post_reboot_action(self):
        self.packet_handler = self.shutdown_view_handler
        self.write_response = {"type": "response", "status": "ok"}
        self.packets = []
        c = Client()
        response = c.post(
            "/system-info/shutdown/reboot",
            {"mode": "reboot"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.packets), 1)
        self.assertTrue(isinstance(response, JsonResponse))
        json_response = json.loads(response.content.decode("utf8"))
        self.assertTrue("status" in json_response)
        self.assertEqual(json_response["status"], "ok")

    def test_post_shutdown_action(self):
        self.packet_handler = self.shutdown_view_handler
        self.write_response = {"type": "response", "status": "ok"}
        self.packets = []
        c = Client()
        response = c.post(
            "/system-info/shutdown/shutdown",
            {"mode": "shutdown"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.packets), 1)
        self.assertTrue(isinstance(response, JsonResponse))
        json_response = json.loads(response.content.decode("utf8"))
        self.assertTrue("status" in json_response)
        self.assertEqual(json_response["status"], "ok")


class TestLightUpgrade(TestCase):
    """The web site tells upgrade.sh which drivers have a new version."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def remember(self, repository, **info):
        info = dict({"status": "ok", "name": repository, "info_date": datetime.datetime.now()}, **info)
        cache.set(f"git/info/{repository}", info, 60)

    def test_drivers_to_update(self):
        for driver in views.GitInfo.DRIVERS:
            self.remember(driver, commits_count=0)
        self.remember("ears_driver", commits_count=2)
        self.assertEqual(views.GitInfo.drivers_to_update(), ["ears_driver"])
        # Never checked: included, to be safe.
        cache.delete("git/info/nabblockly")
        self.assertEqual(views.GitInfo.drivers_to_update(), ["ears_driver", "nabblockly"])

    def test_updatable_when_only_a_driver_changed(self):
        for driver in views.GitInfo.DRIVERS:
            self.remember(driver, commits_count=0)
        pynab = {"status": "ok", "commits_count": 0, "local_commits_count": 0}
        self.assertFalse(views.GitInfo.is_updatable(pynab))
        self.remember("sound_driver", commits_count=1)
        self.assertTrue(views.GitInfo.is_updatable(pynab))
        pynab["local_commits_count"] = 1
        self.assertFalse(views.GitInfo.is_updatable(pynab))

    def test_upgrade_passes_the_drivers(self):
        for driver in views.GitInfo.DRIVERS:
            self.remember(driver, commits_count=0)
        self.remember("nfc_driver", commits_count=3)
        with mock.patch.object(views.GitInfo, "get_root_dir", return_value="/opt/pynab"), \
                mock.patch("os.stat") as stat, \
                mock.patch.object(views, "_run_command", return_value=(0, "OK", "")), \
                mock.patch("builtins.open", mock.mock_open()), \
                mock.patch("subprocess.Popen") as popen:
            stat.return_value.st_uid = 1000
            response = Client().post("/upgrade/now")
        self.assertEqual(response.json()["status"], "ok")
        command = popen.call_args[0][0]
        self.assertEqual(command[-2:], ["/opt/pynab/upgrade.sh", "--drivers=nfc_driver"])

    def test_skipped_steps_reported(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as skipped:
            skipped.write("2\n5\n5\n11\n")
        with mock.patch.object(views.NabWebUpgradeNowView, "SKIPPED_FILE", skipped.name), \
                mock.patch.object(views.NabWebUpgradeNowView, "current_step", return_value="Updating data models - 10/14"):
            data = Client().get("/upgrade/now").json()
        os.unlink(skipped.name)
        self.assertEqual(data, {"status": "ok", "message": "Updating data models - 10/14", "skipped": [2, 5, 11]})
