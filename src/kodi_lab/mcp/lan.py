"""Opt-in LAN endpoint configuration and credential-free readiness checks."""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
import ssl
from dataclasses import dataclass
from pathlib import Path
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


def validate_lan_url(value: str | None) -> str | None:
    """Accept an explicit HTTPS /mcp URL; never infer one from the Web URL."""
    if value is None or value == "":
        return None
    try:
        parsed = urlsplit(value)
        port = parsed.port
        host = parsed.hostname or ""
    except ValueError:
        raise ValueError(
            "MCP LAN URL must be an explicit HTTPS /mcp endpoint"
        ) from None
    if (
        len(value) > 512
        or any(ord(character) < 33 or ord(character) > 126 for character in value)
        or parsed.scheme != "https"
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
        raise ValueError("MCP LAN URL must be an explicit HTTPS /mcp endpoint")
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


@dataclass(frozen=True, slots=True)
class McpLanStatus:
    url: str | None
    ready: bool
    message: str


async def check_lan_endpoint(
    url: str | None, *, instance_id: str, ca_file: Path | None = None
) -> McpLanStatus:
    """Verify TLS and this process's anonymous 401; never send a bearer token."""
    if url is None:
        return McpLanStatus(
            None,
            False,
            "Direct LAN MCP is not configured. An administrator must provide a "
            "LAN-only HTTPS endpoint and a certificate trusted by your agent.",
        )
    unavailable = McpLanStatus(
        url,
        False,
        "The configured LAN endpoint could not be verified. Check the proxy, "
        "private LAN DNS, certificate trust and firewall before connecting.",
    )
    parsed = urlsplit(url)
    try:
        async with asyncio.timeout(2):
            addresses = await asyncio.to_thread(
                socket.getaddrinfo,
                parsed.hostname,
                parsed.port or 443,
                socket.AF_UNSPEC,
                socket.SOCK_STREAM,
            )
            if not addresses or not all(
                is_private_lan_address(str(address[4][0])) for address in addresses
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
        "HTTPS reached this Kodi Lab MCP server without a token. Your agent "
        "must also be on an allowed LAN and trust this endpoint's certificate.",
    )
