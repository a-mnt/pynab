"""
The two sets of wake up / sleep times of the rabbit.

- The usual times are the historical fields of Config (common times, or
  one time per day with settings_per_day).
- The other times ("Vacances" by default) are kept as JSON in
  Config.alt_schedule.
Config.use_alt_schedule tells which set the rabbit follows.

Each set can be written in three ways (its "mode"), only for the page:
- "all": the same times every day;
- "weekend": week times, and week-end times for the nights of Friday and
  Saturday and the mornings of Saturday and Sunday;
- "days": one time per day.
Whatever the mode, the times are stored day by day, so the clock always
reads them the same way. A day runs until 3 am: going to bed at 1 am on
Saturday morning is the Friday night.

No Django import here: the clock daemon uses this module too.
"""

import json

DAYS = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)
MODES = ("all", "weekend", "days")
DEFAULT_ALT_NAME = "Vacances"
# Week-end: mornings of Saturday and Sunday, nights of Friday and Saturday.
WEEKEND_MORNINGS = ("saturday", "sunday")
WEEKEND_NIGHTS = ("friday", "saturday")


def _time(hour, minute):
    return [int(hour), int(minute)]


def main_days(config):
    """Usual times, day by day: {day: [wake_h, wake_m, sleep_h, sleep_m]}."""
    if config.settings_per_day:
        return {
            day: [
                getattr(config, "wakeup_hour_" + day),
                getattr(config, "wakeup_min_" + day),
                getattr(config, "sleep_hour_" + day),
                getattr(config, "sleep_min_" + day),
            ]
            for day in DAYS
        }
    return {
        day: [
            config.wakeup_hour,
            config.wakeup_min,
            config.sleep_hour,
            config.sleep_min,
        ]
        for day in DAYS
    }


def main_mode(config):
    if config.settings_per_day:
        return config.main_mode if config.main_mode in ("weekend", "days") else "days"
    return "all"


def _valid_days(days):
    try:
        return all(
            len(days[day]) == 4
            and 0 <= int(days[day][0]) < 24
            and 0 <= int(days[day][1]) < 60
            and 0 <= int(days[day][2]) < 24
            and 0 <= int(days[day][3]) < 60
            for day in DAYS
        )
    except (KeyError, TypeError, ValueError):
        return False


def alt_plan(config):
    """The other times: {"name", "mode", "days"}, with sensible defaults."""
    try:
        plan = json.loads(config.alt_schedule or "{}")
    except ValueError:
        plan = {}
    if not isinstance(plan, dict):
        plan = {}
    days = plan.get("days")
    if not _valid_days(days):
        # Default: a little later than the usual times, every day.
        days = expand("all", {"wake": "09:00", "sleep": "23:00"})
    mode = plan.get("mode") if plan.get("mode") in MODES else "all"
    name = str(plan.get("name") or "").strip()[:30] or DEFAULT_ALT_NAME
    return {
        "name": name,
        "mode": mode,
        "days": {day: [int(v) for v in days[day]] for day in DAYS},
    }


def times_for(config, day):
    """(wake_h, wake_m, sleep_h, sleep_m) followed on `day`."""
    if config.use_alt_schedule:
        return tuple(alt_plan(config)["days"][day])
    return tuple(main_days(config)[day])


def parse_time(text):
    """'07:30' -> [7, 30]. ValueError if it is not a time."""
    hour, minute = str(text).strip().split(":")[:2]
    hour, minute = int(hour), int(minute)
    if not (0 <= hour < 24 and 0 <= minute < 60):
        raise ValueError(text)
    return [hour, minute]


def expand(mode, values):
    """
    Day by day times from what the page sends, in `mode`:
    - all: wake, sleep
    - weekend: week_wake, week_sleep, weekend_wake, weekend_sleep
    - days: wake_<day>, sleep_<day>
    """
    days = {}
    for day in DAYS:
        if mode == "all":
            wake, sleep = values["wake"], values["sleep"]
        elif mode == "weekend":
            wake = values["weekend_wake" if day in WEEKEND_MORNINGS else "week_wake"]
            sleep = values["weekend_sleep" if day in WEEKEND_NIGHTS else "week_sleep"]
        else:
            wake, sleep = values["wake_" + day], values["sleep_" + day]
        days[day] = parse_time(wake) + parse_time(sleep)
    return days


def collapse(days):
    """What the page shows for each mode, from day by day times."""

    def text(hour, minute):
        return f"{hour:02d}:{minute:02d}"

    return {
        "wake": text(*days["monday"][:2]),
        "sleep": text(*days["monday"][2:]),
        "week_wake": text(*days["monday"][:2]),
        "week_sleep": text(*days["monday"][2:]),
        "weekend_wake": text(*days["saturday"][:2]),
        "weekend_sleep": text(*days["saturday"][2:]),
        "days": [
            (day, text(*days[day][:2]), text(*days[day][2:])) for day in DAYS
        ],
    }


def set_main(config, mode, days):
    """Store the usual times in the historical fields."""
    config.main_mode = mode
    if mode == "all":
        config.settings_per_day = False
        (
            config.wakeup_hour,
            config.wakeup_min,
            config.sleep_hour,
            config.sleep_min,
        ) = days["monday"]
        return
    config.settings_per_day = True
    for day in DAYS:
        wake_h, wake_m, sleep_h, sleep_m = days[day]
        setattr(config, "wakeup_hour_" + day, wake_h)
        setattr(config, "wakeup_min_" + day, wake_m)
        setattr(config, "sleep_hour_" + day, sleep_h)
        setattr(config, "sleep_min_" + day, sleep_m)


def set_alt(config, name, mode, days):
    config.alt_schedule = json.dumps(
        {
            "name": (name or "").strip()[:30] or DEFAULT_ALT_NAME,
            "mode": mode if mode in MODES else "all",
            "days": days,
        }
    )


def summary(config, day):
    """Name of the set followed and its times for `day`, for the home page."""
    wake_h, wake_m, sleep_h, sleep_m = times_for(config, day)
    return {
        "alt": bool(config.use_alt_schedule),
        "name": alt_plan(config)["name"] if config.use_alt_schedule else None,
        "wake": f"{wake_h:02d}:{wake_m:02d}",
        "sleep": f"{sleep_h:02d}:{sleep_m:02d}",
    }
