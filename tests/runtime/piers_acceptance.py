"""Explicit isolated RC1 runtime acceptance; never a production installation.

This live harness consumes Phase 3 evidence in a private experiment directory.
It validates actual framebuffer/native execution, preserving existing resources.
"""

from __future__ import annotations

import dataclasses
import hashlib
import io
import json
import os
import stat
import subprocess
import tarfile
import time
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import docker  # type: ignore[import-untyped]

from kodi_lab.broker.screenshots import FIXED_SCREENSHOT_COMMAND
from kodi_lab.domain import KodiReleaseIdentity
from kodi_lab.kodi.readiness import KodiApplicationIdentity
from kodi_lab.kodi.screenshots import (
    inspect_x11_frame,
    png_pixel_statistics,
    x11_frame_to_png,
)
from kodi_lab.runtime.ostree import (
    CommandResult,
    _read_file,
    _validate_signature_status,
    detached_signatures,
    signed_component_metadata,
    trusted_key_bytes,
)
from kodi_lab.runtime.ostree_assembly import ASSETS_SHA256, validate_image_config
from kodi_lab.runtime.recipes import OstreeComponent

NATIVE_REF = "runtime/tv.kodi.Kodi.Addon.screensaver.greynetic/x86_64/beta"
NATIVE_COMMIT = "6ef36f452bd36f81824a571e85e6f83e6def01e26893a0322867c616c8e39aa7"
NATIVE_INVENTORY = "4e31c81a0669750d16b560bd0bc6027ceb57e403a27ef86901872ca83f9c09b3"
NATIVE_METADATA = "eeb99de60585f80141eeeb716803e586ca04bb9f4f6ed96eb65929f95920ae3f"
NATIVE_LIBRARY = "8d8ddb85ecf31c08804ae2850b0f1b0f5d541ef539a1ee06fe821941e4ccd3d2"


def native_archive(root: Path) -> bytes:
    """Recheck retained real signatures and bind every copied regular byte."""
    native = root / "native"
    proof = json.loads(_read_file(native / "evidence.json"))
    assert proof["ref"] == NATIVE_REF and proof["commit"] == NATIVE_COMMIT
    assert (
        hashlib.sha256(
            json.dumps(proof["files"], sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        == NATIVE_INVENTORY
    )
    objects = native / "repo/objects" / NATIVE_COMMIT[:2]
    raw = objects / (NATIVE_COMMIT[2:] + ".commit")
    component = OstreeComponent.model_construct(
        role="app",
        repository_url="https://dl.flathub.org/beta-repo/",
        ref=NATIVE_REF,
        commit=NATIVE_COMMIT,
        collection="org.flathub.Beta",
        metadata_sha256=NATIVE_METADATA,
    )
    # Test-only closed extension identity, not an installable provider component.
    metadata = signed_component_metadata(_read_file(raw), component)
    assert "[ExtensionOf]\nref=app/tv.kodi.Kodi/x86_64/beta\n" in metadata
    gpg = native / ("validation-gpg-" + uuid4().hex)
    gpg.mkdir(mode=0o700, exist_ok=False)
    key = gpg / "flathub.gpg"
    key.write_bytes(trusted_key_bytes())
    for i, sig in enumerate(
        detached_signatures(_read_file(objects / (NATIVE_COMMIT[2:] + ".commitmeta")))
    ):
        path = gpg / (str(i) + ".sig")
        path.write_bytes(sig)
        r = subprocess.run(
            [
                "/usr/bin/gpgv",
                "--homedir",
                str(gpg),
                "--keyring",
                str(key),
                "--status-fd=1",
                str(path),
                str(raw),
            ],
            capture_output=True,
            timeout=30,
            env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": str(gpg)},
        )
        _validate_signature_status(CommandResult(r.returncode, r.stdout + r.stderr))
    buffer = io.BytesIO()
    found = {}
    checkout = native / "checkout"
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for path in sorted((checkout / "files").rglob("*")):
            rel = str(path.relative_to(checkout))
            info = os.lstat(path)
            member = tarfile.TarInfo(
                "screensaver.greynetic/" + str(path.relative_to(checkout / "files"))
            )
            member.mode = stat.S_IMODE(info.st_mode)
            if stat.S_ISDIR(info.st_mode):
                member.type = tarfile.DIRTYPE
                archive.addfile(member)
            elif stat.S_ISLNK(info.st_mode):
                target = os.readlink(path)
                assert "/" not in target and target.startswith(
                    "screensaver.greynetic.so"
                )
                member.type = tarfile.SYMTYPE
                member.linkname = target
                archive.addfile(member)
            else:
                payload = _read_file(path, maximum=1024**2)
                found[rel] = hashlib.sha256(payload).hexdigest()
                assert proof["files"].get(rel) == found[rel]
                member.size = len(payload)
                archive.addfile(member, io.BytesIO(payload))
    assert found == {k: v for k, v in proof["files"].items() if k.startswith("files/")}
    assert found["files/screensaver.greynetic.so.22.0.5"] == NATIVE_LIBRARY
    return buffer.getvalue()


def profile_archive() -> bytes:
    addon = b"""<addon id="script.kodi_lab.phase3" name="Phase 3 isolated probe"
version="1.0.0" provider-name="Kodi Lab">
<requires><import addon="xbmc.python" version="3.0.0"/></requires>
<extension point="xbmc.python.script" library="probe.py"/>
<extension point="xbmc.addon.metadata">
<summary lang="en_GB">Isolated test</summary>
<description lang="en_GB">Isolated test</description>
<platform>all</platform><license>MIT</license></extension></addon>"""
    script = b"""import sys,json,pathlib,xbmc,xbmcvfs
result={"python":sys.version,"prefix":sys.prefix,"python_api":xbmc.__version__,"xbmcbin":xbmcvfs.translatePath("special://xbmcbin/addons")}
pathlib.Path("/home/kodi/.kodi/phase3-python.json").write_text(json.dumps(result))
if len(sys.argv)>1 and sys.argv[1]=="native":xbmc.executebuiltin("ActivateScreensaver")
"""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        for name in ("addons", "addons/script.kodi_lab.phase3"):
            directory = tarfile.TarInfo(name)
            directory.type = tarfile.DIRTYPE
            directory.uid = directory.gid = 1000
            directory.mode = 0o755
            t.addfile(directory)
        for name, raw in [
            ("addons/script.kodi_lab.phase3/addon.xml", addon),
            ("addons/script.kodi_lab.phase3/probe.py", script),
        ]:
            m = tarfile.TarInfo(name)
            m.size = len(raw)
            m.uid = m.gid = 1000
            m.mode = 0o644
            t.addfile(m, io.BytesIO(raw))
    return buf.getvalue()


def run_acceptance(root: Path) -> dict[str, Any]:
    assert (
        root.is_absolute()
        and root.resolve() == root
        and root.name.startswith("klrc1-phase3-")
    )
    assert (
        stat.S_IMODE(root.stat().st_mode) == 0o700
        and root.stat().st_uid == os.geteuid()
    )
    evidence = json.loads(_read_file(root / "assembly-evidence.json"))
    operation = UUID(evidence["operation_id"])
    image_id = evidence["image_id"]
    assert evidence["assembly_assets_digest"] == "sha256:" + ASSETS_SHA256
    assert (
        evidence["production_approval"] is False
        and evidence["installation_status"] == "inactive"
    )
    client = docker.from_env(timeout=30)
    labels = {"io.kodi-lab.experiment": root.name, "io.kodi-lab.phase": "3"}
    native = native_archive(root)
    report: dict[str, Any] = {
        "image_id": image_id,
        "checks": {},
        "screenshots": {},
        "cycles": [],
        "assembly_evidence_sha256": hashlib.sha256(
            (root / "assembly-evidence.json").read_bytes()
        ).hexdigest(),
    }
    created = json.loads((root / "created-resources.json").read_text())
    containers = []
    network = None

    def record(kind: str, obj: Any) -> None:
        created.append({"kind": kind, "id": obj.id, "name": obj.name})
        (root / "created-resources.json").write_text(json.dumps(created, indent=2))

    def execute(container: Any, cmd: list[str], limit: int = 128 * 1024) -> bytes:
        result = container.exec_run(cmd)
        assert result.exit_code == 0, result.output[:1000].decode(errors="replace")
        assert len(result.output) <= limit
        assert isinstance(result.output, bytes)
        return result.output

    def rpc(container: Any, method: str, params: dict[str, Any] | None = None) -> Any:
        q: dict[str, Any] = {"jsonrpc": "2.0", "id": "phase3", "method": method}
        if params is not None:
            q["params"] = params
        script = (
            "import json,urllib.request; q="
            + repr(q)
            + ";print(urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:8080/jsonrpc',data=json.dumps(q).encode(),headers={'Content-Type':'application/json'}),timeout=2).read().decode())"
        )
        response = json.loads(
            execute(container, ["/usr/bin/python3", "-I", "-c", script])
        )
        assert "error" not in response, (method, response)
        return response["result"]

    def readiness(container: Any) -> dict[str, Any]:
        for _ in range(120):
            container.reload()
            assert container.attrs["State"]["Running"], container.logs().decode()[
                -2000:
            ]
            try:
                assert rpc(container, "JSONRPC.Ping") == "pong"
                result = rpc(
                    container,
                    "Application.GetProperties",
                    {"properties": ["name", "version"]},
                )
                assert isinstance(result, dict)
                v = result["version"]
                assert v == {
                    "major": 22,
                    "minor": 0,
                    "revision": "28ea2eac-dirty",
                    "tag": "releasecandidate",
                    "tagversion": "1",
                } or (
                    v["major"] == 22
                    and v["minor"] == 0
                    and v["tag"] == "releasecandidate"
                    and str(v["tagversion"]) == "1"
                )
                identity = KodiApplicationIdentity(
                    result["name"],
                    v["major"],
                    v["minor"],
                    v["tag"],
                    int(v["tagversion"]),
                )
                assert identity.kodi_release == KodiReleaseIdentity.parse("22.0-rc.1")
                return result
            except (AssertionError, KeyError, RuntimeError, ValueError):
                time.sleep(0.5)
        raise RuntimeError("RC1 readiness timeout")

    def screenshot(container: Any, name: str) -> bytes:
        raw = execute(container, list(FIXED_SCREENSHOT_COMMAND), 8 * 1024**2)
        layout = inspect_x11_frame(raw)
        assert (layout.width, layout.height) == (1280, 720)
        png = x11_frame_to_png(raw)
        stats = png_pixel_statistics(png)
        assert stats.has_pixel_variation and stats.rgb_max - stats.rgb_min > 32
        (root / (name + ".png")).write_bytes(png)
        report["screenshots"][name] = {
            "layout": dataclasses.asdict(layout),
            "statistics": dataclasses.asdict(stats),
            "sha256": hashlib.sha256(png).hexdigest(),
        }
        return raw

    def file_from(container: Any, path: str) -> bytes:
        chunks, _ = container.get_archive(path)
        payload = b"".join(chunks)
        assert len(payload) < 2 * 1024**2
        with tarfile.open(fileobj=io.BytesIO(payload)) as t:
            member = t.getmembers()[0]
            stream = t.extractfile(member)
            assert stream is not None
            return stream.read()

    try:
        image = client.api.inspect_image(image_id)
        validate_image_config(
            image,
            operation,
            {
                "io.kodi-lab.recipe-digest": evidence["recipe_digest"],
                "io.kodi-lab.acquisition-digest": evidence["acquisition_digest"],
                "io.kodi-lab.assets-digest": evidence["assembly_assets_digest"],
                "io.kodi-lab.support-digest": evidence["supporting_closure_digest"],
            },
        )
        network = client.networks.create(
            root.name + "-runtime-" + uuid4().hex[:8], internal=True, labels=labels
        )
        record("network", network)
        for index in range(2):
            container = client.containers.create(
                image_id,
                name=root.name + "-runtime-" + str(index),
                user="1000:1000",
                network=network.name,
                cap_drop=["ALL"],
                security_opt=["no-new-privileges:true"],
                mem_limit="1g",
                pids_limit=128,
                labels=labels,
                restart_policy={"Name": "no"},
            )
            containers.append(container)
            record("container", container)
            if index == 0:
                assert container.put_archive("/app/lib/kodi/addons", native)
                assert container.put_archive("/home/kodi/.kodi", profile_archive())
            for cycle in range(2 if index == 0 else 1):
                container.start()
                report["identity"] = readiness(container)
                container.reload()
                attrs = container.attrs
                host = attrs["HostConfig"]
                assert (
                    attrs["Config"]["User"] == "1000:1000"
                    and not host["Privileged"]
                    and not host["Devices"]
                    and not attrs["Mounts"]
                    and not host["Binds"]
                    and not host["PortBindings"]
                    and not host["CapAdd"]
                    and host["CapDrop"] == ["ALL"]
                )
                assert (
                    len(attrs["NetworkSettings"]["Networks"]) == 1
                    and network.name in attrs["NetworkSettings"]["Networks"]
                )
                status = execute(
                    container,
                    [
                        "/usr/bin/python3",
                        "-I",
                        "-c",
                        "print(''.join(s for s in open('/proc/1/status') "
                        "if s.startswith(('Uid:','Gid:','CapEff:','CapBnd:',"
                        "'Seccomp:','NoNewPrivs:'))))",
                    ],
                ).decode()
                assert (
                    "CapEff:\t0000000000000000" in status
                    and "CapBnd:\t0000000000000000" in status
                    and "Seccomp:\t2" in status
                    and "NoNewPrivs:\t1" in status
                )
                report["security"] = status
                execute(
                    container,
                    [
                        "/usr/bin/python3",
                        "-I",
                        "/usr/local/lib/kodi-lab/healthcheck.py",
                    ],
                )
                bus_probe = """import pathlib,json,os
bus=[];addresses=[]
for p in pathlib.Path('/proc').iterdir():
 if not p.name.isdecimal():continue
 try:
  argv=(p/'cmdline').read_bytes().split(b'\\0')
  if b'/app/lib/kodi/kodi.bin' in argv:
   parts=(p/'environ').read_bytes().split(b'\\0')
   env=dict(x.split(b'=',1) for x in parts if b'=' in x)
   addresses.append(env[b'DBUS_SESSION_BUS_ADDRESS'].decode())
  if argv and argv[0].endswith(b'dbus-daemon'):
   assert b'--session' in argv and p.stat().st_uid==1000
   bus.append(int(p.name))
 except FileNotFoundError:pass
assert bus and addresses
assert all(a.startswith(('unix:path=/tmp/','unix:abstract=')) for a in addresses)
print(json.dumps({'session_bus_pids':bus,'private_addresses':addresses}))
"""
                report["session_dbus"] = json.loads(
                    execute(container, ["/usr/bin/python3", "-I", "-c", bus_probe])
                )
                stats = container.stats(stream=False)
                report.setdefault("resource_samples", []).append(
                    {
                        "container": index,
                        "cycle": cycle,
                        "memory_usage": stats["memory_stats"].get("usage"),
                        "memory_limit": stats["memory_stats"].get("limit"),
                        "pids_current": stats.get("pids_stats", {}).get("current"),
                    }
                )
                if index == 0 and cycle == 0:
                    report["executable_version"] = execute(
                        container, ["/usr/local/bin/kodi-lab-launcher", "--version"]
                    ).decode()
                    assert (
                        "22.0-RC1" in report["executable_version"]
                        and "28ea2eac" in report["executable_version"]
                    )
                    assert (
                        execute(
                            container,
                            ["readlink", "/opt/fdo/lib/x86_64-linux-gnu/addons"],
                        ).strip()
                        == b"/app/lib/kodi/addons"
                    )
                    report["xrandr"] = execute(
                        container, ["/opt/fdo/lib/x86_64-linux-gnu/kodi-xrandr"]
                    ).decode()
                    for method in (
                        "Input.Home",
                        "Input.Down",
                        "Input.Up",
                        "Input.Back",
                    ):
                        assert rpc(container, method) == "OK"
                    time.sleep(1)
                    first = screenshot(container, "home")
                    assert (
                        rpc(container, "GUI.ActivateWindow", {"window": "settings"})
                        == "OK"
                    )
                    time.sleep(1)
                    second = screenshot(container, "settings")
                    report["changed_pixels"] = sum(
                        first[i : i + 4] != second[i : i + 4]
                        for i in range(56, len(first), 4)
                    )
                    assert report["changed_pixels"] > 1280 * 720 * 0.01
                    for addon in ("script.kodi_lab.phase3", "screensaver.greynetic"):
                        details = rpc(
                            container,
                            "Addons.GetAddonDetails",
                            {"addonid": addon, "properties": ["enabled", "version"]},
                        )
                        if not details["addon"]["enabled"]:
                            assert (
                                rpc(
                                    container,
                                    "Addons.SetAddonEnabled",
                                    {"addonid": addon, "enabled": True},
                                )
                                == "OK"
                            )
                    assert (
                        rpc(
                            container,
                            "Addons.ExecuteAddon",
                            {"addonid": "script.kodi_lab.phase3", "wait": True},
                        )
                        == "OK"
                    )
                    report["python"] = json.loads(
                        execute(
                            container, ["cat", "/home/kodi/.kodi/phase3-python.json"]
                        )
                    )
                    assert (
                        "3.14.7" in report["python"]["python"]
                        and report["python"]["python_api"] == "3.1.0"
                        and report["python"]["prefix"] == "/opt/fdo"
                    )
                    assert (
                        rpc(
                            container,
                            "Settings.SetSettingValue",
                            {
                                "setting": "screensaver.mode",
                                "value": "screensaver.greynetic",
                            },
                        )
                        is True
                    )
                    # Script argument shape differs across Kodi APIs; explicitly
                    # execute the fixed builtin through the test script below.
                    activate = (
                        b'import xbmc\nxbmc.executebuiltin("ActivateScreensaver")\n'
                    )
                    buf = io.BytesIO()
                    with tarfile.open(fileobj=buf, mode="w") as t:
                        m = tarfile.TarInfo("probe.py")
                        m.uid = m.gid = 1000
                        m.mode = 0o644
                        m.size = len(activate)
                        t.addfile(m, io.BytesIO(activate))
                    assert container.put_archive(
                        "/home/kodi/.kodi/addons/script.kodi_lab.phase3", buf.getvalue()
                    )
                    assert (
                        rpc(
                            container,
                            "Addons.ExecuteAddon",
                            {"addonid": "script.kodi_lab.phase3", "wait": True},
                        )
                        == "OK"
                    )
                    time.sleep(2)
                    maps = execute(
                        container,
                        [
                            "/usr/bin/python3",
                            "-I",
                            "-c",
                            "import pathlib; print(''.join(p.read_text() "
                            "for p in pathlib.Path('/proc').glob('[0-9]*/maps') "
                            "if '/app/lib/kodi/kodi.bin' in p.read_text()))",
                        ],
                        2 * 1024**2,
                    ).decode()
                    (root / "loaded-maps.txt").write_text(maps)
                    assert "screensaver.greynetic.so.22.0.5" in maps
                    assert (
                        "/opt/mesa/lib/libEGL_mesa.so" in maps
                        and "/opt/fdo/lib/x86_64-linux-gnu/libtag.so.2.3.1" in maps
                    )
                    native_frame = screenshot(container, "greynetic")
                    active = rpc(
                        container,
                        "XBMC.GetInfoBooleans",
                        {"booleans": ["System.ScreenSaverActive"]},
                    )
                    assert active["System.ScreenSaverActive"] is True
                    assert native_frame != second
                    report["native_active_state"] = active
                    report["taglib_sha256"] = (
                        execute(
                            container,
                            [
                                "sha256sum",
                                "/opt/fdo/lib/x86_64-linux-gnu/libtag.so.2.3.1",
                            ],
                        )
                        .decode()
                        .split()[0]
                    )
                    assert report["taglib_sha256"] == (
                        "2f65f918dc718e5e1190f7dc9a1efb2b0aeefea42a31cfcce8cc2373b5b851ee"
                    )
                    report["acquisition_tools_absent"] = (
                        execute(
                            container,
                            [
                                "/usr/bin/python3",
                                "-I",
                                "-c",
                                "import shutil; assert all(shutil.which(x) is None "
                                "for x in ('apt','apt-get','gpgv','gpg',"
                                "'ostree','curl',"
                                "'gcc','g++','make','flatpak')); print('absent')",
                            ],
                        )
                        .decode()
                        .strip()
                        == "absent"
                    )
                    assert rpc(container, "Input.Back") == "OK"
                    report["native_addon"] = {
                        "id": "screensaver.greynetic",
                        "version": "22.0.5",
                        "commit": NATIVE_COMMIT,
                        "library_sha256": NATIVE_LIBRARY,
                        "mapped": True,
                        "activated": True,
                    }
                    execute(
                        container,
                        [
                            "/usr/bin/python3",
                            "-I",
                            "-c",
                            "from pathlib import Path; "
                            "Path('/home/kodi/.kodi/phase3-marker')"
                            ".write_text('profile-A')",
                        ],
                    )
                elif index == 0:
                    assert (
                        execute(
                            container, ["cat", "/home/kodi/.kodi/phase3-marker"]
                        ).strip()
                        == b"profile-A"
                    )
                    screenshot(container, "restart")
                else:
                    assert (
                        execute(
                            container,
                            [
                                "/usr/bin/python3",
                                "-I",
                                "-c",
                                "from pathlib import Path; assert not "
                                "Path('/home/kodi/.kodi/phase3-marker').exists();"
                                "print('isolated')",
                            ],
                        ).strip()
                        == b"isolated"
                    )
                started = time.monotonic()
                container.stop(timeout=10)
                container.reload()
                assert (
                    container.attrs["State"]["ExitCode"] == 0
                    and not container.attrs["State"]["OOMKilled"]
                )
                cycles = [
                    json.loads(line)
                    for line in file_from(
                        container, "/home/kodi/.kodi/supervisor-cycles.jsonl"
                    )
                    .decode()
                    .splitlines()
                ]
                assert (
                    len(cycles) == cycle + 1
                    and not cycles[-1]["forced_kill"]
                    and cycles[-1]["kodi_exit_code"] == 0
                )
                assert cycles[-1]["shutdown_seconds"] < 8
                report["cycles"].append(
                    {
                        "container": index,
                        "cycle": cycle,
                        "seconds": round(time.monotonic() - started, 3),
                        "record": cycles[-1],
                    }
                )
                for path in ("/tmp/.X99-lock", "/tmp/.X11-unix/X99"):
                    try:
                        container.get_archive(path)
                    except docker.errors.NotFound:
                        pass
                    else:
                        raise AssertionError("stale display artifact " + path)
            log = file_from(container, "/home/kodi/.kodi/temp/kodi.log").decode(
                errors="replace"
            )
            (root / ("container-" + str(index) + "-kodi.log")).write_text(log)
            if index == 0:
                assert "llvmpipe" in log and "26.2.2" in log
                report["renderer"] = [
                    s
                    for s in log.splitlines()
                    if any(
                        x in s
                        for x in (
                            "GL_RENDERER",
                            "GL_VERSION",
                            "FFmpeg",
                            "TagLib",
                            "Python interpreter",
                        )
                    )
                ]
                assert "9.0.2" in log
        report["checks"] = {
            k: True
            for k in (
                "exact_rc1",
                "canonical_identity",
                "upstream_revision",
                "python_addon",
                "native_addon_discovery",
                "native_addon_execution",
                "x11_xvfb",
                "llvmpipe",
                "nonblank_1280x720",
                "navigation_changed_frame",
                "two_clean_cycles",
                "no_forced_termination",
                "no_stale_display",
                "profile_persistence",
                "profile_isolation",
                "nonroot_capabilities_seccomp",
                "no_mounts_devices_ports",
                "local_session_dbus",
            )
        }
        report["status"] = "runtime-acceptance-passed-production-integration-disabled"
        return report
    finally:
        report["cleanup"] = []
        for container in reversed(containers):
            container.reload()
            if container.attrs["State"]["Running"]:
                container.stop(timeout=10)
            report["cleanup"].append({"kind": "container", "id": container.id})
            container.remove(force=False)
        if network is not None:
            report["cleanup"].append({"kind": "network", "id": network.id})
            network.remove()
        (root / "runtime-acceptance.json").write_text(
            json.dumps(report, sort_keys=True, indent=2) + "\n"
        )
        client.close()
