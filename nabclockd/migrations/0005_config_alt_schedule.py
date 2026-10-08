from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("nabclockd", "0004_config_sleep_wakeup_override"),
    ]

    operations = [
        migrations.AddField(
            model_name="config",
            name="use_alt_schedule",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="config",
            name="alt_schedule",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="config",
            name="main_mode",
            field=models.CharField(blank=True, default="", max_length=8),
        ),
    ]
