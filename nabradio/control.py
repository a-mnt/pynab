"""
Radio state stored in database, shared by the web views and the daemon.

The web views never talk to nabd: they only write the *desired* state here
(which station, playing or not) and signal the nabradio daemon. The daemon
is the only one to send packets to nabd, and it writes the state back when
playback ends for any reason, so `is_playing` reflects what the rabbit is
really doing.

Every function here is synchronous. The daemon calls them through
`sync_to_async`.
"""
from typing import Optional, Tuple

from .models import Config, RadioStation


def active_stations():
    return RadioStation.objects.filter(is_active=True).order_by(
        "position", "id"
    )


def load_config() -> Config:
    """
    Load configuration, selecting the first station if none is selected.
    """
    config = Config.load()
    if config.selected_station_id is None:
        station = active_stations().first()
        if station is not None:
            config.selected_station = station
            config.save(update_fields=["selected_station"])
    return config


def get_desired() -> Tuple[bool, str]:
    """
    Return what should be playing: (playing?, stream URL).
    """
    config = load_config()
    station = config.selected_station
    if station is None:
        return (False, "")
    return (bool(config.is_playing), station.stream_url)


def set_playing(play: bool, station: Optional[RadioStation] = None) -> bool:
    """
    Ask for playback to start (optionally on a given station) or to stop.
    Return False if playback was requested but there is no station.
    """
    config = load_config()
    if station is not None:
        config.selected_station = station
    if play and config.selected_station_id is None:
        return False
    config.is_playing = play
    config.save(update_fields=["selected_station", "is_playing"])
    return True


def switch_station(direction: str) -> bool:
    """
    Select next or previous station and ask for playback.
    Return False if there is no station to switch to.
    """
    config = load_config()
    stations = list(active_stations())
    if not stations:
        return False
    current_index = next(
        (
            index
            for index, station in enumerate(stations)
            if station.id == config.selected_station_id
        ),
        0,
    )
    step = 1 if direction == "next" else -1
    config.selected_station = stations[(current_index + step) % len(stations)]
    config.is_playing = True
    config.save(update_fields=["selected_station", "is_playing"])
    return True


def mark_stopped() -> None:
    """
    Record that nothing is playing. Only touches `is_playing` so that a
    station selected at the same time from the website is not overwritten.
    """
    Config.load()
    Config.objects.filter(pk=1).update(is_playing=False)


def play_stream_url(stream_url: str) -> None:
    """
    Select the station with this stream URL (creating it if needed) and ask
    for playback. Used for RFID tags.
    """
    station = RadioStation.objects.filter(stream_url=stream_url).first()
    if station is None:
        last = RadioStation.objects.order_by("-position", "-id").first()
        station = RadioStation.objects.create(
            name="Station RFID",
            stream_url=stream_url,
            position=(last.position + 1) if last else 0,
            is_favorite=False,
            is_active=True,
        )
    set_playing(True, station)


def status_payload() -> dict:
    """
    Current radio state, as returned to the website.
    """
    config = load_config()
    station = config.selected_station
    return {
        "is_playing": bool(config.is_playing) and station is not None,
        "station": (
            {"id": station.id, "name": station.name} if station else None
        ),
        "can_switch": active_stations().count() > 1,
    }
