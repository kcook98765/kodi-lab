"""Closed portable facts and authenticated operation, with no host escape."""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from kodi_lab.domain import ExternalProtocolKind
from kodi_lab.kodi.bridge import ExternalBridgeClient, ExternalBridgeError
from kodi_lab.kodi.external import ValidatedExternalEndpoint
from kodi_lab.kodi.system_info import (
    BridgeSystemSnapshot,
    collect_system_info,
    enrich_system_info,
)
from tests.kodi.test_bridge_otp_bootstrap import _bootstrap, _server
from tests.kodi.test_bridge_otp_bootstrap import runtime as runtime
from tests.kodi.test_narrow_bridge_addon import _load


def _snapshot_bridge(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    bridge = _load(monkeypatch)
    monkeypatch.setattr(
        bridge["xbmcvfs"], "translatePath", lambda label: "", raising=False
    )
    return bridge


def test_direct_facts_partial_failures_profile_scope_and_no_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge = _snapshot_bridge(monkeypatch)
    # Fail every command-capable fallback if it is accidentally introduced.
    import subprocess

    monkeypatch.setattr(
        subprocess, "Popen", lambda *args, **kwargs: pytest.fail("command forbidden")
    )
    monkeypatch.setattr(bridge["os"], "cpu_count", lambda: 8)
    monkeypatch.setattr(
        bridge["os"],
        "uname",
        lambda: SimpleNamespace(sysname="Linux", release="6.1", machine="aarch64"),
    )
    translated: list[str] = []
    paths: list[str] = []

    def translate(path: str) -> str:
        translated.append(path)
        return "/private/kodi/profile"

    def disk(path: str) -> Any:
        paths.append(path)
        return SimpleNamespace(total=2**54 + 5, used=100, free=2**54 - 100)

    monkeypatch.setattr(bridge["xbmcvfs"], "translatePath", translate)
    monkeypatch.setattr(bridge["shutil"], "disk_usage", disk)
    raw = BridgeSystemSnapshot.model_validate(bridge["_system_snapshot"]())
    assert raw.logical_cpu_count == 8 and raw.machine == "aarch64"
    assert raw.profile_storage_total == 2**54 + 5
    assert translated == ["special://profile"] and paths == ["/private/kodi/profile"]
    native = collect_system_info(lambda method, params: {})
    merged = enrich_system_info(native, raw)
    assert merged.enrichment is not None
    assert merged.enrichment.metrics["profile_storage_total"].value == 2**54 + 5
    assert isinstance(merged.enrichment.metrics["profile_storage_total"].value, int)
    assert "/private" not in merged.model_dump_json()
    assert (
        merged.enrichment.metrics["profile_storage_free"].scope
        == "kodi_profile_filesystem"
    )
    assert all(m.source == "narrow_bridge" for m in merged.enrichment.metrics.values())

    def unavailable(path: str) -> Any:
        raise OSError("private failure detail")

    monkeypatch.setattr(bridge["shutil"], "disk_usage", unavailable)
    partial = bridge["_system_snapshot"]()
    assert partial["logical_cpu_count"] == 8
    assert partial["profile_storage_total"] is None
    assert "private failure" not in json.dumps(partial)


@pytest.mark.parametrize("path", ["", "special://profile", "relative/profile"])
def test_unavailable_profile_translation_never_probes_cwd(
    monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    bridge = _snapshot_bridge(monkeypatch)
    monkeypatch.setattr(bridge["xbmcvfs"], "translatePath", lambda label: path)
    monkeypatch.setattr(
        bridge["shutil"], "disk_usage", lambda value: pytest.fail("unresolved profile")
    )
    assert bridge["_system_snapshot"]()["profile_storage_total"] is None


@pytest.mark.parametrize("value", [True, -1, 0, 1048577, "8"])
def test_invalid_cpu_count_is_null(
    monkeypatch: pytest.MonkeyPatch, value: object
) -> None:
    bridge = _snapshot_bridge(monkeypatch)
    monkeypatch.setattr(bridge["os"], "cpu_count", lambda: value)
    assert bridge["_system_snapshot"]()["logical_cpu_count"] is None


@pytest.mark.parametrize(
    "damage", ["extra", "bool_bytes", "inconsistent", "control", "oversized", "missing"]
)
def test_closed_response_rejects_sensitive_or_invalid_values(
    monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    bridge = _snapshot_bridge(monkeypatch)
    raw = bridge["_system_snapshot"]()
    if damage == "extra":
        raw["path"] = "/private"
    elif damage == "bool_bytes":
        raw["profile_storage_total"] = True
    elif damage == "inconsistent":
        raw.update(profile_storage_total=5, profile_storage_free=9)
    elif damage == "control":
        raw["machine"] = "bad\ntext"
    elif damage == "oversized":
        raw["os_release"] = "x" * 161
    else:
        raw.pop("machine")
    with pytest.raises(ValidationError):
        BridgeSystemSnapshot.model_validate(raw)


def test_authenticated_empty_operation_exact_identity_and_no_response_cache(
    runtime: Any,
) -> None:
    bridge, state_path, _, _ = runtime
    with _server(bridge) as port:
        client = ExternalBridgeClient()
        endpoint = ValidatedExternalEndpoint(
            ExternalProtocolKind.JSONRPC_HTTP, "127.0.0.1", port
        )
        _, transaction, secret = _bootstrap(client, bridge, port)
        client.commit_pairing(endpoint, bridge_port=port, transaction=transaction)
        target, bridge_id = transaction.target_id, transaction.bridge_id
        identity, snapshot = client.system_snapshot(
            endpoint,
            bridge_port=port,
            target_id=target,
            bridge_id=bridge_id,
            secret=secret,
        )
        assert (
            identity.bridge_id == bridge_id
            and "system_snapshot" in identity.capabilities
        )
        assert snapshot.schema_version == 1
        persisted = state_path.read_text()
        assert (
            "logical_cpu_count" not in persisted and "profile_storage" not in persisted
        )
        with pytest.raises(ExternalBridgeError):
            client.system_snapshot(
                endpoint,
                bridge_port=port,
                target_id=uuid4(),
                bridge_id=bridge_id,
                secret=secret,
            )
        with pytest.raises(ExternalBridgeError):
            client._authenticated_json(
                endpoint,
                bridge_port=port,
                target_id=target,
                bridge_id=bridge_id,
                secret=secret,
                operation="system_snapshot",
                payload={"path": "/"},
            )


def test_system_snapshot_trickle_response_has_a_total_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    times = iter([0.0, 0.0, 7.0])
    monkeypatch.setattr("kodi_lab.kodi.bridge.monotonic", lambda: next(times))

    class SlowBody(httpx.SyncByteStream):
        def __iter__(self) -> Any:
            yield b"{"
            yield b"}"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.extensions["timeout"]["read"] <= 1.0
        return httpx.Response(
            200, headers={"content-type": "application/json"}, stream=SlowBody()
        )

    client = ExternalBridgeClient(transport=httpx.MockTransport(handler))
    endpoint = ValidatedExternalEndpoint(
        ExternalProtocolKind.JSONRPC_HTTP, "127.0.0.1", 8080
    )
    with pytest.raises(ExternalBridgeError, match="bridge_unreachable"):
        client.system_snapshot(
            endpoint,
            bridge_port=8765,
            target_id=uuid4(),
            bridge_id=uuid4(),
            secret=os.urandom(32),
        )


def test_windows_uses_direct_version_api_without_platform_fallbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge = _snapshot_bridge(monkeypatch)
    collect = bridge["_system_snapshot"]
    monkeypatch.setitem(
        collect.__globals__, "os", SimpleNamespace(cpu_count=lambda: 4, path=os.path)
    )
    monkeypatch.setitem(
        collect.__globals__,
        "sys",
        SimpleNamespace(
            platform="win32",
            getwindowsversion=lambda: SimpleNamespace(major=10, minor=0, build=26100),
        ),
    )
    result = collect()
    assert result["os_name"] == "Windows" and result["os_release"] == "10.0.26100"
    assert result["machine"] is None and result["logical_cpu_count"] == 4
