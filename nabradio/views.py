import datetime
import json
import socket
from typing import Dict, List

from django.http import JsonResponse
from django.shortcuts import render
from django.views.generic import TemplateView

from . import rfid_data
from .models import Config


RADIO_REQUEST_ID = "nabradio-live"


def _default_radios() -> List[Dict[str, str]]:
    return [
        {
            "name": "FIP",
            "url": "https://icecast.radiofrance.fr/fip-hifi.aac",
        },
        {
            "name": "France Inter",
            "url": "https://icecast.radiofrance.fr/franceinter-hifi.aac",
        },
        {
            "name": "France Info",
            "url": "https://icecast.radiofrance.fr/franceinfo-hifi.aac",
        },
        {
            "name": "Nostalgie",
            "url": "https://scdn.nrjaudio.fm/fr/30601/aac_64.mp3",
        },
    ]


def _normalize_radios(radios: List[Dict[str, str]]) -> List[Dict[str, str]]:
    cleaned: List[Dict[str, str]] = []
    seen_urls = set()

    for radio in radios:
        name = str(radio.get("name", "")).strip()
        url = str(radio.get("url", "")).strip()

        if not name or not url:
            continue
        if url in seen_urls:
            continue

        cleaned.append({"name": name, "url": url})
        seen_urls.add(url)

    return cleaned


def _load_radio_state(config: Config) -> Dict[str, object]:
    radios = _default_radios()

    if config.json_data_base:
        try:
            raw = json.loads(config.json_data_base)

            if isinstance(raw, dict):
                radios = raw.get("radios", radios)
            elif isinstance(raw, list):
                radios = raw
        except (json.JSONDecodeError, TypeError, ValueError):
            pass

    radios = _normalize_radios(radios)

    if not radios:
        radios = _default_radios()

    selected_url = (config.streaming_url or "").strip()

    if not selected_url:
        selected_url = radios[0]["url"]

    if selected_url and all(r["url"] != selected_url for r in radios):
        radios.append(
            {
                "name": "Station personnalisée",
                "url": selected_url,
            }
        )

    selected_name = ""
    for radio in radios:
        if radio["url"] == selected_url:
            selected_name = radio["name"]
            break

    return {
        "radios": radios,
        "selected_url": selected_url,
        "selected_name": selected_name,
    }


def _save_radio_state(config: Config, radios: List[Dict[str, str]], selected_url: str) -> None:
    radios = _normalize_radios(radios)

    if not radios:
        radios = _default_radios()

    selected_url = selected_url.strip()

    if selected_url and all(r["url"] != selected_url for r in radios):
        radios.append(
            {
                "name": "Station personnalisée",
                "url": selected_url,
            }
        )

    if not selected_url:
        selected_url = radios[0]["url"]

    config.streaming_url = selected_url
    config.json_data_base = json.dumps(
        {"radios": radios},
        ensure_ascii=False,
    )
    config.save()


def _send_nabd_packet(packet: Dict[str, object]) -> None:
    payload = json.dumps(packet) + "\r\n"

    with socket.create_connection(("127.0.0.1", 10543), timeout=3) as sock:
        sock.sendall(payload.encode("utf8"))


def _play_radio(streaming_url: str) -> None:
    now = datetime.datetime.now(datetime.timezone.utc)
    expiration = now + datetime.timedelta(minutes=10)

    packet = {
        "type": "message",
        "request_id": RADIO_REQUEST_ID,
        "signature": {
            "audio": ["nabradio/*.mp3"],
        },
        "body": [
            {
                "audio": [streaming_url],
            }
        ],
        "expiration": expiration.isoformat(),
    }
    _send_nabd_packet(packet)


def _stop_radio() -> None:
    packet = {
        "type": "cancel",
        "request_id": RADIO_REQUEST_ID,
    }
    _send_nabd_packet(packet)


class SettingsView(TemplateView):
    template_name = "nabradio/settings.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        config = Config.load()
        state = _load_radio_state(config)

        context["radios"] = state["radios"]
        context["selected_url"] = state["selected_url"]
        context["selected_name"] = state["selected_name"]
        context["flash_message"] = kwargs.get("flash_message", "")
        context["flash_type"] = kwargs.get("flash_type", "success")
        return context

    def get(self, request, *args, **kwargs):
        context = self.get_context_data(**kwargs)
        return render(request, self.template_name, context=context)

    def post(self, request, *args, **kwargs):
        config = Config.load()
        state = _load_radio_state(config)
        radios = list(state["radios"])
        selected_url = str(state["selected_url"])

        action = request.POST.get("action", "")
        flash_message = ""
        flash_type = "success"

        if action == "save_selection":
            selected_url = request.POST.get("selected_radio", "").strip()

            if not selected_url:
                flash_message = "Choisissez une radio."
                flash_type = "danger"
            else:
                _save_radio_state(config, radios, selected_url)
                flash_message = "Sélection enregistrée."

        elif action == "add_radio":
            radio_name = request.POST.get("radio_name", "").strip()
            radio_url = request.POST.get("radio_url", "").strip()

            if not radio_name or not radio_url:
                flash_message = "Nom et URL sont requis."
                flash_type = "danger"
            else:
                radios.append({"name": radio_name, "url": radio_url})
                _save_radio_state(config, radios, selected_url or radio_url)
                flash_message = "Radio ajoutée."

        elif action == "delete_radio":
            radio_url = request.POST.get("radio_url", "").strip()
            radios = [radio for radio in radios if radio["url"] != radio_url]

            if selected_url == radio_url:
                selected_url = radios[0]["url"] if radios else ""

            _save_radio_state(config, radios, selected_url)
            flash_message = "Radio supprimée."

        context = self.get_context_data(
            flash_message=flash_message,
            flash_type=flash_type,
        )
        return render(request, self.template_name, context=context)


class ControlView(TemplateView):
    def post(self, request, *args, **kwargs):
        config = Config.load()
        state = _load_radio_state(config)
        selected_url = request.POST.get("selected_radio", "").strip() or str(state["selected_url"])
        action = request.POST.get("action", "").strip()

        if action == "play":
            if not selected_url:
                return JsonResponse(
                    {"status": "error", "message": "Aucune radio sélectionnée."},
                    status=400,
                )

            _save_radio_state(config, list(state["radios"]), selected_url)
            _play_radio(selected_url)
            return JsonResponse(
                {
                    "status": "ok",
                    "message": "Lecture démarrée.",
                }
            )

        if action == "pause":
            _stop_radio()
            return JsonResponse(
                {
                    "status": "ok",
                    "message": "Pause appliquée (lecture interrompue).",
                }
            )

        if action == "stop":
            _stop_radio()
            return JsonResponse(
                {
                    "status": "ok",
                    "message": "Lecture arrêtée.",
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
        data = "DATA_IN_LOCAL_DB"
        uid = ""

        if "radio_uid" in request.POST:
            uid = request.POST["radio_uid"]

        if "streaming_url" in request.POST:
            streaming_url = request.POST["streaming_url"]
        else:
            streaming_url = uid

        rfid_data.write_data_ui_for_views(uid, streaming_url)
        return JsonResponse({"data": data})