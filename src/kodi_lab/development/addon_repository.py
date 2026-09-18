"""Pinned-address HTTPS transport and bounded Kodi repository metadata parsing."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import socket
import ssl
import unicodedata
import xml.etree.ElementTree as ET
import zlib
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import quote, unquote_to_bytes, urljoin, urlsplit, urlunsplit

from kodi_lab.development.sources import (
    DevelopmentSourceValidationError,
    copied_video_plugin_library,
    parse_captured_addon_xml,
)
from kodi_lab.domain import AddonZipDependency
from kodi_lab.domain.addon_repositories import (
    AddonRepositoryCandidateClassification,
    AddonRepositoryCandidateSupport,
)

MAX_URL_PATH_BYTES = 2048
MAX_TRANSFER_BYTES = 8 * 1024 * 1024
MAX_EXPANDED_BYTES = 32 * 1024 * 1024
MAX_HEADERS_BYTES = 32 * 1024
MAX_DNS_ANSWERS = 8
MAX_REDIRECTS = 3
MAX_CANDIDATES = 2048
MAX_XML_DEPTH = 32
MAX_AGGREGATE_TEXT = 8 * 1024 * 1024
DNS_DEADLINE_SECONDS = 3.0
CONNECT_TIMEOUT_SECONDS = 10.0
_INDEX_SUFFIXES = ("/addons.xml", "/addons.xml.gz")
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9._~-]+$")
_SAFE_PACKAGE_VALUE = re.compile(r"^[a-z0-9][a-z0-9._~-]{0,127}$")
_XML_TYPES = frozenset({"application/xml", "text/xml"})
_GZIP_TYPES = frozenset({"application/gzip", "application/x-gzip"})


class AddonRepositoryError(ValueError):
    """One stable private repository failure with a safe public code."""

    def __init__(self, code: str, safe_message: str) -> None:
        self.code = code
        self.safe_message = safe_message
        super().__init__(safe_message)


@dataclass(frozen=True, slots=True)
class CanonicalRepositoryUrl:
    url: str
    hostname: str
    path: str

    @property
    def index_directory(self) -> str:
        return self.path.rsplit("/", 1)[0] + "/"


@dataclass(frozen=True, slots=True)
class RepositoryHttpResponse:
    status: int
    content_type: str
    body: bytes
    final_url: str


@dataclass(frozen=True, slots=True)
class ParsedRepositoryCandidate:
    addon_id: str
    name: str
    version: str
    classification: AddonRepositoryCandidateClassification
    dependencies: tuple[AddonZipDependency, ...]
    support: AddonRepositoryCandidateSupport
    unsupported_reason: str | None


@dataclass(frozen=True, slots=True)
class ParsedRepositoryMetadata:
    transfer_digest: str
    canonical_metadata_digest: str
    transfer_size: int
    expanded_size: int
    candidates: tuple[ParsedRepositoryCandidate, ...]


def _reject_controls(value: str, *, code: str) -> None:
    if any(unicodedata.category(character) == "Cc" for character in value):
        raise AddonRepositoryError(code, "Repository URL contains unsafe characters.")


def _canonicalize_https_url(
    value: str, *, require_index: bool
) -> CanonicalRepositoryUrl:
    _reject_controls(value, code="repository_url_unsafe")
    try:
        parsed = urlsplit(value)
    except ValueError as error:
        raise AddonRepositoryError(
            "repository_url_invalid", "Repository URL is invalid."
        ) from error
    if parsed.scheme.lower() != "https":
        raise AddonRepositoryError(
            "repository_https_required", "Repository URL must use HTTPS."
        )
    if parsed.username is not None or parsed.password is not None:
        raise AddonRepositoryError(
            "repository_credentials_forbidden", "Repository credentials are forbidden."
        )
    if parsed.query or parsed.fragment:
        raise AddonRepositoryError(
            "repository_url_components_forbidden",
            "Repository query strings and fragments are forbidden.",
        )
    try:
        port = parsed.port
    except ValueError as error:
        raise AddonRepositoryError(
            "repository_port_forbidden", "Repository URL must use port 443."
        ) from error
    if port not in {None, 443}:
        raise AddonRepositoryError(
            "repository_port_forbidden", "Repository URL must use port 443."
        )
    hostname = parsed.hostname
    if hostname is None or not hostname:
        raise AddonRepositoryError(
            "repository_hostname_required", "Repository URL needs a DNS hostname."
        )
    try:
        ipaddress.ip_address(hostname.strip("[]"))
    except ValueError:
        pass
    else:
        raise AddonRepositoryError(
            "repository_ip_literal_forbidden", "Repository IP literals are forbidden."
        )
    try:
        ascii_hostname = hostname.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError as error:
        raise AddonRepositoryError(
            "repository_hostname_invalid", "Repository hostname is invalid."
        ) from error
    labels = ascii_hostname.split(".")
    if (
        not ascii_hostname
        or len(ascii_hostname) > 253
        or any(
            not label
            or len(label) > 63
            or label.startswith("-")
            or label.endswith("-")
            or not re.fullmatch(r"[a-z0-9-]+", label)
            for label in labels
        )
    ):
        raise AddonRepositoryError(
            "repository_hostname_invalid", "Repository hostname is invalid."
        )
    path = parsed.path
    if not path.startswith("/") or len(path.encode("utf-8")) > MAX_URL_PATH_BYTES:
        raise AddonRepositoryError(
            "repository_path_invalid", "Repository URL path is invalid."
        )
    lower_path = path.lower()
    if "%2f" in lower_path or "%5c" in lower_path:
        raise AddonRepositoryError(
            "repository_encoded_separator", "Encoded path separators are forbidden."
        )
    try:
        decoded = unquote_to_bytes(path).decode("utf-8", "strict")
    except UnicodeDecodeError as error:
        raise AddonRepositoryError(
            "repository_path_invalid", "Repository URL path is invalid."
        ) from error
    _reject_controls(decoded, code="repository_path_invalid")
    components = decoded.split("/")[1:]
    if any(
        not component
        or component in {".", ".."}
        or not _SAFE_SEGMENT.fullmatch(component)
        for component in components
    ):
        raise AddonRepositoryError(
            "repository_path_invalid", "Repository URL path is invalid."
        )
    canonical_path = "/" + "/".join(quote(item, safe="._~-") for item in components)
    if require_index and not canonical_path.endswith(_INDEX_SUFFIXES):
        raise AddonRepositoryError(
            "repository_index_suffix_required",
            "Repository URL must end in addons.xml or addons.xml.gz.",
        )
    canonical = urlunsplit(("https", ascii_hostname, canonical_path, "", ""))
    return CanonicalRepositoryUrl(canonical, ascii_hostname, canonical_path)


def canonicalize_repository_url(value: str) -> CanonicalRepositoryUrl:
    """Validate and canonicalize one direct HTTPS addons.xml endpoint."""

    return _canonicalize_https_url(value, require_index=True)


_BLOCKED_V4 = tuple(
    ipaddress.ip_network(value)
    for value in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "168.63.129.16/32",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.0.0.0/24",
        "192.0.2.0/24",
        "192.88.99.0/24",
        "192.168.0.0/16",
        "198.18.0.0/15",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "224.0.0.0/4",
        "240.0.0.0/4",
        "255.255.255.255/32",
    )
)
_BLOCKED_V6 = tuple(
    ipaddress.ip_network(value)
    for value in (
        "::/128",
        "::1/128",
        "::ffff:0:0/96",
        "64:ff9b::/96",
        "100::/64",
        "2001::/32",
        "2001:2::/48",
        "2001:db8::/32",
        "2001:10::/28",
        "fc00::/7",
        "fe80::/10",
        "fec0::/10",
        "ff00::/8",
    )
)


def is_explicit_global_unicast(
    value: str | ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> bool:
    """Return true only for explicitly admitted ordinary global-unicast addresses."""

    try:
        address = (
            value
            if isinstance(value, (ipaddress.IPv4Address, ipaddress.IPv6Address))
            else ipaddress.ip_address(value)
        )
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return is_explicit_global_unicast(address.ipv4_mapped)
    blocked = _BLOCKED_V4 if isinstance(address, ipaddress.IPv4Address) else _BLOCKED_V6
    if any(address in network for network in blocked):
        return False
    return not (
        address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_unspecified
        or address.is_reserved
        or address.is_private
    )


class _RepositorySocket(Protocol):
    def settimeout(self, value: float) -> None: ...
    def sendall(self, value: bytes) -> None: ...
    def recv(self, count: int) -> bytes: ...
    def close(self) -> None: ...


type Resolver = Callable[[str], tuple[str, ...]]
type Connector = Callable[[str, str, float], _RepositorySocket]


def resolve_public_addresses(hostname: str) -> tuple[str, ...]:
    """Resolve one hostname under a fixed deadline and reject every unsafe set."""

    def work() -> tuple[str, ...]:
        answers = socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
        result = tuple(dict.fromkeys(str(item[4][0]) for item in answers))
        return result

    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="repo-dns")
    future = executor.submit(work)
    try:
        answers = future.result(timeout=DNS_DEADLINE_SECONDS)
    except FutureTimeoutError as error:
        future.cancel()
        raise AddonRepositoryError(
            "repository_dns_timeout", "Repository DNS resolution timed out."
        ) from error
    except OSError as error:
        raise AddonRepositoryError(
            "repository_dns_failed", "Repository DNS resolution failed."
        ) from error
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
    return validate_dns_answers(answers)


def validate_dns_answers(answers: Iterable[str]) -> tuple[str, ...]:
    unique = tuple(dict.fromkeys(answers))
    if not unique or len(unique) > MAX_DNS_ANSWERS:
        raise AddonRepositoryError(
            "repository_dns_answer_count", "Repository DNS answer set is invalid."
        )
    normalized: list[str] = []
    for value in unique:
        try:
            address = ipaddress.ip_address(value)
        except ValueError as error:
            raise AddonRepositoryError(
                "repository_dns_answer_invalid", "Repository DNS answer is invalid."
            ) from error
        if not is_explicit_global_unicast(address):
            raise AddonRepositoryError(
                "repository_dns_non_public",
                "Repository DNS includes a non-public address.",
            )
        normalized.append(address.compressed)
    return tuple(normalized)


def _default_connector(
    address: str, hostname: str, timeout: float
) -> _RepositorySocket:
    raw = socket.create_connection((address, 443), timeout=timeout)
    context = ssl.create_default_context()
    try:
        return context.wrap_socket(raw, server_hostname=hostname)
    except BaseException:
        raw.close()
        raise


class PinnedHttpsTransport:
    """Minimal HTTPS GET transport; it never consults ambient proxy settings."""

    def __init__(
        self,
        *,
        resolver: Resolver = resolve_public_addresses,
        connector: Connector = _default_connector,
        timeout: float = CONNECT_TIMEOUT_SECONDS,
    ) -> None:
        self._resolver = resolver
        self._connector = connector
        self._timeout = timeout

    def get(
        self,
        url: str,
        *,
        maximum_body: int = MAX_TRANSFER_BYTES,
        require_index: bool = True,
    ) -> RepositoryHttpResponse:
        current = _canonicalize_https_url(url, require_index=require_index)
        registered = current
        for redirect_count in range(MAX_REDIRECTS + 1):
            addresses = validate_dns_answers(self._resolver(current.hostname))
            response = self._get_once(current, addresses[0], maximum_body)
            if response.status not in {301, 302, 303, 307, 308}:
                return RepositoryHttpResponse(
                    response.status,
                    response.content_type,
                    response.body,
                    current.url,
                )
            if redirect_count == MAX_REDIRECTS:
                raise AddonRepositoryError(
                    "repository_redirect_limit", "Repository redirect limit exceeded."
                )
            location = response.location
            if location is None:
                raise AddonRepositoryError(
                    "repository_redirect_invalid", "Repository redirect is invalid."
                )
            try:
                next_url = _canonicalize_https_url(
                    urljoin(current.url, location), require_index=require_index
                )
            except AddonRepositoryError as error:
                raise AddonRepositoryError(
                    "repository_redirect_invalid", "Repository redirect is invalid."
                ) from error
            if next_url.hostname != registered.hostname:
                raise AddonRepositoryError(
                    "repository_redirect_cross_origin",
                    "Cross-origin repository redirects are forbidden.",
                )
            current = next_url
        raise AssertionError("unreachable")

    @dataclass(frozen=True, slots=True)
    class _Once:
        status: int
        content_type: str
        body: bytes
        location: str | None

    def _get_once(
        self, target: CanonicalRepositoryUrl, address: str, maximum_body: int
    ) -> _Once:
        try:
            stream = self._connector(address, target.hostname, self._timeout)
            try:
                stream.settimeout(self._timeout)
                request = (
                    f"GET {target.path} HTTP/1.1\r\n"
                    f"Host: {target.hostname}\r\n"
                    "Accept: application/xml, text/xml, application/gzip, "
                    "application/x-gzip, application/zip\r\n"
                    "Accept-Encoding: identity\r\n"
                    "User-Agent: kodi-lab-repository/1\r\n"
                    "Connection: close\r\n\r\n"
                ).encode("ascii")
                stream.sendall(request)
                header, remainder = self._read_header(stream)
                status, headers = self._parse_header(header)
                body = self._read_body(stream, headers, remainder, maximum_body)
            finally:
                stream.close()
        except AddonRepositoryError:
            raise
        except (OSError, ssl.SSLError) as error:
            raise AddonRepositoryError(
                "repository_transport_failed", "Repository HTTPS request failed."
            ) from error
        content_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()
        return self._Once(status, content_type, body, headers.get("location"))

    @staticmethod
    def _read_header(stream: _RepositorySocket) -> tuple[bytes, bytes]:
        value = bytearray()
        while b"\r\n\r\n" not in value:
            chunk = stream.recv(min(4096, MAX_HEADERS_BYTES + 1 - len(value)))
            if not chunk:
                raise AddonRepositoryError(
                    "repository_headers_invalid",
                    "Repository response headers are invalid.",
                )
            value.extend(chunk)
            if len(value) > MAX_HEADERS_BYTES:
                raise AddonRepositoryError(
                    "repository_headers_too_large",
                    "Repository response headers are too large.",
                )
        header, body = bytes(value).split(b"\r\n\r\n", 1)
        return header, body

    @staticmethod
    def _parse_header(value: bytes) -> tuple[int, dict[str, str]]:
        try:
            lines = value.decode("iso-8859-1").split("\r\n")
            version, status_text, _reason = lines[0].split(" ", 2)
            status = int(status_text)
        except (UnicodeDecodeError, ValueError) as error:
            raise AddonRepositoryError(
                "repository_headers_invalid", "Repository response headers are invalid."
            ) from error
        if version not in {"HTTP/1.0", "HTTP/1.1"} or not 100 <= status <= 599:
            raise AddonRepositoryError(
                "repository_headers_invalid", "Repository response headers are invalid."
            )
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if not line or line[0] in " \t" or ":" not in line:
                raise AddonRepositoryError(
                    "repository_headers_invalid",
                    "Repository response headers are invalid.",
                )
            name, raw = line.split(":", 1)
            lowered = name.lower()
            if lowered in headers:
                raise AddonRepositoryError(
                    "repository_headers_invalid",
                    "Duplicate response headers are forbidden.",
                )
            headers[lowered] = raw.strip()
        return status, headers

    @staticmethod
    def _read_body(
        stream: _RepositorySocket,
        headers: dict[str, str],
        initial: bytes,
        maximum: int,
    ) -> bytes:
        if headers.get("content-encoding", "identity").lower() not in {"", "identity"}:
            raise AddonRepositoryError(
                "repository_content_encoding",
                "Automatic HTTP decompression is forbidden.",
            )
        if headers.get("transfer-encoding", "").lower() not in {"", "identity"}:
            raise AddonRepositoryError(
                "repository_transfer_encoding",
                "Repository transfer encoding is unsupported.",
            )
        length_text = headers.get("content-length")
        if length_text is not None:
            try:
                length = int(length_text)
            except ValueError as error:
                raise AddonRepositoryError(
                    "repository_body_invalid", "Repository response length is invalid."
                ) from error
            if length < 0 or length > maximum:
                raise AddonRepositoryError(
                    "repository_body_too_large", "Repository response is too large."
                )
            value = bytearray(initial)
            while len(value) < length:
                chunk = stream.recv(min(65_536, length - len(value)))
                if not chunk:
                    raise AddonRepositoryError(
                        "repository_body_incomplete",
                        "Repository response is incomplete.",
                    )
                value.extend(chunk)
            if len(value) != length:
                raise AddonRepositoryError(
                    "repository_body_invalid", "Repository response length is invalid."
                )
            return bytes(value)
        value = bytearray(initial)
        while True:
            if len(value) > maximum:
                raise AddonRepositoryError(
                    "repository_body_too_large", "Repository response is too large."
                )
            chunk = stream.recv(min(65_536, maximum + 1 - len(value)))
            if not chunk:
                return bytes(value)
            value.extend(chunk)


def derive_repository_package_url(index_url: str, addon_id: str, version: str) -> str:
    index = canonicalize_repository_url(index_url)
    if not _SAFE_PACKAGE_VALUE.fullmatch(addon_id) or not _SAFE_PACKAGE_VALUE.fullmatch(
        version
    ):
        raise AddonRepositoryError(
            "repository_package_identity_unsafe",
            "Candidate package identity is unsafe.",
        )
    package_path = (
        index.index_directory
        + quote(addon_id, safe="._~-")
        + "/"
        + quote(f"{addon_id}-{version}.zip", safe="._~-")
    )
    return urlunsplit(("https", index.hostname, package_path, "", ""))


def _expand_gzip(value: bytes) -> bytes:
    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
    result = bytearray()
    try:
        for offset in range(0, len(value), 65_536):
            result.extend(
                decoder.decompress(
                    value[offset : offset + 65_536],
                    MAX_EXPANDED_BYTES + 1 - len(result),
                )
            )
            if len(result) > MAX_EXPANDED_BYTES:
                raise AddonRepositoryError(
                    "repository_metadata_expanded_too_large",
                    "Repository metadata expands beyond its limit.",
                )
        result.extend(decoder.flush(MAX_EXPANDED_BYTES + 1 - len(result)))
    except zlib.error as error:
        raise AddonRepositoryError(
            "repository_metadata_gzip_invalid", "Repository gzip metadata is invalid."
        ) from error
    if not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
        raise AddonRepositoryError(
            "repository_metadata_gzip_invalid", "Repository gzip metadata is invalid."
        )
    if len(result) > MAX_EXPANDED_BYTES or len(result) > max(1, len(value)) * 100:
        raise AddonRepositoryError(
            "repository_metadata_expansion_ratio",
            "Repository metadata expansion ratio is unsafe.",
        )
    return bytes(result)


def parse_repository_metadata(
    transfer: bytes, *, request_path: str, content_type: str
) -> ParsedRepositoryMetadata:
    if not transfer or len(transfer) > MAX_TRANSFER_BYTES:
        raise AddonRepositoryError(
            "repository_metadata_transfer_size",
            "Repository metadata transfer is outside its limit.",
        )
    gzip_expected = request_path.endswith(".gz")
    allowed = _GZIP_TYPES if gzip_expected else _XML_TYPES
    if content_type.lower() not in allowed:
        raise AddonRepositoryError(
            "repository_metadata_content_type",
            "Repository metadata content type is unsupported.",
        )
    expanded = _expand_gzip(transfer) if gzip_expected else transfer
    if len(expanded) > MAX_EXPANDED_BYTES:
        raise AddonRepositoryError(
            "repository_metadata_expanded_too_large",
            "Repository metadata is too large.",
        )
    try:
        text = expanded.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise AddonRepositoryError(
            "repository_metadata_xml_invalid", "Repository metadata must be UTF-8 XML."
        ) from error
    upper = text.upper()
    if "<!DOCTYPE" in upper or "<!ENTITY" in upper:
        raise AddonRepositoryError(
            "repository_metadata_xml_unsafe",
            "Repository metadata declarations are unsafe.",
        )
    if (
        text.lstrip().lower().startswith("<!doctype html")
        or "<html" in text[:512].lower()
    ):
        raise AddonRepositoryError(
            "repository_metadata_html", "Repository metadata must not be HTML."
        )
    try:
        root = ET.fromstring(text)
    except ET.ParseError as error:
        raise AddonRepositoryError(
            "repository_metadata_xml_invalid", "Repository metadata XML is invalid."
        ) from error
    if root.tag != "addons" or root.attrib or (root.text or "").strip():
        raise AddonRepositoryError(
            "repository_metadata_shape", "Repository metadata root must be addons."
        )
    aggregate = 0
    stack: list[tuple[ET.Element, int]] = [(root, 1)]
    while stack:
        element, depth = stack.pop()
        if depth > MAX_XML_DEPTH:
            raise AddonRepositoryError(
                "repository_metadata_depth", "Repository metadata XML is too deep."
            )
        aggregate += len(element.text or "") + len(element.tail or "")
        aggregate += sum(len(key) + len(value) for key, value in element.attrib.items())
        if aggregate > MAX_AGGREGATE_TEXT:
            raise AddonRepositoryError(
                "repository_metadata_text", "Repository metadata text is too large."
            )
        stack.extend((child, depth + 1) for child in element)
    if len(root) > MAX_CANDIDATES:
        raise AddonRepositoryError(
            "repository_candidate_count",
            "Repository candidate count exceeds its limit.",
        )
    candidates: list[ParsedRepositoryCandidate] = []
    identities: set[tuple[str, str]] = set()
    case_identities: set[tuple[str, str]] = set()
    for child in root:
        if child.tag != "addon":
            raise AddonRepositoryError(
                "repository_metadata_shape",
                "Repository metadata may contain only addon entries.",
            )
        if set(child.attrib) - {"id", "name", "version", "provider-name"} or any(
            item.tag not in {"requires", "extension"} for item in child
        ):
            raise AddonRepositoryError(
                "repository_candidate_authority_forbidden",
                "Repository candidate contains unsupported authority fields.",
            )
        document = ET.tostring(child, encoding="utf-8", xml_declaration=False)
        try:
            parsed = parse_captured_addon_xml("candidate", document)
        except DevelopmentSourceValidationError as error:
            raise AddonRepositoryError(
                "repository_candidate_invalid",
                "Repository candidate metadata is invalid.",
            ) from error
        identity = (parsed.addon_id, parsed.observed_version)
        folded = (parsed.addon_id.casefold(), parsed.observed_version.casefold())
        if identity in identities or folded in case_identities:
            raise AddonRepositoryError(
                "repository_candidate_duplicate",
                "Repository candidate identity is duplicated.",
            )
        identities.add(identity)
        case_identities.add(folded)
        library = copied_video_plugin_library(parsed)
        repository_descriptor = any(
            extension.point == "xbmc.addon.repository"
            for extension in parsed.extensions
        )
        supported = (
            library is not None
            and not parsed.has_auto_start_behavior
            and not repository_descriptor
            and _SAFE_PACKAGE_VALUE.fullmatch(parsed.addon_id) is not None
            and _SAFE_PACKAGE_VALUE.fullmatch(parsed.observed_version) is not None
            and all(
                extension.point != "xbmc.addon.metadata"
                or (extension.library is None and not extension.provides)
                for extension in parsed.extensions
            )
        )
        reason = (
            None
            if supported
            else (
                "repository_descriptor"
                if repository_descriptor
                else (
                    "unsafe_package_identity"
                    if _SAFE_PACKAGE_VALUE.fullmatch(parsed.addon_id) is None
                    or _SAFE_PACKAGE_VALUE.fullmatch(parsed.observed_version) is None
                    else "unsupported_addon_type"
                )
            )
        )
        candidates.append(
            ParsedRepositoryCandidate(
                addon_id=parsed.addon_id,
                name=parsed.observed_name,
                version=parsed.observed_version,
                classification=(
                    AddonRepositoryCandidateClassification.PYTHON_VIDEO_PLUGIN
                    if supported
                    else AddonRepositoryCandidateClassification.UNSUPPORTED
                ),
                dependencies=tuple(
                    AddonZipDependency(
                        dependency_id=item.dependency_id,
                        version=item.version,
                        minversion=item.minversion,
                        optional=item.optional,
                    )
                    for item in parsed.dependencies
                ),
                support=(
                    AddonRepositoryCandidateSupport.SUPPORTED
                    if supported
                    else AddonRepositoryCandidateSupport.UNSUPPORTED
                ),
                unsupported_reason=reason,
            )
        )
    candidates.sort(key=lambda item: (item.addon_id, item.version))
    canonical = json.dumps(
        [
            {
                "addon_id": item.addon_id,
                "classification": item.classification,
                "dependencies": [
                    {
                        "id": dependency.dependency_id,
                        "minversion": dependency.minversion,
                        "optional": dependency.optional,
                        "version": dependency.version,
                    }
                    for dependency in item.dependencies
                ],
                "name": item.name,
                "support": item.support,
                "unsupported_reason": item.unsupported_reason,
                "version": item.version,
            }
            for item in candidates
        ],
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return ParsedRepositoryMetadata(
        transfer_digest="sha256:" + hashlib.sha256(transfer).hexdigest(),
        canonical_metadata_digest="sha256:" + hashlib.sha256(canonical).hexdigest(),
        transfer_size=len(transfer),
        expanded_size=len(expanded),
        candidates=tuple(candidates),
    )
