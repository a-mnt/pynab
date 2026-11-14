from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="Config",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("enabled", models.BooleanField(default=True)),
                ("engine", models.CharField(default="", max_length=20)),
                ("speed", models.IntegerField(default=100)),
                ("pitch", models.IntegerField(default=100)),
                ("volume", models.IntegerField(default=100)),
            ],
        ),
    ]
