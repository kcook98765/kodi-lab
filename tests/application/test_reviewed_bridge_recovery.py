"""Closed admission and prepared-to-authenticated trust recovery."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any
from uuid import UUID

import pytest

from kodi_lab.application.managed_bridge_trust import (
    ManagedBridgeTrustError,
    invalidate_managed_bridge_trust,
)
from kodi_lab.broker.managed_bridge_trust import approved_bridge_build
from kodi_lab.storage.secrets import SecretStoreError
from tests.application.test_managed_bridge_trust import Native, row
from tests.application.test_managed_bridge_trust import setup as setup
from tests.web.conftest import INSTANCE_ID
from tests.web.conftest import database as database


class RCNative(Native):
    missing = True

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
                            "major": 22,
                            "minor": 0,
                            "tag": "releasecandidate",
                            "tagversion": "1",
                        },
                    },
                }
            ).encode()
        if request["method"] == "Addons.GetAddonDetails":
            if self.missing:
                raise RuntimeError("missing reviewed addon")
            return json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": request["id"],
                    "result": {
                        "addon": {
                            "addonid": "service.kodi_lab_bridge",
                            "version": "1.1.32",
                            "enabled": self.enabled,
                        }
                    },
                }
            ).encode()
        return super().post(body=body, **kwargs)


def rc(
    owner: Any, broker: Any, client: Any, monkeypatch: pytest.MonkeyPatch
) -> RCNative:
    observed, profile = owner._admit(INSTANCE_ID)
    monkeypatch.setattr(
        owner,
        "_admit",
        lambda _: (
            observed,
            replace(
                profile,
                expected_kodi_version="22.0",
                expected_version_tag="rc",
                expected_prerelease_ordinal=1,
            ),
        ),
    )
    approved = approved_bridge_build("22.0-rc.1")
    client.value = replace(
        client.value,
        kodi_version="22.0",
        kodi_release="22.0-rc.1",
        addon_version="1.1.32",
        build_fingerprint=approved["build_fingerprint"],
    )
    original = broker.establish_managed_bridge_trust

    def establish(**kwargs: Any) -> Any:
        kwargs.pop("install_reviewed", None)
        return original(**kwargs)

    monkeypatch.setattr(broker, "establish_managed_bridge_trust", establish)
    native = RCNative()
    owner.transport = native
    return native


def test_staging_is_not_active_and_restart_requires_fresh_authenticated_identity(
    setup: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, broker, client, secrets = setup
    native = rc(owner, broker, client, monkeypatch)
    owner.stage_reviewed(INSTANCE_ID)
    prepared = row(owner)
    assert prepared.state == "prepared" and prepared.identity_json is None
    native.missing = False
    native.enabled = False
    assert owner.prepare(INSTANCE_ID)
    active = row(owner)
    assert active.state == "active" and active.generation > prepared.generation
    assert active.identity_json is not None
    assert json.loads(active.identity_json)["kodi_release"] == "22.0-rc.1"
    with pytest.raises(SecretStoreError):
        secrets.read(
            INSTANCE_ID, UUID(prepared.secret_reference), "request_authentication"
        )
    with owner.sessions.begin() as session:
        invalidate_managed_bridge_trust(session, INSTANCE_ID, secrets=secrets)
    assert row(owner).secret_reference is None
    with pytest.raises(ManagedBridgeTrustError):
        owner.prepare(INSTANCE_ID)
    with pytest.raises(SecretStoreError):
        secrets.read(
            INSTANCE_ID, UUID(active.secret_reference), "request_authentication"
        )
    native.missing = True
    owner.stage_reviewed(INSTANCE_ID)
    native.missing = False
    assert owner.prepare(INSTANCE_ID)
    assert row(owner).state == "active" and row(owner).generation > active.generation


def test_explicit_stable_reviewed_upgrade_is_prepared_until_restart(
    setup: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, broker, _, _ = setup
    original = broker.establish_managed_bridge_trust
    admitted = []

    def establish(**kwargs: Any) -> Any:
        admitted.append(kwargs.pop("install_reviewed", False))
        return original(**kwargs)

    monkeypatch.setattr(broker, "establish_managed_bridge_trust", establish)
    owner.stage_reviewed(INSTANCE_ID)
    assert admitted == [True] and broker.establishments == 1
    assert row(owner).state == "prepared" and row(owner).reviewed_installation
    assert owner.prepare(INSTANCE_ID)
    assert row(owner).state == "active"


@pytest.mark.parametrize("failure", ["wrong_release", "wrong_build", "authentication"])
def test_recovery_does_not_adopt_wrong_or_stale_identity(
    setup: Any, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    owner, broker, client, _ = setup
    native = rc(owner, broker, client, monkeypatch)
    owner.stage_reviewed(INSTANCE_ID)
    native.missing = False
    if failure == "wrong_release":
        client.value = replace(client.value, kodi_release="22.0-rc.2")
    elif failure == "wrong_build":
        client.value = replace(client.value, build_fingerprint="0" * 64)
    else:
        client.fail = "bridge_authentication_failed"
    with pytest.raises(ManagedBridgeTrustError):
        owner.prepare(INSTANCE_ID)
    assert row(owner).state == "invalidated" and row(owner).secret_reference is None


def test_stable_disabled_prepared_bridge_is_still_optional(setup: Any) -> None:
    owner, broker, _, _ = setup
    assert owner.prepare(INSTANCE_ID)
    from kodi_lab.storage.models import ManagedBridgeTrustRow

    with owner.sessions.begin() as session:
        value = session.get(ManagedBridgeTrustRow, str(INSTANCE_ID))
        assert value is not None
        value.state = "prepared"
        value.bridge_id = None
        value.identity_json = None
    owner.transport.enabled = False
    assert not owner.prepare(INSTANCE_ID)
    assert broker.establishments == 1 and row(owner).state == "prepared"


def stage_stable(owner: Any, broker: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    establish = broker.establish_managed_bridge_trust

    def staged(**kwargs: Any) -> Any:
        kwargs.pop("install_reviewed", None)
        return establish(**kwargs)

    monkeypatch.setattr(broker, "establish_managed_bridge_trust", staged)
    owner.stage_reviewed(INSTANCE_ID)
    return row(owner)


def test_explicit_prepared_continuation_authenticates_and_retires_old_secret(
    setup: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, broker, _client, secrets = setup
    prepared = stage_stable(owner, broker, monkeypatch)
    assert owner.status(INSTANCE_ID)["resume_available"] is True
    owner.resume_reviewed(INSTANCE_ID, expected_generation=prepared.generation)
    active = row(owner)
    assert active.state == "active" and active.generation > prepared.generation
    assert owner.status(INSTANCE_ID)["resume_available"] is False
    assert broker.establishments == 2
    with pytest.raises(SecretStoreError):
        secrets.read(
            INSTANCE_ID, UUID(prepared.secret_reference), "request_authentication"
        )


@pytest.mark.parametrize(
    "defect", ["generation", "container", "nonreviewed", "invalidated", "active"]
)
def test_prepared_continuation_cannot_reuse_stale_or_unreviewed_intent(
    setup: Any, monkeypatch: pytest.MonkeyPatch, defect: str
) -> None:
    from kodi_lab.storage.models import ManagedBridgeTrustRow

    owner, broker, _client, _secrets = setup
    prepared = stage_stable(owner, broker, monkeypatch)
    generation = prepared.generation
    if defect == "active":
        owner.prepare(INSTANCE_ID)
    elif defect == "generation":
        generation += 1
    else:
        with owner.sessions.begin() as session:
            current = session.get(ManagedBridgeTrustRow, str(INSTANCE_ID))
            assert current is not None
            if defect == "container":
                current.container_id = "f" * 64
            elif defect == "nonreviewed":
                current.reviewed_installation = False
            else:
                invalidate_managed_bridge_trust(
                    session, INSTANCE_ID, secrets=owner.secrets
                )
    before = row(owner)
    calls = broker.establishments
    with pytest.raises(ManagedBridgeTrustError, match="resume_unavailable"):
        owner.resume_reviewed(INSTANCE_ID, expected_generation=generation)
    after = row(owner)
    assert (after.generation, after.state, after.secret_reference) == (
        before.generation,
        before.state,
        before.secret_reference,
    )
    assert broker.establishments == calls


def test_prepared_without_installed_artifact_remains_unconfirmed(
    setup: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, broker, _client, _secrets = setup
    prepared = stage_stable(owner, broker, monkeypatch)
    original = owner._call

    def absent(*args: Any) -> Any:
        if args[2] == "Addons.GetAddonDetails":
            return {}
        return original(*args)

    monkeypatch.setattr(owner, "_call", absent)
    with pytest.raises(ManagedBridgeTrustError, match="installation_incomplete"):
        owner.resume_reviewed(INSTANCE_ID, expected_generation=prepared.generation)
    assert (
        row(owner).state == "prepared" and row(owner).generation == prepared.generation
    )


def test_explicit_continuation_holds_real_lifecycle_lease_until_authentication(
    setup: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kodi_lab.application.readiness_tasks import ReadinessTaskRegistry
    from kodi_lab.application.runtime_lifecycle import ManagedRuntimeLifecycleService
    from kodi_lab.application.runtime_status import ManagedRuntimeStatusService
    from kodi_lab.broker.managed_bridge_trust import require_reviewed_installation_lease
    from kodi_lab.storage.models import ManagedInstanceMutationLeaseRow

    owner, broker, _client, _secrets = setup
    prepared = stage_stable(owner, broker, monkeypatch)
    native = owner._resident_identity

    def authenticated(*args: Any, **kwargs: Any) -> Any:
        with owner.sessions() as session:
            require_reviewed_installation_lease(session, str(INSTANCE_ID))
        return native(*args, **kwargs)

    monkeypatch.setattr(owner, "_resident_identity", authenticated)
    readiness = ReadinessTaskRegistry()
    status = ManagedRuntimeStatusService(owner.sessions, broker, readiness)
    lifecycle = ManagedRuntimeLifecycleService(
        owner.sessions, broker, readiness, status, bridge_trust=owner
    )
    lifecycle.resume_reviewed_bridge(
        INSTANCE_ID, expected_generation=prepared.generation
    )
    assert row(owner).state == "active"
    with owner.sessions() as session:
        assert session.get(ManagedInstanceMutationLeaseRow, str(INSTANCE_ID)) is None
    assert not any(
        call[0] in {"start", "stop", "provision", "deprovision"}
        for call in broker.calls
    )
    readiness.close()
