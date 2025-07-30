import json

from django.http import JsonResponse
from django.shortcuts import render
from django.views.generic import TemplateView

from .models import Config
from . import rfid_data

class SettingsView(TemplateView):
    template_name = "nabradio/settings.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["config"] = Config.load()
        return context

    def get(self, request, *args, **kwargs):
        context = self.get_context_data(**kwargs)
        uid = request.GET.get("uid", None)

        streaming_url = rfid_data.read_data_ui_for_views(uid)

        context["streaming_url"] = streaming_url
        context["radio_uid"] = uid

        return render(request, SettingsView.template_name, context=context)

    def post(self, request, *args, **kwargs):
        # Récupère le singleton de configuration
        config = Config.load()

        # Met à jour les paramètres à partir des données du formulaire
        if "radio_uid" in request.POST:
            config.radio_uid = request.POST["radio_uid"]
        if "streaming_url" in request.POST:
            config.streaming_url = request.POST["streaming_url"]
        else:
            config.streaming_url = request.POST.get("radio_uid", "")
        
        # Sauvegarde la configuration dans la base de données
        config.save()
        context = self.get_context_data(**kwargs)
        return render(request, SettingsView.template_name, context=context)

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

from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from django.utils.decorators import method_decorator
from .models import WebRadio
from .nabradio import NabRadio  


radio_controller = NabRadio()


    @csrf_exempt
    @require_http_methods(["POST"])
    def delete_webradio(request):
        radio_id = request.POST.get("radio_id")
        if not radio_id:
            return JsonResponse({"error": "Missing radio_id"}, status=400)
        try:
            WebRadio.objects.get(id=radio_id).delete()
            return JsonResponse({"status": "deleted"})
        except Exception as e:
            return JsonResponse({"error": str(e)}, status=500)


    @csrf_exempt
    @require_http_methods(["POST"])
    def control_webradio(request):
        action = request.POST.get("action")
        radio_id = request.POST.get("radio_id")

        if action not in ("play", "stop") or not radio_id:
            return JsonResponse({"error": "Invalid parameters"}, status=400)

        try:
            if action == "play":
                radio = WebRadio.objects.get(id=radio_id)
                radio_controller.loop.create_task(radio_controller._launch_radio(radio.url))
                return JsonResponse({"status": "playing"})
            elif action == "stop":
                radio_controller.loop.create_task(radio_controller.stop_radio())
                return JsonResponse({"status": "stopped"})
        except Exception as e:
            return JsonResponse({"error": str(e)}, status=500)
