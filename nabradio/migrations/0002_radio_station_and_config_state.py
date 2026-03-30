from django.db import migrations, models
import django.db.models.deletion


DEFAULT_RADIOS = [
    {
        "name": "Pomme d'Api",
        "url": "radiopommedapi.com/radio.mp3",
    },
    {
        "name": "Radio Barbapapa",
        "url": "http://streaming.radio.rtl.fr/barbapapa-1-128.mp3",
    },
    {
        "name": "Radio Disney",
        "url": "http://radiodisney.ice.infomaniak.ch/radiodisney-high.mp3",
    },
    {
        "name": "Allzic Radio Enfants",
        "url": "http://allzic21.ice.infomaniak.ch/allzic-enfants.mp3",
    },
    {
        "name": "Crooner Radio Kids",
        "url": "http://stream.crooner.fr/croonerkids-128.mp3",
    },
    {
        "name": "Fun Kids Radio",
        "url": "http://media-ice.musicradio.com/FunKidsMP3",
    },
]


def populate_default_radios(apps, schema_editor):
    RadioStation = apps.get_model("nabradio", "RadioStation")
    Config = apps.get_model("nabradio", "Config")

    if RadioStation.objects.exists():
        return

    created = []
    for index, radio in enumerate(DEFAULT_RADIOS):
        station = RadioStation.objects.create(
            name=radio["name"],
            stream_url=radio["url"],
            position=index,
            is_favorite=(index == 0),
            is_active=True,
        )
        created.append(station)

    config = Config.load()
    if created and config.selected_station_id is None:
        config.selected_station = created[0]
        config.is_playing = False
        config.save()


def renumber_positions(apps, schema_editor):
    RadioStation = apps.get_model("nabradio", "RadioStation")
    for index, station in enumerate(RadioStation.objects.order_by("position", "id")):
        if station.position != index:
            station.position = index
            station.save(update_fields=["position"])


class Migration(migrations.Migration):

    dependencies = [
        ("nabradio", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="RadioStation",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=255)),
                ("stream_url", models.TextField(unique=True)),
                ("position", models.PositiveIntegerField(db_index=True, default=0)),
                ("is_favorite", models.BooleanField(default=False)),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "ordering": ["position", "id"],
            },
        ),
        migrations.RemoveField(
            model_name="config",
            name="streaming_url",
        ),
        migrations.RemoveField(
            model_name="config",
            name="json_data_base",
        ),
        migrations.AddField(
            model_name="config",
            name="is_playing",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="config",
            name="selected_station",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to="nabradio.radiostation",
            ),
        ),
        migrations.RunPython(populate_default_radios, migrations.RunPython.noop),
        migrations.RunPython(renumber_positions, migrations.RunPython.noop),
    ]
