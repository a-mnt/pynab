from django.urls import path

from .views import RFIDDataView, ScheduleView, SettingsView

urlpatterns = [
    path("settings", SettingsView.as_view()),
    path("rfid-data", RFIDDataView.as_view()),
    path("schedule", ScheduleView.as_view(), name="nabclockd.schedule"),
]
