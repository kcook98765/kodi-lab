"""Nexus uses the exact stable candidate and independently fenced managed trust."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any
from uuid import UUID

import pytest

from kodi_lab.application.managed_bridge_trust import ManagedBridgeTrustError
from kodi_lab.broker.managed_bridge_trust import approved_bridge_build
from kodi_lab.storage.models import (
    RuntimeDefinitionRow,
    RuntimeExecutionProfileRow,
    TargetRow,
)
from kodi_lab.storage.secrets import SecretStoreError
from tests.application.test_managed_bridge_trust import Native, row
from tests.application.test_managed_bridge_trust import setup as setup
from tests.web.conftest import DEFINITION_ID, INSTANCE_ID, REVISION_ID
from tests.web.conftest import database as database


def set_release(factory: Any, release: str) -> None:
    """Persist the real target/definition/profile rather than bypassing admission."""
    with factory.begin() as session:
        session.get(TargetRow, str(INSTANCE_ID)).kodi_version = release
        session.get(RuntimeDefinitionRow, str(DEFINITION_ID)).kodi_version = release
        session.get(
            RuntimeExecutionProfileRow, str(REVISION_ID)
        ).expected_kodi_version = release


class NexusNative(Native):
    missing = True
    release = "20.5"

    def post(self, *, body: bytes, **kwargs: Any) -> bytes:
        request = json.loads(body)
        if request["method"] == "Application.GetProperties":
            return json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": request["id"],
                    "result": {
                        "name": "Kodi",
                        "version": {
                            "major": int(self.release.split(".")[0]),
                            "minor": int(self.release.split(".")[1]),
                            "tag": "stable",
                        },
                    },
                }
            ).encode()
        if request["method"] == "Addons.GetAddonDetails" and self.missing:
            return json.dumps(
                {"jsonrpc": "2.0", "id": request["id"], "result": {}}
            ).encode()
        return super().post(body=body, **kwargs)


def nexus(setup: Any, monkeypatch: pytest.MonkeyPatch) -> NexusNative:
    owner, broker, client, _ = setup
    set_release(owner.sessions, "20.5")
    client.value = replace(client.value, kodi_version="20.5")
    original = broker.establish_managed_bridge_trust

    def establish(**kwargs: Any) -> Any:
        kwargs.pop("install_reviewed", None)
        return original(**kwargs)

    monkeypatch.setattr(broker, "establish_managed_bridge_trust", establish)
    native = NexusNative()
    owner.transport = native
    return native


def test_clean_nexus_candidate_stages_then_authenticates_independent_secret(
    setup: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, broker, client, secrets = setup
    native = nexus(setup, monkeypatch)
    assert owner.status(INSTANCE_ID) == {
        "state": "uninstalled",
        "generation": 0,
        "resume_available": False,
        "addon_version": "1.1.31",
    }
    assert approved_bridge_build("20.5") == approved_bridge_build("21.3")
    # Ordinary start does not bootstrap missing files or allocate a credential.
    assert not owner.prepare(INSTANCE_ID)
    assert broker.establishments == 0
    owner.stage_reviewed(INSTANCE_ID)
    prepared = row(owner)
    assert prepared.state == "prepared" and prepared.identity_json is None
    assert prepared.reviewed_installation
    old_secret = secrets.read(
        INSTANCE_ID, UUID(prepared.secret_reference), "request_authentication"
    )
    native.missing = False
    native.enabled = False
    owner.resume_reviewed(INSTANCE_ID, expected_generation=prepared.generation)
    active = row(owner)
    assert active.state == "active" and active.generation > prepared.generation
    assert active.bridge_id == str(client.value.bridge_id)
    assert active.identity_json is not None
    assert json.loads(active.identity_json)["kodi_version"] == "20.5"
    assert (
        secrets.read(
            INSTANCE_ID, UUID(active.secret_reference), "request_authentication"
        )
        != old_secret
    )
    with pytest.raises(SecretStoreError):
        secrets.read(
            INSTANCE_ID, UUID(prepared.secret_reference), "request_authentication"
        )
    # Same-container restart authenticates without replacing trust.
    assert owner.prepare(INSTANCE_ID)
    assert row(owner).generation == active.generation
    with pytest.raises(ManagedBridgeTrustError, match="resume_unavailable"):
        owner.resume_reviewed(INSTANCE_ID, expected_generation=prepared.generation)
    assert row(owner).generation == active.generation


@pytest.mark.parametrize("failure", ["omega", "build", "authentication"])
def test_nexus_refuses_wrong_resident_identity(
    setup: Any, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    owner, _, client, _ = setup
    native = nexus(setup, monkeypatch)
    owner.stage_reviewed(INSTANCE_ID)
    native.missing = False
    if failure == "omega":
        client.value = replace(client.value, kodi_version="21.3")
    elif failure == "build":
        client.value = replace(client.value, build_fingerprint="0" * 64)
    else:
        client.fail = "bridge_authentication_failed"
    with pytest.raises(ManagedBridgeTrustError):
        owner.prepare(INSTANCE_ID)
    assert row(owner).state == "invalidated" and row(owner).secret_reference is None


@pytest.mark.parametrize("release", ["20.4", "19.4", "21.2"])
def test_other_releases_remain_ineligible(setup: Any, release: str) -> None:
    owner, broker, _, _ = setup
    set_release(owner.sessions, release)
    owner.transport = NexusNative()
    owner.transport.release = release
    with pytest.raises(ManagedBridgeTrustError, match="candidate_unavailable"):
        owner.status(INSTANCE_ID)
    with pytest.raises(ManagedBridgeTrustError, match="candidate_unavailable"):
        owner.stage_reviewed(INSTANCE_ID)
    assert broker.establishments == 0


def test_nexus_lifecycle_installation_keeps_real_typed_lease(
    setup: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kodi_lab.application.readiness_tasks import ReadinessTaskRegistry
    from kodi_lab.application.runtime_lifecycle import ManagedRuntimeLifecycleService
    from kodi_lab.application.runtime_status import ManagedRuntimeStatusService
    from kodi_lab.broker.managed_bridge_trust import require_reviewed_installation_lease
    from kodi_lab.storage.models import ManagedInstanceMutationLeaseRow

    owner, broker, _, _ = setup
    nexus(setup, monkeypatch)
    establish = broker.establish_managed_bridge_trust

    def leased(**kwargs: Any) -> Any:
        with owner.sessions() as session:
            require_reviewed_installation_lease(session, str(INSTANCE_ID))
        return establish(**kwargs)

    monkeypatch.setattr(broker, "establish_managed_bridge_trust", leased)
    readiness = ReadinessTaskRegistry()
    lifecycle = ManagedRuntimeLifecycleService(
        owner.sessions,
        broker,
        readiness,
        ManagedRuntimeStatusService(owner.sessions, broker, readiness),
        bridge_trust=owner,
    )
    restarted = []
    monkeypatch.setattr(lifecycle, "_stop", lambda *args: restarted.append("stop"))
    monkeypatch.setattr(lifecycle, "_start", lambda *args: restarted.append("start"))
    # This proof isolates synchronous staging/lease ownership from native probing.
    monkeypatch.setattr(lifecycle, "_queue_readiness", lambda *args: None)
    try:
        lifecycle.install_reviewed_bridge(INSTANCE_ID)
        assert restarted == ["stop", "start"]
        assert row(owner).state == "prepared"
        with owner.sessions() as session:
            assert (
                session.get(ManagedInstanceMutationLeaseRow, str(INSTANCE_ID)) is None
            )
    finally:
        readiness.close()


def test_saved_nexus_prerequisites_match_candidate_and_prepared_continuation(
    setup: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kodi_lab.application.managed_capability_prerequisites import (
        ManagedCapabilityPrerequisiteService,
    )

    owner, broker, client, _ = setup
    native = nexus(setup, monkeypatch)
    service = ManagedCapabilityPrerequisiteService(owner.sessions)
    before = service.inspect(INSTANCE_ID)
    assert before.candidate_addon_version == "1.1.31"
    assert broker.establishments == client.calls == 0 and not native.calls
    owner.stage_reviewed(INSTANCE_ID)
    prepared = row(owner)
    result = service.inspect(INSTANCE_ID)
    assert result.candidate_addon_version == "1.1.31"
    assert result.next_action == "resume_reviewed_bridge"
    assert result.native_readiness == "unknown"
    assert row(owner).generation == prepared.generation
    native.missing = False
    owner.resume_reviewed(INSTANCE_ID, expected_generation=prepared.generation)
    active = service.inspect(INSTANCE_ID)
    assert active.last_authenticated_addon_version == "1.1.31"
    assert active.native_readiness == "unknown"
