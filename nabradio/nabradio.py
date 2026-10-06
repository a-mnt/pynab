import asyncio
import datetime
import json
import logging
import sys
from typing import Optional

from asgiref.sync import sync_to_async

from nabcommon.nabservice import NabService

from . import rfid_data

# A single request id: nabd plays one radio message at a time, and a fixed id
# lets a restarted daemon cancel a stream left over by the previous one.
RADIO_REQUEST_ID = "nabradio-live"
# nabd only checks expiration when it takes the message out of its queue. If
# the rabbit is asleep or busy for longer than this, the radio is dropped
# instead of starting much later, out of the blue.
START_TIMEOUT_SECONDS = 30
# nabd refuses to cancel a message that is still waiting in its queue.
CANCEL_RETRY_SECONDS = 1
EARS_STEPS = 17
# Value returned by rfid_data when a tag has no stream associated.
RFID_NO_STREAM = "NO_EVENT_NAME"
CANCEL_REFUSED_CLASSES = ("NotPlaying", "NotCancelable")


class NabRadio(NabService):
    """
    Radio daemon.

    The website writes the desired state in database (see control.py) and
    sends SIGUSR1. This daemon is the only one to talk to nabd: it starts and
    cancels the stream so that what plays matches the desired state, and it
    listens to nabd responses to know when playback really ended (stopped
    from the button, stream error, end of stream...).
    """

    def __init__(self):
        super().__init__()
        # Imported here and not at the top of the file: control.py loads the
        # database models, which is only possible once Django is configured,
        # and that is done by NabService.__init__ just above.
        from . import control

        self.control = control
        # True from the moment a message is sent to nabd until nabd reports
        # it ended. The message may be playing or still in nabd's queue.
        self.active = False
        self.active_url = ""
        self.cancel_sent = False
        # False until the startup cleanup is done.
        self.ready = False
        self.right_ear_position: Optional[int] = None
        self._lock: Optional[asyncio.Lock] = None
        self._retry_task: Optional[asyncio.Future] = None

    def _get_lock(self) -> asyncio.Lock:
        # Created lazily so that it is bound to the running event loop.
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def _send_packet(self, packet):
        assert self.writer is not None
        self.writer.write((json.dumps(packet) + "\r\n").encode("utf8"))
        await self.writer.drain()

    async def _launch(self, stream_url: str):
        logging.info("nabradio: play %s", stream_url)
        now = datetime.datetime.now(datetime.timezone.utc)
        expiration = now + datetime.timedelta(seconds=START_TIMEOUT_SECONDS)
        self.active = True
        self.active_url = stream_url
        self.cancel_sent = False
        await self._send_packet(
            {
                "type": "message",
                "request_id": RADIO_REQUEST_ID,
                "signature": {"audio": ["nabradio/*.mp3"]},
                "body": [{"audio": [stream_url]}],
                "expiration": expiration.isoformat(),
            }
        )

    async def _cancel(self):
        logging.info("nabradio: stop %s", self.active_url)
        self.cancel_sent = True
        await self._send_packet(
            {"type": "cancel", "request_id": RADIO_REQUEST_ID}
        )

    async def _reconcile(self):
        """
        Make nabd play what the database says should be playing.
        Must be called with the lock held.
        """
        want_playing, stream_url = await sync_to_async(
            self.control.get_desired
        )()
        if not self.active:
            if want_playing and stream_url:
                await self._launch(stream_url)
        elif not want_playing or stream_url != self.active_url:
            # Stop, or change of station: the current stream must end first.
            # The new station is started when nabd confirms the end.
            if not self.cancel_sent:
                await self._cancel()

    async def reload_config(self):
        async with self._get_lock():
            if self.ready:
                await self._reconcile()

    async def _startup(self):
        """
        Nothing can be playing on our behalf when we start, except a stream
        left over by a previous instance of this daemon: cancel it, and
        record that the radio is stopped.
        """
        async with self._get_lock():
            await sync_to_async(self.control.mark_stopped)()
            await self._send_packet(
                {"type": "cancel", "request_id": RADIO_REQUEST_ID}
            )
            self.ready = True

    async def _retry_later(self):
        await asyncio.sleep(CANCEL_RETRY_SECONDS)
        self._retry_task = None
        try:
            await self.reload_config()
        except Exception as err:  # connection to nabd lost meanwhile
            logging.debug("nabradio: retry failed: %s", err)

    async def _process_response(self, packet):
        async with self._get_lock():
            if not self.active:
                # Answer to the startup cancel, or to a cancel that crossed
                # the end of the stream.
                return

            status = packet.get("status")
            if status == "error" and (
                packet.get("class") in CANCEL_REFUSED_CLASSES
            ):
                # Our message is still in nabd's queue: try again shortly.
                self.cancel_sent = False
                if self._retry_task is None:
                    self._retry_task = asyncio.ensure_future(
                        self._retry_later()
                    )
                return

            # Any other response means the message is over: played to the
            # end, canceled, expired before starting, or failed.
            ended_url = self.active_url
            canceled_by_us = self.cancel_sent
            self.active = False
            self.active_url = ""
            self.cancel_sent = False
            logging.info("nabradio: ended (%s) %s", status, ended_url)

            if not canceled_by_us:
                # Stopped from the rabbit's button, stream error or end of
                # stream. Unless another station was asked for meanwhile,
                # the radio is now stopped.
                want_playing, stream_url = await sync_to_async(
                    self.control.get_desired
                )()
                if want_playing and stream_url == ended_url:
                    await sync_to_async(self.control.mark_stopped)()

            await self._reconcile()

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

        direction = "next" if delta <= EARS_STEPS // 2 else "previous"
        if await sync_to_async(self.control.switch_station)(direction):
            await self.reload_config()

    async def process_nabd_packet(self, packet):
        packet_type = packet.get("type")

        if (
            packet_type == "response"
            and packet.get("request_id") == RADIO_REQUEST_ID
        ):
            await self._process_response(packet)
            return

        if (
            packet_type == "rfid_event"
            and packet.get("app") == "nabradio"
            and packet.get("event") == "detected"
        ):
            stream_url = await rfid_data.read_data_ui(packet["uid"])
            if stream_url and stream_url != RFID_NO_STREAM:
                await sync_to_async(self.control.play_stream_url)(stream_url)
                await self.reload_config()
            return

        if packet_type == "ears_event":
            right_position = packet.get("right")
            if isinstance(right_position, int):
                if self.active:
                    await self._handle_right_ear_rotation(right_position)
                else:
                    self.right_ear_position = right_position

    def start_service_loop(self, loop):
        loop.create_task(self._startup())
        return None


if __name__ == "__main__":
    NabRadio.main(sys.argv[1:])
