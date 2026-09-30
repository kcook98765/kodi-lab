"""Read-only, narrow Kodi repository HTTP server with no application state mount."""

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

from kodi_lab.host_bootstrap import HostBootstrapError
from kodi_lab.repository.development import (
    VERSION as DEVELOPMENT_VERSION,
)
from kodi_lab.repository.development import (
    initialize as initialize_development,
)
from kodi_lab.repository.development import (
    load_current as load_development_current,
)
from kodi_lab.repository.policy import DEVELOPMENT_REPOSITORY_ID
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
DEVELOPMENT_PACKAGE_PATH = re.compile(
    r"/development/zips/([a-zA-Z0-9_.-]+)/([a-zA-Z0-9_.-]+)\.zip(\.sha256)?\Z"
)
BYTE_RANGE = re.compile(r"(\d*)-(\d*)\Z")
MAX_RANGE_HEADER_LENGTH = 128
WRITE_CHUNK_BYTES = 64 * 1024


class RangeSyntaxError(ValueError):
    """The request's Range header is malformed or unsupported."""


class RangeNotSatisfiable(ValueError):
    """A valid single byte range selects no bytes from this resource."""


def single_byte_range(header: str, total: int) -> tuple[int, int] | None:
    """Return an inclusive byte interval; ignore valid multi-range requests.

    Kodi uses a single interval. Returning the full representation for a valid
    multi-range request is permitted by HTTP and avoids multipart responses.
    Limit header length before parsing integers to bound hostile input.
    """
    if len(header) > MAX_RANGE_HEADER_LENGTH:
        raise RangeSyntaxError("invalid byte range")
    unit, separator, specifier = header.strip().partition("=")
    if not separator:
        raise RangeSyntaxError("invalid range")
    if unit.lower() != "bytes":
        # An origin server must ignore a range unit it does not understand.
        return None
    ranges = specifier.split(",")
    parsed: list[tuple[str, str]] = []
    for item in ranges:
        match = BYTE_RANGE.fullmatch(item.strip())
        if match is None or not any(match.groups()):
            raise RangeSyntaxError("invalid byte range")
        first, last = match.groups()
        if first and last and int(first) > int(last):
            raise RangeSyntaxError("range end precedes start")
        parsed.append((first, last))
    if len(parsed) > 1:
        return None
    first, last = parsed[0]
    if total == 0:
        raise RangeNotSatisfiable("empty resource")
    if first:
        start = int(first)
        if start >= total:
            raise RangeNotSatisfiable("range starts after resource")
        return start, min(int(last), total - 1) if last else total - 1
    suffix_length = int(last)
    if suffix_length == 0:
        raise RangeNotSatisfiable("empty suffix")
    return max(0, total - suffix_length), total - 1


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
        if (self.root / "TRANSITION.json").exists() or (
            self.root / "TRANSITION.json"
        ).is_symlink():
            self.send_error(HTTPStatus.SERVICE_UNAVAILABLE)
            return
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
                dev_name = f"{DEVELOPMENT_REPOSITORY_ID}-{DEVELOPMENT_VERSION}.zip"
                try:
                    load_development_current(
                        self.root / "development-data",
                        expected_base_url=self.expected_base_url,
                    )
                    dev_link = (
                        "<li>Kodi Lab Development (optional; "
                        "development/test Kodi only): "
                        f'<a href="{html.escape(dev_name, quote=True)}">'
                        f"{html.escape(dev_name)}</a></li>"
                    )
                except (OSError, ValueError, KeyError, json.JSONDecodeError):
                    dev_link = ""
                content = (
                    "<!doctype html><html><head><meta charset='utf-8'>"
                    "<title>Kodi Lab</title></head>"
                    '<body><h1>Kodi Lab repository</h1><ul><li><a href="'
                    + html.escape(name, quote=True)
                    + '">'
                    + html.escape(name)
                    + "</a></li>"
                    + dev_link
                    + "</ul></body></html>\n"
                ).encode()
                content_type = "text/html; charset=utf-8"
            elif (
                path.startswith("/development/")
                or path
                == f"/install/{DEVELOPMENT_REPOSITORY_ID}-{DEVELOPMENT_VERSION}.zip"
            ):
                dev_root = self.root / "development-data"
                dev_generation, dev_provenance = load_development_current(
                    dev_root, expected_base_url=self.expected_base_url
                )
                if path == "/development/health":
                    content = b"ok\n"
                    content_type = "text/plain; charset=utf-8"
                elif (
                    path
                    == f"/install/{DEVELOPMENT_REPOSITORY_ID}-{DEVELOPMENT_VERSION}.zip"
                ):
                    content = (
                        dev_generation
                        / "install"
                        / f"{DEVELOPMENT_REPOSITORY_ID}-{DEVELOPMENT_VERSION}.zip"
                    ).read_bytes()
                    content_type = "application/zip"
                elif path == "/development/addons.xml":
                    content = (dev_generation / "development/addons.xml").read_bytes()
                    content_type = "application/xml; charset=utf-8"
                elif path == "/development/addons.xml.sha256":
                    content = (
                        dev_generation / "development/addons.xml.sha256"
                    ).read_bytes()
                    content_type = "text/plain; charset=utf-8"
                else:
                    dev_match = DEVELOPMENT_PACKAGE_PATH.fullmatch(path)
                    if dev_match is None:
                        self.send_error(HTTPStatus.NOT_FOUND)
                        return
                    addon_id, name, sidecar = dev_match.groups()
                    record = next(
                        (
                            item
                            for item in cast(
                                list[dict[str, str]], dev_provenance["packages"]
                            )
                            if item["id"] == addon_id
                            and name == f"{addon_id}-{item['version']}"
                        ),
                        None,
                    )
                    if record is None:
                        self.send_error(HTTPStatus.NOT_FOUND)
                        return
                    package = dev_root / "packages" / addon_id / f"{name}.zip"
                    content = (
                        package.with_name(package.name + ".sha256")
                        if sidecar
                        else package
                    ).read_bytes()
                    if (
                        content.decode().strip() if sidecar else sha256(content)
                    ) != record["sha256"]:
                        raise ValueError("development package checksum differs")
                    content_type = (
                        "text/plain; charset=utf-8" if sidecar else "application/zip"
                    )
                    if not sidecar:
                        content_sha256 = base64.b64encode(
                            hashlib.sha256(content).digest()
                        ).decode()
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
            selected: tuple[int, int] | None = None
            if send_body:
                range_headers = self.headers.get_all("Range", [])
                try:
                    if len(range_headers) > 1:
                        raise RangeSyntaxError("multiple Range headers")
                    # No strong validator is emitted, so If-Range cannot match.
                    if range_headers and self.headers.get("If-Range") is None:
                        selected = single_byte_range(range_headers[0], len(content))
                except RangeSyntaxError:
                    self._range_error(HTTPStatus.BAD_REQUEST)
                    return
                except RangeNotSatisfiable:
                    self._range_error(
                        HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE,
                        total=len(content),
                    )
                    return
            self.send_response(
                HTTPStatus.PARTIAL_CONTENT if selected is not None else HTTPStatus.OK
            )
            self.send_header("Content-Type", content_type)
            self.send_header("Accept-Ranges", "bytes")
            if selected is not None:
                start, end = selected
                self.send_header("Content-Range", f"bytes {start}-{end}/{len(content)}")
                self.send_header("Content-Length", str(end - start + 1))
            else:
                start, end = 0, len(content) - 1
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
                view = memoryview(content)
                for offset in range(start, end + 1, WRITE_CHUNK_BYTES):
                    self.wfile.write(
                        view[offset : min(offset + WRITE_CHUNK_BYTES, end + 1)]
                    )
        except (OSError, ValueError, KeyError, StopIteration, json.JSONDecodeError):
            self.send_error(HTTPStatus.SERVICE_UNAVAILABLE)

    def _range_error(self, status: HTTPStatus, *, total: int | None = None) -> None:
        self.send_response(status)
        self.send_header("Accept-Ranges", "bytes")
        if total is not None:
            self.send_header("Content-Range", f"bytes */{total}")
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        # Paths can contain credentials in malformed requests. Log only status and peer.
        pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command", choices=("generate", "serve", "verify", "transition")
    )
    parser.add_argument(
        "--data-root", type=Path, default=Path("/srv/kodi-lab-repository")
    )
    parser.add_argument("--bundle", type=Path, default=Path("/candidate"))
    parser.add_argument(
        "--base-url", default=os.getenv("KODI_LAB_REPOSITORY_BASE_URL", "")
    )
    parser.add_argument("--port", type=int, default=8110)
    parser.add_argument("--previous-bundle", type=Path)
    parser.add_argument("--expected-old-bundle")
    parser.add_argument("--expected-new-bundle")
    parser.add_argument("--backup-id")
    parser.add_argument("--state-root", type=Path, default=Path("/var/lib/kodi-lab"))
    parser.add_argument("--rollback", action="store_true")
    args = parser.parse_args()
    try:
        if args.command != "transition" and (
            (args.data_root / "TRANSITION.json").exists()
            or (args.data_root / "TRANSITION.json").is_symlink()
        ):
            raise ValueError("repository candidate transition is incomplete")
        if args.command == "generate":
            result = generate(args.bundle, args.data_root, args.base_url)
            development = initialize_development(
                args.data_root / "development-data", args.base_url, args.bundle
            )
            # Stable files remain root-owned. Only the development subtree is writable
            # by the authenticated Web process; the HTTP process mounts it read-only.
            if os.geteuid() == 0:
                dev_root = args.data_root / "development-data"
                for path in dev_root.rglob("*"):
                    os.chown(path, 10001, 10001, follow_symlinks=False)
                for path in (
                    dev_root,
                    dev_root / "generations",
                    dev_root / "packages",
                    dev_root / "pending",
                ):
                    os.chown(path, 10001, 10001)
            print(
                json.dumps(
                    {"stable": result, "development": development}, sort_keys=True
                )
            )
        elif args.command == "verify":
            _, result = load_current(
                args.data_root,
                expected_base_url=args.base_url or None,
                expected_bundle_sha256=os.getenv("KODI_LAB_REPOSITORY_BUNDLE_SHA256")
                or None,
            )
            _, dev_result = load_development_current(
                args.data_root / "development-data",
                expected_base_url=args.base_url or None,
            )
            print(
                json.dumps(
                    {"stable": result, "development": dev_result}, sort_keys=True
                )
            )
        elif args.command == "transition":
            from kodi_lab.repository.transition import transition

            if not all(
                (
                    args.previous_bundle,
                    args.expected_old_bundle,
                    args.expected_new_bundle,
                    args.backup_id,
                )
            ):
                raise ValueError(
                    "transition requires both candidates, exact hashes and backup ID"
                )
            configured = os.getenv("KODI_LAB_REPOSITORY_BUNDLE_SHA256")
            if configured and configured != (
                args.expected_old_bundle if args.rollback else args.expected_new_bundle
            ):
                raise ValueError("transition differs from configured candidate")
            result = transition(
                args.previous_bundle,
                args.bundle,
                args.data_root,
                args.state_root,
                args.base_url,
                args.expected_old_bundle,
                args.expected_new_bundle,
                args.backup_id,
                rollback=args.rollback,
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
    except (
        OSError,
        ValueError,
        KeyError,
        json.JSONDecodeError,
        HostBootstrapError,
    ) as error:
        parser.exit(1, f"repository {args.command} failed: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
