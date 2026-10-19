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
                            "version": "1.1.26",
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
        addon_version="1.1.26",
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
