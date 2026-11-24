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

PREDECESSOR = "0014_human_reset_bridge"
REVISION = "0015_log_scan_capability"


@pytest.mark.parametrize("admit_scan", [False, True])
def test_scan_capability_direct_predecessor_and_downgrade(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    admit_scan: bool,
) -> None:
    config, database = _config(tmp_path, monkeypatch)
    command.upgrade(config, PREDECESSOR)
    with sqlite3.connect(database) as connection:
        _review(connection, '["log_read","screenshot"]', 1)
        rows = connection.execute("SELECT * FROM reviewed_bridge_builds").fetchall()
    original = _triggers(database)
    command.upgrade(config, REVISION)
    assert _triggers(database) == original
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute("SELECT * FROM reviewed_bridge_builds").fetchall()
            == rows
        )
        if admit_scan:
            _review(connection, '["log_read","log_scan","screenshot"]', 2)
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            _review(connection, '["filesystem","screenshot"]', 3)
    if admit_scan:
        with pytest.raises(RuntimeError, match="refusing lossy downgrade"):
            command.downgrade(config, PREDECESSOR)
        assert _revision(database) == REVISION
    else:
        command.downgrade(config, PREDECESSOR)
        assert _revision(database) == PREDECESSOR
        with sqlite3.connect(database) as connection:
            assert (
                connection.execute("SELECT * FROM reviewed_bridge_builds").fetchall()
                == rows
            )
            with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
                _review(connection, '["log_scan","screenshot"]', 2)
    assert _triggers(database) == original


def test_current_migrated_pairing_preserves_rows_triggers_and_reinitialization(
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
    assert _triggers(database) == original
    assert _revision(database) == REVISION
    command.upgrade(config, REVISION)
    assert _triggers(database) == original
    engine.dispose()


def test_real_otp_coordinator_persists_log_capability_and_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runtime: Any,
) -> None:
    config, database = _config(tmp_path, monkeypatch)
    command.upgrade(config, PREDECESSOR)
    original = _triggers(database)
    # The current resident advertises the additive system_snapshot capability.
    command.upgrade(config, "head")
    current_triggers = _triggers(database)
    assert original.items() <= current_triggers.items()
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
        assert "log_scan" in result.observed_capabilities
    with sqlite3.connect(database) as connection:
        before = connection.execute("SELECT * FROM external_bridge_pairings").fetchall()
        capabilities = json.loads(
            connection.execute(
                "SELECT observed_capabilities_json FROM external_bridge_pairings"
            ).fetchone()[0]
        )
        assert "log_read" in capabilities
        assert "log_scan" in capabilities
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute("SELECT * FROM external_bridge_pairings").fetchall()
            == before
        )
    assert _revision(database) == "0019_snapshot_recovery"
    assert _triggers(database) == current_triggers
    engine.dispose()
