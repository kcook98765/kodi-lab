"""Upgrade installed schemas; refuse discarding repository/effect evidence."""

from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import inspect, text

from kodi_lab.storage.engine import create_database_engine
from tests.application.test_addon_zip_installations import factory as factory_fixture
from tests.application.test_native_zip_installations import Fixture
from tests.application.test_native_zip_installations import native as native_fixture
from tests.storage.test_migrations import (  # noqa: F401
    migration_setup,
    migration_templates,
)

factory = factory_fixture
native = native_fixture


def test_native_upgrade_and_empty_downgrade(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, url = migration_setup(
        monkeypatch, tmp_path, start_revision="0078_workspace_deploy_recovery"
    )
    command.upgrade(config, "0079_native_zip_installation")
    engine = create_database_engine(url)
    assert "native_zip_installations" in inspect(engine).get_table_names()
    checks = inspect(engine).get_check_constraints("addon_zip_artifacts")
    assert any(
        c["name"] == "addon_zip_extension_classification"
        and "kodi_repository" in c["sqltext"]
        for c in checks
    )
    with engine.connect() as connection:
        assert connection.scalar(text("PRAGMA integrity_check")) == "ok"
        assert not connection.execute(text("PRAGMA foreign_key_check")).all()
    command.downgrade(config, "0078_workspace_deploy_recovery")
    assert "native_zip_installations" not in inspect(engine).get_table_names()


def test_repository_artifacts_prevent_destructive_downgrade(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, url = migration_setup(
        monkeypatch, tmp_path, start_revision="0078_workspace_deploy_recovery"
    )
    engine = create_database_engine(url)
    # The historical artifact table is independent of managed target/profile rows.
    with engine.begin() as connection:
        connection.execute(
            text("""INSERT INTO addon_zip_artifacts
            (id,logical_label,sha256_digest,compressed_size,total_uncompressed_bytes,
             member_count,addon_id,addon_name,addon_version,extension_classification,
             dependencies_json,payload_manifest_digest,imported_at,origin,validation_state)
            VALUES ('00000000-0000-4000-8000-000000000079','Prior video',:digest,1,1,1,
                'plugin.video.prior','Prior','1.0.0','python_video_plugin','[]',:digest,
                '2026-10-21T00:00:00.000000Z','uploaded_zip','accepted')"""),
            {"digest": "sha256:" + "a" * 64},
        )
    command.upgrade(config, "0079_native_zip_installation")
    with engine.begin() as connection:
        assert (
            connection.scalar(
                text("SELECT extension_classification FROM addon_zip_artifacts")
            )
            == "python_video_plugin"
        )
        connection.execute(
            text(
                "UPDATE addon_zip_artifacts "
                "SET extension_classification='kodi_repository'"
            )
        )
    with pytest.raises(RuntimeError, match="must be preserved"):
        command.downgrade(config, "0078_workspace_deploy_recovery")
    with engine.connect() as connection:
        assert (
            connection.scalar(text("SELECT version_num FROM alembic_version"))
            == "0079_native_zip_installation"
        )
        assert (
            connection.scalar(
                text("SELECT extension_classification FROM addon_zip_artifacts")
            )
            == "kodi_repository"
        )


def test_native_coordinator_runs_on_migrated_schema(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, native: Fixture
) -> None:
    from sqlalchemy import MetaData, Table

    from kodi_lab.application.native_zip_installations import (
        NativeZipInstallationService,
    )
    from kodi_lab.mcp.authorization import McpAuthorizationService
    from kodi_lab.mcp.registry import build_mcp_tool_registry
    from kodi_lab.storage.engine import create_session_factory
    from tests.application.test_native_zip_installations import Authority, NativeBroker

    config, url = migration_setup(
        monkeypatch,
        tmp_path / "migrated",
        start_revision="0078_workspace_deploy_recovery",
    )
    command.upgrade(config, "0079_native_zip_installation")
    engine = create_database_engine(url)
    tables = (
        "runtime_definitions",
        "runtime_revisions",
        "runtime_execution_profiles",
        "targets",
        "managed_instances",
        "addon_zip_artifacts",
        "mcp_presets",
        "mcp_preset_permissions",
        "mcp_clients",
        "mcp_credentials",
        "mcp_target_preset_assignments",
        "mcp_client_target_grants",
    )
    with native.sessions() as source, engine.begin() as destination:
        for name in tables:
            table = Table(name, MetaData(), autoload_with=engine)
            rows = [dict(row) for row in source.execute(table.select()).mappings()]
            for row in rows:
                from sqlalchemy import and_

                key = and_(
                    *(column == row[column.name] for column in table.primary_key)
                )
                existing = (
                    destination.execute(table.select().where(key)).mappings().first()
                )
                if existing is None:
                    destination.execute(table.insert(), row)
                else:
                    assert dict(existing) == row
    sessions = create_session_factory(engine)
    authorization = McpAuthorizationService(
        sessions, build_mcp_tool_registry(), targets=Authority(), clock=native.clock
    )
    service = NativeZipInstallationService(
        sessions,
        NativeBroker(sessions, native.root),
        authorization,
        data_root=native.root,
        base_url="http://lab.example.org",
        inventory_factory=lambda _o, _p: native.inventory,
        clock=native.clock,
        resolve=lambda _h: ("8.8.8.8",),
    )
    result = service.request(native.principal, native.request)
    assert result.state == "requested" and result.source_url is not None
    assert service.verify(native.principal, result.operation_id).state == "unverified"
