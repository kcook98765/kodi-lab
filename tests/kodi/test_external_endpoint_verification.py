from __future__ import annotations

import ipaddress
import json

import httpx
import pytest

from kodi_lab.domain import ExternalProtocolKind
from kodi_lab.kodi.external import (
    ExternalEndpointError,
    ExternalEndpointPolicy,
    ExternalProbeError,
    NativeExternalKodiVerifier,
    ValidatedExternalEndpoint,
)
from kodi_lab.storage.secrets import ExternalKodiCredential


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        "::1",
        "169.254.1.1",
        "fe80::1",
        "224.0.0.1",
        "ff02::1",
        "0.0.0.0",
        "::",
        "8.8.8.8",
        "100.64.0.1",
        "169.254.169.254",
        "10.250.0.8",
        "192.0.2.1",
        "198.18.0.1",
        "::ffff:192.168.1.8",
    ],
)
def test_endpoint_policy_rejects_forbidden_address_classes(host: str) -> None:
    policy = ExternalEndpointPolicy(allowed_ports=(8080,))
    with pytest.raises(ExternalEndpointError):
        policy.validate(ExternalProtocolKind.JSONRPC_HTTP, host, 8080)


def test_endpoint_policy_accepts_only_configured_lan_literals_and_ports() -> None:
    policy = ExternalEndpointPolicy(
        allowed_ports=(8080, 9090), ipv6_ula_networks=("fd42:1234::/48",)
    )
    assert (
        policy.validate(ExternalProtocolKind.JSONRPC_HTTP, "192.168.50.10", 8080).url
        == "http://192.168.50.10:8080/jsonrpc"
    )
    assert (
        policy.validate(ExternalProtocolKind.JSONRPC_HTTPS, "fd42:1234::10", 9090).url
        == "https://[fd42:1234::10]:9090/jsonrpc"
    )
    for host, port in (("kodi.lan", 8080), ("192.168.1.10", 8081)):
        with pytest.raises(ExternalEndpointError):
            policy.validate(ExternalProtocolKind.JSONRPC_HTTP, host, port)


def test_loopback_requires_explicit_test_configuration() -> None:
    normal = ExternalEndpointPolicy(allowed_ports=(8080,))
    test = ExternalEndpointPolicy(allowed_ports=(8080,), test_loopback_enabled=True)
    with pytest.raises(ExternalEndpointError):
        normal.validate(ExternalProtocolKind.JSONRPC_HTTP, "127.0.0.1", 8080)
    assert (
        test.validate(ExternalProtocolKind.JSONRPC_HTTP, "127.0.0.1", 8080).address
        == "127.0.0.1"
    )


def test_native_probe_uses_fixed_methods_path_auth_and_bounded_identity() -> None:
    seen: list[tuple[str, str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        document = json.loads(request.content)
        seen.append(
            (
                document["method"],
                request.url.path,
                request.headers.get("authorization"),
            )
        )
        result: object
        if document["method"] == "JSONRPC.Ping":
            result = "pong"
        elif document["method"] == "JSONRPC.Version":
            result = {"version": {"major": 13, "minor": 0, "patch": 0}}
        else:
            result = {
                "name": "Kodi",
                "version": {
                    "major": 21,
                    "minor": 3,
                    "tag": "stable",
                    "tagversion": "",
                },
            }
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": document["id"], "result": result},
        )

    result = NativeExternalKodiVerifier(transport=httpx.MockTransport(handler)).probe(
        ValidatedExternalEndpoint(
            ExternalProtocolKind.JSONRPC_HTTP, "192.168.1.9", 8080
        ),
        ExternalKodiCredential("kodi", "not-in-logs"),
        frozenset({"target_inspect", "screenshot", "workspace_deployment"}),
    )
    assert [item[0] for item in seen] == [
        "JSONRPC.Ping",
        "JSONRPC.Version",
        "Application.GetProperties",
    ]
    assert all(item[1] == "/jsonrpc" for item in seen)
    assert all(item[2] is not None and item[2].startswith("Basic ") for item in seen)
    assert result.kodi_version == "21.3"
    assert result.protocol_version == "13.0.0"
    assert result.verified_capabilities == ("target_inspect",)


def test_native_probe_rejects_redirect_without_contacting_destination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(302, headers={"location": "http://169.254.169.254/"})

    verifier = NativeExternalKodiVerifier(transport=httpx.MockTransport(handler))
    with pytest.raises(ExternalProbeError, match="endpoint_not_allowed"):
        verifier.probe(
            ValidatedExternalEndpoint(
                ExternalProtocolKind.JSONRPC_HTTP, "192.168.1.9", 8080
            ),
            None,
            frozenset({"target_inspect"}),
        )
    assert calls == 1


def test_no_hostname_resolution_or_mixed_dns_answer_path_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    def forbidden(*_args: object, **_kwargs: object) -> object:
        nonlocal called
        called = True
        return [(None, None, None, None, ("192.168.1.2", 0))]

    monkeypatch.setattr("socket.getaddrinfo", forbidden)
    with pytest.raises(ExternalEndpointError):
        ExternalEndpointPolicy(allowed_ports=(8080,)).validate(
            ExternalProtocolKind.JSONRPC_HTTP, "mixed.example.test", 8080
        )
    assert called is False
    assert ipaddress.ip_address("192.168.1.2").is_private


@pytest.mark.parametrize(
    ("major", "minor", "canonical"),
    [(19, 5, "19.5"), (20, 5, "20.5"), (21, 3, "21.3"), (22, 0, "22.0")],
)
def test_native_probe_supports_closed_kodi_compatibility_lines(
    major: int, minor: int, canonical: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        document = json.loads(request.content)
        method = document["method"]
        if method == "JSONRPC.Ping":
            result: object = "pong"
        elif method == "JSONRPC.Version":
            result = {"version": {"major": 13, "minor": 0, "patch": 0}}
        else:
            result = {
                "name": "Kodi",
                "version": {
                    "major": major,
                    "minor": minor,
                    "tag": "stable",
                    "tagversion": "",
                },
            }
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": document["id"], "result": result},
        )

    result = NativeExternalKodiVerifier(transport=httpx.MockTransport(handler)).probe(
        ValidatedExternalEndpoint(
            ExternalProtocolKind.JSONRPC_HTTP, "192.168.1.9", 8080
        ),
        None,
        frozenset({"target_inspect"}),
    )
    assert result.kodi_version == canonical
