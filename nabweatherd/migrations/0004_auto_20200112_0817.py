# Generated by Django 3.0.1 on 2020-01-12 08:17

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('nabweatherd', '0003_config_weather_animation_type'),
    ]

    operations = [
        migrations.AlterField(
            model_name='config',
            name='weather_animation_type',
            field=models.TextField(null=True),
        ),
    ]
