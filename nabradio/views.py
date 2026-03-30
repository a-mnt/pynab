import datetime
import json
import socket
from typing import Optional

from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import render
from django.views.generic import TemplateView

from . import rfid_data
from .models import Config, RadioStation


RADIO_REQUEST_ID = "nabradio-live"


def _send_nabd_packet(packet) -> None:
    payload = json.dumps(packet) + "\r\n"
    with socket.create_connection(("127.0.0.1", 10543), timeout=3) as sock:
        sock.sendall(payload.encode("utf8"))


def _play_stream(stream_url: str) -> None:
    now = datetime.datetime.now(datetime.timezone.utc)
    expiration = now + datetime.timedelta(hours=12)

    packet = {
        "type": "message",
        "request_id": RADIO_REQUEST_ID,
        "signature": {
            "audio": ["nabradio/*.mp3"],
        },
        "body": [
            {
                "audio": [stream_url],
            }
        ],
        "expiration": expiration.isoformat(),
    }
    _send_nabd_packet(packet)


def _stop_stream() -> None:
    packet = {
        "type": "cancel",
        "request_id": RADIO_REQUEST_ID,
    }
    _send_nabd_packet(packet)


def _signal_radio_daemon() -> None:
    try:
        from .nabradio import NabRadio

        NabRadio.signal_daemon()
    except Exception:
        pass


def _all_stations():
    return list(RadioStation.objects.all().order_by("position", "id"))


def _normalize_positions() -> None:
    stations = _all_stations()
    for index, station in enumerate(stations):
        if station.position != index:
            station.position = index
            station.save(update_fields=["position"])


def _ensure_default_stations() -> None:
    if RadioStation.objects.exists():
        return

    defaults = [
        ("FIP", "https://icecast.radiofrance.fr/fip-hifi.aac", True),
        ("France Inter", "https://icecast.radiofrance.fr/franceinter-hifi.aac", False),
        ("France Info", "https://icecast.radiofrance.fr/franceinfo-hifi.aac", False),
        ("Nostalgie", "https://scdn.nrjaudio.fm/fr/30601/aac_64.mp3", False),
    ]
    for index, (name, url, favorite) in enumerate(defaults):
        RadioStation.objects.create(
            name=name,
            stream_url=url,
            position=index,
            is_favorite=favorite,
            is_active=True,
        )

    config = Config.load()
    if config.selected_station_id is None:
        config.selected_station = RadioStation.objects.order_by("position", "id").first()
        config.is_playing = False
        config.save()


def _get_or_create_config() -> Config:
    _ensure_default_stations()
    config = Config.load()
    if config.selected_station_id is None:
        config.selected_station = RadioStation.objects.filter(is_active=True).order_by("position", "id").first()
        config.save()
    return config


def _selected_station_from_request(request) -> Optional[RadioStation]:
    station_id = request.POST.get("selected_radio", "").strip()
    if not station_id:
        return None
    try:
        return RadioStation.objects.get(pk=int(station_id))
    except (ValueError, RadioStation.DoesNotExist):
        return None


def _build_context(**extra):
    config = _get_or_create_config()
    stations = _all_stations()
    selected_station = config.selected_station or (stations[0] if stations else None)

    return {
        "radios": stations,
        "selected_station": selected_station,
        "selected_station_id": selected_station.id if selected_station else None,
        "selected_name": selected_station.name if selected_station else "",
        "is_playing": bool(config.is_playing),
        "favorites_count": sum(1 for station in stations if station.is_favorite),
        "flash_message": extra.get("flash_message", ""),
        "flash_type": extra.get("flash_type", "success"),
    }


@transaction.atomic
def _move_station(station: RadioStation, direction: str) -> None:
    stations = _all_stations()
    current_index = next((index for index, item in enumerate(stations) if item.id == station.id), None)
    if current_index is None:
        return

    if direction == "up" and current_index > 0:
        swap_index = current_index - 1
    elif direction == "down" and current_index < len(stations) - 1:
        swap_index = current_index + 1
    else:
        return

    other = stations[swap_index]
    station.position, other.position = other.position, station.position
    station.save(update_fields=["position"])
    other.save(update_fields=["position"])
    _normalize_positions()


class SettingsView(TemplateView):
    template_name = "nabradio/settings.html"

    def get(self, request, *args, **kwargs):
        return render(request, self.template_name, context=_build_context())

    def post(self, request, *args, **kwargs):
        config = _get_or_create_config()
        action = request.POST.get("action", "").strip()
        flash_message = ""
        flash_type = "success"

        if action == "add_radio":
            radio_name = request.POST.get("radio_name", "").strip()
            radio_url = request.POST.get("radio_url", "").strip()
            is_favorite = request.POST.get("radio_favorite") == "1"

            if not radio_name or not radio_url:
                flash_message = "Nom et URL sont requis."
                flash_type = "danger"
            elif RadioStation.objects.filter(stream_url=radio_url).exists():
                flash_message = "Cette radio existe déjà."
                flash_type = "danger"
            else:
                station = RadioStation.objects.create(
                    name=radio_name,
                    stream_url=radio_url,
                    position=RadioStation.objects.count(),
                    is_favorite=is_favorite,
                    is_active=True,
                )
                if config.selected_station_id is None:
                    config.selected_station = station
                    config.save(update_fields=["selected_station"])
                _signal_radio_daemon()
                flash_message = "Radio ajoutée."

        elif action == "edit_radio":
            station_id = request.POST.get("station_id", "").strip()
            radio_name = request.POST.get("radio_name", "").strip()
            radio_url = request.POST.get("radio_url", "").strip()

            try:
                station = RadioStation.objects.get(pk=int(station_id))
            except (ValueError, RadioStation.DoesNotExist):
                station = None

            if station is None:
                flash_message = "Radio introuvable."
                flash_type = "danger"
            elif not radio_name or not radio_url:
                flash_message = "Nom et URL sont requis."
                flash_type = "danger"
            elif RadioStation.objects.exclude(pk=station.pk).filter(stream_url=radio_url).exists():
                flash_message = "Cette URL est déjà utilisée."
                flash_type = "danger"
            else:
                station.name = radio_name
                station.stream_url = radio_url
                station.save(update_fields=["name", "stream_url"])

                if config.selected_station_id == station.id and config.is_playing:
                    try:
                        _play_stream(station.stream_url)
                    except Exception:
                        pass

                _signal_radio_daemon()
                flash_message = "Radio modifiée."

        elif action == "delete_radio":
            station_id = request.POST.get("station_id", "").strip()
            try:
                station = RadioStation.objects.get(pk=int(station_id))
            except (ValueError, RadioStation.DoesNotExist):
                station = None

            if station is None:
                flash_message = "Radio introuvable."
                flash_type = "danger"
            elif RadioStation.objects.count() <= 1:
                flash_message = "Impossible de supprimer la dernière radio."
                flash_type = "danger"
            else:
                was_selected = config.selected_station_id == station.id
                station.delete()
                _normalize_positions()

                if was_selected:
                    config.selected_station = RadioStation.objects.filter(is_active=True).order_by("position", "id").first()
                    config.save(update_fields=["selected_station"])

                _signal_radio_daemon()
                flash_message = "Radio supprimée."

        elif action == "toggle_favorite":
            station_id = request.POST.get("station_id", "").strip()
            try:
                station = RadioStation.objects.get(pk=int(station_id))
            except (ValueError, RadioStation.DoesNotExist):
                station = None

            if station is None:
                flash_message = "Radio introuvable."
                flash_type = "danger"
            else:
                station.is_favorite = not station.is_favorite
                station.save(update_fields=["is_favorite"])
                flash_message = "Favori mis à jour."

        elif action == "move_up":
            station_id = request.POST.get("station_id", "").strip()
            try:
                station = RadioStation.objects.get(pk=int(station_id))
            except (ValueError, RadioStation.DoesNotExist):
                station = None

            if station is None:
                flash_message = "Radio introuvable."
                flash_type = "danger"
            else:
                _move_station(station, "up")
                _signal_radio_daemon()
                flash_message = "Ordre mis à jour."

        elif action == "move_down":
            station_id = request.POST.get("station_id", "").strip()
            try:
                station = RadioStation.objects.get(pk=int(station_id))
            except (ValueError, RadioStation.DoesNotExist):
                station = None

            if station is None:
                flash_message = "Radio introuvable."
                flash_type = "danger"
            else:
                _move_station(station, "down")
                _signal_radio_daemon()
                flash_message = "Ordre mis à jour."

        context = _build_context(
            flash_message=flash_message,
            flash_type=flash_type,
        )
        return render(request, self.template_name, context=context)


class ControlView(TemplateView):
    def post(self, request, *args, **kwargs):
        config = _get_or_create_config()
        action = request.POST.get("action", "").strip()
        station = _selected_station_from_request(request)

        if station is not None and config.selected_station_id != station.id:
            config.selected_station = station
            config.save(update_fields=["selected_station"])

        selected_station = config.selected_station

        if action == "play":
            if selected_station is None:
                return JsonResponse(
                    {"status": "error", "message": "Aucune radio sélectionnée."},
                    status=400,
                )

            try:
                _play_stream(selected_station.stream_url)
            except Exception as exc:
                return JsonResponse(
                    {"status": "error", "message": f"Impossible de lancer la lecture: {exc}"},
                    status=500,
                )

            config.is_playing = True
            config.save(update_fields=["is_playing"])
            _signal_radio_daemon()

            return JsonResponse(
                {
                    "status": "ok",
                    "message": f"Lecture lancée : {selected_station.name}.",
                    "selected_name": selected_station.name,
                    "is_playing": True,
                }
            )

        if action == "stop":
            try:
                _stop_stream()
            except Exception as exc:
                return JsonResponse(
                    {"status": "error", "message": f"Impossible d'arrêter la lecture: {exc}"},
                    status=500,
                )

            config.is_playing = False
            config.save(update_fields=["is_playing"])
            _signal_radio_daemon()

            return JsonResponse(
                {
                    "status": "ok",
                    "message": "Lecture arrêtée.",
                    "selected_name": selected_station.name if selected_station else "",
                    "is_playing": False,
                }
            )

        return JsonResponse(
            {"status": "error", "message": "Action inconnue."},
            status=400,
        )


class RFIDDataView(TemplateView):
    template_name = "nabradio/rfid-data.html"

    def get(self, request, *args, **kwargs):
        context = self.get_context_data(**kwargs)
        uid = request.GET.get("uid", None)
        streaming_url = rfid_data.read_data_ui_for_views(uid)
        context["streaming_url"] = streaming_url
        context["radio_uid"] = uid
        return render(request, RFIDDataView.template_name, context=context)

    def post(self, request, *args, **kwargs):
        uid = request.POST.get("radio_uid", "")
        streaming_url = request.POST.get("streaming_url", uid)
        rfid_data.write_data_ui_for_views(uid, streaming_url)
        return JsonResponse({"data": "DATA_IN_LOCAL_DB"})