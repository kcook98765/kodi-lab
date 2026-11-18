"""Optional enrichment retains native reads and exact reviewed Bridge trust."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, cast

import pytest

from kodi_lab.application.external_bridges import ExternalBridgeServiceError
from kodi_lab.application.managed_bridge_trust import identity_json
from kodi_lab.kodi.system_info import BridgeSystemSnapshot
from kodi_lab.kodi.ui_state_native import UiObservationError
from kodi_lab.storage.models import (
    ExternalBridgePairingRow,
    ManagedBridgeTrustRow,
    ReviewedBridgeBuildRow,
)
from tests.application.test_managed_bridge_trust import setup as setup
from tests.application.test_managed_observation_bridge import consumer
from tests.application.test_production_inspection import _Environment
from tests.application.test_production_inspection import (
    production_inspection as production_inspection,
)
from tests.application.test_production_otp_pairing import _code
from tests.application.test_production_otp_pairing import (
    production_otp as production_otp,
)
from tests.application.test_system_info import _native
from tests.web.conftest import INSTANCE_ID
from tests.web.conftest import database as database


def facts() -> BridgeSystemSnapshot:
    return BridgeSystemSnapshot(
        schema_version=1,
        logical_cpu_count=8,
        os_name="Linux",
        os_release="6.1",
        machine="x86_64",
        profile_storage_total=1000,
        profile_storage_used=400,
        profile_storage_free=500,
    )


def _managed(setup: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    owner, _, client, _ = setup
    client.value = replace(
        client.value, capabilities=("system_snapshot", "ui_observation")
    )
    owner.prepare(INSTANCE_ID)
    monkeypatch.setattr(
        client,
        "system_snapshot",
        lambda *args, **kwargs: (client.identity(*args, **kwargs), facts()),
    )
    return consumer(setup)


def test_managed_exact_trust_enriches_and_does_not_repair(
    setup: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, observed = _managed(setup, monkeypatch)
    assert service.system_snapshot(INSTANCE_ID, observed, "21.3") == facts()
    assert setup[1].establishments == 1


@pytest.mark.parametrize("fault", ["bridge", "revoked", "container", "during"])
def test_managed_trust_mismatch_is_never_native_fallback(
    setup: Any, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    owner, _, client, _ = setup
    service, observed = _managed(setup, monkeypatch)
    if fault == "bridge":
        client.value = replace(client.value, build_fingerprint="a" * 64)
    elif fault == "revoked":
        owner.revoke(INSTANCE_ID)
    elif fault == "container":
        observed = observed.model_copy(update={"container_id": "b" * 64})
    else:
        client.before_return = lambda: owner.revoke(INSTANCE_ID)
    with pytest.raises(UiObservationError, match="identity_mismatch"):
        service.system_snapshot(INSTANCE_ID, observed, "21.3")
    assert setup[1].establishments == 1


def test_managed_old_capability_is_normal_and_historical_identity_remains_valid(
    setup: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kodi_lab.broker.managed_bridge_trust import approved_bridge_build

    owner, _, client, _ = setup
    service, observed = _managed(setup, monkeypatch)
    old = approved_bridge_build(system_predecessor=True)
    client.value = replace(
        client.value,
        addon_version=old["addon_version"],
        build_fingerprint=old["build_fingerprint"],
        capabilities=("ui_observation",),
    )
    with owner.sessions.begin() as session:
        row = session.get(ManagedBridgeTrustRow, str(INSTANCE_ID))
        assert row is not None
        row.identity_json = identity_json(client.value)
    with pytest.raises(UiObservationError, match="bridge_capability_unavailable"):
        service.system_snapshot(INSTANCE_ID, observed, "21.3")


@pytest.mark.parametrize(
    "code",
    [
        "bridge_not_paired",
        "bridge_capability_unavailable",
        "bridge_unreachable",
        "bridge_operation_failed",
    ],
)
def test_optional_bridge_failures_preserve_native(
    production_inspection: _Environment, code: str
) -> None:
    from types import SimpleNamespace

    env = production_inspection
    _native(env)

    def unavailable(target: object) -> None:
        raise ExternalBridgeServiceError(code)

    env.adapters._external_bridges = cast(
        Any, SimpleNamespace(system_snapshot=unavailable)
    )
    value = env.tools.system_info(env.principal, target_id=env.target_id)
    assert value.metrics["kodi_version"].value == "21.3"
    assert value.enrichment is not None and value.enrichment.reason == code


@pytest.mark.parametrize(
    "code",
    [
        "bridge_identity_mismatch",
        "bridge_build_not_reviewed",
        "bridge_reverification_required",
        "transport_acknowledgement_required",
        "bridge_authentication_failed",
    ],
)
def test_trust_failures_remain_typed_denials(
    production_inspection: _Environment, code: str
) -> None:
    from types import SimpleNamespace

    from kodi_lab.mcp.authorization import McpAuthorizationError

    env = production_inspection
    _native(env)

    def untrusted(target: object) -> None:
        raise ExternalBridgeServiceError(code)

    env.adapters._external_bridges = cast(
        Any, SimpleNamespace(system_snapshot=untrusted)
    )
    with pytest.raises(McpAuthorizationError, match=code):
        env.tools.system_info(env.principal, target_id=env.target_id)


@pytest.mark.parametrize("fault", [None, "build", "review", "revoked_during"])
def test_production_authenticated_snapshot_requires_current_review(
    production_otp: Any, monkeypatch: pytest.MonkeyPatch, fault: str | None
) -> None:
    factory, service, bridge, port, _, target = production_otp
    service.begin_otp_pairing(target, bridge_port=port, code=_code(bridge))
    service.commit_otp_pairing(target)
    if fault == "build":
        bridge["_identity"].__globals__["BUILD_FINGERPRINT"] = "f" * 64
    elif fault == "review":
        # A retired reviewed build must never be consumed.
        with factory.begin() as session:
            pairing = session.get(ExternalBridgePairingRow, str(target))
            assert pairing is not None
            reviewed = session.get(ReviewedBridgeBuildRow, pairing.reviewed_build_id)
            assert reviewed is not None
            reviewed.state = "retired"
            reviewed.retired_by = "fixture"
            reviewed.retired_at = "2026-11-18T00:00:00.000000Z"
    elif fault == "revoked_during":
        original = service._client.system_snapshot

        def revoked(*args: Any, **kwargs: Any) -> Any:
            value = original(*args, **kwargs)
            service.revoke(target)
            return value

        monkeypatch.setattr(service._client, "system_snapshot", revoked)
    if fault is not None:
        with pytest.raises(ExternalBridgeServiceError):
            service.system_snapshot(target)
    else:
        before = service.status(target)
        assert service.system_snapshot(target).schema_version == 1
        assert service.status(target) == before
