import asyncio
import json
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor

from nabcommon.nabservice import NabService
from nabcommon.typing import NabdPacket

from . import tts


class NabTtsd(NabService):
    """
    Text-to-speech service. Other services and the website send it texts
    (see client.py); it synthesizes them one at a time, then asks nabd to
    play them as messages (with the streaming choreography).
    """

    PORT = int(os.getenv("NABTTSD_PORT", "10544"))
    HOST = os.getenv("NABTTSD_HOST", "127.0.0.1")

    def __init__(self):
        super().__init__()
        # One synthesis at a time: the Pi Zero has a single core.
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.queue = None
        self.server = None
        self.running = True
        self.counter = 0

    async def reload_config(self):
        pass  # The configuration is read for each sentence.

    async def process_nabd_packet(self, packet: NabdPacket) -> None:
        if packet.get("type") == "response" and packet.get(
            "request_id", ""
        ).startswith("tts-"):
            if packet.get("status") == "error":
                logging.error(f"nabd: {packet.get('message')}")

    def start_service_loop(self, loop):
        return loop.create_task(self.service_loop())

    async def stop_service_loop(self):
        self.running = False
        if self.server is not None:
            self.server.close()
        if self.queue is not None:
            await self.queue.put(None)

    async def service_loop(self):
        self.queue = asyncio.Queue()
        self.server = await asyncio.start_server(
            self.handle_client, NabTtsd.HOST, NabTtsd.PORT
        )
        logging.info(f"listening on {NabTtsd.HOST}:{NabTtsd.PORT}")
        while self.running:
            item = await self.queue.get()
            if item is None:
                break
            packet, writer = item
            response = await self.process_say(packet)
            await self.reply(writer, response)

    async def handle_client(self, reader, writer):
        try:
            while self.running:
                line = await reader.readline()
                if not line:
                    break
                try:
                    packet = json.loads(line.decode("utf8"))
                except ValueError:
                    await self.reply(
                        writer, {"status": "error", "message": "bad json"}
                    )
                    continue
                if packet.get("type") != "say" or not isinstance(
                    packet.get("text"), str
                ):
                    await self.reply(
                        writer,
                        {
                            "request_id": packet.get("request_id"),
                            "status": "error",
                            "message": "expected a say packet with a text",
                        },
                    )
                    continue
                await self.queue.put((packet, writer))
        except (ConnectionError, asyncio.IncompleteReadError):
            pass

    @staticmethod
    async def reply(writer, response):
        response = {"type": "response", **response}
        try:
            writer.write((json.dumps(response) + "\r\n").encode("utf8"))
            await writer.drain()
        except ConnectionError:
            pass  # The client left without waiting.

    async def process_say(self, packet):
        from nabd.i18n import get_locale

        from . import models

        request_id = packet.get("request_id")
        config = await models.Config.load_async()
        if not config.enabled:
            return {
                "request_id": request_id,
                "status": "error",
                "message": "text-to-speech is disabled",
            }
        locale = await get_locale()
        try:
            path = await asyncio.get_event_loop().run_in_executor(
                self.executor,
                lambda: tts.synthesize(
                    packet["text"],
                    config.engine,
                    locale,
                    config.speed,
                    config.pitch,
                    config.volume,
                ),
            )
        except tts.TTSError as err:
            logging.error(str(err))
            return {
                "request_id": request_id,
                "status": "error",
                "message": str(err),
            }
        self.counter += 1
        message = {
            "type": "message",
            "request_id": f"tts-{self.counter}",
            "body": [{"audio": [path.as_posix()]}],
        }
        if packet.get("signature"):
            message["signature"] = {"audio": [packet["signature"]]}
        if packet.get("expiration"):
            message["expiration"] = packet["expiration"]
        self.writer.write((json.dumps(message) + "\r\n").encode("utf8"))
        await self.writer.drain()
        return {"request_id": request_id, "status": "ok"}


if __name__ == "__main__":
    NabTtsd.main(sys.argv[1:])
