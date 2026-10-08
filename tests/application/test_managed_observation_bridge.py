"""Observation only consumes lifecycle trust; it cannot establish or repair it."""

from __future__ import annotations

from dataclasses import replace
from typing import Any
from uuid import UUID, uuid4

import pytest

from kodi_lab.application.managed_observation_bridge import (
    ManagedObservationBridgeService,
)
from kodi_lab.kodi.ui_state_native import UiObservationError
from kodi_lab.storage.models import ManagedBridgeTrustRow
from kodi_lab.storage.secrets import SecretStoreError
from tests.application.test_managed_bridge_trust import row
from tests.application.test_managed_bridge_trust import setup as setup
from tests.web.conftest import INSTANCE_ID
from tests.web.conftest import database as database


def consumer(setup: Any) -> tuple[ManagedObservationBridgeService, Any]:
    owner, broker, client, secrets = setup
    return ManagedObservationBridgeService(
        owner.sessions, secrets, client=client
    ), broker.observe_managed_kodi_runtime(
        managed_instance_id=str(INSTANCE_ID), expected_container_id="a" * 64
    )


def test_missing_trust_never_bootstraps(setup: Any) -> None:
    service, observed = consumer(setup)
    assert service.collect(INSTANCE_ID, observed, "21.3") is None
    assert setup[1].establishments == 0 and setup[2].calls == 0


@pytest.mark.parametrize("version", ["19.4", "20.5", "21.3"])
def test_same_generic_profile_consumes_established_trust(
    setup: Any, version: str
) -> None:
    owner, broker, client, _ = setup
    owner.prepare(INSTANCE_ID)
    client.value = replace(client.value, kodi_version=version)
    from kodi_lab.application.managed_bridge_trust import identity_json

    with owner.sessions.begin() as session:
        session.get(
            ManagedBridgeTrustRow, str(INSTANCE_ID)
        ).identity_json = identity_json(client.value)
    service, observed = consumer(setup)
    assert service.collect(INSTANCE_ID, observed, version) is not None
    assert broker.establishments == 1


@pytest.mark.parametrize(
    "change",
    ["container", "revision", "bridge", "revoked", "missing_secret", "malformed"],
)
def test_stale_trust_fails_closed_without_repair(setup: Any, change: str) -> None:
    owner, broker, client, secrets = setup
    owner.prepare(INSTANCE_ID)
    service, observed = consumer(setup)
    if change == "container":
        observed = observed.model_copy(update={"container_id": "b" * 64})
    elif change == "revision":
        observed = observed.model_copy(update={"runtime_revision_id": str(uuid4())})
    elif change == "bridge":
        client.value = replace(client.value, bridge_id=uuid4())
    elif change == "revoked":
        owner.revoke(INSTANCE_ID)
    elif change == "missing_secret":
        secrets.retire(UUID(row(owner).secret_reference))
    else:
        with owner.sessions.begin() as session:
            session.get(ManagedBridgeTrustRow, str(INSTANCE_ID)).identity_json = "{}"
    with pytest.raises(UiObservationError):
        service.collect(INSTANCE_ID, observed, "21.3")
    assert broker.establishments == 1


def test_unavailable_falls_back_bounded_and_recovers(setup: Any) -> None:
    owner, broker, client, _ = setup
    owner.prepare(INSTANCE_ID)
    service, observed = consumer(setup)
    client.fail = "bridge_unreachable"
    assert service.collect(INSTANCE_ID, observed, "21.3") is None
    assert row(owner).state == "active"
    client.fail = None
    assert (
        service.collect(INSTANCE_ID, observed, "21.3") is not None
        and broker.establishments == 1
    )


def test_revocation_during_request_invalidates_result(setup: Any) -> None:
    owner, _, client, _ = setup
    owner.prepare(INSTANCE_ID)
    service, observed = consumer(setup)
    client.before_return = lambda: owner.revoke(INSTANCE_ID)
    with pytest.raises(UiObservationError):
        service.collect(INSTANCE_ID, observed, "21.3")


def test_consumer_deletion_does_not_own_credential(setup: Any) -> None:
    owner, _, _, secrets = setup
    owner.prepare(INSTANCE_ID)
    reference = UUID(row(owner).secret_reference)
    service, _ = consumer(setup)
    service.close()
    del service
    assert secrets.read(INSTANCE_ID, reference, "request_authentication")
    assert row(owner).state == "active"


def test_credential_target_bound(setup: Any) -> None:
    owner, _, _, secrets = setup
    owner.prepare(INSTANCE_ID)
    with pytest.raises(SecretStoreError):
        secrets.read(
            uuid4(), UUID(row(owner).secret_reference), "request_authentication"
        )
