# Generated by Django 3.2.12 on 2022-03-11 21:11

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("nabmastodond", "0004_auto_20200611_1104"),
    ]

    operations = [
        migrations.AlterField(
            model_name="config",
            name="id",
            field=models.AutoField(
                auto_created=True,
                primary_key=True,
                serialize=False,
                verbose_name="ID",
            ),
        ),
    ]