from django.urls import path

from .views import SettingsView, RFIDDataView, delete_webradio, control_webradio

urlpatterns = [
    path("settings", SettingsView.as_view(), name="settings"),
    path("rfid-data", RFIDDataView.as_view(), name="rfid_data"),
    path("delete/", delete_webradio, name="delete_webradio"),
    path("control/", control_webradio, name="control_webradio"),
]
