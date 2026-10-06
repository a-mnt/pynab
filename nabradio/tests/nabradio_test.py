import json
import os
import subprocess
import sys

from asgiref.sync import async_to_sync
from django.test import TestCase

from nabradio import control
from nabradio.models import Config, RadioStation
from nabradio.nabradio import RADIO_REQUEST_ID, NabRadio


class FakeWriter:
    """Records the packets the daemon sends to nabd."""

    def __init__(self):
        self.packets = []

    def write(self, data):
        self.packets.append(json.loads(data.decode("utf8")))

    async def drain(self):
        pass

    def pop_all(self):
        packets = self.packets
        self.packets = []
        return packets


def response(status, **slots):
    packet = {
        "type": "response",
        "request_id": RADIO_REQUEST_ID,
        "status": status,
    }
    packet.update(slots)
    return packet


class TestNabRadioLaunch(TestCase):
    def test_module_loads_before_django_is_configured(self):
        """
        systemd runs "python -m nabradio.nabradio": the module is imported
        before NabService.__init__ configures Django, so it must not load
        the database models at import time.
        """
        env = dict(os.environ)
        env.pop("DJANGO_SETTINGS_MODULE", None)
        result = subprocess.run(
            [sys.executable, "-c", "import nabradio.nabradio"],
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


class TestNabRadio(TestCase):
    def setUp(self):
        RadioStation.objects.all().delete()
        self.station1 = RadioStation.objects.create(
            name="One", stream_url="http://example.com/one.mp3", position=0
        )
        self.station2 = RadioStation.objects.create(
            name="Two", stream_url="http://example.com/two.mp3", position=1
        )
        config = Config.load()
        config.selected_station = self.station1
        config.is_playing = False
        config.save()

        self.service = NabRadio()
        self.service.writer = FakeWriter()
        self.run_async(self.service._startup)
        # Startup cancels any stream left by a previous instance.
        self.assertEqual(
            self.service.writer.pop_all(),
            [{"type": "cancel", "request_id": RADIO_REQUEST_ID}],
        )

    def run_async(self, function, *args):
        return async_to_sync(function)(*args)

    def reload(self):
        self.run_async(self.service.reload_config)
        return self.service.writer.pop_all()

    def receive(self, packet):
        self.run_async(self.service.process_nabd_packet, packet)
        return self.service.writer.pop_all()

    def is_playing(self):
        return Config.load().is_playing

    def test_startup_marks_stopped(self):
        control.set_playing(True)
        service = NabRadio()
        service.writer = FakeWriter()
        self.run_async(service._startup)
        self.assertFalse(self.is_playing())
        # Answer to the startup cancel is ignored.
        self.run_async(
            service.process_nabd_packet,
            response("error", **{"class": "NotPlaying"}),
        )
        self.assertFalse(service.active)

    def test_nothing_to_do(self):
        self.assertEqual(self.reload(), [])

    def test_play_then_stop(self):
        control.set_playing(True)
        packets = self.reload()
        self.assertEqual(len(packets), 1)
        self.assertEqual(packets[0]["type"], "message")
        self.assertEqual(packets[0]["request_id"], RADIO_REQUEST_ID)
        self.assertEqual(
            packets[0]["body"], [{"audio": [self.station1.stream_url]}]
        )
        self.assertTrue("expiration" in packets[0])
        # Signaled again: the stream is not started twice.
        self.assertEqual(self.reload(), [])

        control.set_playing(False)
        self.assertEqual(
            self.reload(), [{"type": "cancel", "request_id": RADIO_REQUEST_ID}]
        )
        # Cancel is sent once.
        self.assertEqual(self.reload(), [])
        self.assertEqual(self.receive(response("canceled")), [])
        self.assertFalse(self.service.active)
        self.assertFalse(self.is_playing())

    def test_change_station_cancels_first(self):
        control.set_playing(True)
        self.reload()

        control.set_playing(True, self.station2)
        # The new station is not queued behind the current one.
        self.assertEqual(
            self.reload(), [{"type": "cancel", "request_id": RADIO_REQUEST_ID}]
        )
        packets = self.receive(response("canceled"))
        self.assertEqual(len(packets), 1)
        self.assertEqual(packets[0]["type"], "message")
        self.assertEqual(
            packets[0]["body"], [{"audio": [self.station2.stream_url]}]
        )
        self.assertTrue(self.is_playing())

    def test_stopped_from_rabbit_button(self):
        control.set_playing(True)
        self.reload()
        # nabd reports the cancel although we did not ask for it.
        self.assertEqual(self.receive(response("canceled")), [])
        self.assertFalse(self.is_playing())
        self.assertFalse(self.service.active)

    def test_stream_ended_or_failed(self):
        for status in ("ok", "expired", "error"):
            control.set_playing(True)
            self.assertEqual(len(self.reload()), 1)
            self.assertEqual(self.receive(response(status)), [])
            self.assertFalse(self.is_playing())

    def test_other_station_asked_when_stream_ends(self):
        control.set_playing(True)
        self.reload()
        # Website selects another station, stream ends before the signal.
        control.set_playing(True, self.station2)
        packets = self.receive(response("ok"))
        self.assertEqual(len(packets), 1)
        self.assertEqual(
            packets[0]["body"], [{"audio": [self.station2.stream_url]}]
        )
        self.assertTrue(self.is_playing())

    def test_cancel_refused_is_retried(self):
        control.set_playing(True)
        self.reload()
        control.set_playing(False)
        self.assertEqual(len(self.reload()), 1)

        async def refused():
            # Message is still in nabd's queue: cancel is refused.
            await self.service.process_nabd_packet(
                response("error", **{"class": "NotPlaying"})
            )
            # A retry is scheduled; run it now instead of waiting.
            self.assertIsNotNone(self.service._retry_task)
            self.service._retry_task.cancel()
            self.service._retry_task = None

        self.run_async(refused)
        self.assertTrue(self.service.active)
        self.assertFalse(self.service.cancel_sent)
        self.assertEqual(
            self.reload(), [{"type": "cancel", "request_id": RADIO_REQUEST_ID}]
        )
        self.assertEqual(self.receive(response("canceled")), [])
        self.assertFalse(self.service.active)

    def test_other_responses_ignored(self):
        control.set_playing(True)
        self.reload()
        self.assertEqual(
            self.receive({"type": "response", "status": "ok"}), []
        )
        self.assertEqual(
            self.receive(
                {"type": "response", "request_id": "other", "status": "ok"}
            ),
            [],
        )
        self.assertEqual(self.receive({"type": "state", "state": "idle"}), [])
        self.assertTrue(self.service.active)
        self.assertTrue(self.is_playing())

    def test_rfid_without_stream(self):
        packets = self.receive(
            {
                "type": "rfid_event",
                "app": "nabradio",
                "event": "detected",
                "uid": "d0:02:1a:03:00:00:00:01",
            }
        )
        self.assertEqual(packets, [])
        self.assertEqual(RadioStation.objects.count(), 2)
        self.assertFalse(self.is_playing())

    def test_rfid_with_stream(self):
        uid = "d0:02:1a:03:00:00:00:02"
        config = Config.load()
        config.json_data_base = json.dumps({uid: self.station2.stream_url})
        config.save()
        packets = self.receive(
            {
                "type": "rfid_event",
                "app": "nabradio",
                "event": "detected",
                "uid": uid,
            }
        )
        self.assertEqual(len(packets), 1)
        self.assertEqual(
            packets[0]["body"], [{"audio": [self.station2.stream_url]}]
        )
        self.assertTrue(self.is_playing())
