import datetime
import logging
import sys

from nabcommon.nabservice import NabService
from . import rfid_data
from .models import NabradioConfig

class NabRadio(NabService):
    def __init__(self):
        super().__init__()

    async def reload_config(self):
        # Ici, vous pourriez recharger la configuration depuis la BDD
        config = NabradioConfig.objects.first()
        if config is None:
            config = NabradioConfig.objects.create()
        # Par exemple, vous pouvez assigner la liste des URL à un attribut
        self.radio_urls = config.radio_urls

    async def save_radio_urls(self, urls):
        """
        Sauvegarde la liste des URL dans la configuration.
        :param urls: liste de chaînes de caractères (les URL)
        """
        config, created = NabradioConfig.objects.get_or_create(id=1)
        config.radio_urls = urls
        config.save()
        self.radio_urls = urls  # mettre à jour l'attribut local si nécessaire
        logging.info("Radio URLs saved: " + ", ".join(urls))

    async def _launch_radio(self, streaming_url):
        logging.info("Streaming radio " + streaming_url)
        now = datetime.datetime.now(datetime.timezone.utc)
        expiration = now + datetime.timedelta(minutes=1)
        packet = (
            f'{{"type":"message",'
            f'"signature":{{"audio":["nabradio/*.mp3"]}},'
            f'"body":[{{"audio":["{streaming_url}"]}}],'
            f'"expiration":"{expiration.isoformat()}"}}\r\n'
        )
        self.writer.write(packet.encode("utf8"))
        await self.writer.drain()

    async def _stop_radio(self):
        logging.info("Stopping radio")
        now = datetime.datetime.now(datetime.timezone.utc)
        expiration = now + datetime.timedelta(minutes=1)
        packet = (
            f'{{"type":"command",'
            f'"command":"stop_radio",'
            f'"expiration":"{expiration.isoformat()}"}}\r\n'
        )
        self.writer.write(packet.encode("utf8"))
        await self.writer.drain()

    async def process_nabd_packet(self, packet):
        if (
            packet.get("type") == "rfid_event"
            and packet.get("app") == "nabradio"
            and packet.get("event") == "detected"
        ):
            try:
                streaming_url = await rfid_data.read_data_ui(packet["uid"])
                await self._launch_radio(streaming_url)
            except Exception as e:
                logging.error(f"Error launching radio: {e}")

if __name__ == "__main__":
    NabRadio.main(sys.argv[1:])
