from django.core import validators
from django.db import models
from nabcommon import singleton_model

class Config(singleton_model.SingletonModel):
    streaming_url = models.TextField(null=True, default="")
    json_data_base = models.TextField(null=True, default="")
    radio_urls = models.JSONField(null=True, blank=True, default=list)


class WebRadio(models.Model):
    url = models.URLField()
    position = models.PositiveIntegerField(default=0)

    def __str__(self):
        return f"{self.url} (#{self.position})"

    class Meta:
        ordering = ['position']
