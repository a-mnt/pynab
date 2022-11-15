# Generated by Django 3.2.15 on 2022-11-05 07:58

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
                ("webhook_url", models.TextField(default="", null=True)),
                ("json_data_base", models.TextField(default="", null=True)),
            ],
            options={
                "abstract": False,
            },
        ),
    ]