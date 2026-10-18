"""Direct predecessor proof, retained triggers, and real pairing on migrated SQL."""

import json
import sqlite3
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from alembic import command

from kodi_lab.application.external_bridges import ExternalBridgeService
from kodi_lab.kodi.external import ExternalEndpointPolicy
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.storage.secrets import ExternalBridgeSecretStore
from tests.application.test_external_bridges import _decode_token, _seed, _service
from tests.kodi.fake_external_bridge import FakeExternalBridge
from tests.kodi.test_bridge_otp_bootstrap import _server
from tests.kodi.test_bridge_otp_bootstrap import runtime as runtime
from tests.storage.test_ui_observation_migration import (
    _config,
    _review,
    _revision,
    _triggers,
)

PREDECESSOR = "0071_household_onboarding"
REVISION = "0072_kodi_log_capability"


def test_upgrade_preserves_rows_triggers_and_empty_round_trip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, database = _config(tmp_path, monkeypatch)
    command.upgrade(config, PREDECESSOR)
    engine = create_database_engine(f"sqlite:///{database}")
    factory = create_session_factory(engine)
    target = uuid4()
    _seed(factory, target)
    with FakeExternalBridge(target_id=target, token=b"x" * 32) as fake:
        service = _service(factory, tmp_path / "existing-secrets", fake.port)
        token = service.generate_pairing_token(target, bridge_port=fake.port)
        fake.token = _decode_token(token.token)
        service.attempt_pairing(target)
        paired = service.confirm_pairing(target)
    with sqlite3.connect(database) as connection:
        _review(connection, '["screenshot","ui_observation"]', 1)
        previous = connection.execute("SELECT * FROM reviewed_bridge_builds").fetchall()
        existing_pairings = connection.execute(
            "SELECT * FROM external_bridge_pairings"
        ).fetchall()
    original = _triggers(database)
    command.upgrade(config, REVISION)
    assert _triggers(database) == original
    assert service.status(target) == paired
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute("SELECT * FROM reviewed_bridge_builds").fetchall()
            == previous
        )
        assert (
            connection.execute("SELECT * FROM external_bridge_pairings").fetchall()
            == existing_pairings
        )
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            _review(connection, '["arbitrary_execution","screenshot"]', 2)
    command.downgrade(config, PREDECESSOR)
    assert _triggers(database) == original and _revision(database) == PREDECESSOR
    command.upgrade(config, REVISION)
    assert _triggers(database) == original
    engine.dispose()


def test_real_otp_coordinator_persists_log_capability_and_downgrade_refuses_loss(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runtime: Any,
) -> None:
    config, database = _config(tmp_path, monkeypatch)
    command.upgrade(config, PREDECESSOR)
    original = _triggers(database)
    command.upgrade(config, REVISION)
    engine = create_database_engine(f"sqlite:///{database}")
    factory = create_session_factory(engine)
    target = uuid4()
    _seed(factory, target)
    bridge = runtime[0]
    with _server(bridge) as port:
        service = ExternalBridgeService(
            factory,
            endpoint_policy=ExternalEndpointPolicy(
                allowed_ports=(port,), test_loopback_enabled=True
            ),
            secret_store=ExternalBridgeSecretStore(
                tmp_path / "secrets", master_key=b"x" * 32, key_version=1
            ),
        )
        service.begin_otp_pairing(
            target,
            bridge_port=port,
            code=bridge["_onboarding_store"]().status(paired=False)["code"],
        )
        result = service.commit_otp_pairing(target)
        assert "log_read" in result.observed_capabilities
    with sqlite3.connect(database) as connection:
        before = connection.execute("SELECT * FROM external_bridge_pairings").fetchall()
        capabilities = json.loads(
            connection.execute(
                "SELECT observed_capabilities_json FROM external_bridge_pairings"
            ).fetchone()[0]
        )
        assert "log_read" in capabilities
    with pytest.raises(RuntimeError, match="cannot be downgraded"):
        command.downgrade(config, PREDECESSOR)
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute("SELECT * FROM external_bridge_pairings").fetchall()
            == before
        )
    assert _revision(database) == REVISION and _triggers(database) == original
    engine.dispose()
