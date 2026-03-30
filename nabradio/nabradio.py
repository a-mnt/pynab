import datetime
import json
import logging
import sys
from typing import List, Optional

from nabcommon.nabservice import NabService

from . import rfid_data
from .models import Config, RadioStation

RADIO_REQUEST_ID = "nabradio-live"
EARS_STEPS = 17


class NabRadio(NabService):
    def __init__(self):
        super().__init__()
        self.is_playing = False
        self.current_station_id: Optional[int] = None
        self.current_stream_url = ""
        self.right_ear_position: Optional[int] = None

    async def _send_packet(self, packet):
        assert self.writer is not None
        self.writer.write((json.dumps(packet) + "\r\n").encode("utf8"))
        await self.writer.drain()

    async def _launch_radio(self, streaming_url: str):
        logging.info("nabradio: play %s", streaming_url)
        now = datetime.datetime.now(datetime.timezone.utc)
        expiration = now + datetime.timedelta(hours=12)

        packet = {
            "type": "message",
            "request_id": RADIO_REQUEST_ID,
            "signature": {
                "audio": ["nabradio/*.mp3"],
            },
            "body": [
                {
                    "audio": [streaming_url],
                }
            ],
            "expiration": expiration.isoformat(),
        }
        await self._send_packet(packet)

    async def _stop_radio(self):
        logging.info("nabradio: stop")
        packet = {
            "type": "cancel",
            "request_id": RADIO_REQUEST_ID,
        }
        await self._send_packet(packet)

    async def _load_state(self):
        config = await Config.load_async()
        stations = list(await RadioStation.objects.filter(is_active=True).order_by("position", "id"))
        selected_station = config.selected_station

        if selected_station is None and stations:
            selected_station = stations[0]
            config.selected_station = selected_station
            await config.save_async()

        return config, stations, selected_station

    async def _apply_state(self):
        config, stations, selected_station = await self._load_state()

        desired_playing = bool(config.is_playing)
        desired_station_id = selected_station.id if selected_station else None
        desired_stream_url = selected_station.stream_url if selected_station else ""

        if desired_playing and selected_station is not None:
            if (not self.is_playing) or (self.current_stream_url != desired_stream_url):
                await self._launch_radio(desired_stream_url)
        else:
            if self.is_playing:
                await self._stop_radio()

        self.is_playing = desired_playing
        self.current_station_id = desired_station_id
        self.current_stream_url = desired_stream_url

    async def reload_config(self):
        await self._apply_state()

    async def _set_selected_station(self, station: RadioStation, play: bool):
        config = await Config.load_async()
        config.selected_station = station
        config.is_playing = play
        await config.save_async()
        await self._apply_state()

    async def _switch_station(self, direction: str):
        config, stations, selected_station = await self._load_state()
        if not stations or selected_station is None:
            return

        current_index = next(
            (index for index, station in enumerate(stations) if station.id == selected_station.id),
            0,
        )

        if direction == "next":
            new_index = (current_index + 1) % len(stations)
        else:
            new_index = (current_index - 1) % len(stations)

        config.selected_station = stations[new_index]
        config.is_playing = True
        await config.save_async()
        await self._apply_state()

    async def _handle_right_ear_rotation(self, new_position: int):
        if self.right_ear_position is None:
            self.right_ear_position = new_position
            return

        if new_position == self.right_ear_position:
            return

        delta = (new_position - self.right_ear_position) % EARS_STEPS
        self.right_ear_position = new_position

        if delta == 0:
            return

        if delta <= EARS_STEPS // 2:
            await self._switch_station("next")
        else:
            await self._switch_station("previous")

    async def _play_rfid_station(self, streaming_url: str):
        station = await RadioStation.objects.filter(stream_url=streaming_url).afirst()

        if station is None:
            last_station = await RadioStation.objects.order_by("-position", "-id").afirst()
            next_position = (last_station.position + 1) if last_station else 0
            station = await RadioStation.objects.acreate(
                name="Station RFID",
                stream_url=streaming_url,
                position=next_position,
                is_favorite=False,
                is_active=True,
            )

        await self._set_selected_station(station, True)

    async def process_nabd_packet(self, packet):
        packet_type = packet.get("type")

        if (
            packet_type == "rfid_event"
            and packet.get("app") == "nabradio"
            and packet.get("event") == "detected"
        ):
            streaming_url = await rfid_data.read_data_ui(packet["uid"])
            if streaming_url:
                await self._play_rfid_station(streaming_url)
            return

        if packet_type == "ears_event":
            right_position = packet.get("right")
            if isinstance(right_position, int):
                if self.is_playing:
                    await self._handle_right_ear_rotation(right_position)
                else:
                    self.right_ear_position = right_position

    async def start_service_loop(self, loop):
        loop.create_task(self.reload_config())
        return None


if __name__ == "__main__":
    NabRadio.main(sys.argv[1:])