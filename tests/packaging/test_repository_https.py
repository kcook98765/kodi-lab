"""Fresh repository transport through a disposable, verified TLS reverse proxy."""

from __future__ import annotations

import io
import json
import runpy
import ssl
import subprocess
import threading
import zipfile
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener
from uuid import UUID
from xml.etree import ElementTree as ET

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from kodi_lab.repository import development, stable
from kodi_lab.repository.server import RepositoryHandler
from kodi_lab.storage import metadata
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from scripts import install
from tests.packaging.test_linux_v1_bundle import bundle as verified_bundle
from tests.repository.test_development import fixture_zip, phase2_candidate

ROOT = Path(__file__).resolve().parents[2]
bundle_tool = runpy.run_path(str(ROOT / "scripts/linux-v1-bundle"))
bundle = verified_bundle


@pytest.mark.parametrize(
    "origin",
    [
        "http://192.168.50.10:18120",
        "https://repo.example.test",
        "https://repo.example.test:443",
        "https://repo.example.test:18443",
    ],
)
def test_origin_validation_agrees_with_runtime(origin: str) -> None:
    bundle_tool["validate_repository_config"]("192.168.50.10", 18120, origin)
    assert stable.validate_config("192.168.50.10", 18120, origin) == origin


@pytest.mark.parametrize(
    "origin",
    [
        "http://192.168.50.10:18120",
        "https://repo.example.test",
        "https://repo.example.test:443",
        "https://repo.example.test:18443",
    ],
)
def test_fresh_feeds_generate_and_verify_with_selected_transport(
    tmp_path: Path, origin: str
) -> None:
    candidate = phase2_candidate(tmp_path)
    data = tmp_path / "data"
    stable.generate(candidate, data, origin)
    development.initialize(data / "development-data", origin, candidate)
    for module, root in ((stable, data), (development, data / "development-data")):
        generation, manifest = module.load_current(root, expected_base_url=origin)
        filename = f"{manifest['repository_id']}-{manifest['repository_version']}.zip"
        assert (generation / "install" / filename).read_bytes() == module.bootstrap_zip(
            origin
        )
    assert (
        data / "packages/service.kodi_lab_bridge/service.kodi_lab_bridge-1.1.31.zip"
    ).is_file()


@pytest.mark.parametrize(
    "origin",
    [
        "http://repo.example.test",
        "https://repo.example.test:",
        "https://repo.example.test:0",
        "https://repo.example.test:65536",
        "https://repo.example.test:bad",
        "https://user:secret@repo.example.test",
        "https://repo.example.test/path",
        "https://repo.example.test/",
        "https://repo.example.test?token=secret",
        "https://repo.example.test#fragment",
        "https://repo.example.test\n",
        "https://repo.example.test|verifypeer=false",
        'https://repo.example.test"',
        "https://repo.example.test/$VALUE",
        "https://127.0.0.1",
        "https://localhost",
        "file:///tmp/repository",
    ],
)
def test_unsafe_origins_rejected(origin: str) -> None:
    with pytest.raises(ValueError, match="repository"):
        bundle_tool["validate_repository_config"]("192.168.50.10", 18120, origin)


@pytest.mark.parametrize(
    "bind", ["0.0.0.0", "127.0.0.1", "8.8.8.8", "169.254.1.1", "::"]
)
def test_https_keeps_private_listener_boundary(bind: str) -> None:
    with pytest.raises(ValueError, match="repository"):
        bundle_tool["validate_repository_config"](
            bind, 18120, "https://repo.example.test"
        )


def test_https_configuration_keeps_http_compose_backend(
    bundle: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = json.loads((bundle / "BUNDLE.json").read_bytes())

    def image_inspect(command: list[str], **kwargs: Any) -> Any:
        return SimpleNamespace(returncode=0, stdout=f"{command[-1]} linux/amd64\n")

    with monkeypatch.context() as patch:
        patch.setattr(subprocess, "run", image_inspect)
        bundle_tool["configure"](
            bundle, data, 18100, "192.168.50.10", 18120, "https://repo.example.test"
        )
    values = dict(
        line.split("=", 1) for line in (bundle / ".env").read_text().splitlines()
    )
    bundle_tool["verify"](bundle)
    assert values["KODI_LAB_REPOSITORY_BASE_URL"] == "https://repo.example.test"
    assert values["KODI_LAB_REPOSITORY_BIND"] == "192.168.50.10"
    assert values["KODI_LAB_REPOSITORY_PORT"] == "18120"
    # Render only; never create containers or use production configuration.
    rendered = json.loads(
        subprocess.check_output(
            [
                "docker",
                "compose",
                "--env-file",
                str(bundle / ".env"),
                "-f",
                str(bundle / "compose.release.yaml"),
                "config",
                "--format",
                "json",
            ],
            text=True,
        )
    )
    service = rendered["services"]["repository"]
    assert service["ports"] == [
        {
            "mode": "ingress",
            "host_ip": "192.168.50.10",
            "target": 8110,
            "published": "18120",
            "protocol": "tcp",
        }
    ]
    assert (
        service["environment"]["KODI_LAB_REPOSITORY_BASE_URL"]
        == "https://repo.example.test"
    )
    assert service["read_only"] is True


def certificate(
    tmp_path: Path, *, expired: bool, hostname: str = "localhost"
) -> tuple[Path, ssl.SSLContext]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=2))
        .not_valid_after(now + timedelta(days=-1 if expired else 1))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(hostname)]), critical=False
        )
        .sign(key, hashes.SHA256())
    )
    cert_file, key_file = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_file, key_file)
    return cert_file, context


@pytest.fixture
def proxy(tmp_path: Path, request: pytest.FixtureRequest) -> Iterator[Any]:
    option = getattr(request, "param", False)
    hostname = option if isinstance(option, str) else "localhost"
    cert, context = certificate(tmp_path, expired=option is True, hostname=hostname)
    data = tmp_path / "data"
    backend_handler: type[RepositoryHandler] = type(
        "PrivateBackend", (RepositoryHandler,), {"root": data}
    )
    backend = ThreadingHTTPServer(("127.0.0.1", 0), backend_handler)
    backend_url = f"http://127.0.0.1:{backend.server_port}"
    seen: list[str] = []

    class Proxy(BaseHTTPRequestHandler):
        fault = ""
        fault_path = "/stable/addons.xml"

        def do_GET(self) -> None:
            seen.append(self.path)
            if self.fault and self.path == self.fault_path:
                if self.fault == "redirect":
                    self.send_response(302)
                    self.send_header("Location", backend_url + self.path)
                    self.end_headers()
                elif self.fault == "missing":
                    self.send_error(404)
                else:
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"wrong repository\n")
                return
            opener = build_opener(ProxyHandler({}))
            try:
                with opener.open(
                    Request(
                        backend_url + self.path,
                        headers={
                            "Host": self.headers["Host"],
                            "X-Forwarded-Proto": "https",
                            "X-Forwarded-For": "192.168.50.20",
                        },
                    ),
                    timeout=5,
                ) as response:
                    content = response.read()
                    self.send_response(response.status)
                    self.send_header("Content-Type", response.headers["Content-Type"])
                    self.send_header("Content-Length", str(len(content)))
                    self.end_headers()
                    self.wfile.write(content)
            except HTTPError as error:
                self.send_error(error.code)

    frontend = ThreadingHTTPServer(("127.0.0.1", 0), Proxy)
    frontend.socket = context.wrap_socket(frontend.socket, server_side=True)
    base = f"https://{hostname}:{frontend.server_port}"
    candidate = phase2_candidate(tmp_path)
    stable_manifest = stable.generate(candidate, data, base)
    development.initialize(data / "development-data", base, candidate)
    manifests = {
        "stable": stable_manifest,
        "development": development.load_current(data / "development-data")[1],
    }
    backend_handler.expected_base_url = base
    threads = [
        threading.Thread(target=server.serve_forever, daemon=True)
        for server in (backend, frontend)
    ]
    for thread in threads:
        thread.start()
    try:
        yield SimpleNamespace(
            base=base,
            cert=cert,
            manifests=manifests,
            seen=seen,
            handler=Proxy,
            data=data,
        )
    finally:
        for server in (frontend, backend):
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=2)


def test_real_https_requires_trust_and_checks_all_downloads(
    proxy: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ValueError, match="verification failed"):
        install.probe_repository(proxy.base, proxy.manifests)
    assert proxy.seen == []
    monkeypatch.setenv("SSL_CERT_FILE", str(proxy.cert))
    install.probe_repository(proxy.base, proxy.manifests)
    assert (
        "/stable/zips/service.kodi_lab_bridge/service.kodi_lab_bridge-1.1.31.zip"
        in proxy.seen
    )
    for feed in ("stable", "development"):
        assert f"/{feed}/addons.xml" in proxy.seen
        assert f"/{feed}/addons.xml.sha256" in proxy.seen
    proxy.seen.clear()
    wrong_host = proxy.base.replace("localhost", "127.0.0.1")
    wrong_manifests = {
        name: {**value, "base_url": wrong_host}
        for name, value in proxy.manifests.items()
    }
    with pytest.raises(ValueError, match="verification failed"):
        install.probe_repository(wrong_host, wrong_manifests)
    assert proxy.seen == []


@pytest.mark.parametrize("proxy", [True], indirect=True)
def test_expired_certificate_is_rejected(
    proxy: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SSL_CERT_FILE", str(proxy.cert))
    with pytest.raises(ValueError, match="verification failed"):
        install.probe_repository(proxy.base, proxy.manifests)
    assert proxy.seen == []


def test_approved_development_package_downloads_through_https(
    proxy: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SSL_CERT_FILE", str(proxy.cert))
    engine = create_database_engine(f"sqlite:///{tmp_path / 'development.db'}")
    try:
        metadata.create_all(engine)
        service = development.DevelopmentRepositoryService(
            create_session_factory(engine), proxy.data, proxy.base
        )
        service.reconcile()
        content = fixture_zip("1.0.0")
        prepared = service.prepare_import(content, ("matrix", "nexus", "omega"))
        service.approve(UUID(prepared.id), observed_sha256=stable.sha256(content))
        proxy.manifests["development"] = development.load_current(service.root)[1]
        install.probe_repository(proxy.base, proxy.manifests)
        assert (
            "/development/zips/plugin.video.kodi-lab-phase2-synthetic/"
            "plugin.video.kodi-lab-phase2-synthetic-1.0.0.zip"
        ) in proxy.seen
    finally:
        engine.dispose()


@pytest.mark.parametrize("fault", ["redirect", "missing", "tampered"])
@pytest.mark.parametrize(
    "path",
    [
        "/stable/addons.xml",
        "/development/addons.xml.sha256",
        "/install/repository.kodi-lab-dev-1.0.0.zip",
        "/stable/zips/service.kodi_lab_bridge/service.kodi_lab_bridge-1.1.31.zip",
    ],
)
def test_proxy_failures_never_follow_redirects_or_accept_wrong_content(
    proxy: Any, monkeypatch: pytest.MonkeyPatch, fault: str, path: str
) -> None:
    monkeypatch.setenv("SSL_CERT_FILE", str(proxy.cert))
    proxy.handler.fault, proxy.handler.fault_path = fault, path
    with pytest.raises(ValueError, match="verification failed"):
        install.probe_repository(proxy.base, proxy.manifests)
    assert proxy.seen[-1] == path


@pytest.mark.parametrize(
    "major", [19, 20, 21, 22], ids=["matrix", "nexus", "omega", "piers"]
)
@pytest.mark.parametrize(
    "origin", ["http://192.168.50.10:18120", "https://repo.example.test"]
)
def test_repository_bootstraps_use_kodi_19_plus_protocol(
    major: int, origin: str
) -> None:
    for module, feed, addon_id in (
        (stable, "stable", "repository.kodi-lab"),
        (development, "development", "repository.kodi-lab-dev"),
    ):
        with zipfile.ZipFile(io.BytesIO(module.bootstrap_zip(origin))) as archive:
            addon = ET.fromstring(archive.read(addon_id + "/addon.xml"))
        directory = addon.find("extension/dir")
        assert directory is not None
        assert int(directory.attrib["minversion"].split(".")[0]) <= major
        assert directory.findtext("info") == origin + f"/{feed}/addons.xml"
        assert directory.findtext("checksum") == origin + f"/{feed}/addons.xml.sha256"
        assert directory.find("checksum").attrib["verify"] == "sha256"  # type: ignore[union-attr]
        assert directory.findtext("datadir") == origin + f"/{feed}/zips/"
        assert directory.findtext("hashes") == "sha256"
