from django.db import models

from nabcommon import singleton_model


class Config(singleton_model.SingletonModel):
    selected_station = models.ForeignKey(
        "nabradio.RadioStation",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    is_playing = models.BooleanField(default=False)

    def __str__(self) -> str:
        return "Radio configuration"


class RadioStation(models.Model):
    name = models.CharField(max_length=255)
    stream_url = models.TextField(unique=True)
    position = models.PositiveIntegerField(default=0, db_index=True)
    is_favorite = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["position", "id"]

    def __str__(self) -> str:
        return self.name