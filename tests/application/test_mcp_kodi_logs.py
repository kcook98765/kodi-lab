"""Read-only log authority, exact managed trust and real production Bridge reads."""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import delete

from kodi_lab.application.external_bridges import ExternalBridgeServiceError
from kodi_lab.application.managed_bridge_trust import identity_json
from kodi_lab.kodi.logs import KodiLogRequest, KodiLogResult
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
    assert "remote_action" not in env.authorization.discoverable_tools(env.principal)
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
