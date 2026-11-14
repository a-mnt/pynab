from django.db import models

from nabcommon import singleton_model


class Config(singleton_model.SingletonModel):
    enabled = models.BooleanField(default=True)
    # Engine id, see nabttsd.tts.ENGINES. Empty: first engine installed.
    engine = models.CharField(max_length=20, default="")
    # Percentages of the engine's default (100 = default).
    speed = models.IntegerField(default=100)
    pitch = models.IntegerField(default=100)
    volume = models.IntegerField(default=100)

    class Meta:
        app_label = "nabttsd"
