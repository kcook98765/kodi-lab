"""The screenshot policy migration changes only external preferences and default."""

from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import inspect, text

from kodi_lab.application.external_targets import ExternalTargetAdministrationService
from kodi_lab.application.mcp_administration import (
    READ_ONLY_INSPECTION_PRESET_ID,
    McpAdministrationService,
    McpAuthenticationService,
)
from kodi_lab.application.screenshot_preferences import ScreenshotPreferenceService
from kodi_lab.domain import ExternalAuthenticationMode, ExternalProtocolKind, TargetType
from kodi_lab.kodi.external import ExternalEndpointPolicy
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.storage.metadata import Base
from kodi_lab.storage.models import TargetRow
from kodi_lab.storage.secrets import ExternalCredentialStore
from tests.migration_support import migration_setup
from tests.migration_support import migration_templates as migration_templates

OLD, NEW = "0005_managed_web", "0006_external_screenshots"


def test_upgrade_preserves_state_resets_opt_in_and_matches_fresh_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Seed with current services, then restore the actual predecessor schema.
    # Current production policy mappings include the later navigation opt-in.
    config, url = migration_setup(monkeypatch, tmp_path, start_revision="head")
    engine = create_database_engine(url)
    sessions = create_session_factory(engine)
    preferences = ScreenshotPreferenceService(sessions)
    ids = []
    for kind in (TargetType.EXTERNAL_DEVELOPMENT, TargetType.EXTERNAL_PRODUCTION):
        service = ExternalTargetAdministrationService(
            sessions,
            target_type=kind,
            endpoint_policy=ExternalEndpointPolicy(allowed_ports=(8080,)),
            credential_store=ExternalCredentialStore(
                tmp_path / "secrets", master_key=None, key_version=1
            ),
        )
        for number in range(2):
            target = service.register(
                logical_name=f"{kind} {number}",
                protocol_kind=ExternalProtocolKind.JSONRPC_HTTP,
                host=f"192.168.50.{40 + number}",
                port=8080,
                authentication_mode=ExternalAuthenticationMode.NONE,
                configured_capabilities={"target_inspect", "screenshot"},
            )
            ids.append(target.target_id)
            preferences.update(
                target.target_id,
                enabled=True,
                expected_revision=1,
                administrator_reference="previous_admin",
            )
            assert preferences.read(target.target_id).enabled
            if number:
                preferences.update(
                    target.target_id,
                    enabled=False,
                    expected_revision=2,
                    administrator_reference="previous_admin",
                )
    admin = McpAdministrationService(sessions)
    agent = admin.create_client(
        display_name="Preserved agent", preset_id=READ_ONLY_INSPECTION_PRESET_ID
    )
    issued = admin.issue_credential(agent.id)
    admin.assign_target_preset(ids[0], READ_ONLY_INSPECTION_PRESET_ID)
    admin.grant_target(agent.id, ids[0])
    managed_id = str(uuid4())
    with sessions.begin() as session:
        session.add(
            TargetRow(
                id=managed_id,
                name="Managed unchanged",
                target_type=TargetType.MANAGED,
                kodi_version="21.3",
                availability="available",
                health="healthy",
                connection_state="connected",
            )
        )
    command.downgrade(config, OLD)
    with engine.connect() as connection:
        tables = set(inspect(engine).get_table_names()) - {
            "external_screenshot_preferences",
            "alembic_version",
        }
        before = {
            name: connection.execute(text(f'SELECT * FROM "{name}"')).all()
            for name in tables
        }
        old_preferences = {
            row.target_id: row
            for row in connection.execute(
                text("SELECT * FROM external_screenshot_preferences")
            ).mappings()
        }
        preference_schema = connection.scalar(
            text(
                "SELECT sql FROM sqlite_master WHERE type='table' "
                "AND name='external_screenshot_preferences'"
            )
        )
    command.upgrade(config, NEW)
    for target_id in ids:
        old = old_preferences[str(target_id)]
        new = preferences.read(target_id)
        assert new.enabled is False
        assert new.revision == old["revision"] + int(old["enabled"])
        assert new.updated_by == (
            "migration_0006" if old["enabled"] else old["updated_by"]
        )
        if not old["enabled"]:
            assert (
                new.updated_at.isoformat(timespec="microseconds").replace("+00:00", "Z")
                == old["updated_at"]
            )
    with engine.connect() as connection:
        assert before == {
            name: connection.execute(text(f'SELECT * FROM "{name}"')).all()
            for name in tables
        }
        assert preference_schema == connection.scalar(
            text(
                "SELECT sql FROM sqlite_master WHERE type='table' "
                "AND name='external_screenshot_preferences'"
            )
        )
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM external_screenshot_preferences "
                    "WHERE target_id=:id"
                ),
                {"id": managed_id},
            )
            == 0
        )
        migrated_trigger = connection.scalar(
            text(
                "SELECT sql FROM sqlite_master "
                "WHERE name='trg_external_screenshot_preference_initialize'"
            )
        )
        assert connection.scalar(text("PRAGMA integrity_check")) == "ok"
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
    # Inspect historical columns without asking the current client ORM to run
    # against a schema that predates the managed role column.
    with engine.connect() as connection:
        assert connection.scalars(
            text("SELECT target_id FROM mcp_client_target_grants WHERE client_id=:id"),
            {"id": str(agent.id)},
        ).all() == [str(ids[0])]
    fresh = create_database_engine(f"sqlite:///{tmp_path / 'fresh.db'}")
    Base.metadata.create_all(fresh)
    with fresh.connect() as connection:
        fresh_trigger = connection.scalar(
            text(
                "SELECT sql FROM sqlite_master "
                "WHERE name='trg_external_screenshot_preference_initialize'"
            )
        )
    assert "".join(migrated_trigger.split()) == "".join(fresh_trigger.split())
    retained = {target_id: preferences.read(target_id) for target_id in ids}
    command.downgrade(config, OLD)
    assert retained == {target_id: preferences.read(target_id) for target_id in ids}
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == OLD
        assert before == {
            name: connection.execute(text(f'SELECT * FROM "{name}"')).all()
            for name in tables
        }
        assert connection.scalar(text("PRAGMA integrity_check")) == "ok"
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
    legacy_service = ExternalTargetAdministrationService(
        sessions,
        target_type=TargetType.EXTERNAL_DEVELOPMENT,
        endpoint_policy=ExternalEndpointPolicy(allowed_ports=(8080,)),
        credential_store=ExternalCredentialStore(
            tmp_path / "legacy-secrets", master_key=None, key_version=1
        ),
    )
    legacy_target = legacy_service.register(
        logical_name="New after downgrade",
        protocol_kind=ExternalProtocolKind.JSONRPC_HTTP,
        host="192.168.50.43",
        port=8080,
        authentication_mode=ExternalAuthenticationMode.NONE,
        configured_capabilities={"target_inspect", "screenshot"},
    )
    assert preferences.read(legacy_target.target_id).enabled  # Legacy creation rule.
    assert retained == {target_id: preferences.read(target_id) for target_id in ids}
    fresh.dispose()
    command.upgrade(config, "head")
    assert McpAuthenticationService(sessions).authenticate(issued.token) is not None
    engine.dispose()


def test_fresh_migrated_database_defaults_both_external_classes_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, url = migration_setup(monkeypatch, tmp_path, start_revision=None)
    command.upgrade(config, "head")
    engine = create_database_engine(url)
    sessions = create_session_factory(engine)
    for kind in (TargetType.EXTERNAL_DEVELOPMENT, TargetType.EXTERNAL_PRODUCTION):
        service = ExternalTargetAdministrationService(
            sessions,
            target_type=kind,
            endpoint_policy=ExternalEndpointPolicy(allowed_ports=(8080,)),
            credential_store=ExternalCredentialStore(
                tmp_path / "secrets", master_key=None, key_version=1
            ),
        )
        target = service.register(
            logical_name=f"Fresh {kind}",
            protocol_kind=ExternalProtocolKind.JSONRPC_HTTP,
            host="192.168.50.42",
            port=8080,
            authentication_mode=ExternalAuthenticationMode.NONE,
            configured_capabilities={"target_inspect", "screenshot"},
        )
        preference = ScreenshotPreferenceService(sessions).read(target.target_id)
        assert preference.enabled is False and preference.revision == 1
    engine.dispose()
