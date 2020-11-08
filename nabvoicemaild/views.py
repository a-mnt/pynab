from django.shortcuts import render
from django.views.generic import TemplateView
from django.http import JsonResponse
from django.utils.translation import ugettext as _
from .models import Config
from .nabvoicemaild import NabVoicemaild
import datetime


class SettingsView(TemplateView):
    template_name = "nabvoicemaild/settings.html"

    def get_context_data(self, **kwargs):
        # on charge les donnees depuis la base de données
        context = super().get_context_data(**kwargs)
        context["config"] = Config.load()
        return context

    def post(self, request, *args, **kwargs):
        # quand on reçoit une nouvelle config (via interface)
        config = Config.load()
        if "index_voicemail" in request.POST:
            index_voicemaild = request.POST["index_voicemail"]
            config.index_voicemaild = index_voicemaild
        if "visual_voicemail" in request.POST:
            visual_voicemail = request.POST["visual_voicemail"]
            config.visual_voicemaild = visual_voicemaild
        config.save()
        Nabvoicemaildd.signal_daemon()
        context = self.get_context_data(**kwargs)
        return render(request, SettingsView.template_name, context=context)

    def put(self, request, *args, **kwargs):
        # quand on clique sur le bouton de l'intervaface pour jouer tout de suite
        config = Config.load()
        config.next_performance_date = datetime.datetime.now(
            datetime.timezone.utc
        )
        config.next_performance_type = "today"
        config.save()
        Nabvoicemaildd.signal_daemon()
        return JsonResponse({"status": "ok"})
