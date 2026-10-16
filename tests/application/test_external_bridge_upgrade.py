"""Exact paired development Bridge updates preserve credentials and target scope."""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import create_engine

from kodi_lab.application.external_bridges import (
    ExternalBridgeService,
    ExternalBridgeServiceError,
)
from kodi_lab.broker.managed_bridge_trust import approved_bridge_build
from kodi_lab.domain import ExternalBridgePairingState, TargetType
from kodi_lab.storage.engine import create_session_factory
from kodi_lab.storage.metadata import Base
from kodi_lab.storage.models import ExternalBridgePairingRow
from tests.application.test_external_bridges import _decode_token, _seed, _service
from tests.kodi.fake_external_bridge import FakeExternalBridge


@pytest.mark.parametrize(
    "failure",
    [
        None,
        "fingerprint",
        "bridge_id",
        "kodi_version",
        "capabilities",
        "unapproved_previous",
    ],
)
def test_explicit_reverification_adopts_only_the_exact_paired_update(
    tmp_path: Path, failure: str | None
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'state.db'}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    target_id = uuid4()
    _seed(factory, target_id)
    previous = approved_bridge_build(previous=True)
    current = approved_bridge_build()
    with FakeExternalBridge(target_id=target_id, token=b"x" * 32) as fake:
        fake.addon_version = previous["addon_version"]
        fake.build_fingerprint = (
            previous["build_fingerprint"]
            if failure != "unapproved_previous"
            else "f" * 64
        )
        fake.capabilities = ["screenshot", "workspace_deployment", "ui_observation"]
        service = _service(factory, tmp_path / "secrets", fake.port)
        generated = service.generate_pairing_token(target_id, bridge_port=fake.port)
        fake.token = _decode_token(generated.token)
        service.attempt_pairing(target_id)
        service.confirm_pairing(target_id)
        with factory() as session:
            row = session.get(ExternalBridgePairingRow, str(target_id))
            assert row is not None
            reference, paired_at, bridge_id = (
                row.bridge_secret_reference,
                row.paired_at,
                row.pinned_bridge_id,
            )
        secret_files = {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (tmp_path / "secrets").iterdir()
        }
        fake.addon_version = current["addon_version"]
        fake.build_fingerprint = current["build_fingerprint"]
        if failure == "fingerprint":
            fake.build_fingerprint = "e" * 64
        elif failure == "bridge_id":
            fake.bridge_id = uuid4()
        elif failure == "kodi_version":
            fake.kodi_version = "20.5"
        elif failure == "capabilities":
            fake.capabilities.remove("ui_observation")
        service.mark_needs_reverification(target_id)
        if failure is None:
            assert (
                service.reverify(target_id).pairing_state
                is ExternalBridgePairingState.PAIRED
            )
            assert (
                service.reverify(target_id).pairing_state
                is ExternalBridgePairingState.PAIRED
            )
        else:
            with pytest.raises(ExternalBridgeServiceError):
                service.reverify(target_id)
        with factory() as session:
            row = session.get(ExternalBridgePairingRow, str(target_id))
            assert row is not None
            assert (
                row.bridge_secret_reference,
                row.paired_at,
                row.pinned_bridge_id,
            ) == (reference, paired_at, bridge_id)
            assert row.pinned_build_fingerprint == (
                current["build_fingerprint"]
                if failure is None
                else previous["build_fingerprint"]
                if failure != "unapproved_previous"
                else "f" * 64
            )
        assert secret_files == {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (tmp_path / "secrets").iterdir()
        }
    engine.dispose()


def test_development_upgrade_rule_cannot_adopt_a_production_build() -> None:
    snapshot: Any = SimpleNamespace(target_type=TargetType.EXTERNAL_PRODUCTION)
    identity: Any = object()
    assert (
        ExternalBridgeService._known_development_upgrade(snapshot, identity, ())
        is False
    )
