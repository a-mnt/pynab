from django.core import validators
from django.db import models
from nabcommon import singleton_model

class Config(singleton_model.SingletonModel):
    # Champ existant
    streaming_url = models.TextField(null=True, default="")
    json_data_base = models.TextField(null=True, default="")
    
    # Nouveau champ pour sauvegarder plusieurs URL
    radio_urls = models.JSONField(null=True, blank=True, default=list)