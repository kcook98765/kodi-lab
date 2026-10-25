"""Stopped log reads keep fixed paths, identities, byte/time bounds and privacy."""

from __future__ import annotations

import base64
import io
import json
import os
import tarfile
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from kodi_lab.broker.client import BrokerClient
from kodi_lab.broker.errors import BrokerAuthorizationError
from kodi_lab.broker.operations import build_foundation_registry
from kodi_lab.broker.protocol import encode_model, success_response
from kodi_lab.broker.stopped_diagnostics import StoppedDiagnosticsError
from kodi_lab.broker.stopped_logs import (
    LOG_PATHS,
    MAX_ARCHIVE_BYTES,
    MAX_DOCKER_WINDOW_BYTES,
    MAX_SOURCE_BYTES,
    MAX_TAIL_BYTES,
    SqliteStoppedLogResolver,
    StoppedManagedLogProvider,
)
from kodi_lab.broker.transport import MAX_FRAME_PAYLOAD
from tests.broker.test_provisioning_resolver import resolver_with_state
from tests.broker.test_stopped_diagnostics import CONTAINER_ID, operation
from tests.broker.test_stopped_diagnostics import Client as MetadataClient


def archive(
    data: bytes,
    *,
    name: str = "kodi.log",
    kind: bytes = tarfile.REGTYPE,
    uid: int = 1000,
    extra: bool = False,
) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        info = tarfile.TarInfo(name)
        info.uid = uid
        info.gid = 1000
        info.mode = 0o644
        info.size = len(data)
        info.type = kind
        info.linkname = (
            "settings.xml" if kind in {tarfile.SYMTYPE, tarfile.LNKTYPE} else ""
        )
        tar.addfile(info, io.BytesIO(data) if kind == tarfile.REGTYPE else None)
        if extra:
            info = tarfile.TarInfo("other")
            tar.addfile(info)
    return output.getvalue()


class Raw:
    def __init__(self, body: bytes) -> None:
        self.body, self.position = body, 0

    def read1(self, size: int, **kwargs: Any) -> bytes:
        assert 0 < size <= 16384 and kwargs == {"decode_content": False}
        chunk = self.body[self.position : self.position + size]
        self.position += len(chunk)
        return chunk


class Response:
    def __init__(
        self,
        *,
        status: int = 200,
        stat: dict[str, Any] | None = None,
        body: bytes = b"",
        is_archive: bool = False,
    ) -> None:
        self.is_archive = is_archive
        self.status_code, self.raw, self.closed = status, Raw(body), False
        self.headers = (
            {}
            if stat is None
            else {
                "x-docker-container-path-stat": base64.b64encode(
                    json.dumps(stat).encode()
                ).decode()
            }
        )

    def close(self) -> None:
        self.closed = True


class Client(MetadataClient):
    def __init__(self) -> None:
        super().__init__()
        self.value["Config"]["Tty"] = False
        self.value.update(
            Mounts=[],
            HostConfig={
                "Privileged": False,
                "Binds": None,
                "VolumesFrom": None,
                "PidMode": "",
            },
        )
        self.files = {path: b"normal log\n" for path in LOG_PATHS.values()}
        self.overrides: dict[str, Any] = {}
        self.responses: list[Response] = []
        self.reads: list[str] = []
        self.docker_reads = 0
        data = b"normal console\n"
        self.docker_body = b"\x01\x00\x00\x00" + len(data).to_bytes(4, "big") + data
        self.timeout: float = 0
        self.stat_change = False
        self.head_counts: dict[str, int] = {}

    def _url(self, template: str, container: str) -> str:
        assert (
            template in {"/containers/{0}/archive", "/containers/{0}/logs"}
            and container == CONTAINER_ID
        )
        return template.format(container)

    def head(
        self, url: str, *, params: dict[str, str], timeout: float, allow_redirects: bool
    ) -> Response:
        assert (
            url == f"/containers/{CONTAINER_ID}/archive"
            and 0 < timeout <= 1
            and allow_redirects is False
        )
        assert all(
            not response.is_archive or response.closed for response in self.responses
        )
        path = params["path"]
        assert path in LOG_PATHS.values() or path in {
            "/home",
            "/home/kodi",
            "/home/kodi/.kodi",
            "/home/kodi/.kodi/temp",
        }
        self.head_counts[path] = self.head_counts.get(path, 0) + 1
        is_file = path in LOG_PATHS.values()
        stat = {
            "name": path.rsplit("/", 1)[-1],
            "mode": 0o644 if is_file else (1 << 31) | 0o755,
            "size": len(self.files.get(path, b"")),
            "mtime": "2026-10-24T00:00:00Z",
            "linkTarget": "",
        }
        if path in self.overrides:
            stat.update(self.overrides[path])
        if self.stat_change and is_file and self.head_counts[path] > 1:
            stat["mtime"] = "2026-10-24T00:00:01Z"
        response = Response(
            status=404 if is_file and path not in self.files else 200, stat=stat
        )
        self.responses.append(response)
        return response

    def _get(
        self,
        url: str,
        *,
        params: dict[str, str],
        stream: bool,
        headers: dict[str, str],
        timeout: float,
        allow_redirects: bool,
    ) -> Response:
        assert (
            stream
            and headers == {"Accept-Encoding": "identity"}
            and 0 < timeout <= 1
            and not allow_redirects
        )
        if url == f"/containers/{CONTAINER_ID}/logs":
            assert params == {
                "stdout": "true",
                "stderr": "true",
                "tail": "200",
                "timestamps": "true",
                "follow": "false",
            }
            self.docker_reads += 1
            response = Response(
                status=self.overrides.get("docker_status", 200),
                body=self.overrides.get("docker_body", self.docker_body),
            )
            self.responses.append(response)
            return response
        assert url == f"/containers/{CONTAINER_ID}/archive"
        path = params["path"]
        assert path in LOG_PATHS.values()
        self.reads.append(path)
        body = self.overrides.get(
            "archive", archive(self.files[path], name=path.rsplit("/", 1)[-1])
        )
        response = Response(body=body, is_archive=True)
        self.responses.append(response)
        return response


def setup(tmp_path: Path) -> tuple[SqliteStoppedLogResolver, None]:
    base = resolver_with_state(tmp_path, runtime_uid=1000, runtime_gid=1000)
    return SqliteStoppedLogResolver(base._session_factory), None


def capture(tmp_path: Path, client: Client) -> Any:
    resolver, _ = setup(tmp_path)
    return StoppedManagedLogProvider(lambda: client).capture(
        resolver.resolve(operation())
    )


def test_four_fixed_log_tails_are_redacted_before_truncation_and_never_mutate(
    tmp_path: Path,
) -> None:
    client = Client()
    for path in client.files:
        client.files[path] = (
            b'password="PRIVATE-password" token=PRIVATE-token\n'
            b"http://u:PRIVATE-password@example.test Bearer PRIVATE-bearer\n"
            + b"z" * 5000
            + b"\nrecent\n"
        )
    before = deepcopy(client.value)
    result = capture(tmp_path, client)
    assert client.value == before and client.calls[-1] == "close"
    assert len(client.reads) == 4 and all(r.closed for r in client.responses)
    assert (
        result.crash_reports == "not_collected"
        and result.docker_output == "bounded_window"
    )
    assert (
        result.exit_metadata.exit_code == 139
        and result.exit_metadata.cause == "unknown"
    )
    for item in result.artifacts[:4]:
        assert (
            item.state == "available" and len(item.content.encode()) <= MAX_TAIL_BYTES
        )
        assert (
            "PRIVATE" not in item.content
            and item.content.endswith("recent\n")
            and item.tail_truncated
        )
        assert item.rotation_state == "not_observed"
    assert (
        len(
            encode_model(
                success_response("00000000-0000-4000-8000-000000000123", result)
            )
        )
        < MAX_FRAME_PAYLOAD
    )


def test_obvious_secrets_and_controls_are_redacted_in_short_log(tmp_path: Path) -> None:
    client = Client()
    client.files[LOG_PATHS["current"]] = (
        b'Bearer abcdefghijklmnop\nhttps://name:super-secret@example.test\napi_key="sekret"\nghp_12345678901234567890\n\x00safe\x1btext\n'
    )
    text = capture(tmp_path, client).artifacts[0].content
    for secret in (
        "abcdefghijklmnop",
        "super-secret",
        "sekret",
        "ghp_12345678901234567890",
        "\x00",
        "\x1b",
    ):
        assert secret not in text
    assert "[REDACTED]" in text and "safe text" in text


@pytest.mark.parametrize(
    "data",
    [b"", b'\\"' * 9000, "😀".encode() * 4000, b"token=" + b"s" * 10000],
    ids=["empty", "escaped", "unicode", "split-secret"],
)
def test_empty_unicode_escaped_and_split_key_inputs_stay_bounded(
    tmp_path: Path, data: bytes
) -> None:
    client = Client()
    client.files = dict.fromkeys(LOG_PATHS.values(), data)
    result = capture(tmp_path, client)
    assert all(len(a.content.encode()) <= MAX_TAIL_BYTES for a in result.artifacts)
    assert (
        len(
            encode_model(
                success_response("00000000-0000-4000-8000-000000000123", result)
            )
        )
        < MAX_FRAME_PAYLOAD
    )
    if data.startswith(b"token="):
        assert all("ssss" not in a.content for a in result.artifacts)


def test_oversize_is_known_without_reading_or_trimming_file(tmp_path: Path) -> None:
    client = Client()
    path = LOG_PATHS["current"]
    client.overrides[path] = {"size": MAX_SOURCE_BYTES + 1}
    result = capture(tmp_path, client)
    assert (
        result.artifacts[0].state == "too_large"
        and result.artifacts[0].source_size_bytes == MAX_SOURCE_BYTES + 1
    )
    assert path not in client.reads and all(
        a.state == "available" for a in result.artifacts[1:]
    )


def test_missing_file_remains_explicit(tmp_path: Path) -> None:
    client = Client()
    del client.files[LOG_PATHS["previous"]]
    result = capture(tmp_path, client)
    assert result.artifacts[1].state == "missing" and not result.artifacts[1].content


@pytest.mark.parametrize(
    "defect",
    [
        {"mode": (1 << 27) | 0o777, "linkTarget": "/secret"},
        {"mode": 0o666},
        {"mode": 1 << 19},
        {"size": True},
        {"name": "different"},
    ],
)
def test_unsafe_path_metadata_never_reads_file(
    tmp_path: Path, defect: dict[str, Any]
) -> None:
    client = Client()
    path = LOG_PATHS["current"]
    client.overrides[path] = defect
    assert capture(tmp_path, client).artifacts[0].state == "unsafe"
    assert path not in client.reads


def test_symlink_ancestor_is_refused_without_archiving_directory(
    tmp_path: Path,
) -> None:
    client = Client()
    client.overrides["/home/kodi/.kodi"] = {
        "mode": (1 << 27) | 0o777,
        "linkTarget": "/etc",
    }
    assert all(a.state == "unsafe" for a in capture(tmp_path, client).artifacts[:4])
    assert not client.reads


@pytest.mark.parametrize(
    "kwargs",
    [
        {"name": "../kodi.log"},
        {"kind": tarfile.SYMTYPE},
        {"kind": tarfile.LNKTYPE},
        {"kind": tarfile.FIFOTYPE},
        {"uid": 0},
        {"extra": True},
    ],
)
def test_tar_paths_links_types_owners_and_inventory_are_closed(
    tmp_path: Path, kwargs: dict[str, Any]
) -> None:
    client = Client()
    client.files = dict.fromkeys(LOG_PATHS.values(), b"log")
    client.overrides["archive"] = archive(b"log", **kwargs)
    assert all(
        a.state == "unsafe" and not a.content
        for a in capture(tmp_path, client).artifacts[:4]
    )
    assert all(r.closed for r in client.responses)


def test_archive_and_stat_drift_never_return_stitched_content(tmp_path: Path) -> None:
    client = Client()
    client.stat_change = True
    assert all(
        a.state == "changed" and not a.content
        for a in capture(tmp_path / "changed", client).artifacts[:4]
    )
    client = Client()
    client.overrides["archive"] = b"x" * (MAX_ARCHIVE_BYTES + 1)
    assert all(
        a.state == "too_large"
        for a in capture(tmp_path / "oversize", client).artifacts[:4]
    )
    assert all(r.closed for r in client.responses)


@pytest.mark.parametrize(
    "defect", ["mount", "tmpfs", "privileged", "running", "identity"]
)
def test_private_stopped_exact_container_required_before_file_reads(
    tmp_path: Path, defect: str
) -> None:
    client = Client()
    if defect == "mount":
        client.value["Mounts"] = [{"Source": "/private"}]
    elif defect == "tmpfs":
        client.value["HostConfig"]["Tmpfs"] = {"/home/kodi/.kodi": "rw"}
    elif defect == "privileged":
        client.value["HostConfig"]["Privileged"] = True
    elif defect == "running":
        client.value["State"]["Running"] = True
    else:
        client.value["Image"] = "sha256:" + "0" * 64
    with pytest.raises(StoppedDiagnosticsError):
        capture(tmp_path, client)
    assert not client.reads and client.calls[-1] == "close"


def test_deadline_is_shared_across_fixed_reads_and_closes_every_response(
    tmp_path: Path,
) -> None:
    resolver, _ = setup(tmp_path)
    client = Client()
    ticks = iter(range(100))
    with pytest.raises(StoppedDiagnosticsError, match="managed_diagnostics_deadline"):
        StoppedManagedLogProvider(
            lambda: client, clock=lambda: float(next(ticks))
        ).capture(resolver.resolve(operation()))
    assert client.calls[-1] == "close" and all(r.closed for r in client.responses)


def test_real_typed_broker_round_trip_denies_unknown_peer_before_reads(
    tmp_path: Path, socket_config: Any, run_broker: Any
) -> None:
    resolver, _ = setup(tmp_path)
    client = Client()
    registry = build_foundation_registry(
        stopped_log_resolver=resolver,
        stopped_log_provider=StoppedManagedLogProvider(lambda: client),
    )
    with run_broker(socket_config, registry=registry):
        result = BrokerClient(socket_config.socket_path).read_stopped_managed_logs(
            **operation().model_dump()
        )
    assert result.artifacts[0].content == "normal log\n"
    reads = list(client.reads)
    with (
        run_broker(socket_config, registry=registry, expected_uid=os.getuid() + 10000),
        pytest.raises(BrokerAuthorizationError),
    ):
        BrokerClient(socket_config.socket_path).read_stopped_managed_logs(
            **operation().model_dump()
        )
    assert client.reads == reads


def test_unknown_log_layout_is_refused_by_sql_before_any_file_read(
    tmp_path: Path,
) -> None:
    base = resolver_with_state(tmp_path, runtime_uid=1234, runtime_gid=2345)
    resolver = SqliteStoppedLogResolver(base._session_factory)
    with pytest.raises(StoppedDiagnosticsError, match="log_layout_unsupported"):
        resolver.resolve(operation())


def test_installed_docker_sdk_raw_http_reads_keep_timeouts_and_fixed_routes(
    tmp_path: Path,
) -> None:
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from threading import Thread
    from urllib.parse import parse_qs, urlsplit

    import docker  # type: ignore[import-untyped]

    fake = Client()
    methods: list[tuple[str, str]] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args: Any) -> None:
            pass

        def do_HEAD(self) -> None:
            selected = parse_qs(urlsplit(self.path).query)["path"][0]
            methods.append(("HEAD", selected))
            result = fake.head(
                f"/containers/{CONTAINER_ID}/archive",
                params={"path": selected},
                timeout=1,
                allow_redirects=False,
            )
            self.send_response(result.status_code)
            self.send_header("Content-Length", "0")
            for key, value in result.headers.items():
                self.send_header(key, value)
            self.end_headers()
            result.close()

        def do_GET(self) -> None:
            path = urlsplit(self.path).path
            methods.append(("GET", path))
            if path.endswith("/containers/json"):
                payload = json.dumps([{"Id": CONTAINER_ID}]).encode()
            elif path.endswith(f"/containers/{CONTAINER_ID}/json"):
                payload = json.dumps(fake.value).encode()
            elif path.endswith(f"/containers/{CONTAINER_ID}/logs"):
                assert parse_qs(urlsplit(self.path).query) == {
                    "stdout": ["true"],
                    "stderr": ["true"],
                    "tail": ["200"],
                    "timestamps": ["true"],
                    "follow": ["false"],
                }
                payload = fake.docker_body
            else:
                assert path.endswith(f"/containers/{CONTAINER_ID}/archive")
                selected = parse_qs(urlsplit(self.path).query)["path"][0]
                assert selected in LOG_PATHS.values()
                payload = archive(
                    fake.files[selected], name=selected.rsplit("/", 1)[-1]
                )
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = HTTPServer(("127.0.0.1", 0), Handler)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        resolver, _ = setup(tmp_path)
        result = StoppedManagedLogProvider(
            lambda: docker.APIClient(
                base_url=f"http://127.0.0.1:{server.server_port}",
                version="1.45",
                timeout=1,
            )
        ).capture(resolver.resolve(operation()))
        assert all(
            a.state == "available"
            and a.content
            == ("normal console\n" if a.kind == "docker_output" else "normal log\n")
            for a in result.artifacts
        )
        assert all(method in {"GET", "HEAD"} for method, _ in methods)
    finally:
        server.shutdown()
        server.server_close()
        worker.join(5)
    assert not worker.is_alive()


def test_docker_window_decodes_stdout_stderr_without_follow_or_full_history_claim(
    tmp_path: Path,
) -> None:
    client = Client()
    first = b"2026-10-25T00:00:00Z stdout token=PRIVATE\n"
    second = b"2026-10-25T00:00:01Z stderr recent\n"
    client.docker_body = b"".join(
        bytes([kind, 0, 0, 0]) + len(data).to_bytes(4, "big") + data
        for kind, data in ((1, first), (2, second))
    )
    result = capture(tmp_path, client)
    item = result.artifacts[-1]
    assert result.schema_revision == 2 and item.kind == "docker_output"
    assert item.state == "available" and "PRIVATE" not in item.content
    assert "stdout" in item.content and "stderr recent" in item.content
    assert item.source_size_bytes == len(first) + len(second)
    assert item.rotation_state == "not_observed" and item.modified_at is None
    assert client.docker_reads == 1 and all(
        response.closed for response in client.responses
    )


@pytest.mark.parametrize(
    "defect",
    [
        "padding",
        "channel",
        "short_header",
        "short_payload",
        "oversize",
        "driver",
        "unknown_tty",
    ],
)
def test_invalid_or_unavailable_docker_window_is_explicit_without_body(
    tmp_path: Path, defect: str
) -> None:
    client = Client()
    data = {
        "padding": b"\x01\x01\x00\x00\x00\x00\x00\x00",
        "channel": b"\x00\x00\x00\x00\x00\x00\x00\x00",
        "short_header": b"short",
        "short_payload": b"\x01\x00\x00\x00\x00\x00\x00\x09short",
        "oversize": b"x" * (MAX_DOCKER_WINDOW_BYTES + 1),
    }
    client.overrides["docker_body"] = data.get(defect, b"PRIVATE daemon error")
    if defect == "driver":
        client.overrides["docker_status"] = 500
    elif defect == "unknown_tty":
        del client.value["Config"]["Tty"]
    result = capture(tmp_path, client)
    item = result.artifacts[-1]
    assert item.state == (
        "unavailable"
        if defect in {"driver", "unknown_tty"}
        else "too_large"
        if defect == "oversize"
        else "unsafe"
    )
    assert not item.content and item.source_sha256 is None
    assert all(a.state == "available" for a in result.artifacts[:4])
    assert all(response.closed for response in client.responses)


def test_tty_window_is_bounded_and_redacted_as_raw_output(tmp_path: Path) -> None:
    client = Client()
    client.value["Config"]["Tty"] = True
    client.docker_body = b"password=PRIVATE\n" + b"x" * 5000 + b"\nlatest console\n"
    item = capture(tmp_path, client).artifacts[-1]
    assert item.state == "available" and item.tail_truncated
    assert item.content.endswith("latest console\n") and "PRIVATE" not in item.content
    assert len(item.content.encode()) <= MAX_TAIL_BYTES
