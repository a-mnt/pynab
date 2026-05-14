import abc
import asyncio
import base64
import datetime
import json
import os
import platform
import re
import subprocess

from asgiref.sync import async_to_sync
from django.apps import apps
from django.conf import settings
from django.core.cache import cache
from django.http import JsonResponse
from django.shortcuts import render
from django.utils import translation
from django.utils.translation import to_language, to_locale
from django.views.generic import View
from nabradio.views import get_radio_status

from nabcommon import hardware
from nabcommon.nabservice import NabService
from nabd.i18n import Config


def _run_command(cmd, cwd=None):
    proc = subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


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
    def template_name(self):
        return "nabweb/index.html"

    def get_context(self):
        context = super().get_context()
        context["services"] = BaseView.get_services("home")
        context["radio_status"] = self.get_radio_status_safe()
        context["uptime"] = self.get_uptime()
        return context

    def get_uptime(self):
        try:
            with open("/proc/uptime", "r") as uptime_f:
                return int(float(uptime_f.readline().split()[0]))
        except FileNotFoundError:
            return 0
  
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
        context["services"] = BaseView.get_services("services")
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
            rfid = {
                "status": "ok",
                "available": gestalt["result"]["hardware"]["rfid"],
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

        ssh_state = _run_command_stdout(["systemctl", "is-active", "dropbear"])
        if ssh_state == "inactive":
            ssh_state = _run_command_stdout(["systemctl", "is-active", "ssh"])
        if ssh_state == "active" and os.path.isfile("/run/sshwarn"):
            ssh_state = "sshwarn"

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

    def get_context(self):
        context = super().get_context()
        gestalt = async_to_sync(self.query_gestalt)()
        context["gestalt"] = gestalt
        context["os"] = self.get_os_info()
        context["pi"] = self.get_pi_info()
        return context


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

    @staticmethod
    def _git(repo_dir, *args, sudo_uid=None):
        cmd = ["git", "-C", repo_dir, *args]
        if sudo_uid is not None:
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

        rc, head_sha1, _ = GitInfo._git(repo_dir, "rev-parse", "HEAD")
        if rc != 0 or head_sha1 == "":
            return {
                "status": "error",
                "message": "Cannot get HEAD - not a git repository?",
                "info_date": datetime.datetime.now(),
                "name": GitInfo.NAMES[repository],
            }

        info = {
            "head": head_sha1,
            "name": GitInfo.NAMES[repository],
            "info_date": datetime.datetime.now(),
        }

        _, branch, _ = GitInfo._git(repo_dir, "rev-parse", "--abbrev-ref", "HEAD")
        info["branch"] = branch

        _, upstream_branch, _ = GitInfo._git(
            repo_dir, "rev-parse", "--abbrev-ref", "@{upstream}"
        )
        info["upstream_branch"] = upstream_branch

        remote = upstream_branch.split("/")[0] if upstream_branch else ""
        if remote:
            _, url, _ = GitInfo._git(repo_dir, "remote", "get-url", remote)
        else:
            url = ""
        info["url"] = url

        rc, _, _ = GitInfo._git(repo_dir, "diff-index", "--quiet", "HEAD", "--")
        info["local_changes"] = rc != 0

        if force and upstream_branch:
            GitInfo._git(repo_dir, "fetch", "-t", "-f", sudo_uid=repo_owner)

        if upstream_branch:
            rc, commits_count, _ = GitInfo._git(
                repo_dir, "rev-list", "--count", f"HEAD..{upstream_branch}"
            )
            _, local_commits_count, _ = GitInfo._git(
                repo_dir, "rev-list", "--count", f"{upstream_branch}..HEAD"
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

        _, tag, _ = GitInfo._git(
            repo_dir, "describe", "--long", "--tags", "--always"
        )
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
        updatable = (
            "commits_count" in pynab_info
            and pynab_info["commits_count"] > 0
            and "local_commits_count" in pynab_info
            and pynab_info["local_commits_count"] == 0
        )
        context["partial"] = partial
        context["updatable"] = updatable
        context["last_check"] = last_check
        return context


class NabWebUpgradeRepositoryInfoView(View):
    def get(self, request, *args, **kwargs):
        repository = kwargs.get("repository")
        repo_info = GitInfo.get_repository_info(repository)
        pynab_info = GitInfo.get_repository_info("pynab", cached=True)
        updatable = (
            pynab_info is not None
            and "commits_count" in pynab_info
            and pynab_info["commits_count"] > 0
            and "local_commits_count" in pynab_info
            and pynab_info["local_commits_count"] == 0
        )
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

    def get(self, request, *args, **kwargs):
        cmd = [
            "sudo",
            "-u",
            f"#{self.root_owner}",
            "flock",
            "-n",
            "/tmp/pynab.upgrade",
            "bash",
            "-lc",
            "echo 'Not upgrading' || cat /tmp/pynab.upgrade",
        ]
        _, step, _ = _run_command(cmd)
        if step == "Not upgrading":
            return JsonResponse({"status": "done"})
        return JsonResponse({"status": "ok", "message": step})

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
