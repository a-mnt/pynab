import datetime
import logging
import sys
import os
from nabcommon.nabservice import NabService
from pynab.hardware import ears, button

from . import rfid_data
from .models import WebRadio


class NabRadio(NabService):
    def __init__(self):
        super().__init__()
        self.current_index = 0  # Indice de la radio actuellement jouée
        self.is_playing = False  # État de lecture/pause

    async def reload_config(self):
        """
        Recharge les configurations si nécessaire.
        """
        pass

    async def _launch_radio(self, streaming_url):
        """
        Envoie un paquet pour lancer une radio sur le Nabaztag.
        """
        logging.info("Streaming radio: " + streaming_url)
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

    async def play_radio_by_index(self, index):
        """
        Lit une radio en fonction de son index dans la liste triée.
        """
        radios = WebRadio.objects.all().order_by("position")
        if radios.exists() and 0 <= index < radios.count():
            streaming_url = radios[index].url
            self.current_index = index
            self.is_playing = True
            await self._launch_radio(streaming_url)
        else:
            logging.warning("Aucune webradio valide à cet index.")

    async def stop_radio(self):
        """
        Arrête la lecture en envoyant une commande d'arrêt.
        """
        self.is_playing = False
        logging.info("Arrêt de la radio.")
        # Implémentez ici une logique d'arrêt pour le Nabaztag si nécessaire.

    async def toggle_pause(self):
        """
        Met en pause ou reprend la lecture.
        """
        if self.is_playing:
            await self.stop_radio()
        else:
            await self.play_radio_by_index(self.current_index)

    async def next_radio(self):
        """
        Passe à la radio suivante.
        """
        radios = WebRadio.objects.all().order_by("position")
        if radios.exists():
            self.current_index = (self.current_index + 1) % radios.count()
            await self.play_radio_by_index(self.current_index)

    async def previous_radio(self):
        """
        Revient à la radio précédente.
        """
        radios = WebRadio.objects.all().order_by("position")
        if radios.exists():
            self.current_index = (self.current_index - 1) % radios.count()
            await self.play_radio_by_index(self.current_index)

    def handle_ear_rotation(self, ear, direction):
        """
        Gère la rotation des oreilles pour changer de station.
        """
        logging.info(f"Rotation détectée : {ear} - {direction}")
        if ear == "right":
            if direction == "clockwise":
                self.loop.create_task(self.next_radio())
            elif direction == "counterclockwise":
                self.loop.create_task(self.previous_radio())

    def handle_button_press(self):
        """
        Gère l'appui sur le bouton pour mettre en pause ou reprendre.
        """
        logging.info("Bouton appuyé.")
        self.loop.create_task(self.toggle_pause())

    async def process_nabd_packet(self, packet):
        """
        Traite les paquets reçus du daemon Nabd.
        """
        if (
            packet["type"] == "rfid_event"
            and packet["app"] == "nabradio"
            and packet["event"] == "detected"
        ):
            streaming_url = await rfid_data.read_data_ui(packet["uid"])
            await self._launch_radio(streaming_url)

    @classmethod
    def main(cls, args):
        """
        Point d'entrée principal du service.
        """
        instance = cls()
        ears.on_rotation(instance.handle_ear_rotation)
        button.on_press(instance.handle_button_press)
        super().main(args)


if __name__ == "__main__":
    NabRadio.main(sys.argv[1:])
