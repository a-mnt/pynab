from django.urls import path

from .views import SayView, SettingsView

urlpatterns = [
    path("settings", SettingsView.as_view()),
    path("say", SayView.as_view()),
]
