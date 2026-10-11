"""Internal HTTP health cannot substitute TCP/LAN connectivity for a Unix socket."""

from __future__ import annotations

import asyncio
import json
import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from kodi_lab.config import Settings
from kodi_lab.mcp.lan import check_http_proxy

URL = "http://192.168.2.191:8088/mcp"
BODY = b'{"error":"authentication_failed"}'
HEADERS = (
    b"HTTP/1.1 401 Unauthorized\r\nWWW-Authenticate: Bearer\r\n"
    b"X-Kodi-Lab-MCP-Instance: expected\r\nContent-Type: application/json\r\n"
)


@pytest.mark.parametrize(
    "path",
    [
        "relative.sock",
        "/run/../tmp/readiness.sock",
        "/run/bad;path.sock",
        "/" + "x" * 100,
    ],
)
def test_invalid_socket_configuration(path: str) -> None:
    with pytest.raises(ValidationError, match="safe absolute socket path"):
        Settings(
            _env_file=None,
            mcp_lan_http_readiness_socket=path,  # type: ignore[arg-type,call-arg]
        )


def test_socket_configuration_requires_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KODI_LAB_MCP_LAN_HTTP_READINESS_SOCKET", "")
    assert Settings(_env_file=None).mcp_lan_http_readiness_socket is None  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            mcp_lan_http_readiness_socket=Path("/run/kodi-lab-mcp-http/readiness.sock"),
        )  # type: ignore[call-arg]


@pytest.mark.parametrize(
    "headers,body,ready",
    [
        (HEADERS, BODY, True),
        (HEADERS.replace(b"401 Unauthorized", b"200 OK"), BODY, False),
        (HEADERS.replace(b"401 Unauthorized", b"302 Found"), BODY, False),
        (HEADERS.replace(b"Bearer", b"Basic"), BODY, False),
        (HEADERS.replace(b"expected", b"wrong-upstream"), BODY, False),
        (HEADERS.replace(b"application/json", b"text/html"), BODY, False),
        (HEADERS, b"{broken", False),
        (HEADERS, b"[]", False),
        (HEADERS, b'{"error":"wrong_gate"}', False),
        (HEADERS, BODY + b" " * 257, False),
        (HEADERS + b"Content-Encoding: gzip\r\n", BODY, False),
    ],
)
def test_internal_socket_health_requires_exact_bounded_response(
    tmp_path: Path, headers: bytes, body: bytes, ready: bool
) -> None:
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    path = directory / "health.sock"
    requests: list[bytes] = []

    async def exercise() -> None:
        async def serve(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            request = await reader.readuntil(b"\r\n\r\n")
            requests.append(request)
            size = int(request.lower().split(b"content-length: ")[1].split(b"\r\n")[0])
            payload = await reader.readexactly(size)
            assert json.loads(payload)["method"] == "tools/list"
            writer.write(
                headers + f"Content-Length: {len(body)}\r\n\r\n".encode() + body
            )
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        async with await asyncio.start_unix_server(serve, path=path):
            result = await check_http_proxy(
                URL,
                instance_id="expected",
                bind_address="192.168.2.191",
                readiness_socket=path,
            )
            assert result.ready is ready
            assert "separate test" in result.message
        assert len(requests) == 1
        assert requests[0].startswith(b"POST /mcp HTTP/1.1\r\n")
        assert b"Host: 192.168.2.191:8088" in requests[0]
        assert b"authorization:" not in requests[0].lower()
        assert b"cookie:" not in requests[0].lower()

    asyncio.run(exercise())


@pytest.mark.parametrize("send_headers", [False, True])
def test_unresponsive_listener_or_body_has_whole_probe_deadline(
    tmp_path: Path, send_headers: bool
) -> None:
    tmp_path.chmod(0o700)
    path = tmp_path / "hang.sock"

    async def exercise() -> None:
        finished = asyncio.Event()

        async def serve(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            await reader.readuntil(b"\r\n\r\n")
            if send_headers:
                writer.write(HEADERS + b"Content-Length: 100\r\n\r\n")
                await writer.drain()
                # Frequent bytes would defeat a timeout applied only to each read.
                while not finished.is_set():
                    writer.write(b" ")
                    await writer.drain()
                    await asyncio.sleep(0.1)
            else:
                await finished.wait()
            writer.close()
            await writer.wait_closed()

        async with await asyncio.start_unix_server(serve, path=path):
            started = time.monotonic()
            result = await check_http_proxy(
                URL,
                instance_id="expected",
                bind_address="192.168.2.191",
                readiness_socket=path,
            )
            finished.set()
            assert not result.ready and time.monotonic() - started < 2.6

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "condition", ["missing", "file", "permissive", "symlink", "parent-link", "loop"]
)
def test_unsafe_or_missing_socket_never_falls_back_to_lan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, condition: str
) -> None:
    tmp_path.chmod(0o700)
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    path = private / "health.sock"
    listener = socket.socket(socket.AF_UNIX)
    try:
        if condition == "file":
            path.write_bytes(b"not a socket")
        elif condition != "missing":
            listener.bind(str(path))
        if condition == "permissive":
            private.chmod(0o755)
        elif condition == "symlink":
            link = private / "link.sock"
            link.symlink_to(path)
            path = link
        elif condition == "parent-link":
            link = tmp_path / "link"
            link.symlink_to(private, target_is_directory=True)
            path = link / "health.sock"
        elif condition == "loop":
            link = private / "loop.sock"
            link.symlink_to(link)
            path = link

        def forbidden(*args: Any, **kwargs: Any) -> Any:
            pytest.fail("invalid socket attempted DNS or LAN networking")

        monkeypatch.setattr(socket, "getaddrinfo", forbidden)
        result = asyncio.run(
            check_http_proxy(
                URL,
                instance_id="expected",
                bind_address="192.168.2.191",
                readiness_socket=path,
            )
        )
        assert not result.ready
    finally:
        listener.close()


def test_stopped_proxy_is_unhealthy_without_tcp_fallback(tmp_path: Path) -> None:
    tmp_path.chmod(0o700)
    path = tmp_path / "stopped.sock"
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(path))
    listener.close()
    result = asyncio.run(
        check_http_proxy(
            URL,
            instance_id="expected",
            bind_address="192.168.2.191",
            readiness_socket=path,
        )
    )
    assert not result.ready


def test_installed_container_rejects_web_owned_socket_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tmp_path.chmod(0o700)
    path = tmp_path / "web-owned.sock"
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(path))
    try:
        monkeypatch.setenv("KODI_LAB_REMOTE_CONTAINER", "1")
        result = asyncio.run(
            check_http_proxy(
                URL,
                instance_id="expected",
                bind_address="192.168.2.191",
                readiness_socket=path,
            )
        )
        assert not result.ready
    finally:
        listener.close()


def test_named_access_acl_cannot_bypass_private_directory_mode(tmp_path: Path) -> None:
    if shutil.which("setfacl") is None:
        pytest.skip("setfacl unavailable")
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    path = private / "health.sock"
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(path))
    try:
        subprocess.run(["setfacl", "-m", "u:65534:x", str(private)], check=True)
        assert private.stat().st_mode & 0o007 == 0
        result = asyncio.run(
            check_http_proxy(
                URL,
                instance_id="expected",
                bind_address="192.168.2.191",
                readiness_socket=path,
            )
        )
        assert not result.ready
    finally:
        listener.close()
