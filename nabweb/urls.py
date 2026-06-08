"""nabweb URL Configuration"""

from typing import List, Union

from django.apps import apps
from django.contrib.staticfiles.urls import staticfiles_urlpatterns
from django.urls import URLPattern, URLResolver, include, path
from django.views.generic import TemplateView

from .views import (
    NabWebHardwareTestView,
    NabWebLogViewerView,
    NabWebRfidReadView,
    NabWebRfidView,
    NabWebRfidWriteView,
    NabWebServicesView,
    NabWebShutdownView,
    NabWebSytemInfoView,
    NabWebUpgradeCheckNowView,
    NabWebUpgradeNowView,
    NabWebUpgradeRepositoryInfoView,
    NabWebUpgradeStatusView,
    NabWebUpgradeView,
    NabWebView,
)

urlpatterns: List[Union[URLResolver, URLPattern]] = [

    # --- Accueil ---
    path("", NabWebView.as_view(), name="nabweb.home"),

    # --- Services ---
    path("services/", NabWebServicesView.as_view(), name="nabweb.services"),

    # --- Paramètres (NOUVEAU) ---
    path(
        "settings/",
        TemplateView.as_view(template_name="nabweb/settings/index.html"),
        name="nabweb.settings",
    ),

    # --- RFID ---
    path("rfid/", NabWebRfidView.as_view(), name="nabweb.rfid"),
    path("rfid/read", NabWebRfidReadView.as_view(), name="rfid.read"),
    path("rfid/write", NabWebRfidWriteView.as_view(), name="rfid.write"),

    # --- Système ---
    path(
        "system-info/test/<str:test>",
        NabWebHardwareTestView.as_view(),
        name="nabweb.test",
    ),
    path(
        "system-info/",
        NabWebSytemInfoView.as_view(),
        name="nabweb.system",
    ),
    path(
        "system-info/logs",
        NabWebLogViewerView.as_view(),
        name="nabweb.logs",
    ),
    path(
        "system-info/shutdown/<str:mode>",
        NabWebShutdownView.as_view(),
        name="nabweb.shutdown",
    ),

    # --- Upgrade ---
    path("upgrade/", NabWebUpgradeView.as_view(), name="nabweb.upgrade"),
    path(
        "upgrade/info/<str:repository>",
        NabWebUpgradeRepositoryInfoView.as_view(),
        name="nabweb.upgrade.info",
    ),
    path(
        "upgrade/status",
        NabWebUpgradeStatusView.as_view(),
        name="nabweb.upgrade.status",
    ),
    path(
        "upgrade/now",
        NabWebUpgradeNowView.as_view(),
        name="nabweb.upgrade.now",
    ),
    path(
        "upgrade/checknow",
        NabWebUpgradeCheckNowView.as_view(),
        name="nabweb.upgrade.checknow",
    ),

    # --- Help ---
    path(
        "help/",
        TemplateView.as_view(template_name="nabweb/help.html"),
        name="nabweb.help",
    ),
    path(
        "help/weather",
        TemplateView.as_view(
            template_name="nabweatherd/animations_help.html"
        ),
        name="nabweatherd.help.animations",
    ),
    path(
        "help/airquality",
        TemplateView.as_view(
            template_name="nabairqualityd/animations_help.html"
        ),
        name="nabairqualityd.help.animations",
    ),
]

# Static files (dev uniquement)
urlpatterns += staticfiles_urlpatterns()

# Services dynamiques (très important → ne pas casser)
for config in apps.get_app_configs():
    if hasattr(config.module, "NABAZTAG_SERVICE_PRIORITY"):
        urlpatterns.append(
            path(config.name + "/", include(config.name + ".urls"))
        )