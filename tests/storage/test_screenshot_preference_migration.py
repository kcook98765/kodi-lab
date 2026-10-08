from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from alembic import command
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.external_targets import ExternalTargetAdministrationService
from kodi_lab.application.screenshot_preferences import ScreenshotPreferenceService
from kodi_lab.domain import (
    ExternalAdministrativeState,
    ExternalAuthenticationMode,
    ExternalProtocolKind,
    TargetType,
)
from kodi_lab.kodi.external import ExternalEndpointPolicy
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.storage.models import ExternalKodiTargetRow
from kodi_lab.storage.secrets import ExternalCredentialStore
from tests.application.test_external_bridges import _seed
from tests.application.test_production_bridges import (
    _pair_production_bridge,
    _Verifier,
)
from tests.kodi.fake_external_bridge import FakeExternalBridge
from tests.storage.test_migrations import (  # noqa: F401
    migration_setup,
    migration_templates,
)

REVISION = "0070_external_screenshot_preference"
PREDECESSOR = "0069_managed_bridge_trust"


def _register_household(
    sessions: sessionmaker[Session], tmp_path: Path
) -> tuple[UUID, ExternalCredentialStore]:
    credentials = ExternalCredentialStore(
        tmp_path / "native-secrets", master_key=None, key_version=1
    )
    targets = ExternalTargetAdministrationService(
        sessions,
        endpoint_policy=ExternalEndpointPolicy(
            allowed_ports=(8080,), test_loopback_enabled=True
        ),
        credential_store=credentials,
        verifier=cast(Any, _Verifier()),
        target_type=TargetType.EXTERNAL_PRODUCTION,
    )
    target = targets.register(
        logical_name="Household Kodi",
        protocol_kind=ExternalProtocolKind.JSONRPC_HTTP,
        host="127.0.0.1",
        port=8080,
        authentication_mode=ExternalAuthenticationMode.NONE,
        configured_capabilities={"target_inspect", "addon_inventory", "screenshot"},
    )
    observed = targets.probe(target.target_id)
    assert observed.pending_observation_id is not None
    targets.confirm(target.target_id, observed.pending_observation_id)
    targets.confirm_production_designation(
        target.target_id, observed.pending_observation_id
    )
    targets.set_enabled(target.target_id, enabled=True)
    return target.target_id, credentials


@pytest.mark.parametrize("retired", [False, True])
def test_populated_upgrade_preserves_policy_and_all_prior_rows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, retired: bool
) -> None:
    config, url = migration_setup(monkeypatch, tmp_path, start_revision=PREDECESSOR)
    engine = create_database_engine(url)
    sessions = create_session_factory(engine)
    paired, credentials = _register_household(sessions, tmp_path)
    with FakeExternalBridge(target_id=paired, token=b"x" * 32) as fake:
        bridges = _pair_production_bridge(sessions, tmp_path, paired, fake)
        disabled = uuid4()
        _seed(sessions, disabled)
        with sessions.begin() as session:
            external = session.get(ExternalKodiTargetRow, str(disabled))
            assert external is not None
            external.administrative_state = ExternalAdministrativeState.DISABLED
        targets = ExternalTargetAdministrationService(
            sessions,
            endpoint_policy=ExternalEndpointPolicy(allowed_ports=(8080,)),
            credential_store=credentials,
            target_type=TargetType.EXTERNAL_PRODUCTION,
        )
        without_screenshots = targets.register(
            logical_name="Existing inspection only",
            protocol_kind=ExternalProtocolKind.JSONRPC_HTTP,
            host="192.168.50.40",
            port=8080,
            authentication_mode=ExternalAuthenticationMode.NONE,
            configured_capabilities={"target_inspect"},
        ).target_id
        # Retired Bridge authority is also preserved, rather than reconstructed.
        if retired:
            bridges.revoke(paired)
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO external_bridge_request_receipts "
                    "(target_id,request_id,nonce,operation,state,"
                    "created_at,expires_at) "
                    "VALUES (:target,:request,:nonce,'capture_screenshot','pending',"
                    "'2026-09-21T12:00:00.000000Z','2099-01-01T00:00:00.000000Z')"
                ),
                {"target": str(paired), "request": str(uuid4()), "nonce": "n" * 43},
            )
        with engine.connect() as connection:
            tables = [
                name
                for name in inspect(engine).get_table_names()
                if name != "alembic_version"
            ]
            before: dict[str, list[tuple[Any, ...]]] = {
                name: sorted(
                    (
                        tuple(row)
                        for row in connection.execute(text(f'SELECT * FROM "{name}"'))
                    ),
                    key=repr,
                )
                for name in tables
            }
            old_columns = {
                name: ",".join(
                    '"' + column["name"] + '"'
                    for column in inspect(engine).get_columns(name)
                )
                for name in tables
            }
        engine.dispose()
        command.upgrade(config, REVISION)
        engine = create_database_engine(url)
        sessions = create_session_factory(engine)
        with engine.connect() as connection:
            for name, rows in before.items():
                assert (
                    sorted(
                        (
                            tuple(row)
                            for row in connection.execute(
                                text(f'SELECT {old_columns[name]} FROM "{name}"')
                            )
                        ),
                        key=repr,
                    )
                    == rows
                )
            assert (
                connection.scalar(
                    text(
                        "SELECT capture_preference_revision "
                        "FROM external_bridge_request_receipts"
                    )
                )
                == 1
            )
        preference = ScreenshotPreferenceService(sessions)
        assert preference.read(paired).enabled
        assert preference.read(
            disabled
        ).enabled  # Administrative disable still applies.
        assert not preference.read(without_screenshots).enabled
        assert preference.read(paired).revision == 1
        # A no-op preserves revision; toggling is enforceable on the migrated DB.
        preference.update(
            paired, enabled=True, expected_revision=1, administrator_reference="test"
        )
        assert preference.read(paired).revision == 1
        preference.update(
            paired, enabled=False, expected_revision=1, administrator_reference="test"
        )
        preference.update(
            paired, enabled=True, expected_revision=2, administrator_reference="test"
        )
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE external_screenshot_preferences SET revision=1 "
                    "WHERE target_id=:target"
                ),
                {"target": str(paired)},
            )
        engine.dispose()
        with pytest.raises(RuntimeError, match="cannot be safely downgraded"):
            command.downgrade(config, PREDECESSOR)
        engine = create_database_engine(url)
        with engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT version_num FROM alembic_version"))
                == REVISION
            )
            assert (
                connection.scalar(
                    text(
                        "SELECT revision FROM external_screenshot_preferences "
                        "WHERE target_id=:target"
                    ),
                    {"target": str(paired)},
                )
                == 3
            )
        engine.dispose()


def test_empty_upgrade_downgrade_and_new_registration_defaults(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, url = migration_setup(monkeypatch, tmp_path, start_revision=PREDECESSOR)
    command.upgrade(config, REVISION)
    command.downgrade(config, PREDECESSOR)
    engine = create_database_engine(url)
    assert "external_screenshot_preferences" not in inspect(engine).get_table_names()
    engine.dispose()
    command.upgrade(config, REVISION)
    engine = create_database_engine(url)
    sessions = create_session_factory(engine)
    target_id, _ = _register_household(sessions, tmp_path)
    assert ScreenshotPreferenceService(sessions).read(target_id).enabled
    engine.dispose()
    # An untouched default loses no policy on downgrade, even when populated.
    command.downgrade(config, PREDECESSOR)
