"""Advertised address evidence is bounded and independent of local DNS ownership."""

import asyncio

import httpx
import pytest

from kodi_lab.repository.address import probe_advertised_origin


@pytest.mark.parametrize(
    "status,content,expected",
    [
        (200, b"a" * 64 + b"\n", "verified"),
        (200, b"b" * 64, "unexpected_feed"),
        (200, b"a" * 129, "unexpected_feed"),
        (302, b"", "unreachable"),
        (503, b"", "unreachable"),
    ],
)
def test_origin_probe_checks_exact_feed_and_never_follows_redirects(
    monkeypatch: pytest.MonkeyPatch, status: int, content: bytes, expected: str
) -> None:
    original = httpx.AsyncClient
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(
            status,
            content=content,
            headers={"Location": "http://elsewhere.invalid/private"},
        )

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(handle)),
    )
    assert (
        asyncio.run(
            probe_advertised_origin("http://proxy.example.invalid:18120", "a" * 64)
        )
        == expected
    )
    assert calls == ["http://proxy.example.invalid:18120/stable/addons.xml.sha256"]


def test_origin_transport_failure_is_an_actionable_unreachable_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = httpx.AsyncClient

    def unavailable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("not exposed", request=request)

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(unavailable)),
    )
    assert (
        asyncio.run(probe_advertised_origin("http://192.168.50.10:18120", "a" * 64))
        == "unreachable"
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://user:private@example.invalid",
        "http://example.invalid/?token=private",
        "file:///private",
    ],
)
def test_invalid_origins_are_not_requested(url: str) -> None:
    assert asyncio.run(probe_advertised_origin(url, "a" * 64)) == "not_checked"
