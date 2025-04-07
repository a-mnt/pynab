import datetime
import logging
import sys

from nabcommon.nabservice import NabService
from . import rfid_data

class NabRadio(NabService):
    def __init__(self):
        super().__init__()

    async def reload_config(self):
        # Ajoutez ici le rechargement de la configuration si nécessaire
        pass

    async def _launch_radio(self, streaming_url):
        """
        Lance la lecture de la radio en envoyant un paquet contenant l'URL du streaming.
        """
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
        """
        Arrête la lecture de la radio en envoyant une commande d'arrêt.
        """
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
        """
        Traite un paquet entrant.  
        Pour un événement RFID, on lit l'URL du streaming et on lance la radio.
        """
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

        # Ici, vous pouvez éventuellement ajouter d'autres conditions pour d'autres types de paquets.

if __name__ == "__main__":
    NabRadio.main(sys.argv[1:])
