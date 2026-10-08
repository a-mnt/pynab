import datetime
import os

from django.http import JsonResponse
from django.shortcuts import render
from django.utils.translation import gettext_lazy as _
from django.views.generic import TemplateView, View
from pytz import common_timezones

from . import rfid_data, schedule
from .models import Config
from .nabclockd import NabClockd


class SettingsView(TemplateView):
    template_name = "nabclockd/settings.html"
    daysOfTheWeek = [
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
        "saturday",
        "sunday",
    ]

    day_labels = (
        _("Lun"),
        _("Mar"),
        _("Mer"),
        _("Jeu"),
        _("Ven"),
        _("Sam"),
        _("Dim"),
    )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        config = Config.load()
        context["config"] = config
        context["timezones"] = common_timezones
        context["current_timezone"] = self.get_system_tz()
        alt = schedule.alt_plan(config)
        context["plans"] = [
            {
                "prefix": "main",
                "mode": schedule.main_mode(config),
                "times": schedule.collapse(schedule.main_days(config)),
                "active": not config.use_alt_schedule,
            },
            {
                "prefix": "alt",
                "name": alt["name"],
                "mode": alt["mode"],
                "times": schedule.collapse(alt["days"]),
                "active": config.use_alt_schedule,
            },
        ]
        labels = dict(zip(schedule.DAYS, self.day_labels))
        for plan in context["plans"]:
            plan["days"] = [
                (labels[day], day, wake, sleep)
                for day, wake, sleep in plan["times"]["days"]
            ]
        context["alt_name"] = alt["name"]
        return context

    @staticmethod
    def _plan_values(post, prefix):
        start = prefix + "_"
        return {
            key[len(start):]: value
            for key, value in post.items()
            if key.startswith(start)
        }

    def post(self, request, *args, **kwargs):
        config = Config.load()
        if "chime_hour" in request.POST:
            config.chime_hour = request.POST["chime_hour"] == "true"
        if "wakeup_time" in request.POST:
            (hour, min) = self.parse_time(request.POST["wakeup_time"])
            config.wakeup_hour = hour
            config.wakeup_min = min
        if "sleep_time" in request.POST:
            (hour, min) = self.parse_time(request.POST["sleep_time"])
            config.sleep_hour = hour
            config.sleep_min = min
        for dayName in SettingsView.daysOfTheWeek:
            if ("wakeup_time_" + dayName) in request.POST:
                (hour, min) = self.parse_time(
                    request.POST["wakeup_time_" + dayName]
                )
                setattr(config, "wakeup_hour_" + dayName, hour)
                setattr(config, "wakeup_min_" + dayName, min)
            if ("sleep_time_" + dayName) in request.POST:
                (hour, min) = self.parse_time(
                    request.POST["sleep_time_" + dayName]
                )
                setattr(config, "sleep_hour_" + dayName, hour)
                setattr(config, "sleep_min_" + dayName, min)
        if "timezone" in request.POST:
            selected_tz = request.POST["timezone"]
            if selected_tz in common_timezones:
                self.set_system_tz(selected_tz)
        if "play_wakeup_sleep_sounds" in request.POST:
            config.play_wakeup_sleep_sounds = (
                request.POST["play_wakeup_sleep_sounds"] == "true"
            )
        if "settings_per_day" in request.POST:
            config.settings_per_day = (
                request.POST["settings_per_day"] == "true"
            )
        # Usual and other times, as written on the page.
        main_mode = request.POST.get("main_mode")
        if main_mode in schedule.MODES:
            try:
                days = schedule.expand(
                    main_mode, self._plan_values(request.POST, "main")
                )
                schedule.set_main(config, main_mode, days)
            except (KeyError, ValueError):
                pass
        alt_mode = request.POST.get("alt_mode")
        if alt_mode in schedule.MODES:
            try:
                days = schedule.expand(
                    alt_mode, self._plan_values(request.POST, "alt")
                )
                schedule.set_alt(
                    config, request.POST.get("alt_name", ""), alt_mode, days
                )
            except (KeyError, ValueError):
                pass
        if "use_alt_schedule" in request.POST:
            config.use_alt_schedule = (
                request.POST["use_alt_schedule"] == "true"
            )
        config.save()
        NabClockd.signal_daemon()
        context = self.get_context_data(**kwargs)
        context["saved"] = True
        return render(request, SettingsView.template_name, context=context)

    def parse_time(self, hour_str):
        [hour_str, min_str] = hour_str.split(":")
        return (int(hour_str), int(min_str))

    def get_system_tz(self):
        with open("/etc/timezone") as w:
            return w.read().strip()

    def set_system_tz(self, tz):
        if tz != self.get_system_tz():
            with open("/etc/timezone", "w") as w:
                w.write("%s\n" % tz)
                os.system(
                    f"/bin/ln -fs /usr/share/zoneinfo/{tz} /etc/localtime"
                )


class RFIDDataView(TemplateView):
    template_name = "nabclockd/rfid-data.html"

    def get(self, request, *args, **kwargs):
        """
        Unserialize RFID application data
        """
        type = "sleep"
        data = request.GET.get("data", None)
        if data:
            type = rfid_data.unserialize(data.encode("utf8"))
        context = self.get_context_data(**kwargs)
        context["type"] = type
        return render(request, RFIDDataView.template_name, context=context)

    def post(self, request, *args, **kwargs):
        """
        Serialize RFID application data
        """
        type = "sleep"
        if "type" in request.POST:
            type = request.POST["type"]
        data = rfid_data.serialize(type)
        data = data.decode("utf8")
        return JsonResponse({"data": data})


def schedule_summary(config=None):
    """Set of times followed and today's times, for the home page."""
    config = config or Config.load()
    # Until 3am, the night still belongs to the previous day.
    now = datetime.datetime.now() - datetime.timedelta(hours=3)
    return schedule.summary(config, schedule.DAYS[now.weekday()])


class ScheduleView(View):
    """
    GET: set of times followed. POST use_alt_schedule=true|false: switch
    between the usual and the other times, at once.
    """

    def get(self, request, *args, **kwargs):
        return JsonResponse({"status": "ok", **schedule_summary()})

    def post(self, request, *args, **kwargs):
        value = request.POST.get("use_alt_schedule")
        if value not in ("true", "false"):
            return JsonResponse(
                {"status": "error", "message": "Valeur invalide."}, status=400
            )
        config = Config.load()
        config.use_alt_schedule = value == "true"
        config.save()
        NabClockd.signal_daemon()
        return JsonResponse({"status": "ok", **schedule_summary(config)})
