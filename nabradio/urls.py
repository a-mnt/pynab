from django.urls import path

from .views import ControlView, RFIDDataView, SettingsView

urlpatterns = [
    path("settings", SettingsView.as_view(), name="nabradio.settings"),
    path("control", ControlView.as_view(), name="nabradio.control"),
    path("rfid-data", RFIDDataView.as_view(), name="nabradio.rfid-data"),
]