"""Opt-in LAN endpoint configuration and credential-free readiness checks."""

from __future__ import annotations

import asyncio
import errno
import ipaddress
import json
import os
import re
import socket
import ssl
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import httpx

MCP_INSTANCE_HEADER = "X-Kodi-Lab-MCP-Instance"
_PRIVATE_NETWORKS = tuple(
    ipaddress.IPv4Network(network)
    for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)


def is_private_lan_address(value: str) -> bool:
    try:
        address = ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError:
        return False
    return any(address in network for network in _PRIVATE_NETWORKS)


def validate_lan_url(
    value: str | None, *, scheme: Literal["https", "http"] = "https"
) -> str | None:
    """Accept one explicit /mcp URL; HTTP needs separately validated opt-in."""
    if value is None or value == "":
        return None
    try:
        parsed = urlsplit(value)
        port = parsed.port
        host = parsed.hostname or ""
    except ValueError:
        raise ValueError(
            f"MCP LAN URL must be an explicit {scheme.upper()} /mcp endpoint"
        ) from None
    if (
        len(value) > 512
        or any(ord(character) < 33 or ord(character) > 126 for character in value)
        or parsed.scheme != scheme
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path != "/mcp"
        or parsed.query
        or parsed.fragment
        or "?" in value
        or "#" in value
        or not host
        or parsed.netloc != host + (f":{port}" if port is not None else "")
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise ValueError(
            f"MCP LAN URL must be an explicit {scheme.upper()} /mcp endpoint"
        )
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if "." not in host or not all(
            re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
            for label in host.split(".")
        ):
            raise ValueError(
                "MCP LAN host must be a private IPv4 or normalized DNS name"
            ) from None
    else:
        if not is_private_lan_address(host):
            raise ValueError("MCP LAN host must use private IPv4 space")
    return value


def validate_lan_http_configuration(
    *,
    enabled: bool,
    url: str | None,
    bind_address: str | None,
    allowed_cidr: str | None,
) -> None:
    """Require deliberate HTTP opt-in, a concrete interface and a private ACL."""
    if not enabled:
        if url or bind_address or allowed_cidr:
            raise ValueError(
                "Trusted-LAN HTTP configuration requires explicit enablement"
            )
        return
    if not url or not bind_address or not allowed_cidr:
        raise ValueError("Trusted-LAN HTTP requires URL, bind address and allowed CIDR")
    validate_lan_url(url, scheme="http")
    if not is_private_lan_address(bind_address):
        raise ValueError("Trusted-LAN HTTP bind must be a concrete private IPv4")
    try:
        network = ipaddress.IPv4Network(allowed_cidr, strict=True)
    except ValueError:
        raise ValueError("Trusted-LAN HTTP ACL must be a private IPv4 CIDR") from None
    address = ipaddress.IPv4Address(bind_address)
    if (
        not any(network.subnet_of(parent) for parent in _PRIVATE_NETWORKS)
        or address not in network
        or (
            address in (network.network_address, network.broadcast_address)
            and network.prefixlen < 31
        )
    ):
        raise ValueError("Trusted-LAN HTTP ACL must contain the private bind interface")
    host = urlsplit(url).hostname or ""
    if is_private_lan_address(host) and host != bind_address:
        raise ValueError("Trusted-LAN HTTP URL must name the configured interface")


@dataclass(frozen=True, slots=True)
class McpLanStatus:
    url: str | None
    ready: bool
    message: str


def validate_http_readiness_socket(value: Path | None) -> Path | None:
    """Accept a short filesystem socket path, never a TCP health endpoint."""
    if value is not None and (
        not value.is_absolute()
        or ".." in value.parts
        or len(os.fsencode(value)) > 100
        or re.fullmatch(r"/[a-zA-Z0-9_./-]+", str(value)) is None
    ):
        raise ValueError(
            "MCP HTTP readiness socket must be a safe absolute socket path"
        )
    return value


def _protected_readiness_socket(path: Path) -> bool:
    """The directory confines nginx's socket even when nginx creates mode 0666."""
    try:
        resolved = path.resolve(strict=True)
    except RuntimeError:  # Python 3.12 represents symlink loops this way.
        return False
    if resolved != path:
        return False
    container = bool(os.environ.get("KODI_LAB_REMOTE_CONTAINER"))
    trusted_owners = {0} if container else {0, os.geteuid()}
    parent = path.parent.stat()
    sock = path.stat()
    if (
        not stat.S_ISSOCK(sock.st_mode)
        or sock.st_uid not in trusted_owners
        or sock.st_gid != parent.st_gid
        or (container and parent.st_gid != os.getegid())
        or parent.st_mode & 0o027
    ):
        return False
    for directory in path.parents:
        metadata = directory.stat()
        if metadata.st_uid not in trusted_owners:
            return False
        # A root-owned sticky temporary directory is suitable for private fixtures.
        sticky_root = metadata.st_uid == 0 and metadata.st_mode & stat.S_ISVTX
        if metadata.st_mode & 0o022 and not sticky_root:
            return False
    # Named POSIX ACL entries can grant access beyond the apparent mode bits.
    for component in (path, *path.parents):
        try:
            os.getxattr(component, "system.posix_acl_access")
        except OSError as error:
            if error.errno not in (errno.ENODATA, errno.ENOTSUP):
                raise
        else:
            return False
    return True


async def check_http_proxy(
    url: str | None,
    *,
    instance_id: str,
    bind_address: str | None,
    readiness_socket: Path | None,
) -> McpLanStatus:
    """Check the same MCP-only proxy/application over a protected Unix socket.

    No connection traverses the LAN ingress. DNS is configuration validation;
    genuine LAN connectivity and authenticated access require external acceptance.
    """
    unavailable = McpLanStatus(
        url,
        False,
        "Internal HTTP proxy health could not be verified. Check the protected "
        "readiness socket, proxy and application. LAN access requires a separate "
        "test from an allowed client.",
    )
    try:
        validate_lan_url(url, scheme="http")
        validate_http_readiness_socket(readiness_socket)
        if (
            not url
            or not instance_id
            or not bind_address
            or not is_private_lan_address(bind_address)
            or readiness_socket is None
        ):
            return unavailable
        parsed = urlsplit(url)
        async with asyncio.timeout(2):
            if not await asyncio.to_thread(
                _protected_readiness_socket, readiness_socket
            ):
                return unavailable
            addresses = await asyncio.to_thread(
                socket.getaddrinfo,
                parsed.hostname,
                parsed.port or 80,
                socket.AF_UNSPEC,
                socket.SOCK_STREAM,
            )
            if not addresses or any(
                str(address[4][0]) != bind_address for address in addresses
            ):
                return unavailable
            async with (
                httpx.AsyncClient(
                    transport=httpx.AsyncHTTPTransport(uds=str(readiness_socket)),
                    trust_env=False,
                    follow_redirects=False,
                    timeout=1.5,
                ) as client,
                client.stream(
                    "POST",
                    url,
                    headers={
                        "Host": parsed.netloc,
                        "Accept": "application/json, text/event-stream",
                        "MCP-Protocol-Version": "2025-11-25",
                    },
                    json={
                        "jsonrpc": "2.0",
                        "id": 0,
                        "method": "tools/list",
                        "params": {},
                    },
                ) as response,
            ):
                if (
                    response.status_code != 401
                    or response.headers.get("WWW-Authenticate") != "Bearer"
                    or response.headers.get(MCP_INSTANCE_HEADER) != instance_id
                    or response.headers.get("Content-Type", "").split(";", 1)[0]
                    != "application/json"
                    or response.headers.get("Content-Encoding", "identity")
                    != "identity"
                ):
                    return unavailable
                body = bytearray()
                async for chunk in response.aiter_raw(chunk_size=257):
                    body.extend(chunk)
                    if len(body) > 256:
                        return unavailable
                if json.loads(body) != {"error": "authentication_failed"}:
                    return unavailable
    except (OSError, ValueError, TimeoutError, httpx.HTTPError):
        return unavailable
    return McpLanStatus(
        url,
        True,
        "Internal HTTP proxy and this application's anonymous MCP gate are healthy. "
        "LAN reachability and authorized access require a separate test from an "
        "allowed client. HTTP traffic is unencrypted.",
    )


async def check_lan_endpoint(
    url: str | None,
    *,
    instance_id: str,
    ca_file: Path | None = None,
    trusted_http_bind: str | None = None,
) -> McpLanStatus:
    """Check anonymous readiness; HTTP requires an explicit private interface."""
    if url is None:
        return McpLanStatus(
            None,
            False,
            "LAN HTTPS is not configured. To use HTTPS, an administrator must "
            "provide a LAN-only endpoint and a certificate trusted by your agent.",
        )
    unavailable = McpLanStatus(
        url,
        False,
        (
            "The configured HTTP endpoint could not be reached or matched to "
            "this instance. Check the private interface, proxy, DNS, subnet ACL "
            "and firewall before connecting."
            if trusted_http_bind
            else "The configured HTTPS endpoint could not be verified. Check the "
            "proxy, private LAN DNS, certificate trust and firewall before connecting."
        ),
    )
    parsed = urlsplit(url)
    try:
        validate_lan_url(url, scheme="http" if trusted_http_bind else "https")
        if trusted_http_bind and not is_private_lan_address(trusted_http_bind):
            return unavailable
        async with asyncio.timeout(2):
            addresses = await asyncio.to_thread(
                socket.getaddrinfo,
                parsed.hostname,
                parsed.port or (80 if trusted_http_bind else 443),
                socket.AF_UNSPEC,
                socket.SOCK_STREAM,
            )
            if not addresses or not all(
                is_private_lan_address(str(address[4][0])) for address in addresses
            ):
                return unavailable
            if trusted_http_bind and any(
                str(address[4][0]) != trusted_http_bind for address in addresses
            ):
                return unavailable
            tls = ssl.create_default_context(cafile=ca_file)
            # Connect to the vetted literal address, keeping the configured TLS
            # identity and Host. A second DNS lookup must not change destination.
            pinned_url = httpx.URL(url).copy_with(host=str(addresses[0][4][0]))
            async with (
                httpx.AsyncClient(
                    verify=tls, trust_env=False, follow_redirects=False, timeout=1.5
                ) as client,
                client.stream(
                    "POST",
                    pinned_url,
                    extensions={"sni_hostname": parsed.hostname},
                    headers={
                        "Host": parsed.netloc,
                        "Accept": "application/json, text/event-stream",
                        "MCP-Protocol-Version": "2025-11-25",
                    },
                    json={
                        "jsonrpc": "2.0",
                        "id": 0,
                        "method": "tools/list",
                        "params": {},
                    },
                ) as response,
            ):
                if (
                    response.status_code != 401
                    or response.headers.get("WWW-Authenticate") != "Bearer"
                    or response.headers.get(MCP_INSTANCE_HEADER) != instance_id
                ):
                    return unavailable
    except (OSError, ValueError, TimeoutError, httpx.HTTPError):
        return unavailable
    return McpLanStatus(
        url,
        True,
        (
            "HTTP reached the configured interface without a token. This checks "
            "reachability, not server identity or encryption."
            if trusted_http_bind
            else "HTTPS reached this Kodi Lab MCP server without a token. Your agent "
            "must also be on an allowed LAN and trust this endpoint's certificate."
        ),
    )
