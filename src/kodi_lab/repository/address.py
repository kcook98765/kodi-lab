"""Bounded advertised-feed diagnosis without inferring a replacement address."""

import asyncio
from typing import Literal
from urllib.parse import urlsplit

import httpx

OriginReachability = Literal[
    "verified", "unreachable", "unexpected_feed", "not_checked"
]


async def probe_advertised_origin(
    base_url: str, expected_checksum: str
) -> OriginReachability:
    """Check the administrator-configured URL, never a caller-supplied destination."""
    parsed = urlsplit(base_url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        return "not_checked"

    async def probe() -> OriginReachability:
        async with (
            httpx.AsyncClient(
                timeout=2, follow_redirects=False, trust_env=False
            ) as client,
            client.stream(
                "GET", base_url.rstrip("/") + "/stable/addons.xml.sha256"
            ) as response,
        ):
            if response.status_code != 200:
                return "unreachable"
            content = bytearray()
            async for chunk in response.aiter_bytes(chunk_size=129):
                content.extend(chunk)
                if len(content) > 128:
                    return "unexpected_feed"
            return (
                "verified"
                if bytes(content).strip() == expected_checksum.encode("ascii")
                else "unexpected_feed"
            )

    try:
        return await asyncio.wait_for(probe(), timeout=2.5)
    except (OSError, ValueError, httpx.HTTPError, TimeoutError):
        return "unreachable"
