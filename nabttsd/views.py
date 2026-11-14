from django.http import JsonResponse
from django.shortcuts import render
from django.utils.translation import gettext as _
from django.views.generic import TemplateView, View

from nabd.i18n import Config as NabdConfig

from . import client, tts
from .models import Config


def _int(value, default, low, high):
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default


class SettingsView(TemplateView):
    template_name = "nabttsd/settings.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        config = Config.load()
        locale = NabdConfig.load().locale
        engines = tts.available_engines(locale)
        context["config"] = config
        context["engines"] = engines
        context["current_engine"] = (
            tts.resolve_engine(config.engine, locale) if engines else ""
        )
        return context

    def post(self, request, *args, **kwargs):
        config = Config.load()
        config.enabled = request.POST.get("enabled") == "true"
        engine = request.POST.get("engine", "")
        config.engine = engine if engine in tts.ENGINES else ""
        config.speed = _int(request.POST.get("speed"), 100, 50, 200)
        config.pitch = _int(request.POST.get("pitch"), 100, 50, 200)
        config.volume = _int(request.POST.get("volume"), 100, 10, 300)
        config.save()
        context = self.get_context_data(**kwargs)
        context["saved"] = True
        return render(request, self.template_name, context=context)


class SayView(View):
    def post(self, request, *args, **kwargs):
        text = request.POST.get("text", "").strip()
        if not text:
            return JsonResponse(
                {"status": "error", "message": _("Texte vide.")}, status=400
            )
        response = client.say_sync(text)
        status = 200 if response.get("status") == "ok" else 503
        return JsonResponse(response, status=status)
