"""Opt-in LAN endpoint configuration and credential-free readiness checks."""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
import ssl
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
