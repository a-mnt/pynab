import abc
import asyncio
import base64
import datetime
import fcntl
import json
import os
import platform
import re
import signal
import subprocess
import threading
import time

from asgiref.sync import async_to_sync, sync_to_async
from django.apps import apps
from django.conf import settings
from django.core.cache import cache
from django.http import JsonResponse
from django.shortcuts import render
from django.urls import Resolver404, resolve
from django.utils import translation
from django.utils.translation import gettext_lazy as _
from django.utils.translation import to_language, to_locale
from django.views.generic import View
from nabradio.views import get_radio_status

from nabcommon import hardware
from nabcommon.nabservice import NabService
from nabd.i18n import Config

from . import sound, wifi


def _run_command(cmd, cwd=None):
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as err:
        # Program not installed (e.g. on a development machine).
        return 127, "", str(err)
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def get_ssh_state():
    """
    Return "active", "sshwarn" (active with the default password) or the
    state reported by systemd ("inactive", "unknown"...).
    Kept a few seconds in cache: it is shown on the home page.
    """
    ssh_state = cache.get("ssh_state")
    if ssh_state is None:
        ssh_state = _run_command_stdout(["systemctl", "is-active", "dropbear"])
        if ssh_state != "active":
            ssh_state = _run_command_stdout(["systemctl", "is-active", "ssh"])
        if ssh_state == "active" and os.path.isfile("/run/sshwarn"):
            ssh_state = "sshwarn"
        cache.set("ssh_state", ssh_state, 30)
    return ssh_state


EARS_STEPS = 17  # positions in a full turn, see nabd.ears.Ears.STEPS


def parse_ears(gestalt):
    """
    Extract the state of both ears from a gestalt answer. nabd describes
    each ear as "broken", "ok (position unknown)" or "ok (position=5)".
    Return {"left": {"working": bool, "position": int or None}, "right":
    ...}, or None if nabd did not answer.
    """
    if gestalt.get("status") != "ok":
        return None
    hardware_info = gestalt["result"].get("hardware", {})
    ears = {}
    for side in ("left", "right"):
        status = str(hardware_info.get(f"{side}_ear_status", ""))
        match = re.search(r"position=(\d+)", status)
        ears[side] = {
            "working": status != "" and "broken" not in status,
            "position": int(match.group(1)) % EARS_STEPS if match else None,
        }
    return ears


# Names shown on the website for each service, instead of technical names.
SERVICE_LABELS = {
    "nabradio": (_("Radio"), _("Radios web et lecture.")),
    "nabmastodond": (_("Notifications"), _("Mastodon et messages.")),
    "nabclockd": (_("Réveil et coucher"), _("Horaires du lapin et carillon.")),
    "nabtaichid": (_("Oreilles"), _("Tai chi et mouvements d’oreilles.")),
    "nabiftttd": (_("Automatisations"), _("Actions IFTTT.")),
    "nabweatherd": (_("Météo"), _("Prévisions et animations.")),
    "nabairqualityd": (_("Qualité de l’air"), _("Indice et animations.")),
    "nab8balld": (_("Magic 8 Ball"), _("Réponses aléatoires.")),
    "nabsurprised": (_("Surprises"), _("Contenus surprise.")),
    "nabbookd": (_("Livres audio"), _("Histoires interactives.")),
}


def _run_command_stdout(cmd, cwd=None):
    return _run_command(cmd, cwd=cwd)[1]


class NabdConnection:
    async def __aenter__(self):
        conn = asyncio.open_connection(NabService.HOST, NabService.PORT_NUMBER)
        self.reader, self.writer = await asyncio.wait_for(conn, 0.5)
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.writer.close()
        await self.writer.wait_closed()

    @staticmethod
    async def transaction(fun, *args):
        try:
            async with NabdConnection() as conn:
                return await fun(conn.reader, conn.writer, *args)
        except ConnectionRefusedError:
            return {"status": "error", "message": "Nabd is not running."}
        except asyncio.TimeoutError:
            return {
                "status": "error",
                "message": "Communication with Nabd timed out.",
            }

    @staticmethod
    async def send_packet(writer, packet):
        if isinstance(packet, dict):
            payload = json.dumps(packet) + "\r\n"
            writer.write(payload.encode("utf8"))
        elif isinstance(packet, str):
            writer.write((packet + "\r\n").encode("utf8"))
        else:
            writer.write(packet)
        await writer.drain()

    @staticmethod
    async def wait_for_response(reader, request_id, timeout):
        while True:
            line = await asyncio.wait_for(reader.readline(), timeout)
            packet = json.loads(line.decode("utf8"))
            if (
                packet.get("type") == "response"
                and packet.get("request_id") == request_id
            ):
                return packet


class BaseView(View, metaclass=abc.ABCMeta):
    @abc.abstractmethod
    def template_name(self):
        pass

    async def query_gestalt(self):
        return await NabdConnection.transaction(self._do_query_gestalt)

    async def _do_query_gestalt(self, reader, writer):
        await NabdConnection.send_packet(
            writer, {"type": "gestalt", "request_id": "gestalt"}
        )
        packet = await NabdConnection.wait_for_response(
            reader, "gestalt", 0.5
        )
        return {"status": "ok", "result": packet}

    def get_locales(self):
        config = Config.load()
        return [
            (to_locale(lang), name, to_locale(lang) == config.locale)
            for (lang, name) in settings.LANGUAGES
        ]

    def get_context(self):
        user_locale = Config.load().locale
        locales = self.get_locales()
        return {"current_locale": user_locale, "locales": locales}

    def get(self, request, *args, **kwargs):
        context = self.get_context()
        return render(request, self.template_name(), context=context)

    @staticmethod
    def get_services(page):
        services = []
        for config in apps.get_app_configs():
            if hasattr(config.module, "NABAZTAG_SERVICE_PRIORITY"):
                service_page = getattr(
                    config.module, "NABAZTAG_SERVICE_PAGE", "services"
                )
                if service_page == page:
                    services.append(
                        {
                            "priority": config.module.NABAZTAG_SERVICE_PRIORITY,
                            "name": config.name,
                        }
                    )
        services_sorted = sorted(services, key=lambda s: s["priority"])
        return [s["name"] for s in services_sorted]


class NabWebView(BaseView):
    @staticmethod
    def get_schedule():
        """Set of wake up / sleep times followed (see nabclockd)."""
        try:
            from nabclockd.views import schedule_summary

            return schedule_summary()
        except Exception:
            return None

    def template_name(self):
        return "nabweb/index.html"

    def get_context(self):
        context = super().get_context()
        context["services"] = BaseView.get_services("home")
        context["radio_status"] = self.get_radio_status_safe()
        context["uptime"] = self.get_uptime()
        gestalt = async_to_sync(self.query_gestalt)()
        context["rabbit_state"] = self.get_rabbit_state(gestalt)
        context["ears"] = parse_ears(gestalt)
        context["ear_sides"] = [
            ("left", _("Oreille gauche")),
            ("right", _("Oreille droite")),
        ]
        context["ssh"] = get_ssh_state()
        context["alerts"] = self.get_alerts()
        context["schedule"] = self.get_schedule()
        return context

    def get_rabbit_state(self, gestalt):
        """
        State reported by nabd ("idle", "asleep", "playing", "interactive",
        "recording"), or None if nabd does not answer.
        """
        if gestalt["status"] != "ok":
            return None
        return gestalt["result"].get("state")

    def get_alerts(self):
        """
        Things that need attention. Only uses what the Update page already
        checked (kept in cache): the home page never runs git itself.
        """
        alerts = []
        info = GitInfo.get_repository_info("pynab", cached=True)
        if info and info.get("commits_count", 0) > 0:
            alerts.append("update")
        return alerts

    def get_uptime(self):
        try:
            with open("/proc/uptime", "r") as uptime_f:
                uptime_seconds = int(float(uptime_f.readline().split()[0]))
                return datetime.datetime.now() - datetime.timedelta(seconds=uptime_seconds)
        except FileNotFoundError:
            return None
  
    def get_radio_status_safe(self):
        try:
            return get_radio_status()
        except Exception:
            return {
                "selected_station": None,
                "selected_station_id": None,
                "selected_name": "",
                "selected_stream_url": "",
                "is_playing": False,
                "radios": [],
                "favorites_count": 0,
            }

    def post(self, request, *args, **kwargs):
        if "locale" in request.POST:
            config = Config.load()
            config.locale = request.POST["locale"]
            config.save()
            async_to_sync(self.notify_config_update)("nabd", "locale")
            user_language = to_language(config.locale)
            translation.activate(user_language)
            request.LANGUAGE_CODE = translation.get_language()
        context = self.get_context()
        return render(request, self.template_name(), context=context)

    async def notify_config_update(self, service, slot):
        await NabdConnection.transaction(
            self._do_notify_config_update, service, slot
        )

    async def _do_notify_config_update(self, reader, writer, service, slot):
        try:
            await NabdConnection.send_packet(
                writer,
                {
                    "type": "config-update",
                    "service": service,
                    "slot": slot,
                },
            )
        except Exception:
            pass


class NabWebServicesView(BaseView):
    def template_name(self):
        return "nabweb/services/index.html"

    def get_context(self):
        context = super().get_context()
        services = BaseView.get_services("services")
        context["services"] = services
        cards = []
        for name in services:
            url = f"/{name}/settings"
            try:
                resolve(url)
            except Resolver404:
                # Service without a settings page: nothing to show.
                continue
            title, text = SERVICE_LABELS.get(name, (name, ""))
            cards.append(
                {"name": name, "title": title, "text": text, "url": url}
            )
        context["service_cards"] = cards
        return context


class NabWebSettingsView(NabWebView):
    """
    Page Paramètres : langue, RFID / NFC et aide.
    Hérite de NabWebView pour réutiliser l'enregistrement de la langue
    (méthode post).
    """

    def template_name(self):
        return "nabweb/settings/index.html"

    def get_context(self):
        # Contexte de base uniquement (langues) : pas besoin de l'état
        # radio ni de l'uptime calculés pour l'accueil.
        context = BaseView.get_context(self)
        gestalt = async_to_sync(self.query_gestalt)()
        if gestalt["status"] == "ok":
            hardware_info = gestalt["result"].get("hardware", {})
            context["rfid_support"] = {
                "status": "ok",
                "available": bool(hardware_info.get("rfid")),
            }
        else:
            context["rfid_support"] = gestalt
        context["locale_saved"] = (
            self.request.method == "POST" and "locale" in self.request.POST
        )
        context["sound_available"] = sound.available()
        context["wifi_available"] = wifi.available()
        return context


class NabWebRfidView(BaseView):
    def template_name(self):
        return "nabweb/rfid/index.html"

    @staticmethod
    def get_rfid_services():
        services = []
        for config in apps.get_app_configs():
            if hasattr(config.module, "NABAZTAG_SERVICE_PRIORITY"):
                if hasattr(config.module, "NABAZTAG_RFID_APPLICATION_NAME"):
                    if config.module.NABAZTAG_RFID_APPLICATION_NAME:
                        services.append(
                            {
                                "app": config.name,
                                "name": config.module.NABAZTAG_RFID_APPLICATION_NAME,
                            }
                        )
        return sorted(services, key=lambda s: s["name"])

    def get_context(self):
        context = super().get_context()
        gestalt = async_to_sync(self.query_gestalt)()
        if gestalt["status"] == "ok":
            hardware_info = gestalt["result"].get("hardware", {})
            rfid = {
                "status": "ok",
                "available": bool(hardware_info.get("rfid")),
            }
        else:
            rfid = gestalt
        context["rfid_support"] = rfid
        context["rfid_services"] = NabWebRfidView.get_rfid_services()
        return context


async def check_is_idle_state(reader):
    line = await asyncio.wait_for(reader.readline(), 1.0)
    packet = json.loads(line.decode("utf8"))
    if packet.get("type") != "state" or "state" not in packet:
        return False, {
            "status": "error",
            "message": "Expected state packet",
        }
    if packet["state"] != "idle":
        return False, {
            "status": "error",
            "message": f"Nabaztag is busy ({packet['state']})",
        }
    return True, None


class NabWebRfidReadView(View):
    READ_TIMEOUT = 30.0

    async def read_tag(self, timeout):
        return await NabdConnection.transaction(self._do_read_tag, timeout)

    async def _do_read_tag(self, reader, writer, timeout):
        is_idle, error_msg = await check_is_idle_state(reader)
        if not is_idle:
            return error_msg

        await NabdConnection.send_packet(
            writer,
            {
                "type": "mode",
                "mode": "interactive",
                "events": ["rfid/*"],
                "request_id": "mode",
            },
        )

        await NabdConnection.wait_for_response(reader, "mode", 1.0)

        base64chor = base64.b64encode(bytes([0, 7, 4, 255, 0, 0, 0, 0]))
        packet = (
            b'{"type":"command","sequence":['
            b'{"choreography":"data:application/x-nabaztag-mtl-choreography;base64,'
            + base64chor
            + b'"}]}\r\n'
        )
        await NabdConnection.send_packet(writer, packet)

        try:
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout)
                packet = json.loads(line.decode("utf8"))
                if (
                    packet.get("type") == "rfid_event"
                    and packet.get("event") != "removed"
                ):
                    return {"status": "ok", "event": packet}
        except asyncio.TimeoutError:
            return {
                "status": "timeout",
                "message": "No RFID tag was detected.",
            }

    def post(self, request, *args, **kwargs):
        read_result = async_to_sync(self.read_tag)(
            NabWebRfidReadView.READ_TIMEOUT
        )
        return JsonResponse(read_result)


class NabWebRfidWriteView(View):
    WRITE_TIMEOUT = 30.0

    async def write_tag(self, tech, uid, picture, app, data, timeout):
        return await NabdConnection.transaction(
            self._do_write_tag, tech, uid, picture, app, data, timeout
        )

    async def _do_write_tag(
        self, reader, writer, tech, uid, picture, app, data, timeout
    ):
        is_idle, error_msg = await check_is_idle_state(reader)
        if not is_idle:
            return error_msg

        await NabdConnection.send_packet(
            writer,
            {
                "type": "rfid_write",
                "tech": tech,
                "uid": uid,
                "picture": int(picture),
                "app": app,
                "data": data,
                "request_id": "rfid_write",
            },
        )

        try:
            packet = await NabdConnection.wait_for_response(
                reader, "rfid_write", timeout
            )
            response = {
                "status": packet["status"],
                "rfid": {
                    "tech": tech,
                    "uid": uid,
                    "picture": picture,
                    "app": app,
                    "data": data,
                },
            }
            if "message" in packet:
                response["message"] = packet["message"]
            return response
        except asyncio.TimeoutError:
            return {
                "status": "timeout",
                "message": "No RFID tag was detected.",
            }

    def post(self, request, *args, **kwargs):
        if (
            "tech" not in request.POST
            or "uid" not in request.POST
            or "picture" not in request.POST
            or "app" not in request.POST
        ):
            return JsonResponse(
                {"status": "error", "message": "Missing arguments."},
                status=400,
            )

        tech = request.POST["tech"]
        uid = request.POST["uid"]
        picture = request.POST["picture"]
        app = request.POST["app"]
        data = request.POST.get("data", "")

        write_result = async_to_sync(self.write_tag)(
            tech,
            uid,
            picture,
            app,
            data,
            NabWebRfidReadView.READ_TIMEOUT,
        )
        return JsonResponse(write_result)


class NabWebSytemInfoView(BaseView):
    LOG_FILES = [
        "/tmp/pynab-upgrade-stdout.log",
        "/tmp/pynab-upgrade-stderr.log",
    ]

    def template_name(self):
        return "nabweb/system-info/index.html"

    def get_os_info(self):
        version = "(Unknown)"
        if os.path.isdir("/boot/dietpi"):
            variant = "DietPi"
        elif os.path.isfile("/etc/rpi-issue"):
            variant = "Raspberry Pi"
        else:
            variant = ""

        try:
            with open("/etc/os-release") as release_f:
                for line in release_f:
                    match_obj = re.match(r'PRETTY_NAME="(.+)"$', line.strip())
                    if match_obj:
                        version = match_obj.group(1)
                        break
        except FileNotFoundError:
            pass

        kernel_release = platform.release()
        kernel_build = platform.version()
        kernel_machine = platform.machine()
        match_obj = re.match(r"#[0-9]+", kernel_build)
        if match_obj:
            kernel_build = match_obj.group()

        version = (
            f"{version} - "
            f"Kernel {kernel_release} {kernel_build} {kernel_machine}"
        )

        hostname = _run_command_stdout(["hostname"])
        ip_address = _run_command_stdout(["hostname", "-I"])
        wifi_essid = _run_command_stdout(["iwgetid", "-r"])

        try:
            with open("/proc/uptime", "r") as uptime_f:
                uptime = int(float(uptime_f.readline().split()[0]))
        except FileNotFoundError:
            uptime = 0

        ssh_state = get_ssh_state()

        return {
            "variant": variant,
            "version": version,
            "hostname": hostname,
            "address": ip_address,
            "network": wifi_essid,
            "uptime": uptime,
            "ssh": ssh_state,
        }

    def get_pi_info(self):
        return {"model": hardware.device_model()}

    def get_available_logs(self):
        logs = []
        for log_file in self.LOG_FILES:
            if os.path.isfile(log_file):
                logs.append(log_file)
        return logs

    def get_context(self):
        context = super().get_context()
        gestalt = async_to_sync(self.query_gestalt)()
        context["gestalt"] = gestalt
        context["os"] = self.get_os_info()
        context["pi"] = self.get_pi_info()
        context["available_logs"] = self.get_available_logs()
        return context


class NabWebLogViewerView(View):
    ALLOWED_LOGS = [
        "/tmp/pynab-upgrade-stdout.log",
        "/tmp/pynab-upgrade-stderr.log",
    ]

    def post(self, request):
        log_file = request.POST.get("file")

        if not log_file or log_file not in self.ALLOWED_LOGS:
            return JsonResponse(
                {"status": "error", "message": "Invalid log file"},
                status=400,
            )

        try:
            if os.path.isfile(log_file):
                with open(log_file, "r") as f:
                    content = f.read()
                return JsonResponse({"status": "ok", "content": content})
            else:
                return JsonResponse(
                    {"status": "error", "message": "File not found"},
                    status=404,
                )
        except Exception as e:
            return JsonResponse(
                {"status": "error", "message": str(e)},
                status=500,
            )


class NabWebHardwareTestView(View):
    TEST_TIMEOUT = 30.0

    async def hardware_test(self, test, timeout):
        return await NabdConnection.transaction(
            self._do_hardware_test, test, timeout
        )

    async def _do_hardware_test(self, reader, writer, test, timeout):
        try:
            await NabdConnection.send_packet(
                writer,
                {
                    "type": "test",
                    "test": test,
                    "request_id": "test",
                },
            )
            packet = await NabdConnection.wait_for_response(
                reader, "test", timeout
            )
            return {"status": "ok", "result": packet}
        except asyncio.TimeoutError:
            return {
                "status": "error",
                "message": "Communication with Nabd timed out (running test).",
            }

    def post(self, request, *args, **kwargs):
        test = kwargs.get("test")
        test_result = async_to_sync(self.hardware_test)(
            test, NabWebHardwareTestView.TEST_TIMEOUT
        )
        return JsonResponse(test_result)


class GitInfo:
    REPOSITORIES = {
        "pynab": ".",
        "sound_driver": "../wm8960",
        "ears_driver": "../tagtagtag-ears/",
        "rfid_driver": "../cr14/",
        "nfc_driver": "../st25r391x/",
        "nabblockly": "nabblockly",
    }
    NAMES = {
        "pynab": "Pynab",
        "sound_driver": "Tagtagtag sound card driver",
        "ears_driver": "Ears driver",
        "rfid_driver": "RFID reader driver",
        "nfc_driver": "NFC card driver",
        "nabblockly": "NabBlockly",
    }

    # Updated by the upgrade only if the web site found a new version.
    DRIVERS = ("sound_driver", "ears_driver", "rfid_driver", "nfc_driver", "nabblockly")

    @staticmethod
    def drivers_to_update():
        """
        Drivers with a new version, from the last check ("Check now").
        A driver never checked is included, to be safe.
        """
        drivers = []
        for repository in GitInfo.DRIVERS:
            info = GitInfo.get_repository_info(repository, cached=True)
            if info is None or (
                info.get("status") == "ok" and info.get("commits_count", 0) > 0
            ):
                drivers.append(repository)
        return drivers

    @staticmethod
    def is_updatable(pynab_info):
        """Something to update, and no local Pynab commits that a pull would mix."""
        if pynab_info is None or pynab_info.get("local_commits_count", 0) != 0:
            return False
        if pynab_info.get("commits_count", 0) > 0:
            return True
        for repository in GitInfo.DRIVERS:
            info = GitInfo.get_repository_info(repository, cached=True)
            if info is not None and info.get("commits_count", 0) > 0:
                return True
        return False

    @staticmethod
    def _git(repo_dir, *args, sudo_uid=None):
        """
        Run a git command in a repository.

        The web server runs as root while repositories belong to another
        user (pi). Since git 2.35.2 (and Debian security updates of older
        versions), git refuses to work in a repository owned by someone
        else ("dubious ownership"). So every command is run as the owner
        of the repository, given in sudo_uid.
        """
        cmd = ["git", "-C", repo_dir, *args]
        if sudo_uid is not None and str(sudo_uid) != str(os.geteuid()):
            cmd = ["sudo", "-u", f"#{sudo_uid}"] + cmd
        return _run_command(cmd)

    @staticmethod
    def get_root_dir():
        service_file = "/lib/systemd/system/nabd.service"
        try:
            with open(service_file, "r") as f:
                for line in f:
                    if line.startswith("WorkingDirectory="):
                        root_dir = line.split("=", 1)[1].strip()
                        if root_dir:
                            return root_dir
        except FileNotFoundError:
            pass
        return os.path.dirname(os.path.dirname(__file__))

    @staticmethod
    def get_repository_info(repository, cached=False, force=False):
        relpath = GitInfo.REPOSITORIES[repository]
        cache_key = f"git/info/{repository}"
        if not force:
            info = cache.get(cache_key)
            if info is not None:
                return info
        if cached:
            return None
        info = GitInfo.do_get_repository_info(repository, relpath, force=force)
        timeout = 600
        if info["status"] == "ok":
            timeout = 86400
        cache.set(cache_key, info, timeout)
        return info

    @staticmethod
    def do_get_repository_info(repository, relpath, force=False):
        root_dir = GitInfo.get_root_dir()
        if root_dir is None:
            return {
                "status": "error",
                "message": "Cannot locate Pynab installation from OS systemd services.",
                "info_date": datetime.datetime.now(),
                "name": GitInfo.NAMES[repository],
            }

        repo_dir = os.path.join(root_dir, relpath)
        try:
            repo_owner = str(os.stat(repo_dir).st_uid)
        except FileNotFoundError:
            return {
                "status": "error",
                "message": "Repository directory not found.",
                "info_date": datetime.datetime.now(),
                "name": GitInfo.NAMES[repository],
            }

        def git(*args):
            return GitInfo._git(repo_dir, *args, sudo_uid=repo_owner)

        rc, head_sha1, git_error = git("rev-parse", "HEAD")
        if rc != 0 or head_sha1 == "":
            message = "Cannot get HEAD - not a git repository?"
            if git_error:
                # Say what git complained about, to make this diagnosable.
                message += f" ({git_error.splitlines()[0]})"
            return {
                "status": "error",
                "message": message,
                "info_date": datetime.datetime.now(),
                "name": GitInfo.NAMES[repository],
            }

        info = {
            "head": head_sha1,
            "name": GitInfo.NAMES[repository],
            "info_date": datetime.datetime.now(),
        }

        _, branch, _ = git("rev-parse", "--abbrev-ref", "HEAD")
        info["branch"] = branch

        _, upstream_branch, _ = git("rev-parse", "--abbrev-ref", "@{upstream}")
        info["upstream_branch"] = upstream_branch

        remote = upstream_branch.split("/")[0] if upstream_branch else ""
        if remote:
            _, url, _ = git("remote", "get-url", remote)
        else:
            url = ""
        info["url"] = url

        rc, _, _ = git("diff-index", "--quiet", "HEAD", "--")
        info["local_changes"] = rc != 0

        if force and upstream_branch:
            git("fetch", "-t", "-f")

        if upstream_branch:
            rc, commits_count, _ = git(
                "rev-list", "--count", f"HEAD..{upstream_branch}"
            )
            _, local_commits_count, _ = git(
                "rev-list", "--count", f"{upstream_branch}..HEAD"
            )
            if rc != 0 or commits_count == "":
                info["status"] = "error"
                info["message"] = (
                    "Cannot get number of commits from upstream. "
                    "Not connected to the internet?"
                )
            else:
                info["status"] = "ok"
                info["commits_count"] = int(commits_count)
                info["local_commits_count"] = int(local_commits_count or 0)
        else:
            info["status"] = "ok"
            info["commits_count"] = 0
            info["local_commits_count"] = 0

        _, tag, _ = git("describe", "--long", "--tags", "--always")
        info["tag"] = tag

        return info


class NabWebUpgradeView(BaseView):
    def template_name(self):
        return "nabweb/upgrade/index.html"

    def get_context(self):
        context = super().get_context()
        last_check = None
        partial = False
        pynab_info = GitInfo.get_repository_info("pynab")
        for repository in GitInfo.REPOSITORIES.keys():
            info = GitInfo.get_repository_info(repository, cached=True)
            if info is None:
                partial = True
                continue
            context[repository] = info
            if last_check is None:
                last_check = info["info_date"]
            else:
                last_check = min(last_check, info["info_date"])
        updatable = GitInfo.is_updatable(pynab_info)
        context["partial"] = partial
        context["updatable"] = updatable
        context["last_check"] = last_check
        return context


class NabWebUpgradeRepositoryInfoView(View):
    def get(self, request, *args, **kwargs):
        repository = kwargs.get("repository")
        repo_info = GitInfo.get_repository_info(repository)
        pynab_info = GitInfo.get_repository_info("pynab", cached=True)
        updatable = GitInfo.is_updatable(pynab_info)
        template_name = "nabweb/upgrade/_repository.html"
        context = {"repo": repo_info, "updatable": updatable}
        return render(request, template_name, context=context)


class NabWebUpgradeCheckNowView(View):
    def post(self, request, *args, **kwargs):
        for repository in GitInfo.REPOSITORIES.keys():
            GitInfo.get_repository_info(repository, force=True)
        return JsonResponse({"status": "ok"})


class NabWebUpgradeStatusView(View):
    def get(self, request, *args, **kwargs):
        repo_info = GitInfo.get_repository_info("pynab")
        return JsonResponse(repo_info)


class NabWebUpgradeNowView(View):
    root_owner = "1000"
    # Written by upgrade.sh and install.sh with the current step, and kept
    # locked (flock) by the upgrade for as long as it runs.
    UPGRADE_FILE = "/tmp/pynab.upgrade"

    @classmethod
    def current_step(cls):
        """
        Return the step the upgrade is at ("Updating data models - 10/14"),
        an empty string if it is running but said nothing yet, or None if no
        upgrade is running.
        """
        try:
            with open(cls.UPGRADE_FILE, "r") as upgrade_f:
                try:
                    fcntl.flock(upgrade_f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    # Locked: the upgrade is running.
                    return upgrade_f.read().strip()
                fcntl.flock(upgrade_f, fcntl.LOCK_UN)
        except OSError:
            pass
        return None

    # Steps found useless by install.sh, one number per line.
    SKIPPED_FILE = "/tmp/pynab.upgrade.skipped"

    @classmethod
    def skipped_steps(cls):
        try:
            with open(cls.SKIPPED_FILE) as skipped_f:
                return sorted(
                    {int(line) for line in skipped_f if line.strip().isdigit()}
                )
        except OSError:
            return []

    def get(self, request, *args, **kwargs):
        step = self.current_step()
        skipped = self.skipped_steps()
        if step is None:
            return JsonResponse({"status": "done", "skipped": skipped})
        return JsonResponse(
            {"status": "ok", "message": step, "skipped": skipped}
        )

    def post(self, request, *args, **kwargs):
        root_dir = GitInfo.get_root_dir()
        if root_dir is None:
            return JsonResponse(
                {
                    "status": "error",
                    "message": "Cannot locate Pynab installation from OS systemd services.",
                }
            )

        try:
            self.root_owner = str(os.stat(root_dir).st_uid)
        except FileNotFoundError:
            return JsonResponse(
                {
                    "status": "error",
                    "message": "Pynab installation directory not found.",
                }
            )

        cmd = [
            "sudo",
            "-u",
            f"#{self.root_owner}",
            "flock",
            "-n",
            "/tmp/pynab.upgrade",
            "bash",
            "-lc",
            "echo 'OK'",
        ]
        _, locked, _ = _run_command(cmd)

        if locked == "OK":
            stdout_f = open("/tmp/pynab-upgrade-stdout.log", "w")
            stderr_f = open("/tmp/pynab-upgrade-stderr.log", "w")
            command = [
                "/usr/bin/nohup",
                "sudo",
                "-u",
                f"#{self.root_owner}",
                "flock",
                "/tmp/pynab.upgrade",
                "bash",
                f"{root_dir}/upgrade.sh",
                # Only the drivers with a new version are updated.
                "--drivers=" + ",".join(GitInfo.drivers_to_update()),
            ]
            subprocess.Popen(
                command,
                stdout=stdout_f,
                stderr=stderr_f,
                preexec_fn=os.setpgrp,
            )
            return JsonResponse({"status": "ok"})

        if locked == "":
            return JsonResponse({"status": "ok"})

        return JsonResponse(
            {
                "status": "error",
                "message": "Could not acquire lock, a problem occurred.",
            }
        )


class NabWebShutdownView(View):
    SHUTDOWN_TIMEOUT = 30.0

    async def os_shutdown(self, mode):
        return await NabdConnection.transaction(self._do_os_shutdown, mode)

    async def _do_os_shutdown(self, reader, writer, mode):
        try:
            await NabdConnection.send_packet(
                writer,
                {
                    "type": "shutdown",
                    "mode": mode,
                    "request_id": "shutdown",
                },
            )
            packet = await NabdConnection.wait_for_response(
                reader,
                "shutdown",
                NabWebShutdownView.SHUTDOWN_TIMEOUT,
            )
            return {"status": "ok", "result": packet}
        except asyncio.TimeoutError:
            return {
                "status": "error",
                "message": "Communication with Nabd timed out (shutdown).",
            }

    def post(self, request, *args, **kwargs):
        mode = kwargs.get("mode")
        shutdown_result = async_to_sync(self.os_shutdown)(mode)
        return JsonResponse(shutdown_result)


class NabWebEarsView(View):
    """
    State of the ears (GET) and moving them (POST), for the home page.
    """

    MOVE_TIMEOUT = 20.0

    @staticmethod
    async def _read_state(reader):
        # nabd tells its state as soon as a service connects.
        line = await asyncio.wait_for(reader.readline(), 1.0)
        return json.loads(line.decode("utf8")).get("state")

    @staticmethod
    async def _status(reader, writer, state):
        await NabdConnection.send_packet(
            writer, {"type": "gestalt", "request_id": "gestalt"}
        )
        packet = await NabdConnection.wait_for_response(
            reader, "gestalt", 1.0
        )
        return {
            "status": "ok",
            "state": state,
            "ears": parse_ears({"status": "ok", "result": packet}),
        }

    async def _do_status(self, reader, writer):
        state = await self._read_state(reader)
        return await self._status(reader, writer, state)

    async def _do_move(self, reader, writer, positions):
        # Ears only move when the rabbit is idle: otherwise nabd would keep
        # the position and apply it later, by surprise.
        state = await self._read_state(reader)
        if state != "idle":
            return {"status": "busy", "state": state}
        packet = {"type": "ears", "request_id": "ears"}
        packet.update(positions)
        await NabdConnection.send_packet(writer, packet)
        # nabd answers once the ears have reached their position.
        response = await NabdConnection.wait_for_response(
            reader, "ears", self.MOVE_TIMEOUT
        )
        if response.get("status") != "ok":
            return {
                "status": "error",
                "message": response.get("message", "Erreur"),
            }
        return await self._status(reader, writer, state)

    async def _run(self, function, *args):
        try:
            return await NabdConnection.transaction(function, *args)
        except asyncio.TimeoutError:
            return {
                "status": "error",
                "message": "Timeout lors de la communication avec Nabd.",
            }

    def get(self, request, *args, **kwargs):
        response = JsonResponse(async_to_sync(self._run)(self._do_status))
        response["Cache-Control"] = "no-store"
        return response

    def post(self, request, *args, **kwargs):
        positions = {}
        for side in ("left", "right"):
            if side in request.POST:
                try:
                    position = int(request.POST[side])
                except ValueError:
                    position = -1
                if not 0 <= position < EARS_STEPS:
                    return JsonResponse(
                        {"status": "error", "message": "Position invalide."},
                        status=400,
                    )
                positions[side] = position
        if not positions:
            return JsonResponse(
                {"status": "error", "message": "Aucune position donnée."},
                status=400,
            )
        return JsonResponse(
            async_to_sync(self._run)(self._do_move, positions)
        )


CLOCK_PIDFILE = "/run/nabclockd.pid"
CLOCK_SYNCHRONIZED_FILE = "/run/systemd/timesync/synchronized"


def _clock_daemon_active():
    """
    Tell whether nabclockd is running and managing sleep: it only does so
    once the system clock has been synchronized since boot (see
    NabClockd.synchronized_since_boot).
    """
    try:
        with open(CLOCK_PIDFILE, "r") as pid_f:
            os.kill(int(pid_f.read().strip()), 0)
        with open("/proc/uptime", "r") as uptime_f:
            boot_time = time.time() - float(uptime_f.readline().split()[0])
        return os.stat(CLOCK_SYNCHRONIZED_FILE).st_mtime > boot_time
    except (OSError, ValueError):
        return False


def _set_sleep_override(asleep):
    """
    Record that the user wants the rabbit asleep (True) or awake (False)
    whatever the schedule says, and tell nabclockd.

    nabclockd is the service that puts the rabbit to sleep and wakes it up
    according to the schedule. Without this, it undoes at once what the
    website asked for: the rabbit sent to sleep during its waking hours is
    woken up immediately. The override stays until the schedule agrees.
    """
    from nabclockd.models import Config as ClockConfig

    config = ClockConfig.load()
    config.sleep_wakeup_override = asleep
    config.save(update_fields=["sleep_wakeup_override"])
    try:
        with open(CLOCK_PIDFILE, "r") as pid_f:
            os.kill(int(pid_f.read().strip()), signal.SIGUSR1)
    except (OSError, ValueError):
        pass


def _stop_radio():
    """
    A radio stream never ends by itself, and nabd only falls asleep once
    it has nothing left to play.
    """
    from nabradio import control
    from nabradio.views import _signal_radio_daemon

    if control.get_desired()[0]:
        control.set_playing(False)
        _signal_radio_daemon()


async def _wait_for_state(reader, wanted, timeout):
    """
    Read nabd packets until its state satisfies `wanted` (a function).
    Return True if it did before `timeout` seconds.
    """
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        try:
            line = await asyncio.wait_for(reader.readline(), remaining)
        except asyncio.TimeoutError:
            return False
        if line == b"":
            return False
        try:
            packet = json.loads(line.decode("utf8"))
        except ValueError:
            continue
        if packet.get("type") == "state" and wanted(packet.get("state")):
            return True


class NabWebWakeupView(View):
    WAKEUP_TIMEOUT = 5.0
    # Time given to nabclockd to wake the rabbit up by itself.
    CLOCK_TIMEOUT = 8.0

    async def wakeup(self):
        return await NabdConnection.transaction(self._do_wakeup)

    async def _do_wakeup(self, reader, writer):
        try:
            clock_active = await sync_to_async(_clock_daemon_active)()
            await sync_to_async(_set_sleep_override)(False)

            # nabd first tells its current state, then every change.
            if clock_active and await _wait_for_state(
                reader, lambda state: state != "asleep", self.CLOCK_TIMEOUT
            ):
                return {"status": "ok"}

            # nabclockd is not there to do it. Sending wakeup to a rabbit
            # already awake is harmless.
            await NabdConnection.send_packet(
                writer,
                {
                    "type": "wakeup",
                    "request_id": "wakeup",
                },
            )
            packet = await NabdConnection.wait_for_response(
                reader,
                "wakeup",
                NabWebWakeupView.WAKEUP_TIMEOUT,
            )
            return {"status": packet.get("status", "ok")}
        except asyncio.TimeoutError:
            return {
                "status": "error",
                "message": "Timeout lors de la communication avec Nabd.",
            }
        except Exception as e:
            return {
                "status": "error",
                "message": f"Erreur: {str(e)}",
            }

    def post(self, request, *args, **kwargs):
        wakeup_result = async_to_sync(self.wakeup)()
        return JsonResponse(wakeup_result)


class NabWebSleepView(View):
    SLEEP_TIMEOUT = 10.0
    # Time given to nabclockd to put the rabbit to sleep: the radio jingle
    # and the sleep sound may have to end first.
    CLOCK_TIMEOUT = 20.0

    async def sleep(self):
        return await NabdConnection.transaction(self._do_sleep)

    async def _do_sleep(self, reader, writer):
        try:
            await sync_to_async(_stop_radio)()
            clock_active = await sync_to_async(_clock_daemon_active)()
            await sync_to_async(_set_sleep_override)(True)

            if clock_active:
                # nabclockd now puts the rabbit to sleep itself. A second
                # sleep request here would stay in nabd's queue and send
                # the rabbit back to sleep at its next wake-up.
                if await _wait_for_state(
                    reader, lambda state: state == "asleep", self.CLOCK_TIMEOUT
                ):
                    return {"status": "ok"}
                return {
                    "status": "error",
                    "message": "Le lapin n'a pas confirmé qu'il dort.",
                }

            # nabclockd is not there to do it: ask nabd directly.
            await NabdConnection.send_packet(
                writer,
                {
                    "type": "sleep",
                    "request_id": "sleep",
                },
            )
            packet = await NabdConnection.wait_for_response(
                reader,
                "sleep",
                NabWebSleepView.SLEEP_TIMEOUT,
            )
            return {"status": packet.get("status", "ok")}
        except asyncio.TimeoutError:
            return {
                "status": "error",
                "message": "Timeout lors de la communication avec Nabd.",
            }
        except Exception as e:
            return {
                "status": "error",
                "message": f"Erreur: {str(e)}",
            }

    def post(self, request, *args, **kwargs):
        sleep_result = async_to_sync(self.sleep)()
        return JsonResponse(sleep_result)


async def _do_rabbit_model(reader, writer):
    # nabd tells its state as soon as a service connects.
    line = await asyncio.wait_for(reader.readline(), 1.0)
    state = json.loads(line.decode("utf8")).get("state")
    await NabdConnection.send_packet(
        writer, {"type": "gestalt", "request_id": "gestalt"}
    )
    packet = await NabdConnection.wait_for_response(reader, "gestalt", 1.0)
    return {
        "status": "ok",
        "state": state,
        "model": packet.get("hardware", {}).get("model"),
    }


def rabbit_model():
    """Model of the rabbit ("2019_TAG", "2019_TAGTAG"...), kept in cache."""
    model = cache.get("rabbit_model")
    if model is None:
        try:
            answer = async_to_sync(NabdConnection.transaction)(
                _do_rabbit_model
            )
        except Exception:
            answer = {"status": "error"}
        if answer.get("status") != "ok":
            return None
        model = answer.get("model") or ""
        cache.set("rabbit_model", model, 3600)
    return model


class NabWebSoundView(View):
    """
    Volume of the rear wheel (Settings page).
    GET: levels and wheel position. POST: save, or test one level.
    """

    TEST_SOUND = "nabclockd/signature.mp3"
    TEST_TIMEOUT = 15.0
    _test_lock = threading.Lock()

    def _status(self, model):
        low, high = sound.get_levels(model)
        default_low, default_high = sound.default_levels(model)
        return {
            "status": "ok",
            "low": low,
            "high": high,
            "default_low": default_low,
            "default_high": default_high,
            "wheel": sound.wheel_position(model),
        }

    def get(self, request, *args, **kwargs):
        if not sound.available():
            return JsonResponse({"status": "unavailable"})
        response = JsonResponse(self._status(rabbit_model()))
        response["Cache-Control"] = "no-store"
        return response

    @staticmethod
    def _percent(request, name):
        try:
            value = int(request.POST.get(name, ""))
        except ValueError:
            return None
        return value if 0 <= value <= 100 else None

    @staticmethod
    async def _play(reader, writer, sound_file, timeout):
        line = await asyncio.wait_for(reader.readline(), 1.0)
        state = json.loads(line.decode("utf8")).get("state")
        if state != "idle":
            # The sound would wait in nabd's queue and play later, by surprise.
            return {"status": "busy", "state": state}
        await NabdConnection.send_packet(
            writer,
            {
                "type": "command",
                "request_id": "volume-test",
                "sequence": [{"audio": [sound_file]}],
            },
        )
        # nabd answers once the sound has been played.
        await NabdConnection.wait_for_response(reader, "volume-test", timeout)
        return {"status": "ok"}

    def _test(self, model, low, high, which):
        if not self._test_lock.acquire(blocking=False):
            return {"status": "busy", "state": "test"}
        current = sound.read_conf()
        saved = {
            key: current.get(key, default)
            for key, default in sound.DEFAULTS.items()
        }
        try:
            # For the test, both positions of the wheel play the tested
            # level, whatever the wheel is on (except mute).
            level = low if which == "low" else high
            sound.set_levels(model, level, level)
            result = async_to_sync(NabdConnection.transaction)(
                self._play, self.TEST_SOUND, self.TEST_TIMEOUT
            )
        finally:
            sound.write_conf(saved)
            sound.reload_mixer()
            self._test_lock.release()
        result["wheel"] = sound.wheel_position(model)
        return result

    def post(self, request, *args, **kwargs):
        if not sound.available():
            return JsonResponse({"status": "unavailable"}, status=404)
        low = self._percent(request, "low")
        high = self._percent(request, "high")
        if low is None or high is None:
            return JsonResponse(
                {"status": "error", "message": "Valeur invalide."}, status=400
            )
        low = min(low, high)
        model = rabbit_model()
        action = request.POST.get("action")
        if action == "save":
            reloaded = sound.set_levels(model, low, high)
            result = self._status(model)
            result["applied"] = reloaded
            return JsonResponse(result)
        if action == "test" and request.POST.get("which") in ("low", "high"):
            return JsonResponse(
                self._test(model, low, high, request.POST["which"])
            )
        return JsonResponse(
            {"status": "error", "message": "Action inconnue."}, status=400
        )


class NabWebWifiView(View):
    """
    Wi-Fi of the rabbit (Settings page).
    GET: network in use (and nearby networks with ?scan=1).
    POST action=test: Internet test. POST action=connect: join a network.
    """

    def get(self, request, *args, **kwargs):
        if not wifi.available():
            return JsonResponse({"status": "unavailable"})
        result = {"status": "ok", "job": wifi.job()}
        if request.GET.get("scan"):
            networks = wifi.scan()
            if networks is None:
                result["networks"] = []
                result["scan_failed"] = True
            else:
                result["networks"] = networks
        result["current"] = wifi.status()
        response = JsonResponse(result)
        response["Cache-Control"] = "no-store"
        return response

    def post(self, request, *args, **kwargs):
        if not wifi.available():
            return JsonResponse({"status": "unavailable"}, status=404)
        action = request.POST.get("action")
        if action == "test":
            return JsonResponse({"status": "ok", "internet": wifi.internet_test()})
        if action == "connect":
            ssid = request.POST.get("ssid", "").strip()
            password = request.POST.get("password", "")
            if not ssid or len(ssid.encode("utf8")) > 32:
                return JsonResponse(
                    {"status": "error", "reason": "bad-ssid"}, status=400
                )
            if password and not 8 <= len(password) <= 63:
                return JsonResponse(
                    {"status": "error", "reason": "bad-password"}, status=400
                )
            started = wifi.connect(
                ssid,
                password,
                hidden=request.POST.get("hidden") == "1",
                security=request.POST.get("security", ""),
            )
            if not started:
                return JsonResponse({"status": "busy"})
            return JsonResponse({"status": "ok", "job": wifi.job()})
        return JsonResponse(
            {"status": "error", "message": "Action inconnue."}, status=400
        )
