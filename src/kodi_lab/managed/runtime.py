"""Reviewed Kodi runtime and its independent working profile."""

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
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from typing import Any, cast
from urllib.request import ProxyHandler, Request, build_opener
from uuid import UUID, uuid4
from xml.etree import ElementTree as ET

from kodi_lab.domain.external_targets import ExternalProtocolKind
from kodi_lab.kodi.bridge import ExternalBridgeClient
from kodi_lab.kodi.external import ValidatedExternalEndpoint

from . import RELEASES
from .protocol import Checkpoint, Document
from .protocol import Refusal as Refusal

BRIDGE = "service.kodi_lab_bridge"
ESTUARY = "skin.estuary"
REPO = "repository.kodi-lab-v2"
LABEL = "io.kodi-lab.managed-v2"
DOCKER = ["docker", "--host", "unix:///var/run/docker.sock"]
OPENER = build_opener(ProxyHandler({}))


def atomic(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    temporary = path.with_name(".stage-" + uuid4().hex)
    try:
        with temporary.open("xb") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        sync_dir(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def sync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def docker(*args: str) -> str:
    result = subprocess.run(
        [*DOCKER, *args], capture_output=True, text=True, timeout=35, check=True
    )
    return result.stdout.strip()


def wait_until[T](check: Callable[[], T], seconds: float, reason: str) -> T:
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
    directory = ET.SubElement(repo, "dir", minversion="19.0.0")
    for key, params, tail in (
        ("info", {"compressed": "false"}, "/addons.xml"),
        ("checksum", {"verify": "sha256"}, "/addons.xml.sha256"),
        ("datadir", {"zip": "true"}, "/zips/"),
    ):
        ET.SubElement(directory, key, params).text = "http://feed:8090" + tail
    ET.SubElement(directory, "hashes").text = "sha256"
    ET.SubElement(addon, "extension", point="xbmc.addon.metadata")
    return cast(bytes, ET.tostring(addon, encoding="utf-8", xml_declaration=True))


class ManagedBridgeClient(ExternalBridgeClient):
    @staticmethod
    def _envelope(
        *,
        operation: str,
        target_id: UUID,
        bridge_id: UUID,
        request_id: UUID,
        nonce: str,
        requested_at: datetime,
        payload: dict[str, object] | None = None,
    ) -> dict[str, object]:
        if operation not in {
            "bridge_identity",
            "managed_v2_install",
            "managed_v2_setting",
            "capture_screenshot",
        }:
            raise Refusal("invalid_managed_bridge_operation")
        envelope = ExternalBridgeClient._envelope(
            operation="bridge_identity",
            target_id=target_id,
            bridge_id=bridge_id,
            request_id=request_id,
            nonce=nonce,
            requested_at=requested_at,
            payload=payload,
        )
        envelope["operation"] = operation
        return envelope


class Runtime:
    def __init__(
        self,
        root: Path,
        checkpoint: Checkpoint | None = None,
        *,
        daemon_root: Path | None = None,
        reviewed_image: Path | None = None,
        generation: str = "omega",
    ) -> None:
        self.generation = generation
        self.system = "/app/share/kodi" if generation == "piers" else "/usr/share/kodi"
        self.addons_system = self.system + "/addons"
        self.checkpoint = checkpoint or (lambda _event, **_data: None)
        self.root = root.resolve(strict=True)
        self.daemon_root = daemon_root if daemon_root is not None else self.root
        mode = self.root.stat()
        if mode.st_mode & 0o077 or mode.st_uid != os.getuid():
            raise Refusal("private_owner_root_required")
        self.pointer = self.root / "profile"
        self.image_pin = self.root / "image"
        self.reviewed_image = reviewed_image
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
        self.name = (
            "kodi-lab-managed-v2"
            if generation == "omega"
            else f"kodi-lab-managed-v2-{generation}"
        )
        prefix = "kodi-v2-" + self.target.hex[:16]
        self.feed_name = prefix + "-feed"
        self.network_name = prefix + "-net"
        self.volume_prefix = prefix + "-"

    @contextmanager
    def exclusive(self) -> Iterator[None]:
        with (self.root / "controller.lock").open("a") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise Refusal("managed_busy") from error
            yield

    def capacity(self) -> str:
        if shutil.disk_usage(self.root).free < 8 * 1024**3 + 128 * 1024**2:
            raise Refusal("disk_headroom")
        image = self.image
        try:
            observed = docker("image", "inspect", "--format", "{{.Id}}", image)
        except subprocess.CalledProcessError as error:
            raise Refusal("reviewed_image_required") from error
        if observed != image:
            raise Refusal("reviewed_image_required")
        return image

    @property
    def image(self) -> str:
        path = self.image_pin if self.pointer.exists() else self.reviewed_image
        try:
            image = path.read_text().strip() if path is not None else ""
        except FileNotFoundError as error:
            raise Refusal("reviewed_image_required") from error
        if re.fullmatch(r"sha256:[0-9a-f]{64}", image) is None:
            raise Refusal("reviewed_image_required")
        return image

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

    def inspect(self, name: str | None = None) -> Document | None:
        name = name or self.name
        ids = docker("ps", "-aq", "--filter", "name=^/" + name + "$").splitlines()
        if not ids:
            return None
        obj: Document = json.loads(docker("inspect", "--type", "container", ids[0]))[0]
        if (
            len(ids) != 1
            or obj["Name"] != "/" + name
            or obj["Image"] != self.image
            or obj["Config"]["Labels"].get(LABEL) != str(self.target)
        ):
            raise Refusal("container_identity")
        return obj

    def network(self) -> str:
        name = self.network_name
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
        return cast(str, data["Id"])

    def read_pointer(self) -> str:
        value = self.pointer.read_text()
        if not re.fullmatch(r"[0-9a-f]{32}", value):
            raise Refusal("profile_identity")
        return value

    def profile(self, slot: str | None = None) -> Path:
        value = slot or self.read_pointer()
        if not re.fullmatch(r"[0-9a-f]{32}", value):
            raise Refusal("profile_identity")
        path = self.root / "volumes" / value
        if not path.is_dir() or path.is_symlink():
            raise Refusal("profile_missing")
        return path

    def volume(self, slot: str) -> str:
        return self.volume_prefix + slot[:12]

    def daemon_path(self, path: Path) -> Path:
        return self.daemon_root / path.relative_to(self.root)

    def stage(self, clean: bool) -> str:
        self.capacity()
        slot = uuid4().hex
        self.checkpoint("profile_staging", slot=slot)
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
            f"device={self.daemon_path(path)}",
            self.volume(slot),
        )
        return slot

    def require_volume(self, slot: str) -> Path:
        value = json.loads(docker("volume", "inspect", self.volume(slot)))[0]
        path = self.profile(slot)
        if value["Labels"].get(LABEL) != str(self.target) or value["Options"].get(
            "device"
        ) != str(self.daemon_path(path)):
            raise Refusal("volume_identity")
        return path

    def retire(self, slot: str) -> None:
        if self.pointer.exists() and slot == self.read_pointer():
            raise Refusal("cannot_retire_active_profile")
        if not re.fullmatch(r"[0-9a-f]{32}", slot):
            raise Refusal("profile_identity")
        volume = self.volume(slot)
        found = docker("volume", "ls", "-q", "--filter", "name=^" + volume + "$")
        path = self.root / "volumes" / slot
        if found:
            self.require_volume(slot)
            docker("volume", "rm", volume)
        if path.exists() or path.is_symlink():
            shutil.rmtree(self.profile(slot))

    def exchange(self, slot: str) -> None:
        self.require_stopped()
        self.require_volume(slot)
        old = self.read_pointer() if self.pointer.exists() else None
        self.checkpoint("profile_before_commit", prior_slot=old)
        atomic(self.pointer, slot.encode())
        self.checkpoint("profile_committed")
        if old is not None:
            self.retire(old)

    def prepare(self) -> Document:
        if self.pointer.exists():
            self.profile()
            return {"state": "prepared", "profile": self.generation}
        image = self.capacity()
        build = self.root / ("baseline-" + uuid4().hex)
        build.mkdir(mode=0o700)
        image_seed = docker("create", *self.args(), "--network", "none", image)
        try:
            docker(
                "cp",
                f"{image_seed}:{self.system}/system/addon-manifest.xml",
                str(build / "manifest.xml"),
            )
        finally:
            docker("rm", image_seed)
        xml = ET.parse(build / "manifest.xml")
        for addon in (BRIDGE, REPO):
            ET.SubElement(xml.getroot(), "addon").text = addon
        xml.write(build / "manifest.xml", encoding="utf-8")
        bridge = build / "bridge"
        from kodi_lab.kodi.rc1_bridge import packaged_stable_bridge_sources

        for name, data in packaged_stable_bridge_sources().items():
            destination = bridge / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
        service = files("kodi_lab.managed").joinpath("resident_service.py")
        if not service.is_file():
            service = (
                Path(__file__).resolve().parents[3] / "bridge/managed_v2/service.py"
            )
        (bridge / "service.py").write_bytes(service.read_bytes())
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
            "services.webserverauthentication": "true",
            "services.webserverusername": "managed",
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
        atomic(self.image_pin, image.encode())
        self.exchange(self.stage(clean=True))
        return {"state": "prepared", "profile": self.generation}

    def address(self) -> str:
        obj = self.inspect()
        if not obj or not obj["State"]["Running"]:
            raise Refusal("kodi_stopped")
        networks = obj["NetworkSettings"]["Networks"]
        if (
            set(networks) != {self.network_name}
            or obj["HostConfig"]["PortBindings"]
            or not re.fullmatch(
                r"\d+\.\d+\.\d+\.\d+", networks[self.network_name]["IPAddress"]
            )
        ):
            raise Refusal("runtime_network_identity")
        volumes = [
            mount["Name"] for mount in obj["Mounts"] if mount["Type"] == "volume"
        ]
        if volumes != [self.volume(self.read_pointer())]:
            raise Refusal("live_profile_identity")
        return cast(str, networks[self.network_name]["IPAddress"])

    def rpc(self, method: str, **params: object) -> Any:
        payload = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        ).encode()
        password = (self.credentials / "rpc").read_text()
        auth = base64.b64encode(("managed:" + password).encode()).decode()
        request = Request(
            "http://" + self.address() + ":8080/jsonrpc",
            payload,
            {"Content-Type": "application/json", "Authorization": "Basic " + auth},
        )
        with OPENER.open(request, timeout=5) as response:
            data = json.load(response)
        if "error" in data:
            raise Refusal("native_" + method.replace(".", "_") + "_refused")
        return data["result"]

    def signed(
        self,
        operation: str,
        payload: dict[str, object] | None = None,
        credential: Document | None = None,
    ) -> Document:
        value = credential or json.loads((self.credentials / "state.json").read_text())
        endpoint = ValidatedExternalEndpoint(
            ExternalProtocolKind.JSONRPC_HTTP, self.address(), 8080
        )
        client = ManagedBridgeClient(response_timeout=65)
        result = client._authenticated_json(
            endpoint,
            bridge_port=8765,
            target_id=self.target,
            bridge_id=UUID(value["bridge_id"]),
            secret=base64.urlsafe_b64decode(value["secret"] + "="),
            operation=operation,
            payload=payload,
        )

        if not isinstance(result, dict):
            raise Refusal("native_bridge_response_invalid")
        return result

    def screen(self) -> Document:
        value = json.loads((self.credentials / "state.json").read_text())
        endpoint = ValidatedExternalEndpoint(
            ExternalProtocolKind.JSONRPC_HTTP, self.address(), 8080
        )
        image = ManagedBridgeClient().capture_screenshot(
            endpoint,
            bridge_port=8765,
            target_id=self.target,
            bridge_id=UUID(value["bridge_id"]),
            secret=base64.urlsafe_b64decode(value["secret"] + "="),
            request_id=uuid4(),
            nonce=secrets.token_urlsafe(32),
            requested_at=datetime.now(UTC),
        )
        if len(image.png) > 1_450_000:
            raise Refusal("managed_screenshot_limit")
        return {
            "png": base64.b64encode(image.png).decode(),
            "width": image.width,
            "height": image.height,
        }

    def logs(self) -> Document:
        path = self.profile() / "temp/kodi.log"
        if path.is_symlink() or path.parent.is_symlink() or not path.is_file():
            raise Refusal("managed_log_unavailable")
        with path.open("rb") as stream:
            stream.seek(max(0, path.stat().st_size - 16_384))
            return {"text": stream.read(16_384).decode(errors="replace")}

    def ready(self) -> Document | None:
        try:
            version = self.rpc(
                "Application.GetProperties", properties=["name", "version"]
            )
            bridge = self.signed("bridge_identity")
            actual = version["version"]
            if self.generation == "piers":
                from kodi_lab.runtime.ostree_validation import native_identity

                native_identity(version, "22.0-rc.2")
            if (
                version.get("name") != "Kodi"
                or (actual["major"], actual["minor"]) != RELEASES[self.generation]
                or bridge["addon_id"] != BRIDGE
                or "managed_v2_install" not in bridge["capabilities"]
                or bridge["build_fingerprint"] != self.bridge_fingerprint()
            ):
                return None
            return {"state": "ready", "version": version, "bridge": bridge}
        except Exception:
            return None

    def bridge_fingerprint(self) -> str:
        data = (self.material / "bridge/service.py").read_bytes()
        return hashlib.sha256(data).hexdigest()

    def start(self) -> Document:
        self.capacity()
        if self.inspect():
            result = self.ready()
            if result:
                return result
            raise Refusal("existing_runtime_not_ready")
        if docker(
            "ps",
            "-q",
            "--filter",
            "label=" + LABEL,
            "--filter",
            "name=^/kodi-lab-managed-v2(-matrix|-nexus|-piers)?$",
        ):
            raise Refusal("another_managed_runtime_running")
        slot = self.read_pointer()
        self.require_stopped()
        self.require_volume(slot)
        rpc_password = secrets.token_urlsafe(32)
        self.configure_rpc(rpc_password)
        atomic(self.credentials / "rpc", rpc_password.encode())
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
        helper = self.inspect(self.feed_name)
        if not helper:
            docker(
                "run",
                "-d",
                *self.args(),
                "--name",
                self.feed_name,
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
                f"type=bind,src={self.daemon_path(self.feed)},dst=/feed,readonly",
                "--entrypoint",
                "/usr/bin/python3",
                self.image,
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
            f"type=bind,src={self.daemon_path(self.credentials)},dst=/managed-v2-credentials,readonly",
            "--mount",
            f"type=bind,src={self.daemon_path(self.material / 'manifest.xml')},"
            f"dst={self.system}/system/addon-manifest.xml,readonly",
            "--mount",
            f"type=bind,src={self.daemon_path(self.material / 'bridge')},"
            f"dst={self.addons_system}/{BRIDGE},readonly",
            "--mount",
            f"type=bind,src={self.daemon_path(self.material / 'repository')},"
            f"dst={self.addons_system}/{REPO},readonly",
            self.image,
        )
        self.checkpoint("runtime_created")
        result = wait_until(self.ready, 35, "bridge_readiness_unconfirmed")
        assert result is not None
        return result

    def require_stopped(self) -> None:
        if self.inspect() is not None:
            raise Refusal("clean_stop_required")

    def check_live_profile(self, obj: Document) -> None:
        slot = self.read_pointer()
        self.require_volume(slot)
        if [mount["Name"] for mount in obj["Mounts"] if mount["Type"] == "volume"] != [
            self.volume(slot)
        ]:
            raise Refusal("live_profile_identity")

    def remove_exited(self) -> None:
        # Explicit Reset/Load can discard an unclean exited profile after
        # recovery explains the failure. A running instance is never forced down.
        obj = self.inspect()
        if obj:
            self.check_live_profile(obj)
            if obj["State"]["Running"]:
                raise Refusal("clean_stop_required")
            docker("rm", obj["Id"])
        for filename in ("state.json", "rpc"):
            (self.credentials / filename).unlink(missing_ok=True)

    def stop(self) -> Document:
        obj = self.inspect()
        if obj is not None:
            self.check_live_profile(obj)
            if obj["State"]["Running"]:
                self.checkpoint("runtime_quit_requested")
                self.rpc("Application.Quit")
                wait_until(
                    lambda: self._has_exited(),
                    20,
                    "native_exit_unconfirmed",
                )
            stopped = self.inspect()
            if (
                stopped is None
                or stopped["State"]["ExitCode"]
                or stopped["State"]["OOMKilled"]
            ):
                raise Refusal("unclean_native_exit")
            docker("rm", obj["Id"])
            self.checkpoint("runtime_removed")
        (self.credentials / "state.json").unlink(missing_ok=True)
        (self.credentials / "rpc").unlink(missing_ok=True)
        if self.pointer.exists():
            self.configure_rpc(None)
        return {"state": "stopped"}

    def _has_exited(self) -> bool:
        observed = self.inspect()
        return observed is not None and observed["State"]["Running"] is False

    def active_skin(self) -> str:
        return cast(
            str, self.rpc("GUI.GetProperties", properties=["skin"])["skin"]["id"]
        )

    def skin_settings(self) -> tuple[Path, ET.ElementTree[ET.Element]]:
        self.require_stopped()
        file = self.profile() / "userdata/guisettings.xml"
        if (
            file.is_symlink()
            or file.parent.is_symlink()
            or file.stat().st_size > 1024 * 1024
        ):
            raise Refusal("skin_settings_path_invalid")
        parser = ET.XMLParser(
            target=ET.TreeBuilder(insert_comments=True, insert_pis=True)
        )
        tree = ET.parse(file, parser=parser)
        if tree.getroot().tag != "settings":
            raise Refusal("skin_settings_invalid")
        return file, tree

    def configure_rpc(self, password: str | None) -> None:
        file, tree = self.skin_settings()
        root = tree.getroot()
        values = {
            "services.webserver": "true",
            "services.webserverauthentication": "true",
            "services.webserverusername": "managed",
            "services.webserverpassword": password,
        }
        for key, value in values.items():
            for old in root.findall(f"setting[@id='{key}']"):
                root.remove(old)
            if value is not None:
                ET.SubElement(root, "setting", id=key).text = value
        atomic(file, ET.tostring(root, encoding="utf-8", xml_declaration=True))

    def write_skin(
        self, addon: str, saved: Sequence[str | bytes] | None = None
    ) -> None:
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

    def start_with_skin(self, addon: str) -> str:
        self.start()

        def activated() -> bool:
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

    def reset(self) -> Document:
        self.require_stopped()
        self.exchange(self.stage(clean=True))
        return {"state": "stopped", "profile": self.generation}

    def cleanup_trial(self) -> None:
        """Operator-only teardown for this isolated root, including failed trials."""
        with self.exclusive():
            for name in (self.name, self.feed_name):
                obj = self.inspect(name)
                if obj:
                    if obj["State"]["Running"]:
                        docker("stop", "-t", "6", obj["Id"])
                    docker("rm", obj["Id"])
            self.pointer.unlink(missing_ok=True)
            for directory in sorted((self.root / "volumes").iterdir()):
                self.retire(directory.name)
            network = docker(
                "network", "ls", "-q", "--filter", f"label={LABEL}={self.target}"
            )
            if network:
                docker("network", "rm", network)
            self.pointer.unlink(missing_ok=True)
            (self.credentials / "state.json").unlink(missing_ok=True)
            (self.credentials / "rpc").unlink(missing_ok=True)
