from unittest import mock

from django.test import Client, TestCase

from nabradio.models import Config, RadioStation


class TestRadioViews(TestCase):
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

    def control(self, daemon_running=True, **data):
        with mock.patch(
            "nabradio.views._signal_radio_daemon", return_value=daemon_running
        ) as signal_mock:
            response = Client().post("/nabradio/control", data)
        return response, signal_mock

    def test_status(self):
        response = Client().get("/nabradio/status")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "is_playing": False,
                "station": {"id": self.station1.id, "name": "One"},
                "can_switch": True,
            },
        )

    def test_play_and_stop(self):
        response, signal_mock = self.control(action="play")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")
        self.assertTrue(response.json()["is_playing"])
        signal_mock.assert_called_once()
        self.assertTrue(Config.load().is_playing)
        self.assertTrue(Client().get("/nabradio/status").json()["is_playing"])

        response, signal_mock = self.control(action="stop")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["is_playing"])
        signal_mock.assert_called_once()
        self.assertFalse(Config.load().is_playing)

    def test_play_given_station(self):
        response, _ = self.control(
            action="play", selected_radio=self.station2.id
        )
        self.assertEqual(response.json()["station"]["name"], "Two")
        config = Config.load()
        self.assertEqual(config.selected_station_id, self.station2.id)
        self.assertTrue(config.is_playing)

    def test_next_previous(self):
        response, _ = self.control(action="next")
        self.assertEqual(response.json()["station"]["name"], "Two")
        self.assertTrue(response.json()["is_playing"])
        response, _ = self.control(action="next")
        self.assertEqual(response.json()["station"]["name"], "One")
        response, _ = self.control(action="previous")
        self.assertEqual(response.json()["station"]["name"], "Two")

    def test_daemon_not_running(self):
        response, _ = self.control(daemon_running=False, action="play")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["status"], "error")
        self.assertFalse(response.json()["is_playing"])
        self.assertFalse(Config.load().is_playing)
        # Stopping is always possible.
        response, _ = self.control(daemon_running=False, action="stop")
        self.assertEqual(response.status_code, 200)

    def test_unknown_action(self):
        response, signal_mock = self.control(action="dance")
        self.assertEqual(response.status_code, 400)
        signal_mock.assert_not_called()

    def test_views_do_not_talk_to_nabd(self):
        with mock.patch("socket.create_connection") as connect_mock:
            self.control(action="play")
            self.control(action="stop")
        connect_mock.assert_not_called()

    def test_settings_card(self):
        response = Client().get("/nabradio/settings")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "js-nabradio-card")
