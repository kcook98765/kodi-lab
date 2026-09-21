"""SSRF-bounded fixed native JSON-RPC verification for external Kodi."""

from __future__ import annotations

import ipaddress
import json
import time
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import httpx

from kodi_lab.domain import ExternalProtocolKind
from kodi_lab.kodi.readiness import KodiApplicationIdentity
from kodi_lab.storage.secrets import ExternalKodiCredential

MAX_EXTERNAL_RESPONSE_BYTES = 65_536
MAX_JSON_DEPTH = 16
MAX_JSON_NODES = 2048
MANAGED_NETWORKS = (ipaddress.ip_network("10.250.0.0/24"),)
PRIVATE_IPV4_NETWORKS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)


class ExternalEndpointError(ValueError):
    code = "endpoint_not_allowed"


class ExternalProbeError(RuntimeError):
    """Bounded semantic probe failure; raw transport detail never escapes."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class ValidatedExternalEndpoint:
    protocol_kind: ExternalProtocolKind
    address: str
    port: int

    @property
    def url(self) -> str:
        scheme = (
            "http"
            if self.protocol_kind is ExternalProtocolKind.JSONRPC_HTTP
            else "https"
        )
        host = f"[{self.address}]" if ":" in self.address else self.address
        return f"{scheme}://{host}:{self.port}/jsonrpc"


@dataclass(frozen=True, slots=True)
class ExternalKodiProbeResult:
    addresses: tuple[str, ...]
    kodi_name: str
    kodi_version: str
    protocol_version: str
    verified_capabilities: tuple[str, ...]


class ExternalEndpointPolicy:
    """Closed IP-literal-only private-LAN policy for Phase 13 Slice 1."""

    def __init__(
        self,
        *,
        allowed_ports: tuple[int, ...],
        ipv6_ula_networks: tuple[str, ...] = (),
        test_loopback_enabled: bool = False,
        managed_networks: tuple[str, ...] = ("10.250.0.0/24",),
    ) -> None:
        self._allowed_ports = frozenset(allowed_ports)
        try:
            parsed_ula = tuple(
                ipaddress.ip_network(value, strict=True) for value in ipv6_ula_networks
            )
            parsed_managed = tuple(
                ipaddress.ip_network(value, strict=True) for value in managed_networks
            )
        except ValueError as error:
            raise ValueError(
                "external target network configuration is invalid"
            ) from error
        ula_supernet = ipaddress.IPv6Network("fc00::/7")
        for network in parsed_ula:
            if not isinstance(network, ipaddress.IPv6Network) or not network.subnet_of(
                ula_supernet
            ):
                raise ValueError("external IPv6 networks must be explicit ULA networks")
        self._ipv6_ula_networks = parsed_ula
        self._managed_networks = parsed_managed
        self._test_loopback_enabled = test_loopback_enabled

    def validate(
        self, protocol_kind: ExternalProtocolKind, host: str, port: int
    ) -> ValidatedExternalEndpoint:
        if (
            port not in self._allowed_ports
            or not host
            or host != host.strip()
            or "%" in host
        ):
            raise ExternalEndpointError("endpoint is outside the approved policy")
        try:
            address = ipaddress.ip_address(host)
        except ValueError as error:
            raise ExternalEndpointError(
                "Phase 13 Slice 1 requires an IP literal"
            ) from error
        if (
            isinstance(address, ipaddress.IPv6Address)
            and address.ipv4_mapped is not None
        ):
            raise ExternalEndpointError("IPv4-mapped IPv6 is not accepted")
        if address.is_loopback:
            if not self._test_loopback_enabled:
                raise ExternalEndpointError(
                    "loopback requires explicit test configuration"
                )
            return ValidatedExternalEndpoint(protocol_kind, address.compressed, port)
        if (
            address.is_unspecified
            or address.is_link_local
            or address.is_multicast
            or address.is_reserved
        ):
            raise ExternalEndpointError("address class is not accepted")
        if any(
            address in network
            for network in self._managed_networks
            if network.version == address.version
        ):
            raise ExternalEndpointError(
                "managed Kodi Lab networks are not external targets"
            )
        if isinstance(address, ipaddress.IPv4Address):
            if not any(address in network for network in PRIVATE_IPV4_NETWORKS):
                raise ExternalEndpointError("only approved private IPv4 is accepted")
        elif not any(address in network for network in self._ipv6_ula_networks):
            raise ExternalEndpointError("IPv6 requires an explicitly configured ULA")
        return ValidatedExternalEndpoint(protocol_kind, address.compressed, port)


class NativeExternalKodiVerifier:
    """Calls only three fixed read-only Kodi JSON-RPC methods."""

    def __init__(
        self,
        *,
        connect_timeout: float = 2.0,
        response_timeout: float = 3.0,
        total_timeout: float = 10.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._connect_timeout = connect_timeout
        self._response_timeout = response_timeout
        self._total_timeout = total_timeout
        self._transport = transport

    def probe(
        self,
        endpoint: ValidatedExternalEndpoint,
        credential: ExternalKodiCredential | None,
        configured_capabilities: frozenset[str],
    ) -> ExternalKodiProbeResult:
        started = time.monotonic()
        auth = (
            None
            if credential is None
            else httpx.BasicAuth(credential.username, credential.password)
        )
        try:
            with httpx.Client(
                auth=auth,
                follow_redirects=False,
                trust_env=False,
                verify=True,
                transport=self._transport,
            ) as client:
                ping = self._call(client, endpoint, "JSONRPC.Ping", None, started)
                if ping != "pong":
                    raise ExternalProbeError("invalid_kodi_response")
                protocol = self._call(
                    client, endpoint, "JSONRPC.Version", None, started
                )
                protocol_version = self._protocol_version(protocol)
                identity_result = self._call(
                    client,
                    endpoint,
                    "Application.GetProperties",
                    {"properties": ["name", "version"]},
                    started,
                )
        except ExternalProbeError:
            raise
        except (httpx.TimeoutException, httpx.NetworkError) as error:
            raise ExternalProbeError("target_unreachable") from error
        identity = self._identity(identity_result)
        release = identity.kodi_release
        if (release.major, release.minor) not in {
            (19, 5),
            (20, 5),
            (21, 3),
        } and release.major != 22:
            raise ExternalProbeError("unsupported_kodi_version")
        if identity.name.strip().lower() != "kodi":
            raise ExternalProbeError("invalid_kodi_response")
        verified = (
            ("target_inspect",) if "target_inspect" in configured_capabilities else ()
        )
        return ExternalKodiProbeResult(
            addresses=(endpoint.address,),
            kodi_name=identity.name,
            kodi_version=release.canonical,
            protocol_version=protocol_version,
            verified_capabilities=verified,
        )

    def _call(
        self,
        client: httpx.Client,
        endpoint: ValidatedExternalEndpoint,
        method: str,
        params: dict[str, object] | None,
        started: float,
    ) -> Any:
        remaining = self._total_timeout - (time.monotonic() - started)
        if remaining <= 0:
            raise ExternalProbeError("target_unreachable")
        request_id = f"kodi-lab-external-{uuid4()}"
        body: dict[str, object] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            body["params"] = params
        timeout = httpx.Timeout(
            min(remaining, self._response_timeout),
            connect=min(remaining, self._connect_timeout),
        )
        try:
            with client.stream(
                "POST", endpoint.url, json=body, timeout=timeout
            ) as response:
                if response.status_code in {401, 403}:
                    raise ExternalProbeError("authentication_failed")
                if 300 <= response.status_code < 400:
                    raise ExternalProbeError("endpoint_not_allowed")
                if response.status_code != 200:
                    raise ExternalProbeError("invalid_kodi_response")
                chunks: list[bytes] = []
                size = 0
                for chunk in response.iter_bytes():
                    if time.monotonic() - started > self._total_timeout:
                        raise ExternalProbeError("target_unreachable")
                    size += len(chunk)
                    if size > MAX_EXTERNAL_RESPONSE_BYTES:
                        raise ExternalProbeError("invalid_kodi_response")
                    chunks.append(chunk)
                if time.monotonic() - started > self._total_timeout:
                    raise ExternalProbeError("target_unreachable")
        except ExternalProbeError:
            raise
        try:
            document = json.loads(b"".join(chunks))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ExternalProbeError("invalid_kodi_response") from error
        self._validate_json_shape(document)
        if (
            not isinstance(document, dict)
            or set(document) != {"jsonrpc", "id", "result"}
            or document.get("jsonrpc") != "2.0"
            or document.get("id") != request_id
        ):
            raise ExternalProbeError("invalid_kodi_response")
        return document["result"]

    @staticmethod
    def _protocol_version(result: object) -> str:
        if not isinstance(result, dict) or set(result) != {"version"}:
            raise ExternalProbeError("invalid_kodi_response")
        version = result["version"]
        if not isinstance(version, dict) or not {"major", "minor", "patch"}.issubset(
            version
        ):
            raise ExternalProbeError("invalid_kodi_response")
        values = (version["major"], version["minor"], version["patch"])
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in values
        ):
            raise ExternalProbeError("invalid_kodi_response")
        if values[0] < 12 or values[0] > 20:
            raise ExternalProbeError("invalid_kodi_response")
        return ".".join(str(value) for value in values)

    @staticmethod
    def _identity(result: object) -> KodiApplicationIdentity:
        if not isinstance(result, dict) or set(result) != {"name", "version"}:
            raise ExternalProbeError("invalid_kodi_response")
        version = result["version"]
        if not isinstance(version, dict):
            raise ExternalProbeError("invalid_kodi_response")
        name = result["name"]
        major = version.get("major")
        minor = version.get("minor")
        tag = version.get("tag", "stable")
        tagversion = version.get("tagversion")
        if tag == "":
            tag = "stable"
        if isinstance(tagversion, str) and tagversion.isdigit():
            tagversion = int(tagversion)
        if tagversion == "":
            tagversion = None
        if (
            not isinstance(name, str)
            or len(name) > 64
            or not isinstance(major, int)
            or isinstance(major, bool)
            or not isinstance(minor, int)
            or isinstance(minor, bool)
            or not isinstance(tag, str)
            or (
                tagversion is not None
                and (not isinstance(tagversion, int) or isinstance(tagversion, bool))
            )
        ):
            raise ExternalProbeError("invalid_kodi_response")
        try:
            return KodiApplicationIdentity(name, major, minor, tag, tagversion)
        except ValueError as error:
            raise ExternalProbeError("invalid_kodi_response") from error

    @classmethod
    def _validate_json_shape(cls, value: object) -> None:
        count = 0
        stack = [(value, 0)]
        while stack:
            current, depth = stack.pop()
            count += 1
            if count > MAX_JSON_NODES or depth > MAX_JSON_DEPTH:
                raise ExternalProbeError("invalid_kodi_response")
            if isinstance(current, dict):
                if any(not isinstance(key, str) or len(key) > 256 for key in current):
                    raise ExternalProbeError("invalid_kodi_response")
                stack.extend((item, depth + 1) for item in current.values())
            elif isinstance(current, list):
                stack.extend((item, depth + 1) for item in current)
            elif isinstance(current, str) and len(current) > 16_384:
                raise ExternalProbeError("invalid_kodi_response")
