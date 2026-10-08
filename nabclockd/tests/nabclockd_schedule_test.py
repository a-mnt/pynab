import datetime
import json
from unittest import mock

from django.test import Client, TestCase

from nabclockd import models, nabclockd, schedule


def respond(service, now):
    return [r for r in service.clock_response(now) if r != "reset_last_chime"]


def service_with(config, asleep):
    service = nabclockd.NabClockd.__new__(nabclockd.NabClockd)
    service.config = config
    service.asleep = asleep
    service.last_chime = None
    service.synchronized_since_boot = lambda: True
    return service


WEEKEND = {
    "week_wake": "07:00",
    "week_sleep": "22:00",
    "weekend_wake": "09:30",
    "weekend_sleep": "23:45",
}


class TestSchedule(TestCase):
    def test_weekend_mapping(self):
        days = schedule.expand("weekend", WEEKEND)
        # Mornings of Saturday and Sunday, nights of Friday and Saturday.
        self.assertEqual(days["friday"], [7, 0, 23, 45])
        self.assertEqual(days["saturday"], [9, 30, 23, 45])
        self.assertEqual(days["sunday"], [9, 30, 22, 0])
        self.assertEqual(days["monday"], [7, 0, 22, 0])
        shown = schedule.collapse(days)
        self.assertEqual(
            {key: shown[key] for key in WEEKEND}, WEEKEND
        )

    def test_bad_time(self):
        with self.assertRaises(ValueError):
            schedule.expand("all", {"wake": "25:00", "sleep": "22:00"})
        with self.assertRaises(KeyError):
            schedule.expand("days", {"wake": "07:00", "sleep": "22:00"})

    def test_usual_times_use_historical_fields(self):
        config = models.Config.load()
        schedule.set_main(config, "weekend", schedule.expand("weekend", WEEKEND))
        config.save()
        config = models.Config.load()
        self.assertTrue(config.settings_per_day)
        self.assertEqual(schedule.main_mode(config), "weekend")
        self.assertEqual(config.wakeup_hour_saturday, 9)
        self.assertEqual(config.sleep_min_friday, 45)
        schedule.set_main(config, "all", schedule.expand("all", {"wake": "06:15", "sleep": "21:00"}))
        self.assertFalse(config.settings_per_day)
        self.assertEqual((config.wakeup_hour, config.wakeup_min), (6, 15))
        self.assertEqual(schedule.main_mode(config), "all")

    def test_alt_defaults_and_bad_data(self):
        config = models.Config.load()
        plan = schedule.alt_plan(config)
        self.assertEqual(plan["name"], "Vacances")
        self.assertEqual(plan["days"]["monday"], [9, 0, 23, 0])
        config.alt_schedule = "not json"
        self.assertEqual(schedule.alt_plan(config)["mode"], "all")
        config.alt_schedule = json.dumps({"name": "Été", "mode": "days", "days": {"monday": [1]}})
        self.assertEqual(schedule.alt_plan(config)["days"]["monday"], [9, 0, 23, 0])


class TestClockWithOtherTimes(TestCase):
    def config(self):
        config = models.Config.load()
        config.wakeup_hour, config.wakeup_min = 7, 0
        config.sleep_hour, config.sleep_min = 22, 0
        config.settings_per_day = False
        config.chime_hour = False
        schedule.set_alt(config, "Vacances", "weekend", schedule.expand("weekend", WEEKEND))
        return config

    def test_usual_times_unchanged(self):
        config = self.config()
        # Saturday 8:00: usual times say awake.
        saturday_8am = datetime.datetime(2026, 10, 10, 8, 0)
        self.assertEqual(respond(service_with(config, True), saturday_8am), ["wakeup"])

    def test_other_times_followed(self):
        config = self.config()
        config.use_alt_schedule = True
        saturday_8am = datetime.datetime(2026, 10, 10, 8, 0)
        saturday_930 = datetime.datetime(2026, 10, 10, 9, 30)
        friday_2230 = datetime.datetime(2026, 10, 9, 22, 30)
        sunday_2230 = datetime.datetime(2026, 10, 11, 22, 30)
        saturday_0030 = datetime.datetime(2026, 10, 10, 0, 30)  # Friday night
        self.assertEqual(respond(service_with(config, True), saturday_8am), [])
        self.assertEqual(respond(service_with(config, True), saturday_930), ["wakeup"])
        self.assertEqual(respond(service_with(config, False), friday_2230), [])
        self.assertEqual(respond(service_with(config, False), sunday_2230), ["sleep"])
        self.assertEqual(respond(service_with(config, False), saturday_0030), ["sleep"])


class TestScheduleViews(TestCase):
    def setUp(self):
        patch = mock.patch.object(nabclockd.NabClockd, "signal_daemon")
        self.signal = patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch("nabclockd.views.SettingsView.get_system_tz", return_value="Europe/Paris")
        patch.start()
        self.addCleanup(patch.stop)

    def test_settings_page(self):
        response = Client().get("/nabclockd/settings")
        self.assertContains(response, "Horaires habituels")
        self.assertContains(response, 'name="alt_name" value="Vacances"')
        self.assertContains(response, "Horaires différents le week-end")
        self.assertEqual(len(response.context["plans"][0]["days"]), 7)

    def test_save_both_sets(self):
        data = {
            "main_mode": "weekend",
            "main_week_wake": "07:00",
            "main_week_sleep": "22:00",
            "main_weekend_wake": "09:00",
            "main_weekend_sleep": "23:30",
            "alt_mode": "all",
            "alt_name": "Été",
            "alt_wake": "10:00",
            "alt_sleep": "23:59",
            "chime_hour": "false",
        }
        response = Client().post("/nabclockd/settings", data)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["saved"])
        config = models.Config.load()
        self.assertEqual(schedule.main_mode(config), "weekend")
        self.assertEqual(config.sleep_hour_saturday, 23)
        self.assertEqual(config.wakeup_hour_monday, 7)
        plan = schedule.alt_plan(config)
        self.assertEqual((plan["name"], plan["mode"], plan["days"]["sunday"]), ("Été", "all", [10, 0, 23, 59]))
        self.assertFalse(config.chime_hour)
        self.signal.assert_called()

    def test_bad_times_ignored(self):
        Client().post("/nabclockd/settings", {"main_mode": "all", "main_wake": "nope", "main_sleep": "22:00"})
        self.assertEqual(models.Config.load().wakeup_hour, 7)

    def test_switch(self):
        response = Client().post("/nabclockd/schedule", {"use_alt_schedule": "true"})
        data = response.json()
        self.assertEqual((data["status"], data["alt"], data["name"], data["wake"]), ("ok", True, "Vacances", "09:00"))
        self.assertTrue(models.Config.load().use_alt_schedule)
        self.signal.assert_called_once()
        data = Client().post("/nabclockd/schedule", {"use_alt_schedule": "false"}).json()
        self.assertEqual((data["alt"], data["wake"]), (False, "07:00"))
        self.assertEqual(Client().post("/nabclockd/schedule", {"use_alt_schedule": "x"}).status_code, 400)

    def test_home_shows_the_switch(self):
        response = Client().get("/")
        self.assertContains(response, "js-schedule-switch")
        self.assertContains(response, "Habituels · 07:00 → 22:00")
