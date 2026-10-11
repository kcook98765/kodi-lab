"""Real nginx Unix readiness with an isolated UID 10001 container and no network."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from .conftest import FakeBroker, FakeExternalKodi
from .test_mcp_lan import REPOSITORY
from .test_mcp_lan_http import _free_port, _http_app
from .test_mcp_stdio import _live_loopback_server


def _sudo(*args: str) -> bytes:
    return subprocess.check_output(["sudo", "-n", *args], stderr=subprocess.PIPE)


class RootProxy:
    def __init__(self, root: Path, directory: Path, worker: Path, url: str) -> None:
        self.root = root
        self.directory = directory
        self.worker = worker
        self.url = url
        self.process: subprocess.Popen[bytes] | None = None

    def start(self, upstream: str) -> None:
        from urllib.parse import urlsplit

        parsed = urlsplit(self.url)
        template = (
            REPOSITORY / "deployment/mcp-lan-http-nginx.conf.template"
        ).read_text()
        values = {
            "PRIVATE_LAN_IPV4": parsed.hostname or "",
            "HTTP_PORT": str(parsed.port),
            "MCP_HOST": parsed.hostname or "",
            "MCP_AUTHORITY": parsed.netloc,
            "AUTHORIZED_LAN_CIDR": ".".join((parsed.hostname or "").split(".")[:3])
            + ".0/24",
            "LOOPBACK_WEB_PORT": str(urlsplit(upstream).port),
            "READINESS_SOCKET_PATH": str(self.directory / "readiness.sock"),
        }
        for name, value in values.items():
            template = template.replace(f"@{name}@", value)
        assert "@" not in template
        (self.root / "nginx.conf").write_text(
            f"user www-data; worker_processes 1; pid {self.root}/nginx.pid;\n"
            "error_log /dev/null crit; events { worker_connections 64; }\n"
            f"http {{ access_log off; client_body_temp_path {self.worker}/body;\n"
            f"proxy_temp_path {self.worker}/proxy;\n{template}\n}}\n"
        )
        _sudo(
            "setpriv",
            "--regid",
            "10001",
            "--clear-groups",
            "nginx",
            "-t",
            "-c",
            str(self.root / "nginx.conf"),
        )
        self.process = subprocess.Popen(
            [
                "sudo",
                "-n",
                "setpriv",
                "--regid",
                "10001",
                "--clear-groups",
                "nginx",
                "-c",
                str(self.root / "nginx.conf"),
                "-g",
                "daemon off;",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(
                    (parsed.hostname or "", parsed.port or 80), timeout=0.2
                ):
                    return
            except OSError:
                assert self.process.poll() is None
                time.sleep(0.05)
        raise RuntimeError("isolated root proxy failed to start")

    def stop(self) -> None:
        if self.process is not None:
            _sudo("nginx", "-s", "quit", "-c", str(self.root / "nginx.conf"))
            self.process.wait(timeout=10)
            self.process = None


def test_networkless_web_container_checks_same_proxy_and_recovers_after_restart(
    client: TestClient,
    database: sessionmaker[Session],
    broker: FakeBroker,
    external_kodi: FakeExternalKodi,
    tmp_path: Path,
) -> None:
    bind = os.environ.get("KODI_LAB_TEST_LAN_IPV4")
    image = os.environ.get("KODI_LAB_TEST_HTTP_READINESS_CONTAINER_IMAGE")
    if not bind or not image:
        pytest.skip("explicit isolated LAN interface and container image required")
    url = f"http://{bind}:{_free_port(bind)}/mcp"
    app = _http_app(client, database, broker, external_kodi, url=url)
    name = "kodi-lab-http-health-" + uuid4().hex[:12]
    results: dict[str, Any] = {
        "url": url,
        "production_mounts": False,
        "firewall_modified": False,
    }
    with (
        tempfile.TemporaryDirectory(
            prefix="kl-http-root-", dir="/tmp"
        ) as directory_name,
        tempfile.TemporaryDirectory(
            prefix="kl-http-worker-", dir="/tmp"
        ) as worker_name,
        TestClient(app, base_url="http://127.0.0.1"),
        _live_loopback_server(app) as (upstream, _),
    ):
        directory, worker = Path(directory_name), Path(worker_name)
        _sudo("chown", "0:10001", str(directory))
        _sudo("chmod", "0750", str(directory))
        _sudo("chown", "33:33", str(worker))
        proxy = RootProxy(tmp_path, directory, worker, url)
        started = False
        try:
            proxy.start(upstream)
            container_id = (
                subprocess.check_output(
                    [
                        "docker",
                        "run",
                        "--detach",
                        "--name",
                        name,
                        "--network",
                        "none",
                        "--read-only",
                        "--cap-drop",
                        "ALL",
                        "--security-opt",
                        "no-new-privileges:true",
                        "--mount",
                        f"type=bind,src={directory},dst=/run/kodi-lab-mcp-http,readonly",
                        "--mount",
                        "type=bind,src="
                        + str(REPOSITORY / "src/kodi_lab/mcp/lan.py")
                        + ",dst=/usr/local/lib/python3.12/site-packages/"
                        "kodi_lab/mcp/lan.py,readonly",
                        "--entrypoint",
                        "python",
                        image,
                        "-c",
                        "import time; time.sleep(180)",
                    ]
                )
                .decode()
                .strip()
            )
            started = True
            marker = app.state.kodi_lab.mcp_transport.instance_id

            def probe() -> dict[str, Any]:
                code = (
                    "import asyncio,json,os;from pathlib import Path;"
                    "from kodi_lab.mcp.lan import check_http_proxy;"
                    "p=Path('/run/kodi-lab-mcp-http/readiness.sock');"
                    f"s=asyncio.run(check_http_proxy({url!r},instance_id={marker!r},"
                    f"bind_address={bind!r},readiness_socket=p));"
                    "d=p.parent.stat();"
                    "print(json.dumps({'ready':s.ready,'uid':os.geteuid(),'gid':os.getegid(),"
                    "'directory_inode':d.st_ino,'directory_uid':d.st_uid,'directory_gid':d.st_gid,"
                    "'directory_mode':oct(d.st_mode&0o7777),'message':s.message}))"
                )
                return cast(
                    dict[str, Any],
                    json.loads(
                        subprocess.check_output(
                            [
                                "docker",
                                "exec",
                                name,
                                "python",
                                "-c",
                                code,
                            ]
                        )
                    ),
                )

            first = probe()
            assert first["ready"] and first["uid"] == first["gid"] == 10001
            assert first["directory_uid"] == 0 and first["directory_gid"] == 10001
            assert first["directory_mode"] == "0o750"
            results["healthy"] = first
            results["socket_metadata"] = json.loads(
                _sudo(
                    "python3",
                    "-c",
                    "import pathlib,json; p=pathlib.Path("
                    + repr(str(directory / "readiness.sock"))
                    + ");s=p.stat();print(json.dumps({'uid':s.st_uid,'gid':s.st_gid,"
                    "'mode':oct(s.st_mode&0o7777)}))",
                )
            )
            assert results["socket_metadata"] == {
                "uid": 0,
                "gid": 10001,
                "mode": "0o666",
            }
            denied = json.loads(
                subprocess.check_output(
                    [
                        "docker",
                        "exec",
                        "--user",
                        "65534:65534",
                        name,
                        "python",
                        "-c",
                        "import socket,json; s=socket.socket(socket.AF_UNIX);"
                        "print(json.dumps({'connect_result':s.connect_ex('/run/kodi-lab-mcp-http/readiness.sock')}))",
                    ]
                )
            )
            assert denied["connect_result"] == 13
            results["unrelated_uid_denied"] = denied
            readonly = json.loads(
                subprocess.check_output(
                    [
                        "docker",
                        "exec",
                        name,
                        "python",
                        "-c",
                        "import json,os\ntry:"
                        "os.open('/run/kodi-lab-mcp-http/unwanted',"
                        "os.O_CREAT|os.O_WRONLY)\n"
                        "except OSError as e:"
                        "print(json.dumps({'write_errno':e.errno}))",
                    ]
                )
            )
            assert readonly["write_errno"] in {13, 30}
            results["mount_write_denied"] = readonly
            code = (
                "import httpx,json;"
                "t=httpx.HTTPTransport(uds='/run/kodi-lab-mcp-http/readiness.sock');"
                f"url={url!r};"
                "c=httpx.Client(transport=t,trust_env=False,timeout=2,"
                "headers={'MCP-Protocol-Version':'2025-11-25'});"
                "checks=[c.post(url,json={}).status_code,"
                "c.get(url.removesuffix('/mcp')+'/login').status_code,"
                "c.post(url,headers={'Host':'wrong.invalid'},json={}).status_code,"
                "c.post(url,headers={'Origin':'http://wrong.invalid'},json={}).status_code];"
                "print(json.dumps(checks));c.close()"
            )
            statuses = json.loads(
                subprocess.check_output(
                    [
                        "docker",
                        "exec",
                        name,
                        "python",
                        "-c",
                        code,
                    ]
                )
            )
            assert statuses == [401, 404, 421, 403]
            results["uds_auth_admin_host_origin_statuses"] = statuses
            # UDS health succeeds while this container has no TCP route to ingress.
            tcp = json.loads(
                subprocess.check_output(
                    [
                        "docker",
                        "exec",
                        name,
                        "python",
                        "-c",
                        "import socket,json;s=socket.socket();s.settimeout(.5);"
                        f"r=s.connect_ex(({bind!r},"
                        f"{url.rsplit(':', 1)[1].split('/')[0]}));"
                        "print(json.dumps({'connect_result':r}))",
                    ]
                )
            )
            assert tcp["connect_result"] != 0
            results["tcp_unreachable_while_internal_healthy"] = tcp
            raw = socket.socket()
            raw.bind(("127.0.0.1", 0))
            raw.settimeout(2)
            raw.connect((bind, int(url.rsplit(":", 1)[1].split("/")[0])))
            raw.sendall(
                (
                    "POST /mcp HTTP/1.1\r\nHost: "
                    f"{url.removeprefix('http://').removesuffix('/mcp')}\r\n"
                    "Content-Length: 2\r\nConnection: close\r\n\r\n{}"
                ).encode()
            )
            assert b" 403 " in raw.recv(4096).split(b"\r\n")[0]
            raw.close()
            results["non_lan_source_acl_denied"] = True
            with httpx.Client(
                trust_env=False,
                headers={
                    "MCP-Protocol-Version": "2025-11-25",
                    "Content-Type": "application/json",
                },
            ) as wire:
                payload = b'{"jsonrpc":"2.0","id":0,"method":"tools/list","params":{}}'
                assert wire.post(url, content=payload + b" " * 20000).status_code == 401
                assert wire.get(url.removesuffix("/mcp") + "/login").status_code == 404
            results["large_body_worker_temp_and_lan_admin_rejection"] = True
            _sudo("chown", "10001:10001", str(directory / "readiness.sock"))
            assert not probe()["ready"]
            _sudo("chown", "0:10001", str(directory / "readiness.sock"))
            results["web_owned_socket_denied"] = True
            proxy.stop()
            assert not probe()["ready"]
            results["stopped_proxy_denied"] = True
            proxy.start(upstream)
            resumed = probe()
            assert (
                resumed["ready"]
                and resumed["directory_inode"] == first["directory_inode"]
            )
            current = json.loads(subprocess.check_output(["docker", "inspect", name]))[
                0
            ]
            assert (
                current["Id"] == container_id
                and current["HostConfig"]["NetworkMode"] == "none"
            )
            assert all(not mount["RW"] for mount in current["Mounts"])
            results["same_web_mount_recovers_after_restart"] = True
            proxy.stop()
            wrong = _http_app(client, database, broker, external_kodi, url=url)
            with (
                TestClient(wrong, base_url="http://127.0.0.1"),
                _live_loopback_server(wrong) as (other, _),
            ):
                proxy.start(other)
                assert not probe()["ready"]
                results["wrong_actual_mcp_upstream_denied"] = True
                proxy.stop()
            results["status"] = "PASS"
        finally:
            proxy.stop()
            if started:
                subprocess.run(
                    ["docker", "rm", "--force", name], check=True, capture_output=True
                )
            _sudo(
                "chown",
                "-R",
                f"{os.getuid()}:{os.getgid()}",
                str(directory),
                str(worker),
            )
            evidence = os.environ.get("KODI_LAB_TEST_HTTP_READINESS_EVIDENCE")
            if evidence:
                (Path(evidence) / "container-readiness.json").write_text(
                    json.dumps(results, indent=2) + "\n"
                )
