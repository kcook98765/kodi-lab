"""Lifecycle-owned trust: real prepare/confirm/activate, no pairing coordinator."""

from __future__ import annotations

import json
from dataclasses import replace
from itertools import count
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.managed_bridge_trust import (
    ManagedBridgeTrustError,
    ManagedBridgeTrustService,
    identity_json,
    invalidate_managed_bridge_trust,
)
from kodi_lab.broker.docker_inventory import ContainerRuntimeState
from kodi_lab.broker.managed_bridge_trust import approved_bridge_build
from kodi_lab.broker.operations import EstablishManagedBridgeTrustResult
from kodi_lab.kodi.bridge import (
    BridgeIdentity,
    ExternalBridgeClient,
    ExternalBridgeError,
)
from kodi_lab.kodi.ui_state import UiCollection
from kodi_lab.storage.models import ManagedBridgeTrustRow
from kodi_lab.storage.secrets import ExternalBridgeSecretStore, SecretStoreError
from tests.kodi.test_ui_state import movie_collection
from tests.web.conftest import INSTANCE_ID, FakeBroker, inventory_entry
from tests.web.conftest import database as database


class Client(ExternalBridgeClient):
    def __init__(self) -> None:
        approved = approved_bridge_build()
        self.value = BridgeIdentity(
            uuid4(),
            approved["protocol_version"],
            approved["addon_id"],
            approved["addon_version"],
            "21.3",
            ("ui_observation",),
            approved["build_fingerprint"],
        )
        self.fail: str | None = None
        self.calls = 0
        self.before_return: Any = None

    def identity(self, *args: Any, **kwargs: Any) -> BridgeIdentity:
        self.calls += 1
        if self.fail:
            raise ExternalBridgeError(self.fail)
        if self.before_return:
            self.before_return()
        return self.value

    def inspect_ui_state(
        self, *args: Any, **kwargs: Any
    ) -> tuple[BridgeIdentity, UiCollection]:
        return self.identity(*args, **kwargs), movie_collection()


class Broker(FakeBroker):
    client: Client
    fail_establish = False
    establishments = 0

    def establish_managed_bridge_trust(
        self, **kwargs: Any
    ) -> EstablishManagedBridgeTrustResult:
        self.establishments += 1
        if self.fail_establish:
            raise RuntimeError("fixed provisioning failed")
        container = kwargs.pop("expected_container_id")
        return EstablishManagedBridgeTrustResult(
            **kwargs, container_id=container, bridge_id=str(self.client.value.bridge_id)
        )


class Native:
    enabled = True
    calls: list[str]

    def __init__(self) -> None:
        self.calls = []

    def post(self, *, body: bytes, **kwargs: Any) -> bytes:
        request = json.loads(body)
        method = request["method"]
        self.calls.append(method)
        if method == "Application.GetProperties":
            result: Any = {
                "name": "Kodi",
                "version": {"major": 21, "minor": 3, "tag": "stable"},
            }
        elif method == "Addons.GetAddonDetails":
            result = {
                "addon": {
                    "addonid": "service.kodi_lab_bridge",
                    "version": "1.1.25",
                    "enabled": self.enabled,
                }
            }
        elif method == "Addons.SetAddonEnabled":
            self.enabled = request["params"]["enabled"]
            result = "OK"
        else:
            raise AssertionError(method)
        return json.dumps(
            {"jsonrpc": "2.0", "id": request["id"], "result": result}
        ).encode()


@pytest.fixture
def setup(
    database: sessionmaker[Session], tmp_path: Path
) -> tuple[ManagedBridgeTrustService, Broker, Client, ExternalBridgeSecretStore]:
    client = Client()
    broker = Broker(container=inventory_entry(ContainerRuntimeState.RUNNING))
    broker.client = client
    secrets = ExternalBridgeSecretStore(
        tmp_path / "secrets", master_key=b"x" * 32, key_version=1
    )
    owner = ManagedBridgeTrustService(database, broker, secrets, client=client)  # type: ignore[arg-type]
    owner.transport = Native()
    return owner, broker, client, secrets


def row(owner: ManagedBridgeTrustService) -> ManagedBridgeTrustRow:
    with owner.sessions() as session:
        value = session.get(ManagedBridgeTrustRow, str(INSTANCE_ID))
        assert value is not None
        session.expunge(value)
        return value


def test_prepare_confirms_identity_before_active_and_reuses_after_restart(
    setup: Any,
) -> None:
    owner, broker, client, secrets = setup
    assert owner.prepare(INSTANCE_ID)
    active = row(owner)
    assert active.state == "active" and active.bridge_id == str(client.value.bridge_id)
    assert secrets.read(
        INSTANCE_ID, UUID(active.secret_reference), "request_authentication"
    )
    assert owner.prepare(INSTANCE_ID)  # same-container Bridge/Kodi restart
    assert row(owner).generation == active.generation and broker.establishments == 1
    assert all(
        "otp" not in method.lower() and "pair" not in method.lower()
        for method in owner.transport.calls
    )


def test_existing_previous_bridge_keeps_its_credential_and_generation(
    setup: Any,
) -> None:
    owner, broker, client, secrets = setup
    assert owner.prepare(INSTANCE_ID)
    previous = approved_bridge_build(previous=True)
    client.value = replace(
        client.value,
        addon_version=previous["addon_version"],
        build_fingerprint=previous["build_fingerprint"],
    )
    with owner.sessions.begin() as session:
        active = session.get(ManagedBridgeTrustRow, str(INSTANCE_ID))
        assert active is not None
        active.identity_json = identity_json(client.value)
    before = row(owner)
    assert owner.prepare(INSTANCE_ID)
    after = row(owner)
    assert (after.generation, after.secret_reference, after.identity_json) == (
        before.generation,
        before.secret_reference,
        before.identity_json,
    )
    assert after.state == "active" and broker.establishments == 1
    assert secrets.read(
        INSTANCE_ID, UUID(after.secret_reference), "request_authentication"
    )


@pytest.mark.parametrize(
    "code", ["bridge_authentication_failed", "bridge_identity_mismatch"]
)
def test_invalid_authenticated_identity_retires_authority(
    setup: Any, code: str
) -> None:
    owner, _, client, _ = setup
    owner.prepare(INSTANCE_ID)
    client.fail = code
    with pytest.raises(ManagedBridgeTrustError):
        owner.prepare(INSTANCE_ID)
    assert row(owner).state == "invalidated" and row(owner).secret_reference is None


def test_unavailable_restart_does_not_revoke_trust(
    setup: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    ticks = count(0.0, 6.0)
    monkeypatch.setattr(
        "kodi_lab.application.managed_bridge_trust.time.monotonic", lambda: next(ticks)
    )
    owner, broker, client, _ = setup
    owner.prepare(INSTANCE_ID)
    generation = row(owner).generation
    client.fail = "bridge_unreachable"
    with pytest.raises(ManagedBridgeTrustError, match="unavailable"):
        owner.prepare(INSTANCE_ID)
    assert row(owner).state == "active" and row(owner).generation == generation
    client.fail = None
    assert owner.prepare(INSTANCE_ID) and broker.establishments == 1


@pytest.mark.parametrize("field", ["bridge_id", "build_fingerprint", "kodi_version"])
def test_identity_drift_refused(setup: Any, field: str) -> None:
    owner, _, client, _ = setup
    owner.prepare(INSTANCE_ID)
    client.value = replace(
        client.value, **{field: uuid4() if field == "bridge_id" else "different"}
    )
    with pytest.raises(ManagedBridgeTrustError):
        owner.prepare(INSTANCE_ID)
    assert row(owner).state == "invalidated"


def test_partial_establishment_cannot_activate(setup: Any) -> None:
    owner, broker, _, secrets = setup
    broker.fail_establish = True
    with pytest.raises(ManagedBridgeTrustError):
        owner.prepare(INSTANCE_ID)
    assert row(owner).state == "invalidated" and row(owner).secret_reference is None
    assert not list(secrets._root.glob("*.json"))
    assert owner.transport.enabled is False
    broker.fail_establish = False
    assert owner.prepare(INSTANCE_ID, replace=True)
    assert row(owner).state == "active"


def test_missing_optional_bridge_leaves_native_target_available(setup: Any) -> None:
    owner, broker, _, _ = setup
    owner.transport.enabled = False
    assert owner.prepare(INSTANCE_ID) is False and broker.establishments == 0
    with owner.sessions() as session:
        assert session.get(ManagedBridgeTrustRow, str(INSTANCE_ID)) is None


def test_revocation_requires_explicit_lifecycle_reestablishment(setup: Any) -> None:
    owner, broker, _, secrets = setup
    owner.prepare(INSTANCE_ID)
    reference = UUID(row(owner).secret_reference)
    owner.revoke(INSTANCE_ID)
    assert row(owner).state == "revoked"
    with pytest.raises(SecretStoreError):
        secrets.read(INSTANCE_ID, reference, "request_authentication")
    with pytest.raises(ManagedBridgeTrustError, match="reestablishment"):
        owner.prepare(INSTANCE_ID)
    assert owner.prepare(INSTANCE_ID, replace=True) and broker.establishments == 2


def test_snapshot_reset_invalidation_retires_credential(setup: Any) -> None:
    owner, _, _, secrets = setup
    owner.prepare(INSTANCE_ID)
    reference = UUID(row(owner).secret_reference)
    with owner.sessions.begin() as session:
        invalidate_managed_bridge_trust(session, INSTANCE_ID)
    assert row(owner).state == "invalidated"
    with pytest.raises(SecretStoreError):
        secrets.read(INSTANCE_ID, reference, "request_authentication")


def test_unmanaged_target_cannot_establish(setup: Any) -> None:
    owner, broker, _, _ = setup
    with pytest.raises(ManagedBridgeTrustError):
        owner.prepare(uuid4())
    assert broker.establishments == 0


def test_stale_confirmation_cannot_retire_newer_lifecycle_generation(
    setup: Any,
) -> None:
    owner, broker, client, secrets = setup
    owner.prepare(INSTANCE_ID)
    other_client = Client()
    other_client.value = client.value
    other = ManagedBridgeTrustService(
        owner.sessions, broker, secrets, client=other_client
    )
    other.transport = Native()

    def replace_during_confirmation() -> None:
        client.before_return = None
        assert other.prepare(INSTANCE_ID, replace=True)

    client.before_return = replace_during_confirmation
    with pytest.raises(ManagedBridgeTrustError):
        owner.prepare(INSTANCE_ID)
    assert row(owner).state == "active" and row(owner).generation == 2
    assert secrets.read(
        INSTANCE_ID, UUID(row(owner).secret_reference), "request_authentication"
    )
