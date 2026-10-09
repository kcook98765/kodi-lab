import hashlib
import json
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, Engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.external_targets import ExternalTargetAdministrationService
from kodi_lab.application.screenshot_preferences import ScreenshotPreferenceService
from kodi_lab.domain import (
    ExternalAuthenticationMode,
    ExternalProtocolKind,
    TargetType,
)
from kodi_lab.kodi.external import ExternalEndpointPolicy
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.storage.screenshot_preference_sql import SCREENSHOT_PREFERENCE_TRIGGERS
from kodi_lab.storage.secrets import ExternalCredentialStore
from tests.application.test_production_bridges import _Verifier
from tests.storage.test_migrations import (  # noqa: F401
    migration_setup,
    migration_templates,
)

REVISION = "0070_external_screenshot_preference"
PREDECESSOR = "0069_managed_bridge_trust"
NOW = "2026-09-21T12:00:00.000000Z"


def _insert(connection: Connection, table: str, **values: Any) -> None:
    """Insert explicit historical columns without current ORM/service defaults."""
    columns = ",".join(values)
    parameters = ",".join(f":{name}" for name in values)
    connection.execute(
        text(f"INSERT INTO {table} ({columns}) VALUES ({parameters})"), values
    )


def _digest(values: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(values, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _triggers(connection: Connection) -> dict[str, str]:
    return dict(
        connection.execute(
            text(
                "SELECT name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name"
            )
        )
        .tuples()
        .all()
    )


def _assert_preserved(
    connection: Connection,
    before: dict[str, list[tuple[Any, ...]]],
    old_columns: dict[str, str],
) -> None:
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
    assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
    assert connection.scalar(text("PRAGMA integrity_check")) == "ok"


def _seed_0069(engine: Engine, *, retired: bool) -> tuple[UUID, UUID, UUID]:
    """Persist predecessor-native policy, consent and active/retired Bridge state.

    All foreign keys, production binding and revision guards remain enabled.
    No current pairing/retirement API is valid on this historical schema.
    """
    paired, disabled, without_screenshots = uuid4(), uuid4(), uuid4()
    observation, build, bridge = str(uuid4()), str(uuid4()), str(uuid4())
    endpoint_digest = _digest(
        dict(
            bridge_port=8765,
            host="127.0.0.1",
            native_port=8080,
            protocol="jsonrpc_http",
            verified_addresses='["127.0.0.1"]',
            verified_kodi_name="Kodi",
            verified_kodi_version="21.3",
        )
    )
    with engine.begin() as connection:
        assert (
            connection.scalar(text("SELECT version_num FROM alembic_version"))
            == PREDECESSOR
        )
        assert connection.scalar(text("PRAGMA foreign_keys")) == 1
        assert not {
            "external_screenshot_preferences",
            "external_secret_retirements",
            "household_onboarding_operations",
        } & set(inspect(connection).get_table_names())
        assert "capture_preference_revision" not in {
            column["name"]
            for column in inspect(connection).get_columns(
                "external_bridge_request_receipts"
            )
        }
        for target, kind, capabilities, enabled, verified in (
            (
                paired,
                "external_production",
                '["screenshot","target_inspect"]',
                True,
                True,
            ),
            (
                disabled,
                "external_development",
                '["screenshot","target_inspect","workspace_deployment"]',
                False,
                True,
            ),
            (
                without_screenshots,
                "external_production",
                '["target_inspect"]',
                False,
                False,
            ),
        ):
            _insert(
                connection,
                "targets",
                id=str(target),
                name="Historical Kodi",
                target_type=kind,
                kodi_version="21.3" if verified else None,
                availability="available" if verified else "unavailable",
                health="healthy" if verified else "warning",
                connection_state="connected" if verified else "disconnected",
            )
            _insert(
                connection,
                "external_kodi_targets",
                target_id=str(target),
                protocol_kind="jsonrpc_http",
                host="127.0.0.1" if verified else "192.168.50.40",
                port=8080,
                authentication_mode="none",
                configured_capabilities_json=capabilities,
                verified_capabilities_json='["target_inspect"]' if verified else "[]",
                administrative_state="enabled" if enabled else "disabled",
                verification_state="verified" if verified else "unverified",
                observation_state="reachable" if verified else None,
                verified_addresses_json='["127.0.0.1"]' if verified else "[]",
                verified_kodi_name="Kodi" if verified else None,
                verified_kodi_version="21.3" if verified else None,
                observed_kodi_name="Kodi" if verified else None,
                observed_kodi_version="21.3" if verified else None,
                observed_protocol_version="13.0.0" if verified else None,
                pending_observation_id=observation if target == paired else None,
                production_policy_revision=(7 if target == paired else 1)
                if kind == "external_production"
                else None,
                created_at=NOW,
                verified_at=NOW if verified else None,
                last_observed_at=NOW if verified else None,
            )
        _insert(
            connection,
            "external_kodi_target_observations",
            id=observation,
            target_id=str(paired),
            outcome="reachable",
            addresses_json='["127.0.0.1"]',
            kodi_name="Kodi",
            kodi_version="21.3",
            protocol_version="13.0.0",
            verified_capabilities_json='["target_inspect"]',
            attempted_at=NOW,
            completed_at=NOW,
        )
        native_digest = _digest(
            dict(
                addresses=["127.0.0.1"],
                capabilities=["target_inspect"],
                completed_at=NOW,
                host="127.0.0.1",
                kodi_name="Kodi",
                kodi_version="21.3",
                observation_id=observation,
                port=8080,
                protocol_kind="jsonrpc_http",
                protocol_version="13.0.0",
                target_id=str(paired),
            )
        )
        for target in (paired, without_screenshots):
            confirmed = target == paired
            _insert(
                connection,
                "external_production_policies",
                target_id=str(target),
                designation_state="confirmed"
                if confirmed
                else "pending_native_identity",
                confirmed_observation_id=observation if confirmed else None,
                confirmed_observation_digest=native_digest if confirmed else None,
                identity_generation=1,
                policy_revision=7 if confirmed else 1,
                mutations_suspended=True,
                designation_confirmed_by="test" if confirmed else None,
                designation_confirmed_at=NOW if confirmed else None,
                created_at=NOW,
                updated_at=NOW,
            )
        _insert(
            connection,
            "reviewed_bridge_builds",
            id=build,
            semantic_fingerprint="b" * 64,
            zip_sha256="a" * 64,
            addon_id="service.kodi_lab_bridge",
            addon_version="1.1.15",
            protocol_version="1",
            capabilities_json='["screenshot"]',
            supported_kodi_versions_json='["21.3"]',
            source_reference="historical migration fixture",
            reviewed_by="test",
            reviewed_at=NOW,
            state="active",
        )
        _insert(
            connection,
            "production_bridge_http_acknowledgements",
            id=str(uuid4()),
            target_id=str(paired),
            identity_generation=1,
            bridge_trust_revision=1,
            protocol="jsonrpc_http",
            ip_literal="127.0.0.1",
            native_port=8080,
            bridge_port=8765,
            endpoint_digest=endpoint_digest,
            transport_policy_revision=1,
            acknowledged_by="test",
            acknowledged_at=NOW,
        )
        _insert(
            connection,
            "external_bridge_pairings",
            target_id=str(paired),
            bridge_port=8765,
            pairing_state="paired",
            reachability_state="reachable",
            bridge_secret_reference=str(uuid4()),
            observed_bridge_id=bridge,
            observed_protocol_version="1",
            observed_addon_id="service.kodi_lab_bridge",
            observed_addon_version="1.1.15",
            observed_kodi_version="21.3",
            observed_build_fingerprint="b" * 64,
            observed_capabilities_json='["screenshot"]',
            pinned_bridge_id=bridge,
            pinned_protocol_version="1",
            pinned_addon_id="service.kodi_lab_bridge",
            pinned_addon_version="1.1.15",
            pinned_kodi_version="21.3",
            pinned_build_fingerprint="b" * 64,
            verified_capabilities_json='["screenshot"]',
            endpoint_revision_digest=endpoint_digest,
            token_generated_at=NOW,
            pairing_attempted_at=NOW,
            paired_at=NOW,
            verified_at=NOW,
            production_identity_generation=1,
            production_bridge_trust_revision=1,
            reviewed_build_id=build,
            production_confirmed_by="test",
            production_confirmed_at=NOW,
        )
        for event in ("bridge_paired", "production_bridge_confirmed"):
            _insert(
                connection,
                "external_bridge_events",
                id=str(uuid4()),
                target_id=str(paired),
                bridge_id=bridge,
                event_kind=event,
                occurred_at=NOW,
            )
        _insert(
            connection,
            "external_kodi_target_events",
            id=str(uuid4()),
            target_id=str(paired),
            event_kind="production_designation_confirmed",
            observation_id=observation,
            occurred_at=NOW,
        )
        if retired:
            # The predecessor's revoked durable state clears authority and retains
            # identity/history plus the monotonically advanced trust high-water mark.
            connection.execute(
                text(
                    "UPDATE external_bridge_pairings SET pairing_state='revoked',"
                    "reachability_state='unknown',bridge_secret_reference=NULL,"
                    "verified_capabilities_json='[]',revoked_at=:now,"
                    "production_bridge_trust_revision=2,production_identity_generation=NULL,"
                    "reviewed_build_id=NULL,production_confirmed_by=NULL,production_confirmed_at=NULL"
                    " WHERE target_id=:target"
                ),
                {"now": NOW, "target": str(paired)},
            )
            _insert(
                connection,
                "external_bridge_events",
                id=str(uuid4()),
                target_id=str(paired),
                bridge_id=bridge,
                event_kind="bridge_revoked",
                occurred_at=NOW,
            )
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
        assert connection.scalar(text("PRAGMA integrity_check")) == "ok"
    return paired, disabled, without_screenshots


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
    paired, disabled, without_screenshots = _seed_0069(engine, retired=retired)
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
        original_triggers = _triggers(connection)
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
        assert (
            connection.scalar(text("SELECT version_num FROM alembic_version"))
            == REVISION
        )
        _assert_preserved(connection, before, old_columns)
        assert _triggers(connection) == {
            **original_triggers,
            **{
                statement.split()[2]: statement
                for statement in SCREENSHOT_PREFERENCE_TRIGGERS
            },
        }
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
    assert preference.read(disabled).enabled  # Administrative disable still applies.
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
    # Subsequent migrations must preserve this populated predecessor history too.
    command.upgrade(config, "head")
    command.check(config)
    engine = create_database_engine(url)
    with engine.connect() as connection:
        assert (
            connection.scalar(text("SELECT version_num FROM alembic_version"))
            == ScriptDirectory.from_config(config).get_current_head()
        )
        _assert_preserved(connection, before, old_columns)
        assert {
            name: _triggers(connection)[name] for name in original_triggers
        } == original_triggers
        assert {
            "external_secret_retirements",
            "household_onboarding_operations",
        } <= set(inspect(connection).get_table_names())
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
