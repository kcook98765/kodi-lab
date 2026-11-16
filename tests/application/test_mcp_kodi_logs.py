"""Read-only log authority, exact managed trust and real production Bridge reads."""

from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import delete

from kodi_lab.application.external_bridges import ExternalBridgeServiceError
from kodi_lab.application.managed_bridge_trust import (
    ManagedBridgeTrustError,
    identity_json,
    require_approved_identity,
)
from kodi_lab.broker.managed_bridge_trust import approved_bridge_build
from kodi_lab.kodi.bridge import BridgeIdentity
from kodi_lab.kodi.logs import (
    KodiLogRequest,
    KodiLogResult,
    KodiLogScanBridgeRequest,
    KodiLogScanRequest,
    KodiLogScanResult,
)
from kodi_lab.kodi.remote import BrowserRemoteAction
from kodi_lab.kodi.ui_state_native import UiObservationError
from kodi_lab.mcp.authorization import McpAuthorizationError
from kodi_lab.storage.models import (
    ManagedBridgeTrustRow,
    McpClientTargetGrantRow,
)
from tests.application.test_external_bridges import _seed
from tests.application.test_managed_bridge_trust import setup as setup
from tests.application.test_managed_observation_bridge import consumer
from tests.application.test_production_inspection import (
    production_inspection as production_inspection,
)
from tests.application.test_production_otp_pairing import _code
from tests.application.test_production_otp_pairing import (
    production_otp as production_otp,
)
from tests.web.conftest import INSTANCE_ID
from tests.web.conftest import database as database


def excerpt(request: KodiLogRequest) -> KodiLogResult:
    text = "sensitive log marker\n" if request.mode == "tail" else ""
    return KodiLogResult(
        log=request.log,
        mode=request.mode,
        location="special://logpath/kodi.log"
        if request.log == "current"
        else "special://logpath/kodi.old.log",
        snapshot_id="a" * 64,
        size_bytes=len(text),
        modified_at_ns=1,
        observed_at="2026-10-17T00:00:00.000000Z",
        start_byte=0,
        end_byte=len(text),
        bytes_read=len(text),
        lines_returned=1 if text else 0,
        truncated=False,
        partial_first_line=False,
        content=text,
    )


@pytest.mark.parametrize("log", ["current", "previous"])
def test_managed_logs_consume_lifecycle_trust_without_establishing_it(
    setup: Any,
    monkeypatch: pytest.MonkeyPatch,
    log: Any,
) -> None:
    owner, broker, client, _ = setup
    client.value = replace(client.value, capabilities=("ui_observation", "log_read"))
    owner.prepare(INSTANCE_ID)
    service, observed = consumer(setup)
    request = KodiLogRequest.model_validate({"log": log, "mode": "tail"})
    monkeypatch.setattr(
        client, "read_log", lambda *a, **k: (client.identity(), excerpt(request))
    )
    assert service.read_log(INSTANCE_ID, observed, "21.3", request).content
    assert broker.establishments == 1
    client.before_return = lambda: owner.revoke(INSTANCE_ID)
    with pytest.raises(UiObservationError, match="identity_mismatch"):
        service.read_log(INSTANCE_ID, observed, "21.3", request)
    assert broker.establishments == 1


def test_managed_log_missing_capability_and_stale_container_fail_closed(
    setup: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner, _, client, _ = setup
    owner.prepare(INSTANCE_ID)
    service, observed = consumer(setup)
    request = KodiLogRequest()
    monkeypatch.setattr(
        client,
        "read_log",
        lambda *a, **k: pytest.fail("unsupported log read dispatched"),
    )
    with pytest.raises(UiObservationError, match="capability_unavailable"):
        service.read_log(INSTANCE_ID, observed, "21.3", request)
    client.value = replace(client.value, capabilities=("ui_observation", "log_read"))
    with owner.sessions.begin() as session:
        session.get(
            ManagedBridgeTrustRow, str(INSTANCE_ID)
        ).identity_json = identity_json(client.value)
    with pytest.raises(UiObservationError, match="identity_mismatch"):
        service.read_log(
            INSTANCE_ID,
            observed.model_copy(update={"container_id": "b" * 64}),
            "21.3",
            request,
        )


@pytest.mark.parametrize("target_type", ["external_production", "external_development"])
def test_real_external_bridge_reads_both_logs_with_inspection_authority(
    production_otp: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    target_type: str,
) -> None:
    factory, service, bridge, port, _, target = production_otp
    if target_type == "external_development":
        target = uuid4()
        _seed(factory, target)
    service.begin_otp_pairing(target, bridge_port=port, code=_code(bridge))
    service.commit_otp_pairing(target)
    root = tmp_path / "logs"
    root.mkdir()
    vfs = sys.modules["xbmcvfs"]
    original = vfs.translatePath
    monkeypatch.setattr(
        vfs,
        "translatePath",
        lambda p: str(root) if p == "special://logpath" else original(p),
    )
    (root / "kodi.log").write_text("current log\n", encoding="utf-8")
    (root / "kodi.old.log").write_text("crash before restart\n", encoding="utf-8")
    for log, text in [
        ("current", "current log\n"),
        ("previous", "crash before restart\n"),
    ]:
        assert (
            service.read_log(
                target, KodiLogRequest.model_validate({"log": log, "mode": "tail"})
            ).content
            == text
        )
    (root / "kodi.old.log").unlink()
    with pytest.raises(ExternalBridgeServiceError, match="log_missing"):
        service.read_log(target, KodiLogRequest(log="previous"))
    if target_type == "external_production":
        service.retire_reviewed_build(service.list_reviewed_builds()[0].build_id)
        with pytest.raises(ExternalBridgeServiceError, match="bridge_not_paired"):
            service.read_log(target, KodiLogRequest())


@pytest.mark.parametrize("revoke", [False, True])
def test_production_inspection_grant_reads_logs_and_rechecks_after_read(
    production_inspection: Any,
    revoke: bool,
) -> None:
    env = production_inspection
    request = KodiLogRequest(mode="tail")
    calls = []

    def read(target: Any, requested: KodiLogRequest) -> KodiLogResult:
        calls.append(target)
        if revoke:
            with env.factory.begin() as session:
                session.execute(
                    delete(McpClientTargetGrantRow).where(
                        McpClientTargetGrantRow.client_id == str(env.client_id)
                    )
                )
        return excerpt(requested)

    env.adapters._external_bridges = SimpleNamespace(
        read_log=read,
        screenshot_available=lambda t: False,
        deployment_available=lambda t: False,
    )
    assert "log_read" in env.authorization.discoverable_tools(env.principal)
    assert "remote_action" in env.authorization.discoverable_tools(env.principal)
    # Discovery does not grant per-device navigation or dispatch native input.
    with pytest.raises(McpAuthorizationError, match="production_mutations_blocked"):
        env.authorization.authorize_production_navigation(
            env.principal, env.target_id, BrowserRemoteAction.HOME
        )
    if revoke:
        with pytest.raises(
            McpAuthorizationError, match="target_not_found_or_not_authorized"
        ):
            env.tools.read_log(env.principal, target_id=env.target_id, request=request)
    else:
        assert env.tools.read_log(
            env.principal, target_id=env.target_id, request=request
        ).content
    assert calls == [env.target_id]
    assert [call["method"] for call in env.calls] == [
        "JSONRPC.Ping",
        "JSONRPC.Version",
        "Application.GetProperties",
    ]


def test_production_native_identity_mismatch_prevents_log_access(
    production_inspection: Any,
) -> None:
    env = production_inspection
    env.live_name[0] = "Different Kodi"
    env.adapters._external_bridges = SimpleNamespace(
        read_log=lambda *a: pytest.fail("read after identity mismatch"),
        invalidate_production_native_identity=lambda *a: None,
    )
    with pytest.raises(McpAuthorizationError, match="identity_mismatch"):
        env.tools.read_log(
            env.principal, target_id=env.target_id, request=KodiLogRequest()
        )


def scan_excerpt(request: KodiLogScanRequest) -> KodiLogScanResult:
    names = ["current", "previous"] if request.logs == "both" else [request.logs]
    return KodiLogScanResult.model_validate(
        {
            "files": [
                {
                    "log": name,
                    "file_id": "a" * 64,
                    "observed_at": "2026-11-15T12:00:00.000000Z",
                    "boundary_bytes": 100,
                    "scanned_bytes": 100,
                    "verified_bytes": 0,
                    "state": "complete",
                    "counts_exact": True,
                    "records_scanned": 1,
                    "matched_records": 1,
                    "severity_counts": {"ERROR": 1},
                    "unknown_timestamp_records": 0,
                    "append_observed": False,
                }
                for name in names
            ],
            "matches": [
                {
                    "log": names[0],
                    "start_byte": 0,
                    "end_byte": 100,
                    "severity": "ERROR",
                    "timestamp": None,
                    "content": "sensitive scan marker",
                    "content_truncated": False,
                    "before": [],
                    "after": [],
                }
            ]
            if request.max_matches
            else [],
            "complete": True,
            "counts_exact": True,
            "excerpts_truncated": False,
            "reason": "complete",
            "cursor": None,
            "bytes_read": 100,
            "count_unit": "logical_records",
        }
    )


@pytest.mark.parametrize("failure", [None, "missing_capability", "revoked", "identity"])
def test_managed_scan_uses_existing_trust_and_rechecks_identity(
    setup: Any,
    monkeypatch: pytest.MonkeyPatch,
    failure: str | None,
) -> None:
    owner, broker, client, _ = setup
    client.value = replace(
        client.value,
        capabilities=("ui_observation", "log_read")
        + (() if failure == "missing_capability" else ("log_scan",)),
    )
    owner.prepare(INSTANCE_ID)
    service, observed = consumer(setup)
    requested = KodiLogScanBridgeRequest(client_binding="c" * 64)
    calls = []

    def read(*args: Any, **kwargs: Any) -> Any:
        calls.append(kwargs["request"])
        if failure == "revoked":
            owner.revoke(INSTANCE_ID)
        identity = (
            replace(client.value, bridge_id=uuid4())
            if failure == "identity"
            else client.value
        )
        return identity, scan_excerpt(requested)

    monkeypatch.setattr(client, "read_log", read)
    if failure:
        with pytest.raises(
            UiObservationError,
            match="capability_unavailable"
            if failure == "missing_capability"
            else "identity_mismatch",
        ):
            service.read_log(INSTANCE_ID, observed, "21.3", requested)
    else:
        assert service.read_log(INSTANCE_ID, observed, "21.3", requested).counts_exact
    assert len(calls) == (0 if failure == "missing_capability" else 1)
    assert broker.establishments == 1


@pytest.mark.parametrize("target_type", ["external_production", "external_development"])
def test_real_external_scan_both_files_and_old_bridge_capability(
    production_otp: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    target_type: str,
) -> None:
    factory, service, bridge, port, _, target = production_otp
    if target_type == "external_development":
        target = uuid4()
        _seed(factory, target)
    service.begin_otp_pairing(target, bridge_port=port, code=_code(bridge))
    service.commit_otp_pairing(target)
    root = tmp_path / "scan-logs"
    root.mkdir()
    vfs = sys.modules["xbmcvfs"]
    original = vfs.translatePath
    monkeypatch.setattr(
        vfs,
        "translatePath",
        lambda p: str(root) if p == "special://logpath" else original(p),
    )
    for name in ("kodi.log", "kodi.old.log"):
        (root / name).write_text("ERROR: example failure\ntraceback line\n")
    request = KodiLogScanBridgeRequest(client_binding="c" * 64)
    result = service.read_log(target, request)
    assert result.counts_exact and [f.matched_records for f in result.files] == [1, 1]
    # A paired older capability set is refused before sending the new operation.

    from kodi_lab.storage.models import ExternalBridgePairingRow

    with factory.begin() as session:
        pairing = session.get(ExternalBridgePairingRow, str(target))
        pairing.observed_capabilities_json = json.dumps(
            sorted(set(bridge["CAPABILITIES"]) - {"log_scan"}), separators=(",", ":")
        )
    with pytest.raises(
        ExternalBridgeServiceError, match="bridge_capability_unavailable"
    ):
        service.read_log(target, request)


@pytest.mark.parametrize("failure", [None, "revoke", "identity", "ungranted"])
def test_production_scan_mcp_authority_and_publication_fence(
    production_inspection: Any,
    failure: str | None,
) -> None:
    env = production_inspection
    calls = []

    def read(target: Any, requested: KodiLogScanBridgeRequest) -> KodiLogScanResult:
        calls.append(target)
        assert (
            requested.client_binding
            == hashlib.sha256(
                f"{env.principal.client_id}:{env.principal.credential_id}".encode()
            ).hexdigest()
        )
        if failure == "revoke":
            with env.factory.begin() as session:
                session.execute(
                    delete(McpClientTargetGrantRow).where(
                        McpClientTargetGrantRow.client_id == str(env.client_id)
                    )
                )
        return scan_excerpt(requested)

    env.adapters._external_bridges = SimpleNamespace(
        read_log=read,
        screenshot_available=lambda t: False,
        deployment_available=lambda t: False,
        invalidate_production_native_identity=lambda *a: None,
    )
    if failure == "identity":
        env.live_name[0] = "Different Kodi"
    assert "log_scan" in env.authorization.discoverable_tools(env.principal)
    if failure:
        with pytest.raises(McpAuthorizationError):
            env.tools.scan_log(
                env.principal,
                target_id=uuid4() if failure == "ungranted" else env.target_id,
                request=KodiLogScanRequest(),
            )
    else:
        assert env.tools.scan_log(
            env.principal, target_id=env.target_id, request=KodiLogScanRequest()
        ).counts_exact
    assert len(calls) == (0 if failure in ("identity", "ungranted") else 1)


@pytest.mark.parametrize("release", ["21.3", "22.0-rc.1"])
def test_pre_scan_bridge_identity_remains_approved_for_established_reads(
    release: str,
) -> None:
    previous = approved_bridge_build(release, log_predecessor=True)
    identity = BridgeIdentity(
        bridge_id=uuid4(),
        protocol_version=previous["protocol_version"],
        addon_id=previous["addon_id"],
        addon_version=previous["addon_version"],
        kodi_version="22.0" if "-" in release else release,
        kodi_release=release if "-" in release else None,
        capabilities=("log_read", "ui_observation"),
        build_fingerprint=previous["build_fingerprint"],
    )
    require_approved_identity(identity, release, established=True)
    with pytest.raises(ManagedBridgeTrustError):
        require_approved_identity(identity, release)
    with pytest.raises(ManagedBridgeTrustError):
        require_approved_identity(
            replace(identity, build_fingerprint="0" * 64), release, established=True
        )
