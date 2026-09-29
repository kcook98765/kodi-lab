"""Read-only, narrow Kodi repository HTTP server; no application imports."""

from __future__ import annotations

import argparse
import base64
import hashlib
import html
import json
import os
import re
import socket
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

from kodi_lab.repository.stable import (
    BRIDGE_ID,
    BRIDGE_VERSION,
    REPOSITORY_ID,
    REPOSITORY_VERSION,
    generate,
    load_current,
    sha256,
)

PACKAGE_PATH = re.compile(
    r"/stable/zips/([a-zA-Z0-9_.-]+)/([a-zA-Z0-9_.-]+)\.zip(\.sha256)?\Z"
)


class BoundedRepositoryServer(ThreadingHTTPServer):
    """Bound active clients and idle sockets on the LAN distribution surface."""

    request_queue_size = 16
    daemon_threads = True

    def __init__(
        self, address: tuple[str, int], handler: type[BaseHTTPRequestHandler]
    ) -> None:
        self._slots = threading.BoundedSemaphore(16)
        super().__init__(address, handler)

    def process_request(
        self,
        request: socket.socket | tuple[bytes, socket.socket],
        client_address: tuple[str, int],
    ) -> None:
        if not isinstance(request, socket.socket):
            raise TypeError("HTTP request must be a socket")
        request.settimeout(10)
        if not self._slots.acquire(blocking=False):
            try:
                request.sendall(
                    b"HTTP/1.1 503 Service Unavailable\r\n"
                    b"Content-Length: 0\r\nConnection: close\r\n\r\n"
                )
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            raise

    def process_request_thread(
        self,
        request: socket.socket | tuple[bytes, socket.socket],
        client_address: tuple[str, int],
    ) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


class RepositoryHandler(BaseHTTPRequestHandler):
    """Serve only exact distribution routes and an intended install listing."""

    root: Path
    expected_base_url: str | None = None
    expected_bundle_sha256: str | None = None
    server_version = "KodiLabRepository/1"
    sys_version = ""

    def do_GET(self) -> None:
        self._serve(send_body=True)

    def do_HEAD(self) -> None:
        self._serve(send_body=False)

    def do_POST(self) -> None:
        self.send_error(HTTPStatus.METHOD_NOT_ALLOWED)

    do_PUT = do_PATCH = do_DELETE = do_POST

    def _serve(self, *, send_body: bool) -> None:
        parsed = urlsplit(self.path)
        path = parsed.path
        if (
            "%" in path
            or "\\" in path
            or ".." in path
            or parsed.query
            or parsed.fragment
        ):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            generation, provenance = load_current(
                self.root,
                expected_base_url=self.expected_base_url,
                expected_bundle_sha256=self.expected_bundle_sha256,
            )
            content_type = "application/octet-stream"
            content_sha256: str | None = None
            if path == "/health":
                content = b"ok\n"
                content_type = "text/plain; charset=utf-8"
            elif path in {"/install", "/install/"}:
                name = f"{REPOSITORY_ID}-{REPOSITORY_VERSION}.zip"
                content = (
                    "<!doctype html><html><head><meta charset='utf-8'>"
                    "<title>Kodi Lab</title></head>"
                    '<body><h1>Kodi Lab repository</h1><ul><li><a href="'
                    + html.escape(name, quote=True)
                    + '">'
                    + html.escape(name)
                    + "</a></li></ul></body></html>\n"
                ).encode()
                content_type = "text/html; charset=utf-8"
            elif path == f"/install/{REPOSITORY_ID}-{REPOSITORY_VERSION}.zip":
                content = (
                    generation / "install" / f"{REPOSITORY_ID}-{REPOSITORY_VERSION}.zip"
                ).read_bytes()
                content_type = "application/zip"
            elif path == "/stable/addons.xml":
                content = (generation / "stable/addons.xml").read_bytes()
                content_type = "application/xml; charset=utf-8"
            elif path == "/stable/addons.xml.sha256":
                content = (generation / "stable/addons.xml.sha256").read_bytes()
                content_type = "text/plain; charset=utf-8"
            else:
                match = PACKAGE_PATH.fullmatch(path)
                if match is None:
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                addon_id, name, sidecar = match.groups()
                expected_name = {
                    REPOSITORY_ID: f"{REPOSITORY_ID}-{REPOSITORY_VERSION}",
                    BRIDGE_ID: f"{BRIDGE_ID}-{BRIDGE_VERSION}",
                }.get(addon_id)
                if name != expected_name:
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                package = self.root / "packages" / addon_id / f"{name}.zip"
                content = (
                    package.with_name(package.name + ".sha256") if sidecar else package
                ).read_bytes()
                content_type = (
                    "text/plain; charset=utf-8" if sidecar else "application/zip"
                )
                expected = next(
                    item["sha256"]
                    for item in cast(list[dict[str, str]], provenance["packages"])
                    if item["id"] == addon_id
                )
                if (
                    content.decode().strip() if sidecar else sha256(content)
                ) != expected:
                    raise ValueError("repository package checksum differs")
                if not sidecar:
                    content_sha256 = base64.b64encode(
                        hashlib.sha256(content).digest()
                    ).decode()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            if content_sha256 is not None:
                self.send_header("Content-SHA256", content_sha256)
            self.send_header(
                "Cache-Control",
                "no-store"
                if path.endswith(("addons.xml", ".sha256"))
                else "public, max-age=300",
            )
            self.end_headers()
            if send_body:
                self.wfile.write(content)
        except (OSError, ValueError, KeyError, StopIteration, json.JSONDecodeError):
            self.send_error(HTTPStatus.SERVICE_UNAVAILABLE)

    def log_message(self, format: str, *args: object) -> None:
        # Paths can contain credentials in malformed requests. Log only status and peer.
        pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("generate", "serve", "verify"))
    parser.add_argument(
        "--data-root", type=Path, default=Path("/srv/kodi-lab-repository")
    )
    parser.add_argument("--bundle", type=Path, default=Path("/candidate"))
    parser.add_argument(
        "--base-url", default=os.getenv("KODI_LAB_REPOSITORY_BASE_URL", "")
    )
    parser.add_argument("--port", type=int, default=8110)
    args = parser.parse_args()
    try:
        if args.command == "generate":
            result = generate(args.bundle, args.data_root, args.base_url)
            print(json.dumps(result, sort_keys=True))
        elif args.command == "verify":
            _, result = load_current(
                args.data_root,
                expected_base_url=args.base_url or None,
                expected_bundle_sha256=os.getenv("KODI_LAB_REPOSITORY_BUNDLE_SHA256")
                or None,
            )
            print(json.dumps(result, sort_keys=True))
        else:
            load_current(
                args.data_root,
                expected_base_url=args.base_url,
                expected_bundle_sha256=os.environ["KODI_LAB_REPOSITORY_BUNDLE_SHA256"],
            )
            handler = type(
                "InstalledRepositoryHandler",
                (RepositoryHandler,),
                {
                    "root": args.data_root,
                    "expected_base_url": args.base_url,
                    "expected_bundle_sha256": os.environ[
                        "KODI_LAB_REPOSITORY_BUNDLE_SHA256"
                    ],
                },
            )
            server = BoundedRepositoryServer(("0.0.0.0", args.port), handler)
            server.serve_forever()
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        parser.exit(1, f"repository {args.command} failed: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
