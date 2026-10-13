"""Reviewed RC1 acceptance using the Phase 3 runtime checks and fixed RPC/capture."""

from __future__ import annotations

import io
import json
import tarfile
import time
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import docker  # type: ignore[import-untyped]

from kodi_lab.config import Settings
from kodi_lab.runtime.recipes import ReviewedRecipe
from kodi_lab.runtime.temporary_networks import (
    create_temporary_network,
    remove_exact_temporary_network,
)
from kodi_lab.runtime.validation import (
    RecipeValidationError,
    ValidationResult,
    _capture,
    _exec,
    _readiness,
    _rpc,
)

_PROBE = b"""import json,pathlib,sys,xbmc
pathlib.Path("/home/kodi/.kodi/install-python.json").write_text(json.dumps({"python":sys.version.split()[0],"python_api":xbmc.__version__,"prefix":sys.prefix}))
"""
_ADDON = b"""<addon id="script.kodi_lab.validation" name="Reviewed validation"
version="1.0.0" provider-name="Kodi Lab">
<requires>
<import addon="xbmc.python" version="3.0.0"/>
</requires>
<extension point="xbmc.python.script" library="probe.py"/>
<extension point="xbmc.addon.metadata">
<summary lang="en_GB">Temporary validation</summary>
<description lang="en_GB">Temporary validation</description>
<platform>all</platform>
<license>MIT</license>
</extension>
</addon>"""


def probe_archive() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as archive:
        for name in ("addons", "addons/script.kodi_lab.validation"):
            entry = tarfile.TarInfo(name)
            entry.uid = entry.gid = 1000
            entry.mode = 0o755
            entry.type = tarfile.DIRTYPE
            archive.addfile(entry)
        for name, payload in (("addon.xml", _ADDON), ("probe.py", _PROBE)):
            entry = tarfile.TarInfo("addons/script.kodi_lab.validation/" + name)
            entry.uid = entry.gid = 1000
            entry.mode = 0o644
            entry.size = len(payload)
            archive.addfile(entry, io.BytesIO(payload))
    return buf.getvalue()


def _file(container: Any, path: str, maximum: int = 2 * 1024**2) -> bytes:
    chunks, _ = container.get_archive(path)
    raw = bytearray()
    for chunk in chunks:
        raw.extend(chunk)
        if len(raw) > maximum + 16384:
            raise RecipeValidationError("validation file exceeds bound")
    with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
        members = archive.getmembers()
        if len(members) != 1 or not members[0].isfile() or members[0].size > maximum:
            raise RecipeValidationError("validation file differs")
        stream = archive.extractfile(members[0])
        if stream is None:
            raise RecipeValidationError("validation file missing")
        return stream.read()


def native_identity(value: object) -> dict[str, object]:
    from kodi_lab.domain import KodiReleaseIdentity
    from kodi_lab.kodi.readiness import KodiApplicationIdentity

    if not isinstance(value, dict) or value.get("name") != "Kodi":
        raise RecipeValidationError("RC1 identity differs")
    v = value.get("version")
    if (
        not isinstance(v, dict)
        or type(v.get("major")) is not int
        or type(v.get("minor")) is not int
        or any(
            v.get(k) != expected
            for k, expected in {
                "major": 22,
                "minor": 0,
                "tag": "releasecandidate",
                "tagversion": "1",
            }.items()
        )
        or "28ea2eac" not in str(v.get("revision"))
    ):
        raise RecipeValidationError("RC1 upstream identity differs")
    identity = KodiApplicationIdentity("Kodi", 22, 0, "releasecandidate", 1)
    if identity.kodi_release != KodiReleaseIdentity.parse("22.0-rc.1"):
        raise RecipeValidationError("RC1 canonical identity differs")
    return v


def validation_matches(report: object) -> bool:
    if not isinstance(report, dict):
        return False
    try:
        native_identity(report["native_identity"])
        return (
            report.get("python") == "3.14.7"
            and report.get("python_api") == "3.1.0"
            and report.get("ffmpeg") == "9.0.2"
            and report.get("renderer") == "llvmpipe"
            and report.get("windowing") == "x11"
            and report.get("security_contract") == "nonroot-capless-seccomp-v1"
            and report.get("profile") == "/home/kodi/.kodi"
            and report.get("profile_restart") is True
            and report.get("framebuffer_change") is True
            and report.get("session_dbus") is True
            and report.get("stale_display_absent") is True
        )
    except (ValueError, RecipeValidationError, KeyError):
        return False


def validate_ostree_image(
    reviewed: ReviewedRecipe,
    *,
    image_id: str,
    operation_id: UUID,
    settings: Settings,
    client: Any | None = None,
    on_resource: Callable[[str, str], None] | None = None,
    on_log: Callable[[bytes], None] | None = None,
) -> ValidationResult:
    owned = client is None
    client = client or docker.from_env(timeout=15)
    started = datetime.now(UTC)
    network = container = None
    cycles: list[dict[str, object]] = []
    report: dict[str, object] = {}
    try:
        network = create_temporary_network(
            client, settings, operation_id=operation_id, purpose="validation"
        )
        if on_resource is not None:
            on_resource("network", network.id)
        container = client.containers.create(
            image_id,
            name=f"kodi-lab-recipe-validation-{operation_id.hex}",
            user="1000:1000",
            network=network.name,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"],
            mem_limit="1g",
            pids_limit=128,
            labels={
                "io.kodi-lab.resource": "recipe-validation",
                "io.kodi-lab.operation-id": str(operation_id),
            },
        )
        if on_resource is not None:
            on_resource("container", container.id)
        if not container.put_archive("/home/kodi/.kodi", probe_archive()):
            raise RecipeValidationError("Python validation staging failed")
        for number in (1, 2):
            container.start()
            _readiness(container, timeout_seconds=120)
            attrs = client.api.inspect_container(container.id)
            host = attrs.get("HostConfig", {})
            if (
                attrs.get("Image") != image_id
                or attrs.get("Config", {}).get("User") != "1000:1000"
                or host.get("Privileged")
                or host.get("Devices")
                or host.get("Binds")
                or host.get("CapAdd")
                or host.get("CapDrop") != ["ALL"]
                or host.get("PortBindings")
                or attrs.get("Mounts")
                or set(attrs.get("NetworkSettings", {}).get("Networks", {}))
                != {network.name}
                or host.get("SecurityOpt") != ["no-new-privileges:true"]
            ):
                raise RecipeValidationError("RC1 isolation differs")
            status = _exec(
                container,
                [
                    "/usr/bin/python3",
                    "-I",
                    "-c",
                    "print(open('/proc/1/status').read())",
                ],
            ).decode()
            if not all(
                s in status
                for s in (
                    "CapEff:\t0000000000000000",
                    "CapBnd:\t0000000000000000",
                    "Seccomp:\t2",
                    "NoNewPrivs:\t1",
                    "Uid:\t1000\t1000\t1000\t1000",
                    "Gid:\t1000\t1000\t1000\t1000",
                )
            ):
                raise RecipeValidationError("RC1 kernel restrictions differ")
            identity = native_identity(
                _rpc(
                    container,
                    "Application.GetProperties",
                    {"properties": ["name", "version"]},
                )
            )
            executable = _exec(
                container, ["/usr/local/bin/kodi-lab-launcher", "--version"], limit=4096
            )
            if b"22.0-RC1" not in executable or b"28ea2eac" not in executable:
                raise RecipeValidationError("RC1 executable differs")
            _exec(container, ["/usr/bin/xdpyinfo", "-display", ":99"])
            _exec(
                container,
                ["/usr/bin/python3", "-I", "/usr/local/lib/kodi-lab/healthcheck.py"],
            )
            _exec(
                container,
                [
                    "/usr/bin/python3",
                    "-I",
                    "-c",
                    "\n".join(
                        (
                            "import pathlib",
                            "bus=[]; addresses=[]",
                            "for p in pathlib.Path('/proc').iterdir():",
                            " if not p.name.isdecimal(): continue",
                            " try:",
                            "  argv=(p/'cmdline').read_bytes().split(b'\\0')",
                            "  if b'/app/lib/kodi/kodi.bin' in argv:",
                            "   parts=(p/'environ').read_bytes().split(b'\\0')",
                            "   env=dict(x.split(b'=',1) for x in parts if b'=' in x)",
                            "   addresses.append(env[b'DBUS_SESSION_BUS_ADDRESS'])",
                            "  if argv and argv[0].endswith(b'dbus-daemon'):",
                            "   assert b'--session' in argv and p.stat().st_uid==1000",
                            "   bus.append(p.name)",
                            " except FileNotFoundError: pass",
                            "assert bus and addresses",
                            "assert all(a.startswith((b'unix:path=/tmp/',"
                            "b'unix:abstract=')) "
                            "for a in addresses)",
                        )
                    ),
                ],
            )
            if (
                _rpc(container, "Input.Home") != "OK"
                or _rpc(container, "Input.Up") != "OK"
            ):
                raise RecipeValidationError("RC1 navigation failed")
            library = _rpc(
                container, "VideoLibrary.GetMovies", {"properties": ["title"]}
            )
            if not isinstance(library, dict):
                raise RecipeValidationError("RC1 library query failed")
            if number == 1:
                home = _capture(container)
                _rpc(container, "GUI.ActivateWindow", {"window": "settings"})
                time.sleep(1)
                gui = _rpc(
                    container, "GUI.GetProperties", {"properties": ["currentwindow"]}
                )
                if (
                    not isinstance(gui, dict)
                    or gui.get("currentwindow", {}).get("id") != 10004
                ):
                    raise RecipeValidationError("RC1 window navigation failed")
                settings_frame = _capture(container)
                if home["png_sha256"] == settings_frame["png_sha256"]:
                    raise RecipeValidationError("RC1 framebuffer unchanged")
                _rpc(
                    container,
                    "Addons.SetAddonEnabled",
                    {
                        "addonid": "script.kodi_lab.validation",
                        "enabled": True,
                    },
                )
                _rpc(
                    container,
                    "Addons.ExecuteAddon",
                    {"addonid": "script.kodi_lab.validation", "wait": True},
                )
                deadline = time.monotonic() + 20
                while True:
                    try:
                        python = json.loads(
                            _file(
                                container, "/home/kodi/.kodi/install-python.json", 4096
                            )
                        )
                        break
                    except docker.errors.NotFound:
                        if time.monotonic() >= deadline:
                            raise RecipeValidationError(
                                "RC1 Python probe did not complete"
                            ) from None
                        time.sleep(0.1)
                if python != {
                    "python": "3.14.7",
                    "python_api": "3.1.0",
                    "prefix": "/opt/fdo",
                }:
                    raise RecipeValidationError("RC1 Python differs")
                _exec(
                    container,
                    [
                        "/usr/bin/python3",
                        "-I",
                        "-c",
                        "from pathlib import Path; "
                        "Path('/home/kodi/.kodi/install-marker').write_text('persistent')",
                    ],
                )
                report.update(
                    native_identity={"name": "Kodi", "version": identity},
                    screenshot=home,
                    navigation_screenshot=settings_frame,
                    python=python["python"],
                    python_api=python["python_api"],
                )
            else:
                if (
                    _exec(
                        container, ["/usr/bin/cat", "/home/kodi/.kodi/install-marker"]
                    ).strip()
                    != b"persistent"
                ):
                    raise RecipeValidationError("RC1 profile restart failed")
            container.stop(timeout=10)
            container.reload()
            state = container.attrs.get("State", {})
            records = [
                json.loads(line)
                for line in _file(
                    container, "/home/kodi/.kodi/supervisor-cycles.jsonl", 4096
                ).splitlines()
            ]
            if (
                state.get("Running")
                or state.get("ExitCode") != 0
                or state.get("OOMKilled")
                or len(records) != number
                or records[-1].get("forced_kill")
                or records[-1].get("kodi_exit_code") != 0
                or records[-1].get("shutdown_seconds", 99) >= 8
            ):
                raise RecipeValidationError("RC1 clean shutdown differs")
            for path in ("/tmp/.X99-lock", "/tmp/.X11-unix/X99"):
                try:
                    container.get_archive(path)
                except docker.errors.NotFound:
                    pass
                else:
                    raise RecipeValidationError("RC1 stale display remains")
            cycles.append(
                {
                    "cycle": number,
                    "readiness": True,
                    "clean_stop": True,
                    "shutdown_seconds": records[-1]["shutdown_seconds"],
                }
            )
        log = _file(container, "/home/kodi/.kodi/temp/kodi.log").decode(
            errors="replace"
        )
        if "llvmpipe" not in log or "9.0.2" not in log or "OpenGL ES" not in log:
            raise RecipeValidationError("RC1 renderer/media dependencies differ")
        report.update(
            schema=reviewed.recipe.validation_contract,
            result="passed",
            recipe_digest=reviewed.digest,
            release="22.0-rc.1",
            image_id=image_id,
            network_mode="isolated_internal_bridge",
            non_root_uid=1000,
            jsonrpc=True,
            navigation=True,
            library_query=True,
            cycles=cycles,
            renderer="llvmpipe",
            windowing="x11",
            ffmpeg="9.0.2",
            security_contract="nonroot-capless-seccomp-v1",
            profile="/home/kodi/.kodi",
            profile_restart=True,
            framebuffer_change=True,
            session_dbus=True,
            stale_display_absent=True,
        )
        return ValidationResult(started, datetime.now(UTC), report)
    except RecipeValidationError:
        raise
    except Exception as exc:
        raise RecipeValidationError("reviewed RC1 validation failed") from exc
    finally:
        try:
            if container is not None:
                if on_log is not None:
                    with suppress(Exception):
                        on_log(_file(container, "/home/kodi/.kodi/temp/kodi.log"))
                container.reload()
                if container.attrs.get("State", {}).get("Running"):
                    container.stop(timeout=10)
                container.remove(force=False, v=False)
        finally:
            try:
                if network is not None:
                    remove_exact_temporary_network(client, network)
            finally:
                if owned:
                    client.close()
