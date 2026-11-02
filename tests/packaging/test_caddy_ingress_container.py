"""Explicit isolated Docker fixture; never mounts application state or host sockets."""

from __future__ import annotations

import ipaddress
import json
import os
import ssl
import subprocess
import time
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from websockets.sync.client import connect

from scripts import install

# A separately selected existing application image supplies Python/Uvicorn only.
# This fixture never starts Kodi Lab or loads a retained database.
pytestmark = pytest.mark.skipif(
    not os.environ.get("KODI_LAB_INGRESS_TEST_IMAGE"),
    reason="explicit isolated Docker ingress fixture requires a local image ID",
)
BACKEND = """
import json, uvicorn
async def app(scope, receive, send):
    data = json.dumps({k: scope[k] for k in ('scheme', 'client', 'headers')},
                      default=lambda v: v.decode() if isinstance(v, bytes) else v)
    if scope['type'] == 'websocket':
        await receive()
        await send({'type':'websocket.accept'})
        await send({'type':'websocket.send','text':data})
        await send({'type':'websocket.close'})
    elif scope['type'] == 'http':
        await send({'type':'http.response.start','status':200,'headers':[]})
        await send({'type':'http.response.body','body':data.encode()})
uvicorn.run(app, host='0.0.0.0', port=8100, lifespan='off')
"""


def test_pinned_caddy_tls_sanitization_websockets_and_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    image = os.environ["KODI_LAB_INGRESS_TEST_IMAGE"]
    assert image.startswith("sha256:") and len(image) == 71
    project = "issue8-test-" + uuid4().hex[:12]

    def docker(*args: str) -> str:
        return subprocess.check_output(["docker", *args], text=True).strip()

    ids = docker("network", "ls", "-q").split()
    networks = json.loads(docker("network", "inspect", *ids)) if ids else []
    claimed = [
        ipaddress.ip_network(entry["Subnet"])
        for network in networks
        for entry in (network.get("IPAM") or {}).get("Config") or []
        if entry.get("Subnet")
    ]
    routes = json.loads(
        subprocess.check_output(
            ["ip", "-j", "-4", "route", "show", "table", "all"], text=True
        )
    )
    claimed += [
        ipaddress.ip_network(item["dst"], strict=False)
        for item in routes
        if item.get("dst") not in {None, "default"}
    ]
    subnet = next(
        net
        for net in ipaddress.ip_network("10.246.241.0/24").subnets(new_prefix=29)
        if not any(net.overlaps(other) for other in claimed)
    )
    config = install.ingress_config(str(subnet), 18443, ["127.0.0.0/8"])
    # Docker assigns unused loopback ports in this test only.
    overlay = install.windows_overlay(18121, config).replace(
        "127.0.0.1:18443:8443/tcp", "127.0.0.1::8443/tcp"
    )
    (tmp_path / "bundle").mkdir()
    (tmp_path / "bundle/.env").write_text("")
    (tmp_path / "compose.windows.yaml").write_text(overlay)
    (tmp_path / "Caddyfile").write_text(install.caddyfile(config))
    (tmp_path / "bundle/compose.release.yaml").write_text(
        json.dumps(
            {
                "services": {
                    "web": {
                        "image": image,
                        "entrypoint": ["python", "-c", BACKEND],
                        "read_only": True,
                        "cap_drop": ["ALL"],
                        "security_opt": ["no-new-privileges:true"],
                        "ports": ["127.0.0.1::8100"],
                    },
                    "repository": {"image": image},
                }
            }
        )
    )
    command = install.compose(tmp_path, {"project": project, "host": "windows-wsl2"})

    def compose(*args: str) -> str:
        return subprocess.check_output(
            [*command, *args], cwd=tmp_path, text=True, env=install.environment()
        ).strip()

    containers: list[str] = []
    volumes: list[str] = []
    network_ids: list[str] = []
    try:
        compose("up", "-d", "--wait", "--no-build", "web", "caddy")
        containers = docker(
            "ps", "-aq", "--filter", "label=com.docker.compose.project=" + project
        ).split()
        caddy = compose("ps", "--all", "-q", "caddy")
        inspect = json.loads(docker("inspect", caddy))[0]
        assert inspect["State"]["Running"], docker("logs", caddy)
        assert inspect["Config"]["User"] == "10003:10003"
        assert inspect["HostConfig"]["CapDrop"] == ["ALL"]
        assert set(inspect["NetworkSettings"]["Networks"]) == {
            project + "-https-ingress"
        }
        assert {item["Destination"] for item in inspect["Mounts"]} == {
            "/data",
            "/config",
            "/etc/caddy/Caddyfile",
        }
        root_certificate = tmp_path / "root.crt"
        for _ in range(100):
            result = subprocess.run(
                [
                    "docker",
                    "cp",
                    caddy + ":/data/caddy/pki/authorities/local/root.crt",
                    str(root_certificate),
                ],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                break
            time.sleep(0.1)
        else:
            pytest.fail(docker("logs", caddy))
        original_run = install.run

        def with_windows_routes(
            command: list[str], cwd: Path, *, capture: bool = False
        ) -> str:
            if command[0] == "powershell.exe":
                return '["127.0.0.0/8"]'
            return original_run(command, cwd, capture=capture)

        monkeypatch.setattr(install, "run", with_windows_routes)
        install.check_ingress_network(
            tmp_path, {"project": project, "https_ingress": config}
        )
        # A separate container cannot allocate the occupied trusted IP using
        # normal Docker controls. All of its resources belong to this fixture.
        outsider = docker(
            "create",
            "--network",
            project + "_default",
            "--label",
            "com.docker.compose.project=" + project,
            "--read-only",
            "--cap-drop",
            "ALL",
            "--entrypoint",
            "sleep",
            image,
            "60",
        )
        docker("start", outsider)
        claim = subprocess.run(
            [
                "docker",
                "network",
                "connect",
                "--ip",
                config["caddy_ip"],
                project + "-https-ingress",
                outsider,
            ],
            capture_output=True,
            text=True,
        )
        assert claim.returncode != 0 and "already in use" in claim.stderr.lower()
        original_root = root_certificate.read_bytes()
        tls_port = inspect["NetworkSettings"]["Ports"]["8443/tcp"][0]["HostPort"]
        web = json.loads(docker("inspect", compose("ps", "-q", "web")))[0]
        http_port = web["NetworkSettings"]["Ports"]["8100/tcp"][0]["HostPort"]
        context = ssl.create_default_context(cafile=str(root_certificate))
        injected = {
            "X-Forwarded-Proto": "http",
            "X-Forwarded-For": "203.0.113.42",
            "X-Forwarded-Host": "evil.test",
        }
        with httpx.Client(verify=context, trust_env=False) as client:
            response = client.get(
                f"https://127.0.0.1:{tls_port}/",
                headers=[
                    *injected.items(),
                    ("X-Forwarded-Proto", "ftp"),
                    ("X-Forwarded-For", "198.51.100.2"),
                ],
            )
            response.raise_for_status()
            observed = response.json()
            assert observed["scheme"] == "https"
            headers = dict(observed["headers"])
            assert headers["host"] == f"127.0.0.1:{tls_port}"
            assert headers["x-forwarded-proto"] == "https"
            assert "203.0.113.42" not in headers["x-forwarded-for"]
            assert headers["x-forwarded-host"] == f"127.0.0.1:{tls_port}"
            direct = client.get(
                f"http://127.0.0.1:{http_port}/",
                headers={
                    "X-Forwarded-Proto": "https",
                    "X-Forwarded-For": "203.0.113.42",
                },
            ).json()
            assert direct["scheme"] == "http" and direct["client"][0] != "203.0.113.42"
        with connect(
            f"wss://127.0.0.1:{tls_port}/",
            ssl=context,
            proxy=None,
            additional_headers=injected,
        ) as websocket:
            observed = json.loads(websocket.recv())
            assert observed["scheme"] == "wss"
            assert dict(observed["headers"])["x-forwarded-proto"] == "https"
        compose("stop", "caddy")
        compose("up", "-d", "--wait", "--no-build", "caddy")
        docker(
            "cp",
            caddy + ":/data/caddy/pki/authorities/local/root.crt",
            str(root_certificate),
        )
        assert root_certificate.read_bytes() == original_root
    finally:
        # Only exact IDs belonging to this freshly generated unique project.
        for container_id in docker(
            "ps", "-aq", "--filter", "label=com.docker.compose.project=" + project
        ).split():
            print(docker("logs", container_id))
        containers = docker(
            "ps", "-aq", "--filter", "label=com.docker.compose.project=" + project
        ).split()
        volumes = docker(
            "volume",
            "ls",
            "-q",
            "--filter",
            "label=com.docker.compose.project=" + project,
        ).split()
        network_ids = docker(
            "network",
            "ls",
            "-q",
            "--filter",
            "label=com.docker.compose.project=" + project,
        ).split()
        if containers:
            docker("rm", "-f", *containers)
        for network_id in network_ids:
            docker("network", "rm", network_id)
        for volume in volumes:
            docker("volume", "rm", volume)
