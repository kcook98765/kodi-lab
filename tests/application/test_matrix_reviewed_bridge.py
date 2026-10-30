"""Native-qualified Matrix candidate retains exact managed trust boundaries."""

from __future__ import annotations

from dataclasses import replace
from typing import Any
from uuid import UUID, uuid4

import pytest

from kodi_lab.application.managed_bridge_trust import ManagedBridgeTrustError
from kodi_lab.application.managed_launch_bridge import ManagedLaunchBridgeService
from kodi_lab.kodi.bridge import ExternalBridgeError
from kodi_lab.storage.secrets import SecretStoreError
from tests.application.test_managed_bridge_trust import row
from tests.application.test_managed_bridge_trust import setup as setup
from tests.application.test_nexus_reviewed_bridge import nexus, set_release
from tests.web.conftest import INSTANCE_ID
from tests.web.conftest import database as database


def matrix(setup: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    native = nexus(setup, monkeypatch)
    owner, _, client, _ = setup
    set_release(owner.sessions, "19.4")
    native.release = "19.4"
    client.value = replace(client.value, kodi_version="19.4")
    return native


def test_matrix_explicit_install_authentication_restart_and_settings_refusal(
    setup: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, broker, _, secrets = setup
    native = matrix(setup, monkeypatch)
    assert owner.status(INSTANCE_ID)["addon_version"] == "1.1.31"
    assert not owner.prepare(INSTANCE_ID)
    assert broker.establishments == 0
    owner.stage_reviewed(INSTANCE_ID)
    prepared = row(owner)
    assert prepared.reviewed_installation and prepared.state == "prepared"
    native.missing = False
    assert owner.prepare(INSTANCE_ID)
    active = row(owner)
    consumer = ManagedLaunchBridgeService(owner.sessions, secrets)
    observed, _ = owner._admit(INSTANCE_ID)
    assert consumer.binding(observed, "19.4").generation == active.generation
    with pytest.raises(ExternalBridgeError, match="settings_kodi_unsupported"):
        consumer.settings_binding(observed, "19.4")
    with pytest.raises(SecretStoreError):
        secrets.read(uuid4(), UUID(active.secret_reference), "request_authentication")
    assert owner.prepare(INSTANCE_ID)
    assert row(owner).generation == active.generation
    assert row(owner).secret_reference == active.secret_reference


@pytest.mark.parametrize(
    "field,value",
    [
        ("kodi_version", "19.3"),
        ("kodi_version", "19.5"),
        ("kodi_version", "20.5"),
        ("addon_version", "1.1.29"),
        ("build_fingerprint", "0" * 64),
        ("protocol_version", "2"),
    ],
)
def test_matrix_rejects_wrong_authenticated_resident(
    setup: Any, monkeypatch: pytest.MonkeyPatch, field: str, value: str
) -> None:
    owner, _, client, _ = setup
    native = matrix(setup, monkeypatch)
    owner.stage_reviewed(INSTANCE_ID)
    native.missing = False
    client.value = replace(client.value, **{field: value})
    with pytest.raises(ManagedBridgeTrustError, match="establishment_failed"):
        owner.prepare(INSTANCE_ID)
    assert row(owner).state == "invalidated"
    assert row(owner).secret_reference is None


@pytest.mark.parametrize("field", ["container_id", "runtime_revision_id"])
def test_matrix_launch_rejects_container_and_runtime_drift(
    setup: Any, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    owner, _, _, secrets = setup
    native = matrix(setup, monkeypatch)
    owner.stage_reviewed(INSTANCE_ID)
    native.missing = False
    assert owner.prepare(INSTANCE_ID)
    observed, _ = owner._admit(INSTANCE_ID)
    consumer = ManagedLaunchBridgeService(owner.sessions, secrets)
    changed = observed.model_copy(
        update={field: "b" * 64 if field == "container_id" else str(uuid4())}
    )
    with pytest.raises(ExternalBridgeError, match="managed_bridge_upgrade_required"):
        consumer.binding(changed, "19.4")


def test_matrix_prerequisites_offer_only_explicit_reviewed_install(
    setup: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kodi_lab.application.managed_capability_prerequisites import (
        ManagedCapabilityPrerequisiteService,
    )

    owner, broker, client, _ = setup
    native = matrix(setup, monkeypatch)
    service = ManagedCapabilityPrerequisiteService(owner.sessions)
    before = service.inspect(INSTANCE_ID)
    assert before.candidate_addon_version == "1.1.31"
    assert broker.establishments == client.calls == 0 and not native.calls
    owner.stage_reviewed(INSTANCE_ID)
    prepared = service.inspect(INSTANCE_ID)
    assert prepared.next_action == "resume_reviewed_bridge"
    assert prepared.native_readiness == "unknown"
