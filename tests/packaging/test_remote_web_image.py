"""Opt-in acceptance for the built remote-Web image's actual default launcher.

Run after building target web, with KODI_LAB_TEST_REMOTE_WEB_IMAGE set to its
tag or image ID. Ordinary regression runs do not start Docker containers.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import docker  # type: ignore[import-untyped]
import pytest

IMAGE = os.environ.get("KODI_LAB_TEST_REMOTE_WEB_IMAGE")
pytestmark = pytest.mark.skipif(not IMAGE, reason="requires a built remote-Web image")


def test_built_image_executes_default_lf_launcher_and_serves_login() -> None:
    client = docker.from_env()
    volume = None
    container = None
    try:
        image = client.images.get(IMAGE)
        launcher = "/usr/local/bin/kodi-lab-remote-web"
        assert image.attrs["Config"]["Entrypoint"] == [launcher]
        inspected = client.containers.run(
            image.id,
            [
                "-c",
                "import os, pathlib; "
                f"p = pathlib.Path({launcher!r}); "
                "assert p.is_file(); "
                "assert p.stat().st_mode & 0o777 == 0o755; "
                "assert os.access(p, os.X_OK); "
                "assert p.read_bytes().startswith(b'#!/bin/sh\\n'); "
                "assert b'\\r' not in p.read_bytes(); "
                "assert pathlib.Path('/bin/sh').exists(); "
                "print(p.read_bytes().decode(), end='')",
            ],
            entrypoint="python",
            network_mode="none",
            remove=True,
        )
        assert (
            inspected
            == (Path(__file__).parents[2] / "runtime/remote-web/launch.sh").read_bytes()
        )

        volume = client.volumes.create(labels={"io.kodi-lab.test": "lf-launcher"})
        volumes = {volume.name: {"bind": "/var/lib/kodi-lab", "mode": "rw"}}
        environment = {"KODI_LAB_WEB_ALLOWED_HOSTS": '["127.0.0.1"]'}
        client.containers.run(
            image.id,
            ["initialize"],
            entrypoint="kodi-lab-admin",
            volumes=volumes,
            network_mode="none",
            environment=environment,
            remove=True,
        )
        # No entrypoint/command override: Docker must execute the shebang itself.
        container = client.containers.run(
            image.id,
            detach=True,
            volumes=volumes,
            network_mode="none",
            read_only=True,
            tmpfs={"/tmp": "rw,noexec,nosuid,size=64m"},
            cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"],
            environment=environment,
        )
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            container.reload()
            assert container.status == "running", container.logs().decode()
            response = container.exec_run(
                [
                    "python",
                    "-c",
                    "import urllib.request; "
                    "r = urllib.request.urlopen('http://127.0.0.1:8100/login', "
                    "timeout=2); assert r.status == 200",
                ]
            )
            if response.exit_code == 0:
                assert container.attrs["State"]["ExitCode"] == 0
                return
            time.sleep(0.25)
        pytest.fail(f"default launcher did not serve login: {container.logs()!r}")
    finally:
        if container is not None:
            container.remove(force=True)
        if volume is not None:
            volume.remove()
        client.close()
