"""Kodi's remote ZIP reader needs exact HTTP byte ranges from the LAN server."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from kodi_lab.repository.development import initialize as initialize_development
from kodi_lab.repository.server import RepositoryHandler
from kodi_lab.repository.stable import generate
from tests.repository.test_development import phase2_candidate
from tests.repository.test_stable import BASE


@contextmanager
def serving(tmp_path: Path) -> Iterator[str]:
    bundle = phase2_candidate(tmp_path)
    data = tmp_path / "repository-data"
    generate(bundle, data, BASE)
    initialize_development(data / "development-data", BASE, bundle)
    handler = type("RangeHandler", (RepositoryHandler,), {"root": data})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize(
    "path",
    [
        "/install/repository.kodi-lab-1.0.0.zip",
        "/install/repository.kodi-lab-dev-1.0.0.zip",
        "/stable/zips/service.kodi_lab_bridge/service.kodi_lab_bridge-1.1.14.zip",
        "/stable/addons.xml",
        "/development/addons.xml",
    ],
)
def test_get_head_and_single_ranges(tmp_path: Path, path: str) -> None:
    with serving(tmp_path) as base:
        with urlopen(base + path) as response:
            whole = response.read()
            assert response.status == 200
            assert response.headers["Content-Length"] == str(len(whole))
            assert response.headers["Accept-Ranges"] == "bytes"
            assert "Content-Range" not in response.headers
        assert len(whole) > 32
        with urlopen(Request(base + path, method="HEAD")) as response:
            assert response.status == 200
            assert response.read() == b""
            assert response.headers["Content-Length"] == str(len(whole))
            assert response.headers["Accept-Ranges"] == "bytes"

        cases = [
            ("bytes=0-31", 0, 31),
            ("bytes=32-", 32, len(whole) - 1),
            ("bytes=-16", len(whole) - 16, len(whole) - 1),
            ("bytes=1-1", 1, 1),
            (f"bytes=0-{len(whole) - 1}", 0, len(whole) - 1),
            (
                f"bytes={len(whole) - 8}-{len(whole) + 100}",
                len(whole) - 8,
                len(whole) - 1,
            ),
        ]
        if len(whole) > 607:
            cases.append(("bytes=607-638", 607, min(638, len(whole) - 1)))
        for header, first, last in cases:
            with urlopen(Request(base + path, headers={"Range": header})) as response:
                body = response.read()
                assert response.status == 206
                assert response.headers["Accept-Ranges"] == "bytes"
                assert response.headers["Content-Range"] == (
                    f"bytes {first}-{last}/{len(whole)}"
                )
                assert response.headers["Content-Length"] == str(last - first + 1)
                assert body == whole[first : last + 1]
        with urlopen(Request(base + path, headers={"Range": "Bytes=0-1"})) as response:
            assert response.status == 206
            assert response.read() == whole[:2]
        # This server sends no strong validator, so If-Range cannot match.
        with urlopen(
            Request(
                base + path,
                headers={"Range": "bytes=0-1", "If-Range": '"old-etag"'},
            )
        ) as response:
            assert response.status == 200
            assert response.read() == whole
        # HEAD describes the complete selected resource even if a client sends Range.
        with urlopen(
            Request(base + path, method="HEAD", headers={"Range": "bytes=0-1"})
        ) as response:
            assert response.status == 200
            assert response.headers["Content-Length"] == str(len(whole))
            assert "Content-Range" not in response.headers
            assert response.read() == b""


def test_unsatisfiable_malformed_and_multiple_ranges(tmp_path: Path) -> None:
    with serving(tmp_path) as base:
        url = base + "/install/repository.kodi-lab-1.0.0.zip"
        with urlopen(url) as response:
            whole = response.read()
        for header in (
            f"bytes={len(whole)}-",
            f"bytes={len(whole) + 1}-{len(whole) + 2}",
            "bytes=-0",
        ):
            with pytest.raises(HTTPError) as error:
                urlopen(Request(url, headers={"Range": header}))
            assert error.value.code == 416
            assert error.value.headers["Content-Range"] == f"bytes */{len(whole)}"
            assert error.value.headers["Accept-Ranges"] == "bytes"
            assert error.value.headers["Content-Length"] == "0"
            assert error.value.read() == b""
        for header in (
            "bytes=2-1",
            "bytes=abc-def",
            "bytes=0-1junk",
            "bytes=" + "9" * 200 + "-",
        ):
            with pytest.raises(HTTPError) as error:
                urlopen(Request(url, headers={"Range": header}))
            assert error.value.code == 400
            assert error.value.read() == b""
        # A single-range server may ignore multi-range and send the full object.
        with urlopen(Request(url, headers={"Range": "bytes=0-1,3-4"})) as response:
            assert response.status == 200
            assert response.read() == whole
            assert response.headers["Content-Length"] == str(len(whole))
            assert "Content-Range" not in response.headers
        # Unknown range units must be ignored by an origin server.
        with urlopen(Request(url, headers={"Range": "items=0-1"})) as response:
            assert response.status == 200
            assert response.read() == whole


def test_ranges_do_not_expand_routes_or_methods(tmp_path: Path) -> None:
    with serving(tmp_path) as base:
        for path in ("/private", "/stable/../PROVENANCE.json", "/%2e%2e/private"):
            with pytest.raises(HTTPError) as error:
                urlopen(Request(base + path, headers={"Range": "bytes=0-1"}))
            assert error.value.code == 404
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            with pytest.raises(HTTPError) as error:
                urlopen(
                    Request(
                        base + "/install/repository.kodi-lab-1.0.0.zip",
                        data=b"x",
                        headers={"Range": "bytes=0-1"},
                        method=method,
                    )
                )
            assert error.value.code == 405
