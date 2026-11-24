"""Bounded native acceptance using the SDK client over authenticated loopback MCP."""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import shutil
import socket
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from contextlib import asynccontextmanager
from pathlib import Path
from zipfile import ZipFile

import httpx2
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client

from .controller import Controller
from .packages import sha
from .runtime import BRIDGE, IMAGE, atomic, docker, wait_until

ROOT_ADDON = "script.labnative.probe"
DEPS = ["script.module.labnative." + tail for tail in ("a", "b", "leaf")]
DECOY = "script.labnative.decoy"
MISSING = "script.labnative.missing"
SKIN = "skin.kodiv2fixture"


def zip_to(source, destination):
    with ZipFile(source) as archive:
        # Operator intake validated the fixture before this workspace copy.
        for info in archive.infolist():
            if info.is_dir():
                continue
            _, rel = info.filename.split("/", maxsplit=1)
            file = destination / rel
            file.parent.mkdir(exist_ok=True, parents=True)
            file.write_bytes(archive.read(info))


def rebuild(source, target, marker):
    with ZipFile(source) as old, ZipFile(target, "w") as new:
        for item in old.infolist():
            value = old.read(item)
            if item.filename.endswith("/default.py"):
                value = value.replace(b"target-1.1.0", marker.encode())
            new.writestr(item, value)
    return target.read_bytes()


@asynccontextmanager
async def session(url, credential):
    async with (
        httpx2.AsyncClient(
            headers={"Authorization": "Bearer " + credential},
            timeout=75,
            trust_env=False,
        ) as client,
        Client(
            streamable_http_client(url, http_client=client, terminate_on_close=False),
            mode="auto",
            cache=None,
        ) as connection,
    ):
        yield connection


def skin_candidate(runtime, root, version):
    working = root / "workspaces" / SKIN
    if not working.exists():
        obj = runtime.inspect()
        stock = obj["Id"] + ":/usr/share/kodi/addons/skin.estuary"
        docker("cp", stock, str(working))
        # Use the original reviewed font to make this private fixture self-contained.
        fonts = working / "fonts"
        regular = fonts / "NotoSans-Regular.ttf"
        if regular.is_symlink():
            regular.unlink()
            docker("cp", "-L", stock + "/fonts/NotoSans-Regular.ttf", str(regular))
        for font in fonts.glob("*.ttf"):
            if font.is_symlink():
                font.unlink()
                shutil.copyfile(regular, font)
    path = working / "addon.xml"
    xml = ET.parse(path)
    xml.getroot().attrib.update(
        id=SKIN, name="Omega Switch Fixture " + version, version=version
    )
    xml.write(path, encoding="utf-8")
    return Controller(root).packages.workspace_package(SKIN)


async def native_run(root, fixtures, no_skin=False, skin_only=False):
    controller = Controller(root)
    runtime = controller.runtime
    evidence = root / "evidence"
    evidence.mkdir()
    records = []
    runtime_ids = []
    preexisting = docker("ps", "-q", "--no-trunc").splitlines()
    context = {
        "runtime_image": IMAGE,
        "target": str(runtime.target),
        "disk_free_before": __import__("shutil").disk_usage(root).free,
        "noncampaign_running_count_before": len(preexisting),
    }
    atomic(evidence / "context.json", json.dumps(context, indent=2).encode())
    p = controller.packages
    dep_pkgs = [
        p.approve((fixtures / f"{name}-1.0.0.zip").read_bytes()) for name in DEPS
    ]
    decoy1 = p.approve((fixtures / f"{DECOY}-1.0.0.zip").read_bytes())
    decoy2 = p.approve((fixtures / f"{DECOY}-1.1.0.zip").read_bytes())
    missing = p.approve((fixtures / f"{MISSING}-1.0.0.zip").read_bytes())
    update = (fixtures / f"{ROOT_ADDON}-1.1.0.zip").read_bytes()
    replacement = rebuild(
        fixtures / f"{ROOT_ADDON}-1.1.0.zip", root / "replacement.zip", "same-version"
    )
    a = p.approve((fixtures / f"{ROOT_ADDON}-1.0.0.zip").read_bytes())
    zip_to(fixtures / f"{ROOT_ADDON}-1.0.0.zip", p.workspaces / ROOT_ADDON)
    atomic(p.catalog_file, json.dumps([x.digest for x in dep_pkgs]).encode())
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    url = f"http://127.0.0.1:{port}/mcp"
    with (evidence / "server.log").open("wb") as serverlog:
        server = subprocess.Popen(
            [
                sys.executable,
                "-B",
                "-m",
                "experiments.managed_v2.server",
                str(root),
                "--port",
                str(port),
            ],
            stdout=serverlog,
            stderr=serverlog,
        )
        try:
            await asyncio.to_thread(
                wait_until,
                lambda: (root / "observer.token").exists(),
                10,
                "mcp_server_wait",
            )
            await asyncio.sleep(0.25)
            async with session(url, (root / "owner.token").read_text()) as owner:

                async def call(tool, args=None, *, required=True, client=owner):
                    started = time.monotonic()
                    reply = await client.call_tool(tool, arguments=args or {})
                    body = json.loads(reply.content[0].text)
                    row = {
                        "tool": tool,
                        "seconds": round(time.monotonic() - started, 3),
                        "error": bool(reply.is_error),
                        "body": body,
                    }
                    records.append(row)
                    atomic(
                        evidence / "calls.json", json.dumps(records, indent=2).encode()
                    )
                    print(
                        tool,
                        row["seconds"],
                        "error" if reply.is_error else "ok",
                        body.get("error")
                        or (
                            body.get("result", {}).get("native")
                            if isinstance(body.get("result"), dict)
                            else ""
                        ),
                        flush=True,
                    )
                    if required == bool(reply.is_error):
                        raise AssertionError(row)
                    return body.get("result", body)

                async def launch(marker):
                    log = runtime.profile() / "temp/kodi.log"
                    previous = log.stat().st_size
                    await call("addon_launch", {"addon": ROOT_ADDON})

                    def seen():
                        with log.open("rb") as stream:
                            stream.seek(previous)
                            return next(
                                (
                                    line
                                    for line in stream.read()
                                    .decode(errors="replace")
                                    .splitlines()
                                    if "LABNATIVE_EXEC " + marker in line
                                ),
                                "",
                            )

                    observed = await asyncio.to_thread(
                        wait_until, seen, 9, "fresh_native_marker"
                    )
                    with (evidence / "execution.txt").open("a") as stream:
                        stream.write(observed + "\n")

                async def install(pkg, required=True):
                    return await call(
                        "addon_install",
                        {"addon": pkg.id, "version": pkg.version, "digest": pkg.digest},
                        required=required,
                    )

                async def check_skin(replacement):
                    skin = skin_candidate(runtime, root, "1.0.0")
                    await install(skin)
                    selected = await call("managed_skin", {"addon": SKIN})
                    assert selected.get("skin") == SKIN, selected
                    program = await install(replacement)
                    assert (
                        program["native"]["prior_skin"]
                        == program["native"]["final_skin"]
                        == SKIN
                    )
                    updated_skin = skin_candidate(runtime, root, "1.1.0")
                    native = await install(updated_skin)
                    assert (
                        native["native"]["prior_skin"]
                        == native["native"]["final_skin"]
                        == SKIN
                    )
                    await launch(
                        "same-version 1.1.0 before=snapshot-sentinel deps=a+leaf,b"
                    )

                if skin_only:
                    await call("managed_prepare")
                    await call("managed_start")
                    await install(a)
                    await call(
                        "addon_setting",
                        {
                            "addon": ROOT_ADDON,
                            "key": "marker",
                            "value": "snapshot-sentinel",
                        },
                    )
                    await check_skin(p.approve(replacement))
                    await launch(
                        "same-version 1.1.0 before=snapshot-sentinel deps=a+leaf,b"
                    )
                    await call("managed_stop")
                    await call("snapshot_save", {"name": "Delete control"})
                    await call("snapshot_delete", {"name": "Delete control"})
                    assert await call("snapshot_list") == []
                    return

                available = await owner.list_tools()
                atomic(
                    evidence / "tools.json",
                    json.dumps([t.name for t in available.tools]).encode(),
                )
                async with httpx2.AsyncClient(trust_env=False) as noauth:
                    assert (await noauth.post(url, json={})).status_code == 401
                async with session(
                    url, (root / "observer.token").read_text()
                ) as observer:
                    denied = await call(
                        "addon_install",
                        {"addon": a.id, "version": a.version, "digest": a.digest},
                        required=False,
                        client=observer,
                    )
                    assert denied["error"] == "managed_write_denied"

                await call("managed_prepare")
                await call("managed_start")
                runtime_ids.append(runtime.inspect()["Id"])
                credential = json.loads(
                    (runtime.credentials / "state.json").read_text()
                )
                status = await call("managed_status")
                assert (
                    status["state"] == "ready"
                    and status["bridge"]["addon_id"] == BRIDGE
                )
                absent = {x["addonid"] for x in await call("addon_inventory")}
                assert not absent & {ROOT_ADDON, *DEPS}
                wrong = await call(
                    "addon_install",
                    {"addon": a.id, "version": "9.0.0", "digest": a.digest},
                    required=False,
                )
                assert wrong["error"] == "package_identity_mismatch"
                with runtime.exclusive():
                    busy = await call("managed_reset", required=False)
                    assert busy["error"] == "managed_busy"

                pkg = await call("workspace_package", {"addon": ROOT_ADDON})
                fresh = await call("addon_install", pkg)
                assert {d["addon"] for d in fresh["dependencies"]} == set(DEPS)
                await call(
                    "addon_setting",
                    {
                        "addon": ROOT_ADDON,
                        "key": "marker",
                        "value": "snapshot-sentinel",
                    },
                )
                await launch(
                    "target-1.0.0 1.0.0 before=snapshot-sentinel deps=a+leaf,b"
                )
                await call("managed_stop")
                saved = await call("snapshot_save", {"name": "Cold baseline"})
                assert saved["bytes"] > 0
                assert (await call("snapshot_list"))[0]["name"] == "Cold baseline"

                await call("managed_start")
                runtime_ids.append(runtime.inspect()["Id"])
                assert runtime_ids[-1] != runtime_ids[-2]
                try:
                    runtime.signed("bridge_identity", credential=credential)
                except Exception:
                    atomic(
                        evidence / "old-authority.txt",
                        b"Old Bridge credentials rejected by the recreated runtime.\n",
                    )
                else:
                    raise AssertionError("old authority accepted")

                await install(decoy1)
                atomic(
                    p.catalog_file,
                    json.dumps([x.digest for x in (*dep_pkgs, decoy2)]).encode(),
                )
                uploaded = await call(
                    "package_import",
                    {
                        "encoded": base64.b64encode(update).decode(),
                        "digest": sha(update),
                    },
                )
                await call("addon_install", uploaded)
                await launch(
                    "target-1.1.0 1.1.0 before=snapshot-sentinel deps=a+leaf,b"
                )
                cached = (
                    runtime.profile() / "addons/packages" / (ROOT_ADDON + "-1.1.0.zip")
                )
                assert cached.is_file() and sha(cached.read_bytes()) == sha(update)
                replace = p.approve(replacement)
                await install(replace)
                assert sha(cached.read_bytes()) == replace.digest
                await launch(
                    "same-version 1.1.0 before=snapshot-sentinel deps=a+leaf,b"
                )
                assert (
                    next(
                        a
                        for a in await call("addon_inventory")
                        if a["addonid"] == DECOY
                    )["version"]
                    == "1.0.0"
                )

                failure = await install(missing, required=False)
                assert failure["native"]["error"] == "missing_dependency", failure
                assert all(
                    a["addonid"] != MISSING for a in await call("addon_inventory")
                )
                runtime.rpc("GUI.ActivateWindow", window="shutdownmenu")
                await asyncio.to_thread(
                    wait_until,
                    lambda: (
                        runtime.rpc("GUI.GetProperties", properties=["currentwindow"])[
                            "currentwindow"
                        ]["id"]
                        not in {10000, 10040}
                    ),
                    5,
                    "native_negative_dialog",
                )
                failure = await install(replace, required=False)
                assert failure["native"]["error"] == "unexpected_window_before_install"
                runtime.rpc("Input.Back")

                await call("managed_stop")
                src = p.workspaces / ROOT_ADDON
                zip_to(root / "replacement.zip", src)
                main = src / "default.py"
                main.write_bytes(
                    main.read_bytes().replace(b"same-version", b"direct-deploy")
                )
                await call("workspace_deploy", {"addon": ROOT_ADDON})
                await call("managed_start")
                runtime_ids.append(runtime.inspect()["Id"])
                await launch(
                    "direct-deploy 1.1.0 before=snapshot-sentinel deps=a+leaf,b"
                )
                await call("managed_stop")
                await call("managed_start")
                runtime_ids.append(runtime.inspect()["Id"])
                await launch(
                    "direct-deploy 1.1.0 before=snapshot-sentinel deps=a+leaf,b"
                )
                await call("managed_stop")
                await call("managed_reset")
                await call("managed_start")
                after_reset = {x["addonid"] for x in await call("addon_inventory")}
                assert not after_reset & {ROOT_ADDON, *DEPS}
                await call("managed_stop")
                await call("snapshot_load", {"name": "Cold baseline"})
                await call("managed_start")
                runtime_ids.append(runtime.inspect()["Id"])
                after_restore = {
                    x["addonid"]: (x["version"], x["enabled"])
                    for x in await call("addon_inventory")
                }
                assert all(
                    after_restore[name] == ("1.0.0", True)
                    for name in [ROOT_ADDON, *DEPS]
                )
                await launch(
                    "target-1.0.0 1.0.0 before=snapshot-sentinel deps=a+leaf,b"
                )
                if not no_skin:
                    await check_skin(replace)
                await call("managed_stop")
                await call("snapshot_save", {"name": "Delete control"})
                await call("snapshot_delete", {"name": "Delete control"})
                assert len(await call("snapshot_list")) == 1
                atomic(
                    evidence / "runtime-ids.json",
                    json.dumps(runtime_ids, indent=2).encode(),
                )
        finally:
            server.terminate()
            server.wait(timeout=12)
            if runtime.pointer.exists():
                path = runtime.profile() / "temp/kodi.log"
                if path.exists():
                    keep = "\n".join(
                        line
                        for line in path.read_text(errors="replace").splitlines()
                        if any(
                            k in line
                            for k in (
                                "LABNATIVE",
                                "kodi-lab-v2",
                                "CAddonInstaller",
                                "CAddonInstallJob",
                                "DialogConfirm",
                                "skin.kodiv2fixture",
                                "Narrow Bridge",
                                "service.kodi_lab_bridge",
                                "Window Init",
                                "CRepositoryUpdateJob",
                                "ERROR",
                            )
                        )
                    )
                    atomic(evidence / "native.log", keep.encode())
                db_dir = runtime.profile() / "userdata/Database"
                import sqlite3

                dbs = list(db_dir.glob("Addons*.db"))
                if dbs:
                    with sqlite3.connect(dbs[0].as_uri() + "?mode=ro", uri=True) as db:
                        rows = db.execute(
                            "SELECT addonID,checksum FROM repo"
                        ).fetchall()
                        atomic(
                            evidence / "native-catalog.json", json.dumps(rows).encode()
                        )
            runtime.cleanup_trial()
            final = docker("ps", "-q", "--no-trunc").splitlines()
            labels = f"label=io.kodi-lab.managed-v2={runtime.target}"
            cleanup = {
                "preexisting_running_id_set_equal": sorted(preexisting)
                == sorted(final),
                "owned_containers": docker(
                    "ps", "-aq", "--filter", labels
                ).splitlines(),
                "owned_networks": docker(
                    "network", "ls", "-q", "--filter", labels
                ).splitlines(),
                "owned_volumes": docker(
                    "volume", "ls", "-q", "--filter", labels
                ).splitlines(),
            }
            atomic(evidence / "cleanup.json", json.dumps(cleanup, indent=2).encode())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("state", type=Path)
    parser.add_argument("fixtures", type=Path)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--no-skin", action="store_true")
    group.add_argument("--skin-only", action="store_true")
    args = parser.parse_args()
    asyncio.run(
        native_run(
            args.state.resolve(), args.fixtures.resolve(), args.no_skin, args.skin_only
        )
    )


if __name__ == "__main__":
    main()
