"""The LAN process exposes only reviewed repository GET/HEAD paths."""

from __future__ import annotations

import base64
import hashlib
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from kodi_lab.repository.development import initialize as initialize_development
from kodi_lab.repository.server import RepositoryHandler
from kodi_lab.repository.stable import generate, sha256
from tests.repository.test_development import phase2_candidate
from tests.repository.test_stable import BASE, candidate


def test_distribution_allowlist_and_methods(tmp_path: Path) -> None:
    bundle = candidate(tmp_path)
    data = tmp_path / "repository-data"
    provenance = generate(bundle, data, BASE)
    handler = type("TestRepositoryHandler", (RepositoryHandler,), {"root": data})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    try:
        for path in (
            "/health",
            "/install/",
            "/install/repository.kodi-lab-1.0.0.zip",
            "/stable/addons.xml",
            "/stable/addons.xml.sha256",
            "/stable/zips/repository.kodi-lab/repository.kodi-lab-1.0.0.zip",
            "/stable/zips/service.kodi_lab_bridge/service.kodi_lab_bridge-1.1.17.zip",
            "/stable/zips/service.kodi_lab_bridge/service.kodi_lab_bridge-1.1.17.zip.sha256",
        ):
            with urlopen(url + path) as response:
                body = response.read()
                assert response.status == 200
                assert int(response.headers["Content-Length"]) == len(body)
                if path == "/install/":
                    assert b'<a href="repository.kodi-lab-1.0.0.zip">' in body
            with urlopen(Request(url + path, method="HEAD")) as response:
                assert response.status == 200
                assert response.read() == b""
                if path.endswith(".zip") and "/stable/zips/" in path:
                    assert (
                        response.headers["Content-SHA256"]
                        == base64.b64encode(hashlib.sha256(body).digest()).decode()
                    )
            if path.endswith("addons.xml"):
                assert sha256(body) == provenance["addons_xml_sha256"]
        for path in (
            "/",
            "/api",
            "/admin",
            "/private",
            "/stable/../PROVENANCE.json",
            "/%2e%2e/private",
            "/stable/addons.xml?other=1",
        ):
            with pytest.raises(HTTPError) as error:
                urlopen(url + path)
            assert error.value.code == 404
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            with pytest.raises(HTTPError) as error:
                urlopen(Request(url + "/install/", data=b"x", method=method))
            assert error.value.code == 405
        (data / "TRANSITION.json").write_text("pending\n")
        for path in ("/health", "/install/", "/stable/addons.xml"):
            with pytest.raises(HTTPError) as error:
                urlopen(url + path)
            assert error.value.code == 503
        (data / "TRANSITION.json").unlink()
        (data / "CURRENT").write_text("f" * 64 + "\n")
        with pytest.raises(HTTPError) as error:
            urlopen(url + "/stable/addons.xml")
        assert error.value.code == 503
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_development_routes_fail_independently_of_stable(tmp_path: Path) -> None:
    bundle = phase2_candidate(tmp_path)
    data = tmp_path / "repository-data"
    generate(bundle, data, BASE)
    initialize_development(data / "development-data", BASE, bundle)
    handler = type("DevelopmentHandler", (RepositoryHandler,), {"root": data})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    try:
        with urlopen(url + "/install/") as response:
            assert b"repository.kodi-lab-dev-1.0.0.zip" in response.read()
        for path in (
            "/install/repository.kodi-lab-dev-1.0.0.zip",
            "/development/addons.xml",
            "/development/addons.xml.sha256",
        ):
            with urlopen(url + path) as response:
                assert response.status == 200
        for path in ("/development/../PROVENANCE.json", "/development/private", "/api"):
            with pytest.raises(HTTPError) as error:
                urlopen(url + path)
            assert error.value.code == 404
        (data / "development-data/ACK").unlink()
        with pytest.raises(HTTPError) as error:
            urlopen(url + "/development/addons.xml")
        assert error.value.code == 503
        with urlopen(url + "/stable/addons.xml") as response:
            assert b"service.kodi_lab_bridge" in response.read()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
