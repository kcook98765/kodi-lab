"""Single Omega runtime and profile for the fresh-install experiment."""

from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.request import ProxyHandler, Request, build_opener
from uuid import UUID, uuid4
from xml.etree import ElementTree as ET

from kodi_lab.domain.external_targets import ExternalProtocolKind
from kodi_lab.kodi.bridge import ExternalBridgeClient, ValidatedExternalEndpoint

IMAGE = "sha256:7f779626cd56fb3bf86e14e39657fee5e8c5299a8f6f7abf59449a1163e7aa8e"
BRIDGE = "service.kodi_lab_bridge"
ESTUARY = "skin.estuary"
REPO = "repository.kodi-lab-v2"
LABEL = "io.kodi-lab.managed-v2"
DOCKER = ["docker", "--host", "unix:///var/run/docker.sock"]
HERE = Path(__file__).resolve().parent
OPENER = build_opener(ProxyHandler({}))


class Refusal(Exception):
    pass


def atomic(path: Path, data: bytes) -> None:
    temporary = path.with_name(".stage-" + uuid4().hex)
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def docker(*args: str) -> str:
    result = subprocess.run(
        [*DOCKER, *args], capture_output=True, text=True, timeout=35, check=True
    )
    return result.stdout.strip()


def wait_until(check, seconds: float, reason: str):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        result = check()
        if result:
            return result
        time.sleep(0.15)
    raise Refusal(reason)


def descriptor() -> bytes:
    addon = ET.Element("addon", id=REPO, name="Kodi Lab Managed Feed", version="1.0.0")
    repo = ET.SubElement(
        addon, "extension", point="xbmc.addon.repository", name="Kodi Lab Managed Feed"
    )
    directory = ET.SubElement(repo, "dir", minversion="21.0.0")
    for key, params, tail in (
        ("info", {"compressed": "false"}, "/addons.xml"),
        ("checksum", {"verify": "sha256"}, "/addons.xml.sha256"),
        ("datadir", {"zip": "true"}, "/zips/"),
    ):
        ET.SubElement(directory, key, params).text = "http://feed:8090" + tail
    ET.SubElement(directory, "hashes").text = "sha256"
    ET.SubElement(addon, "extension", point="xbmc.addon.metadata")
    return ET.tostring(addon, encoding="utf-8", xml_declaration=True)


class ManagedBridgeClient(ExternalBridgeClient):
    @staticmethod
    def _envelope(*, operation, **fields):
        if operation not in {
            "bridge_identity",
            "managed_v2_install",
            "managed_v2_setting",
        }:
            raise Refusal("invalid_managed_bridge_operation")
        envelope = ExternalBridgeClient._envelope(operation="bridge_identity", **fields)
        envelope["operation"] = operation
        return envelope


class Runtime:
    def __init__(self, root: Path):
        self.root = root.resolve(strict=True)
        mode = self.root.stat()
        if mode.st_mode & 0o077 or mode.st_uid != os.getuid():
            raise Refusal("private_owner_root_required")
        self.pointer = self.root / "profile"
        self.material = self.root / "baseline"
        self.credentials = self.root / "live"
        self.feed = self.root / "feed"
        identity = self.root / "identity"
        with self.exclusive():
            if not identity.exists():
                if any(file.name != "controller.lock" for file in self.root.iterdir()):
                    raise Refusal("fresh_state_directory_required")
                atomic(identity, str(uuid4()).encode())
            self.target = UUID(identity.read_text())
            for name in (
                "live",
                "feed",
                "workspaces",
                "approved",
                "snapshots",
                "volumes",
            ):
                directory = self.root / name
                if directory.is_symlink():
                    raise Refusal("invalid_state_directory")
                directory.mkdir(mode=0o700, exist_ok=True)
        self.name = "kodi-v2-" + self.target.hex[:16]

    @contextmanager
    def exclusive(self):
        with (self.root / "controller.lock").open("a") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise Refusal("managed_busy") from error
            yield

    def capacity(self):
        if shutil.disk_usage(self.root).free < 8 * 1024**3 + 128 * 1024**2:
            raise Refusal("disk_headroom")
        if docker("image", "inspect", "--format", "{{.Id}}", IMAGE) != IMAGE:
            raise Refusal("reviewed_image_required")

    def args(self) -> list[str]:
        return [
            "--pull",
            "never",
            "--label",
            f"{LABEL}={self.target}",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--restart",
            "no",
            "--log-opt",
            "max-size=2m",
            "--log-opt",
            "max-file=1",
        ]

    def inspect(self, name=None):
        name = name or self.name
        ids = docker("ps", "-aq", "--filter", "name=^/" + name + "$").splitlines()
        if not ids:
            return None
        obj = json.loads(docker("inspect", "--type", "container", ids[0]))[0]
        if (
            len(ids) != 1
            or obj["Name"] != "/" + name
            or obj["Image"] != IMAGE
            or obj["Config"]["Labels"].get(LABEL) != str(self.target)
        ):
            raise Refusal("container_identity")
        return obj

    def network(self) -> str:
        name = self.name + "-net"
        matches = docker(
            "network", "ls", "-q", "--filter", "name=^" + name + "$"
        ).splitlines()
        if not matches:
            return docker(
                "network",
                "create",
                "--internal",
                "--label",
                f"{LABEL}={self.target}",
                name,
            )
        data = json.loads(docker("network", "inspect", matches[0]))[0]
        if (
            len(matches) != 1
            or not data["Internal"]
            or data["Labels"].get(LABEL) != str(self.target)
        ):
            raise Refusal("network_identity")
        return data["Id"]

    def read_pointer(self) -> str:
        value = self.pointer.read_text()
        if not re.fullmatch(r"[0-9a-f]{32}", value):
            raise Refusal("profile_identity")
        return value

    def profile(self, slot=None) -> Path:
        value = slot or self.read_pointer()
        if not re.fullmatch(r"[0-9a-f]{32}", value):
            raise Refusal("profile_identity")
        path = self.root / "volumes" / value
        if not path.is_dir() or path.is_symlink():
            raise Refusal("profile_missing")
        return path

    def volume(self, slot):
        return self.name + "-" + slot[:12]

    def stage(self, clean):
        self.capacity()
        slot = uuid4().hex
        path = self.root / "volumes" / slot
        path.mkdir(mode=0o700)
        if clean:
            shutil.copytree(self.material / "profile", path, dirs_exist_ok=True)
        docker(
            "volume",
            "create",
            "--label",
            f"{LABEL}={self.target}",
            "--opt",
            "type=none",
            "--opt",
            "o=bind",
            "--opt",
            f"device={path}",
            self.volume(slot),
        )
        return slot

    def require_volume(self, slot):
        value = json.loads(docker("volume", "inspect", self.volume(slot)))[0]
        path = self.profile(slot)
        if value["Labels"].get(LABEL) != str(self.target) or value["Options"].get(
            "device"
        ) != str(path):
            raise Refusal("volume_identity")
        return path

    def retire(self, slot):
        path = self.require_volume(slot)
        docker("volume", "rm", self.volume(slot))
        shutil.rmtree(path)

    def exchange(self, slot):
        self.require_stopped()
        old = self.read_pointer() if self.pointer.exists() else None
        atomic(self.pointer, slot.encode())
        if old is not None:
            self.retire(old)
        (self.root / "uncertain").unlink(missing_ok=True)

    def prepare(self):
        if self.pointer.exists():
            self.profile()
            return {"state": "prepared", "profile": "omega"}
        self.capacity()
        build = self.root / ("baseline-" + uuid4().hex)
        build.mkdir(mode=0o700)
        image_seed = docker("create", *self.args(), "--network", "none", IMAGE)
        try:
            docker(
                "cp",
                image_seed + ":/usr/share/kodi/system/addon-manifest.xml",
                str(build / "manifest.xml"),
            )
        finally:
            docker("rm", image_seed)
        xml = ET.parse(build / "manifest.xml")
        for addon in (BRIDGE, REPO):
            ET.SubElement(xml.getroot(), "addon").text = addon
        xml.write(build / "manifest.xml", encoding="utf-8")
        bridge = build / "bridge"
        src = HERE.parents[1] / "bridge" / BRIDGE
        shutil.copytree(src, bridge, ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copyfile(HERE / "bridge_addon.py", bridge / "service.py")
        # The required system add-on uses fresh per-run credentials.
        repo = build / "repository"
        repo.mkdir()
        (repo / "addon.xml").write_bytes(descriptor())
        userdata = build / "profile/userdata"
        userdata.mkdir(parents=True)
        settings = ET.Element("settings", version="1")
        for key, value in {
            "services.webserver": "true",
            "services.webserverport": "8080",
            "services.webserverauthentication": "false",
            "services.esenabled": "false",
            "general.addonupdates": "2",
            "addons.unknownsources": "false",
            "locale.language": "resource.language.en_gb",
            "lookandfeel.skin": "skin.estuary",
        }.items():
            ET.SubElement(settings, "setting", id=key).text = value
        ET.ElementTree(settings).write(userdata / "guisettings.xml", encoding="utf-8")
        # Native debug log enables fresh execution checks. Profiles remain private.
        (userdata / "advancedsettings.xml").write_text(
            '<advancedsettings><loglevel hide="true">1</loglevel></advancedsettings>'
        )
        if self.material.exists():
            shutil.rmtree(self.material)
        build.rename(self.material)
        self.exchange(self.stage(clean=True))
        return {"state": "prepared", "profile": "omega"}

    def address(self):
        obj = self.inspect()
        if not obj or not obj["State"]["Running"]:
            raise Refusal("kodi_stopped")
        networks = obj["NetworkSettings"]["Networks"]
        if (
            set(networks) != {self.name + "-net"}
            or obj["HostConfig"]["PortBindings"]
            or not re.fullmatch(
                r"\d+\.\d+\.\d+\.\d+", networks[self.name + "-net"]["IPAddress"]
            )
        ):
            raise Refusal("runtime_network_identity")
        volumes = [
            mount["Name"] for mount in obj["Mounts"] if mount["Type"] == "volume"
        ]
        if volumes != [self.volume(self.read_pointer())]:
            raise Refusal("live_profile_identity")
        return networks[self.name + "-net"]["IPAddress"]

    def rpc(self, method, **params):
        payload = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        ).encode()
        request = Request(
            "http://" + self.address() + ":8080/jsonrpc",
            payload,
            {"Content-Type": "application/json"},
        )
        with OPENER.open(request, timeout=5) as response:
            data = json.load(response)
        if "error" in data:
            raise Refusal("native_" + method.replace(".", "_") + "_refused")
        return data["result"]

    def signed(self, operation, payload=None, credential=None):
        value = credential or json.loads((self.credentials / "state.json").read_text())
        endpoint = ValidatedExternalEndpoint(
            ExternalProtocolKind.JSONRPC_HTTP, self.address(), 8080
        )
        client = ManagedBridgeClient(response_timeout=65)
        return client._authenticated_json(
            endpoint,
            bridge_port=8765,
            target_id=self.target,
            bridge_id=UUID(value["bridge_id"]),
            secret=base64.urlsafe_b64decode(value["secret"] + "="),
            operation=operation,
            payload=payload,
        )

    def ready(self):
        try:
            version = self.rpc(
                "Application.GetProperties", properties=["name", "version"]
            )
            bridge = self.signed("bridge_identity")
            if (
                (version["version"]["major"], version["version"]["minor"]) != (21, 3)
                or bridge["addon_id"] != BRIDGE
                or "managed_v2_install" not in bridge["capabilities"]
                or bridge["build_fingerprint"] != self.bridge_fingerprint()
            ):
                return None
            return {"state": "ready", "version": version, "bridge": bridge}
        except Exception:
            return None

    def bridge_fingerprint(self):
        data = (self.material / "bridge/service.py").read_bytes()
        return hashlib.sha256(data).hexdigest()

    def start(self):
        self.capacity()
        if self.inspect():
            result = self.ready()
            if result:
                return result
            raise Refusal("existing_runtime_not_ready")
        slot = self.read_pointer()
        self.require_stopped()
        self.require_volume(slot)
        key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")
        seed = {
            "format": 2,
            "target_id": str(self.target),
            "bridge_id": str(uuid4()),
            "secret": key,
            "replay": [],
            "deployments": {},
            "rollback": None,
        }
        atomic(self.credentials / "state.json", json.dumps(seed).encode())
        network = self.network()
        helper = self.inspect(self.name + "-feed")
        if not helper:
            docker(
                "run",
                "-d",
                *self.args(),
                "--name",
                self.name + "-feed",
                "--network",
                network,
                "--network-alias",
                "feed",
                "--read-only",
                "--memory",
                "96m",
                "--pids-limit",
                "32",
                "--mount",
                f"type=bind,src={self.feed},dst=/feed,readonly",
                "--entrypoint",
                "/usr/bin/python3",
                IMAGE,
                "-u",
                "-B",
                "-m",
                "http.server",
                "8090",
                "--directory",
                "/feed",
            )
        elif not helper["State"]["Running"]:
            docker("start", helper["Id"])
        docker(
            "run",
            "-d",
            *self.args(),
            "--name",
            self.name,
            "--network",
            network,
            "--memory",
            "2g",
            "--memory-swap",
            "2g",
            "--cpus",
            "1.25",
            "--pids-limit",
            "220",
            "--env",
            "LP_NUM_THREADS=2",
            "--env",
            "PYTHONDONTWRITEBYTECODE=1",
            "--tmpfs",
            "/run/kodi:rw,uid=1000,gid=1000,mode=700,size=32m",
            "--mount",
            f"type=volume,src={self.volume(slot)},dst=/home/kodi/.kodi,volume-nocopy",
            "--mount",
            f"type=bind,src={self.credentials},dst=/managed-v2-credentials,readonly",
            "--mount",
            f"type=bind,src={self.material / 'manifest.xml'},"
            "dst=/usr/share/kodi/system/addon-manifest.xml,readonly",
            "--mount",
            f"type=bind,src={self.material / 'bridge'},"
            f"dst=/usr/share/kodi/addons/{BRIDGE},readonly",
            "--mount",
            f"type=bind,src={self.material / 'repository'},"
            f"dst=/usr/share/kodi/addons/{REPO},readonly",
            IMAGE,
        )
        return wait_until(self.ready, 35, "bridge_readiness_unconfirmed")

    def require_stopped(self):
        if self.inspect() is not None:
            raise Refusal("clean_stop_required")
        if (self.root / "unclean").exists():
            raise Refusal("native_exit_unconfirmed")

    def stop(self, *, clear_fence=True):
        obj = self.inspect()
        if obj is None:
            self.require_stopped()
            return {"state": "stopped"}
        if obj["State"]["Running"]:
            atomic(self.root / "unclean", b"quit dispatched")
            self.rpc("Application.Quit")
            wait_until(
                lambda: not self.inspect()["State"]["Running"],
                20,
                "native_exit_unconfirmed",
            )
            stopped = self.inspect()
            if stopped["State"]["ExitCode"] or stopped["State"]["OOMKilled"]:
                raise Refusal("unclean_native_exit")
            docker("rm", obj["Id"])
            (self.root / "unclean").unlink()
        else:
            raise Refusal("already_exited_without_confirmed_stop")
        (self.credentials / "state.json").unlink(missing_ok=True)
        if clear_fence:
            (self.root / "uncertain").unlink(missing_ok=True)
        return {"state": "stopped"}

    def active_skin(self):
        return self.rpc("GUI.GetProperties", properties=["skin"])["skin"]["id"]

    def skin_settings(self):
        self.require_stopped()
        file = self.profile() / "userdata/guisettings.xml"
        if (
            file.is_symlink()
            or file.parent.is_symlink()
            or file.stat().st_size > 1024 * 1024
        ):
            raise Refusal("skin_settings_path_invalid")
        tree = ET.parse(file)
        if tree.getroot().tag != "settings":
            raise Refusal("skin_settings_invalid")
        return file, tree

    def write_skin(self, addon, saved=None):
        if not re.fullmatch(r"skin\.[a-z0-9_.-]+", addon):
            raise Refusal("invalid_skin")
        file, tree = self.skin_settings()
        root = tree.getroot()
        if saved is not None:
            for entry in list(root):
                if entry.get("id", "").startswith("lookandfeel."):
                    root.remove(entry)
            root.extend(ET.fromstring(value) for value in saved)
        entries = root.findall("setting[@id='lookandfeel.skin']")
        if len(entries) > 1:
            raise Refusal("duplicate_skin_settings")
        setting = (
            entries[0]
            if entries
            else ET.SubElement(root, "setting", id="lookandfeel.skin")
        )
        setting.attrib.pop("default", None)
        setting.text = addon
        atomic(file, ET.tostring(root, encoding="utf-8", xml_declaration=True))

    def start_with_skin(self, addon):
        self.start()

        def activated():
            gui = self.rpc("GUI.GetProperties", properties=["skin", "currentwindow"])
            if (
                gui.get("skin", {}).get("id") != addon
                or gui.get("currentwindow", {}).get("id") != 10000
            ):
                return False
            current = self.rpc("Settings.GetSettingValue", setting="lookandfeel.skin")
            if current.get("value") != addon:
                return False
            log = self.profile() / "temp/kodi.log"
            with log.open(errors="replace") as stream:
                return any(
                    "load skin from:" in line and "/" + addon + "/" in line
                    for line in stream
                )

        wait_until(activated, 8, "skin_boot_readback_unconfirmed")
        return addon

    def reset(self):
        self.require_stopped()
        self.exchange(self.stage(clean=True))
        return {"state": "stopped", "profile": "omega"}

    def cleanup_trial(self):
        """Operator-only teardown for this isolated root, including failed trials."""
        with self.exclusive():
            for name in (self.name, self.name + "-feed"):
                obj = self.inspect(name)
                if obj:
                    if obj["State"]["Running"]:
                        docker("stop", "-t", "6", obj["Id"])
                    docker("rm", obj["Id"])
            for directory in sorted((self.root / "volumes").iterdir()):
                self.retire(directory.name)
            network = docker(
                "network", "ls", "-q", "--filter", f"label={LABEL}={self.target}"
            )
            if network:
                docker("network", "rm", network)
            self.pointer.unlink(missing_ok=True)
            (self.credentials / "state.json").unlink(missing_ok=True)
