"""Current schema initializes screenshot opt-in without widening access."""

from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import text

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
from kodi_lab.storage.secrets import ExternalCredentialStore
from tests.migration_support import migration_setup
from tests.migration_support import migration_templates as migration_templates


def test_fresh_migrated_database_defaults_both_external_classes_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, url = migration_setup(monkeypatch, tmp_path, start_revision=None)
    command.upgrade(config, "head")
    engine = create_database_engine(url)
    sessions = create_session_factory(engine)
    targets = []
    preferences = ScreenshotPreferenceService(sessions)
    for number, kind in enumerate(
        (TargetType.EXTERNAL_DEVELOPMENT, TargetType.EXTERNAL_PRODUCTION), start=42
    ):
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
            host=f"192.168.50.{number}",
            port=8080,
            authentication_mode=ExternalAuthenticationMode.NONE,
            configured_capabilities={"target_inspect", "screenshot"},
        )
        targets.append(target.target_id)
        preference = preferences.read(target.target_id)
        assert preference.enabled is False and preference.revision == 1

    admin = McpAdministrationService(sessions)
    agent = admin.create_client(
        display_name="Preserved agent", preset_id=READ_ONLY_INSPECTION_PRESET_ID
    )
    issued = admin.issue_credential(agent.id)
    admin.assign_target_preset(targets[0], READ_ONLY_INSPECTION_PRESET_ID)
    admin.grant_target(agent.id, targets[0])
    preferences.update(
        targets[0], enabled=True, expected_revision=1, administrator_reference="admin"
    )
    before = [preferences.read(target_id) for target_id in targets]
    assert [(item.enabled, item.revision) for item in before] == [(True, 2), (False, 1)]

    command.upgrade(config, "head")
    assert [preferences.read(target_id) for target_id in targets] == before
    assert McpAuthenticationService(sessions).authenticate(issued.token) is not None
    with engine.connect() as connection:
        assert connection.scalars(
            text("SELECT target_id FROM mcp_client_target_grants WHERE client_id=:id"),
            {"id": str(agent.id)},
        ).all() == [str(targets[0])]
        assert connection.scalar(text("PRAGMA integrity_check")) == "ok"
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
    engine.dispose()
