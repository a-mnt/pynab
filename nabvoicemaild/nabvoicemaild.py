import sys
import datetime
from asgiref.sync import sync_to_async
from nabcommon.nabservice import NabInfoCachedService
from . import aqicn


class NabVoicemaild(NabInfoCachedService):

    MESSAGES = ["bad", "moderate", "good"]
    ANIMATION_1 = (
        '{"tempo":42,"colors":['
        '{"top":"00ffff","center":"00ffff","right":"00ffff"},'
        '{"top":"00ffff","center":"00ffff","right":"00ffff"},'
        '{"top":"00ffff","center":"00ffff","right":"00ffff"},'
        '{"top":"000000","center":"000000","right":"000000"}]}'
    )
    

    ANIMATIONS = [ANIMATION_1]

    async def get_config(self):
        from . import models
        config = await models.Config.load_async()        
        return (
            config.next_performance_date,
            config.next_performance_type,
            (config.index_voicemail,
            config.visual_voicemail),
        )

    async def update_next(self, next_date, next_args):
        from . import models
        config = await models.Config.load_async()
        config.next_performance_date = next_date
        config.next_performance_type = next_args
        await config.save_async()

    async def fetch_info_data(self, config_t):

        index_voicemail, visual_voicemail = config_t
        client = aqicn.aqicnClient(index_voicemail)
        await sync_to_async(client.update)()

        # Save inferred localization to configuration for display on web
        # interface
        from . import models

        config = await models.Config.load_async()
        new_city = client.get_city()
        if new_city != config.localisation:
            config.localisation = new_city
            await config.save_async()

        return {
            "visual_voicemail": visual_voicemail,
            "data": client.get_data(),
        }


    def get_animation(self, info_data):

        if (info_data["visual_voicemail"] == "nothing") or \
            (info_data["visual_voicemail"] == "alert" and info_data["data"] == 2):
            return None
        info_animation = Nabvoicemaild.ANIMATIONS[info_data["data"]]
        return info_animation

    async def perform_additional(self, expiration, type, info_data, config_t):
        if info_data is None:
            return

        if type == "today":
            message = Nabvoicemaild.MESSAGES[info_data["data"]]
            packet = (
                '{"type":"message",'
                '"signature":{"audio":["nabvoicemaild/signature.mp3"]},'
                '"body":[{"audio":["nabvoicemaild/' + message + '.mp3"]}],'
                '"expiration":"' + expiration.isoformat() + '"}\r\n'
            )
            self.writer.write(packet.encode("utf8"))
            await self.writer.drain()

    async def process_nabd_packet(self, packet):
        if (
            packet["type"] == "asr_event"
            and packet["nlu"]["intent"] == "nabvoicemaild/forecast"
        ):
            next_date, next_args, config_t = await self.get_config()
            now = datetime.datetime.now(datetime.timezone.utc)
            expiration = now + datetime.timedelta(minutes=1)
            await self.perform(expiration, "today", config_t)


if __name__ == "__main__":
    Nabvoicemaild.main(sys.argv[1:])
