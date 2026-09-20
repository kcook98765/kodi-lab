import os
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from alembic.util.exc import CommandError
from sqlalchemy import Connection, String, inspect, text
from sqlalchemy.exc import IntegrityError

from kodi_lab.storage.engine import create_database_engine

pytestmark = pytest.mark.slow


class MigrationTemplateStore:
    """Closed, integrity-checked SQLite templates at exact Alembic revisions."""

    def __init__(
        self, root: Path, templates: dict[str, Path], *, head_revision: str
    ) -> None:
        self.root = root
        self._templates = templates
        self._head_revision = head_revision

    def clone(self, revision: str, destination: Path) -> None:
        exact_revision = self._head_revision if revision == "head" else revision
        source = self._templates[exact_revision]
        assert not Path(f"{source}-wal").exists()
        assert not Path(f"{source}-shm").exists()
        shutil.copy2(source, destination)
        with sqlite3.connect(destination) as connection:
            assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
            assert connection.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchone() == (exact_revision,)


_active_migration_templates: MigrationTemplateStore | None = None


@pytest.fixture(scope="session", autouse=True)
def migration_templates(
    tmp_path_factory: pytest.TempPathFactory,
) -> MigrationTemplateStore:
    """Build every exact revision once by advancing one pristine closed DB."""
    project_root = Path(__file__).parents[2]
    root = tmp_path_factory.mktemp("migration-templates")
    data_root = root / "data"
    database_dir = data_root / "database"
    database_dir.mkdir(parents=True)
    database_path = database_dir / "kodi-lab.db"
    config = Config(str(project_root / "alembic.ini"))
    revisions = [
        revision.revision
        for revision in reversed(
            list(ScriptDirectory.from_config(config).walk_revisions())
        )
    ]
    templates: dict[str, Path] = {}
    previous_data_root = os.environ.get("KODI_LAB_DATA_ROOT")
    previous_runtime_root = os.environ.pop("KODI_LAB_RUNTIME_ROOT", None)
    os.environ["KODI_LAB_DATA_ROOT"] = str(data_root)
    try:
        for revision in revisions:
            command.upgrade(config, revision)
            template = root / f"{revision}.db"
            with sqlite3.connect(database_path) as connection:
                assert connection.execute("PRAGMA integrity_check").fetchone() == (
                    "ok",
                )
                assert connection.execute(
                    "SELECT version_num FROM alembic_version"
                ).fetchone() == (revision,)
            assert not Path(f"{database_path}-wal").exists()
            assert not Path(f"{database_path}-shm").exists()
            shutil.copy2(database_path, template)
            templates[revision] = template
    finally:
        if previous_data_root is None:
            os.environ.pop("KODI_LAB_DATA_ROOT", None)
        else:
            os.environ["KODI_LAB_DATA_ROOT"] = previous_data_root
        if previous_runtime_root is not None:
            os.environ["KODI_LAB_RUNTIME_ROOT"] = previous_runtime_root
    store = MigrationTemplateStore(root, templates, head_revision=revisions[-1])
    global _active_migration_templates
    _active_migration_templates = store
    return store


def migration_setup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    start_revision: str | None = None,
) -> tuple[Config, str]:
    project_root = Path(__file__).parents[2]
    data_root = tmp_path / "data"
    (data_root / "database").mkdir(parents=True)
    monkeypatch.chdir(project_root)
    monkeypatch.setenv("KODI_LAB_DATA_ROOT", str(data_root))
    monkeypatch.delenv("KODI_LAB_RUNTIME_ROOT", raising=False)
    config = Config(str(project_root / "alembic.ini"))
    database_path = data_root / "database" / "kodi-lab.db"
    if start_revision is not None:
        migration_templates = _active_migration_templates
        assert migration_templates is not None
        migration_templates.clone(start_revision, database_path)
    database_url = f"sqlite:///{database_path}"
    return config, database_url


def test_revision_template_clones_are_private_and_pristine(
    migration_templates: MigrationTemplateStore, tmp_path: Path
) -> None:
    first = tmp_path / "first.db"
    second = tmp_path / "second.db"
    later = tmp_path / "later.db"
    migration_templates.clone("head", first)
    migration_templates.clone("head", second)

    assert first != second
    with sqlite3.connect(first) as connection:
        connection.execute("CREATE TABLE private_mutation (value TEXT NOT NULL)")
    with sqlite3.connect(second) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name = 'private_mutation'"
        ).fetchone() == (0,)

    migration_templates.clone("head", later)
    with sqlite3.connect(later) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name = 'private_mutation'"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == ("0041_mcp_workspace_validation_retention",)


def test_failed_migration_command_cannot_poison_later_clone(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    migration_templates: MigrationTemplateStore,
) -> None:
    config, _database_url = migration_setup(
        monkeypatch,
        tmp_path,
        start_revision="0034_phase9_completion_remediation",
    )
    with pytest.raises(CommandError, match="Can't locate revision"):
        command.upgrade(config, "not_a_revision")

    later = tmp_path / "later.db"
    migration_templates.clone("0034_phase9_completion_remediation", later)
    with sqlite3.connect(later) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == ("0034_phase9_completion_remediation",)


def test_empty_database_upgrades_to_runtime_catalog_head(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(monkeypatch, tmp_path, start_revision=None)

    command.upgrade(config, "head")

    engine = create_database_engine(database_url)
    inspector = inspect(engine)
    assert set(inspector.get_table_names()) == {
        "addon_repository_candidates",
        "addon_repository_attachment_operations",
        "addon_repository_fetch_operations",
        "addon_repository_metadata_snapshots",
        "addon_repository_refresh_operations",
        "addon_repository_sources",
        "addon_zip_artifacts",
        "addon_zip_enablement_operations",
        "addon_zip_import_operations",
        "addon_zip_installation_evidence",
        "addon_zip_installation_operations",
        "addon_zip_smoke_test_operations",
        "agent_workspace_creation_operations",
        "agent_workspace_cleanup_operations",
        "agent_workspace_lifecycle_events",
        "agent_workspace_mutation_events",
        "agent_workspace_promotion_handoffs",
        "agent_workspace_promotion_journal",
        "agent_workspace_promotion_operations",
        "agent_workspace_promotion_plan_paths",
        "agent_workspace_promotion_plans",
        "agent_workspaces",
        "promotion_verification_log_evidence",
        "promotion_verification_operations",
        "alembic_version",
        "development_source_attachment_operations",
        "development_source_deployment_operations",
        "development_source_deployments",
        "development_source_promotion_leases",
        "development_source_enablement_operations",
        "development_source_smoke_test_operations",
        "development_sources",
        "fixture_definitions",
        "global_heavy_operation_lease",
        "managed_instances",
        "managed_screenshots",
        "mcp_clients",
        "mcp_client_target_grants",
        "mcp_client_development_source_grants",
        "mcp_agent_workspace_bindings",
        "mcp_credentials",
        "mcp_development_requests",
        "mcp_presets",
        "mcp_preset_permissions",
        "mcp_remote_action_receipts",
        "mcp_security_events",
        "mcp_target_preset_assignments",
        "managed_instance_delete_operations",
        "managed_instance_addon_repository_sources",
        "managed_instance_development_sources",
        "managed_instance_lifecycle_operations",
        "managed_instance_mutation_leases",
        "reset_operations",
        "repository_replacement_operations",
        "repository_replacement_plans",
        "runtime_definitions",
        "runtime_container_artifacts",
        "runtime_execution_profiles",
        "runtime_revisions",
        "runtime_validation_evidence",
        "snapshot_create_operations",
        "snapshot_delete_operations",
        "snapshot_restore_operations",
        "snapshots",
        "targets",
        "web_admin_credentials",
        "web_sessions",
    }
    managed_foreign_keys = {
        foreign_key["referred_table"]: foreign_key
        for foreign_key in inspector.get_foreign_keys("managed_instances")
    }
    assert managed_foreign_keys["targets"]["options"] == {"ondelete": "CASCADE"}
    assert managed_foreign_keys["runtime_revisions"]["options"] == {
        "ondelete": "RESTRICT"
    }
    assert managed_foreign_keys["fixture_definitions"]["options"] == {
        "ondelete": "RESTRICT"
    }
    assert inspector.get_foreign_keys("reset_operations") == []
    reset_indexes = {
        index["name"]: index for index in inspector.get_indexes("reset_operations")
    }
    assert reset_indexes["uq_reset_operations_active_instance"]["unique"] == 1
    revision_foreign_key = inspector.get_foreign_keys("runtime_revisions")[0]
    assert revision_foreign_key["referred_table"] == "runtime_definitions"
    assert revision_foreign_key["options"] == {"ondelete": "RESTRICT"}
    artifact_foreign_key = inspector.get_foreign_keys("runtime_container_artifacts")[0]
    assert artifact_foreign_key["referred_table"] == "runtime_revisions"
    assert artifact_foreign_key["options"] == {"ondelete": "RESTRICT"}
    profile_foreign_key = inspector.get_foreign_keys("runtime_execution_profiles")[0]
    assert profile_foreign_key["referred_table"] == "runtime_revisions"
    assert profile_foreign_key["options"] == {"ondelete": "RESTRICT"}
    unique_constraints = {
        constraint["name"]: constraint["column_names"]
        for constraint in inspector.get_unique_constraints("runtime_revisions")
    }
    assert unique_constraints["uq_runtime_revision_number"] == [
        "runtime_definition_id",
        "revision_number",
    ]
    engine.dispose()

    command.check(config)


def test_0035_managed_screenshot_populated_downgrade_is_guarded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0034_phase9_completion_remediation"
    )
    command.upgrade(config, "0035_managed_screenshots")
    engine = create_database_engine(database_url)
    with engine.begin() as connection:
        definition_id = str(uuid4())
        revision_id = str(uuid4())
        instance_id = str(uuid4())
        capture_id = str(uuid4())
        connection.execute(
            text(
                "INSERT INTO runtime_definitions "
                "(id, slug, kodi_version, support_tier) VALUES "
                "(:id, :slug, '21.3', 'current')"
            ),
            {"id": definition_id, "slug": f"screenshots-{definition_id}"},
        )
        connection.execute(
            text(
                "INSERT INTO runtime_revisions "
                "(id, runtime_definition_id, revision_number, source_kind, "
                "source_reference, content_digest, manifest_reference, "
                "manifest_digest, built_at, approval_state, "
                "approved_at, requires_trusted_build_confirmation) VALUES "
                "(:id, :definition, 1, 'test', 'test', :digest, 'test', "
                ":digest, '2026-09-19T00:00:00Z', 'approved', "
                "'2026-09-19T00:00:00Z', 0)"
            ),
            {
                "id": revision_id,
                "definition": definition_id,
                "digest": "sha256:" + "a" * 64,
            },
        )
        connection.execute(
            text(
                "INSERT INTO targets "
                "(id, name, target_type, kodi_version, availability, health, "
                "connection_state) VALUES "
                "(:id, 'screenshot', 'managed', '21.3', 'available', "
                "'healthy', 'connected')"
            ),
            {"id": instance_id},
        )
        connection.execute(
            text(
                "INSERT INTO managed_instances "
                "(target_id, instance_type, runtime_revision_id, runtime_state, "
                "start_on_host_boot) VALUES "
                "(:id, 'disposable', :revision, 'stopped', 0)"
            ),
            {"id": instance_id, "revision": revision_id},
        )
        connection.execute(
            text(
                "INSERT INTO managed_screenshots "
                "(managed_instance_id, capture_id, captured_at, mime_type, width, "
                "height, byte_size, last_attempt_at, last_error_code) VALUES "
                "(:instance, :capture, :at, 'image/png', 1280, 720, 1024, :at, NULL)"
            ),
            {
                "instance": instance_id,
                "capture": capture_id,
                "at": "2026-09-19T00:00:00.000000Z",
            },
        )
    engine.dispose()

    with pytest.raises(RuntimeError, match="populated managed screenshot"):
        command.downgrade(config, "0034_phase9_completion_remediation")

    engine = create_database_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM managed_screenshots"))
    engine.dispose()
    command.downgrade(config, "0034_phase9_completion_remediation")
    command.upgrade(config, "0035_managed_screenshots")


def test_0036_mcp_foundation_seeds_builtins_and_guards_populated_downgrade(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0035_managed_screenshots"
    )
    command.upgrade(config, "0036_mcp_core_foundation")
    engine = create_database_engine(database_url)
    with engine.begin() as connection:
        preset_rows = connection.execute(
            text(
                "SELECT display_name, builtin, assignable FROM mcp_presets ORDER BY id"
            )
        ).fetchall()
        assert [tuple(row) for row in preset_rows] == [
            ("Add-on Development", 1, 1),
            ("Read-only Inspection", 1, 1),
            ("Full Lab Control", 1, 1),
            ("Production Remote Kodi", 1, 0),
        ]
        permission_rows = connection.execute(
            text(
                "SELECT permission, COUNT(*) FROM mcp_preset_permissions "
                "GROUP BY permission"
            )
        ).fetchall()
        assert [tuple(row) for row in permission_rows] == [("target.inspect", 3)]
        connection.execute(
            text(
                "INSERT INTO mcp_clients "
                "(id, display_name, enabled, preset_id, created_at, last_used_at) "
                "VALUES (:id, 'migration client', 1, :preset, :at, NULL)"
            ),
            {
                "id": str(uuid4()),
                "preset": "11000000-0000-4000-8000-000000000002",
                "at": "2026-09-19T00:00:00.000000Z",
            },
        )
    engine.dispose()

    with pytest.raises(RuntimeError, match="populated MCP foundation"):
        command.downgrade(config, "0035_managed_screenshots")

    engine = create_database_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM mcp_clients"))
    engine.dispose()
    command.downgrade(config, "0035_managed_screenshots")
    command.upgrade(config, "0036_mcp_core_foundation")


def test_0037_seeds_visual_permission_and_guards_authority_downgrade(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0036_mcp_core_foundation"
    )
    command.upgrade(config, "0037_mcp_observation_permissions")
    engine = create_database_engine(database_url)
    with engine.begin() as connection:
        permission_rows = connection.execute(
            text(
                "SELECT permission, COUNT(*) FROM mcp_preset_permissions "
                "GROUP BY permission ORDER BY permission"
            )
        ).fetchall()
        assert [tuple(row) for row in permission_rows] == [
            ("target.inspect", 3),
            ("target.visual", 3),
        ]
        production_visual = connection.scalar(
            text(
                "SELECT COUNT(*) FROM mcp_preset_permissions "
                "WHERE preset_id = '11000000-0000-4000-8000-000000000004' "
                "AND permission = 'target.visual'"
            )
        )
        assert production_visual == 0
        connection.execute(
            text(
                "INSERT INTO mcp_clients "
                "(id, display_name, enabled, preset_id, created_at, last_used_at) "
                "VALUES (:id, 'visual authority', 1, :preset, :at, NULL)"
            ),
            {
                "id": str(uuid4()),
                "preset": "11000000-0000-4000-8000-000000000002",
                "at": "2026-09-19T00:00:00.000000Z",
            },
        )
    engine.dispose()

    with pytest.raises(RuntimeError, match="observation authority"):
        command.downgrade(config, "0036_mcp_core_foundation")

    engine = create_database_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM mcp_clients"))
    engine.dispose()
    command.downgrade(config, "0036_mcp_core_foundation")

    engine = create_database_engine(database_url)
    with engine.connect() as connection:
        assert (
            connection.scalar(
                text(
                    "SELECT COUNT(*) FROM mcp_preset_permissions "
                    "WHERE permission = 'target.visual'"
                )
            )
            == 0
        )
    engine.dispose()
    command.upgrade(config, "0037_mcp_observation_permissions")

    engine = create_database_engine(database_url)
    with engine.connect() as connection:
        assert (
            connection.scalar(
                text(
                    "SELECT COUNT(*) FROM mcp_preset_permissions "
                    "WHERE permission = 'target.visual'"
                )
            )
            == 3
        )
    engine.dispose()


def test_0039_development_inspection_constraints_and_guarded_downgrade(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0038_mcp_remote_control"
    )
    custom_preset_id = str(uuid4())
    engine = create_database_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO mcp_presets "
                "(id, display_name, builtin, assignable) "
                "VALUES (:id, 'Pre-0039 snapshot', 0, 1)"
            ),
            {"id": custom_preset_id},
        )
        connection.execute(
            text(
                "INSERT INTO mcp_preset_permissions (preset_id, permission) "
                "VALUES (:id, 'target.inspect')"
            ),
            {"id": custom_preset_id},
        )
    engine.dispose()
    command.upgrade(config, "0039_mcp_development_inspection")
    engine = create_database_engine(database_url)
    inspector = inspect(engine)
    assert {
        "mcp_client_development_source_grants",
        "mcp_agent_workspace_bindings",
    } <= set(inspector.get_table_names())
    assert {
        "development_source_id",
        "workspace_id",
    } <= {item["name"] for item in inspector.get_columns("mcp_security_events")}
    client_id = str(uuid4())
    source_id = str(uuid4())
    other_source_id = str(uuid4())
    workspace_id = str(uuid4())
    timestamp = "2026-09-20T00:00:00.000000Z"
    digest = "sha256:" + "a" * 64
    with engine.begin() as connection:
        assert [
            tuple(row)
            for row in connection.execute(
                text(
                    "SELECT preset_id, permission FROM mcp_preset_permissions "
                    "WHERE permission = 'workspace.inspect' ORDER BY preset_id"
                )
            )
        ] == [
            ("11000000-0000-4000-8000-000000000001", "workspace.inspect"),
            ("11000000-0000-4000-8000-000000000003", "workspace.inspect"),
        ]
        assert (
            connection.scalar(
                text(
                    "SELECT COUNT(*) FROM mcp_preset_permissions "
                    "WHERE preset_id = :id AND permission = 'workspace.inspect'"
                ),
                {"id": custom_preset_id},
            )
            == 0
        )
        connection.execute(
            text(
                "INSERT INTO mcp_clients "
                "(id, display_name, enabled, preset_id, created_at, last_used_at) "
                "VALUES (:id, 'Development client', 1, "
                "'11000000-0000-4000-8000-000000000003', :at, NULL)"
            ),
            {"id": client_id, "at": timestamp},
        )
        for current, locator, addon in (
            (source_id, "plugin.video.one", "plugin.video.one"),
            (other_source_id, "plugin.video.two", "plugin.video.two"),
        ):
            connection.execute(
                text(
                    "INSERT INTO development_sources "
                    "(id, logical_name, source_kind, relative_locator, addon_id, "
                    "observed_name, observed_version, extension_types_json, "
                    "registered_at, validated_at) VALUES "
                    "(:id, :name, 'local_directory', :locator, :addon, 'Example', "
                    "'1.0.0', '[\"xbmc.python.pluginsource\"]', :at, :at)"
                ),
                {
                    "id": current,
                    "name": f"Source {addon}",
                    "locator": locator,
                    "addon": addon,
                    "at": timestamp,
                },
            )
        connection.execute(
            text(
                "INSERT INTO agent_workspaces "
                "(id, development_source_id, source_addon_id, "
                "source_logical_name, source_observed_name, "
                "source_observed_version, state, baseline_artifact_id, "
                "baseline_manifest_digest, generation, current_tree_digest, "
                "capture_started_at, captured_at, created_at, updated_at, "
                "bytes_state, promoted_at, abandoned_at, cleaned_at) VALUES "
                "(:id, :source, 'plugin.video.one', 'Source one', 'Example', "
                "'1.0.0', 'ready', :baseline, :digest, 0, :digest, :at, :at, "
                ":at, :at, 'present', NULL, NULL, NULL)"
            ),
            {
                "id": workspace_id,
                "source": source_id,
                "baseline": str(uuid4()),
                "digest": digest,
                "at": timestamp,
            },
        )
        connection.execute(
            text(
                "INSERT INTO mcp_client_development_source_grants "
                "(client_id, development_source_id, created_at) "
                "VALUES (:client, :source, :at)"
            ),
            {"client": client_id, "source": source_id, "at": timestamp},
        )
        for invalid_client, invalid_source in (
            (str(uuid4()), source_id),
            (client_id, str(uuid4())),
        ):
            with pytest.raises(IntegrityError):
                connection.execute(
                    text(
                        "INSERT INTO mcp_client_development_source_grants "
                        "(client_id, development_source_id, created_at) "
                        "VALUES (:client, :source, :at)"
                    ),
                    {
                        "client": invalid_client,
                        "source": invalid_source,
                        "at": timestamp,
                    },
                )
        with pytest.raises(IntegrityError):
            connection.execute(
                text(
                    "INSERT INTO mcp_client_development_source_grants "
                    "(client_id, development_source_id, created_at) "
                    "VALUES (:client, :source, :at)"
                ),
                {"client": client_id, "source": source_id, "at": timestamp},
            )
        with pytest.raises(IntegrityError):
            connection.execute(
                text(
                    "INSERT INTO mcp_agent_workspace_bindings "
                    "(workspace_id, client_id, development_source_id, "
                    "binding_origin, created_at) VALUES "
                    "(:workspace, :client, :source, 'web_admin', :at)"
                ),
                {
                    "workspace": workspace_id,
                    "client": client_id,
                    "source": other_source_id,
                    "at": timestamp,
                },
            )
        for invalid_workspace, invalid_client in (
            (str(uuid4()), client_id),
            (workspace_id, str(uuid4())),
        ):
            with pytest.raises(IntegrityError):
                connection.execute(
                    text(
                        "INSERT INTO mcp_agent_workspace_bindings "
                        "(workspace_id, client_id, development_source_id, "
                        "binding_origin, created_at) VALUES "
                        "(:workspace, :client, :source, 'web_admin', :at)"
                    ),
                    {
                        "workspace": invalid_workspace,
                        "client": invalid_client,
                        "source": source_id,
                        "at": timestamp,
                    },
                )
        connection.execute(
            text(
                "INSERT INTO mcp_agent_workspace_bindings "
                "(workspace_id, client_id, development_source_id, "
                "binding_origin, created_at) VALUES "
                "(:workspace, :client, :source, 'web_admin', :at)"
            ),
            {
                "workspace": workspace_id,
                "client": client_id,
                "source": source_id,
                "at": timestamp,
            },
        )
        with pytest.raises(IntegrityError):
            connection.execute(
                text(
                    "INSERT INTO mcp_agent_workspace_bindings "
                    "(workspace_id, client_id, development_source_id, "
                    "binding_origin, created_at) VALUES "
                    "(:workspace, :client, :source, 'mcp_created', :at)"
                ),
                {
                    "workspace": workspace_id,
                    "client": client_id,
                    "source": source_id,
                    "at": timestamp,
                },
            )
    engine.dispose()

    with pytest.raises(RuntimeError, match="development inspection authority"):
        command.downgrade(config, "0038_mcp_remote_control")
    engine = create_database_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM mcp_agent_workspace_bindings"))
        connection.execute(text("DELETE FROM mcp_client_development_source_grants"))
        connection.execute(
            text("DELETE FROM mcp_clients WHERE id = :id"), {"id": client_id}
        )
    engine.dispose()
    command.downgrade(config, "0038_mcp_remote_control")
    engine = create_database_engine(database_url)
    assert {
        "mcp_client_development_source_grants",
        "mcp_agent_workspace_bindings",
    }.isdisjoint(inspect(engine).get_table_names())
    engine.dispose()
    command.upgrade(config, "0039_mcp_development_inspection")
    engine = create_database_engine(database_url)
    with engine.connect() as connection:
        assert (
            connection.scalar(
                text(
                    "SELECT COUNT(*) FROM mcp_preset_permissions "
                    "WHERE permission = 'workspace.inspect'"
                )
            )
            == 2
        )
    engine.dispose()


def test_0040_workspace_mutation_constraints_and_guarded_downgrade(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0039_mcp_development_inspection"
    )
    custom_preset_id = str(uuid4())
    engine = create_database_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO mcp_presets "
                "(id, display_name, builtin, assignable) "
                "VALUES (:id, 'Pre-0040 snapshot', 0, 1)"
            ),
            {"id": custom_preset_id},
        )
        connection.execute(
            text(
                "INSERT INTO mcp_preset_permissions (preset_id, permission) "
                "VALUES (:id, 'workspace.inspect')"
            ),
            {"id": custom_preset_id},
        )
    engine.dispose()
    command.upgrade(config, "0040_mcp_workspace_mutation")
    engine = create_database_engine(database_url)
    inspector = inspect(engine)
    assert "mcp_development_requests" in inspector.get_table_names()
    assert {
        item["name"] for item in inspector.get_indexes("mcp_development_requests")
    } == {
        "ix_mcp_development_request_client_created",
        "ix_mcp_development_request_created",
    }
    client_id = str(uuid4())
    source_id = str(uuid4())
    workspace_id = str(uuid4())
    operation_id = str(uuid4())
    request_id = str(uuid4())
    timestamp = "2026-09-20T14:00:00.000000Z"
    with engine.begin() as connection:
        assert [
            tuple(row)
            for row in connection.execute(
                text(
                    "SELECT preset_id, permission FROM mcp_preset_permissions "
                    "WHERE permission = 'workspace.edit' ORDER BY preset_id"
                )
            )
        ] == [
            ("11000000-0000-4000-8000-000000000001", "workspace.edit"),
            ("11000000-0000-4000-8000-000000000003", "workspace.edit"),
        ]
        assert (
            connection.scalar(
                text(
                    "SELECT COUNT(*) FROM mcp_preset_permissions "
                    "WHERE preset_id = :id AND permission = 'workspace.edit'"
                ),
                {"id": custom_preset_id},
            )
            == 0
        )
        connection.execute(
            text(
                "INSERT INTO mcp_clients "
                "(id, display_name, enabled, preset_id, created_at, last_used_at) "
                "VALUES (:id, 'Mutation client', 1, "
                "'11000000-0000-4000-8000-000000000003', :at, NULL)"
            ),
            {"id": client_id, "at": timestamp},
        )
        connection.execute(
            text(
                "INSERT INTO development_sources "
                "(id, logical_name, source_kind, relative_locator, addon_id, "
                "observed_name, observed_version, extension_types_json, "
                "registered_at, validated_at) VALUES "
                "(:id, 'Mutation source', 'local_directory', 'plugin.video.mutate', "
                "'plugin.video.mutate', 'Mutation', '1.0.0', "
                "'[\"xbmc.python.pluginsource\"]', :at, :at)"
            ),
            {"id": source_id, "at": timestamp},
        )
        connection.execute(
            text(
                "INSERT INTO agent_workspaces "
                "(id, development_source_id, source_addon_id, source_logical_name, "
                "source_observed_name, source_observed_version, state, "
                "baseline_artifact_id, baseline_manifest_digest, generation, "
                "current_tree_digest, capture_started_at, captured_at, created_at, "
                "updated_at, bytes_state, promoted_at, abandoned_at, cleaned_at) "
                "VALUES (:id, :source, 'plugin.video.mutate', 'Mutation source', "
                "'Mutation', '1.0.0', 'creating', :baseline, NULL, 0, NULL, :at, "
                "NULL, :at, :at, 'present', NULL, NULL, NULL)"
            ),
            {
                "id": workspace_id,
                "source": source_id,
                "baseline": str(uuid4()),
                "at": timestamp,
            },
        )
        connection.execute(
            text(
                "INSERT INTO agent_workspace_creation_operations "
                "(id, workspace_id, development_source_id, requested_at, started_at, "
                "completed_at, state, phase, failure_code, lease_token_digest, "
                "lease_expires_at) VALUES "
                "(:id, :workspace, :source, :at, NULL, NULL, 'queued', 'queued', "
                "NULL, NULL, NULL)"
            ),
            {
                "id": operation_id,
                "workspace": workspace_id,
                "source": source_id,
                "at": timestamp,
            },
        )
        connection.execute(
            text(
                "INSERT INTO mcp_agent_workspace_bindings "
                "(workspace_id, client_id, development_source_id, binding_origin, "
                "created_at) VALUES (:workspace, :client, :source, 'mcp_created', :at)"
            ),
            {
                "workspace": workspace_id,
                "client": client_id,
                "source": source_id,
                "at": timestamp,
            },
        )
        values = {
            "id": str(uuid4()),
            "client": client_id,
            "request": request_id,
            "source": source_id,
            "workspace": workspace_id,
            "operation": operation_id,
            "fingerprint": "a" * 64,
            "at": timestamp,
        }
        insert = text(
            "INSERT INTO mcp_development_requests "
            "(id, client_id, request_id, operation_kind, parameter_fingerprint, "
            "development_source_id, workspace_id, creation_operation_id, state, "
            "created_at, updated_at) VALUES "
            "(:id, :client, :request, 'workspace_create', :fingerprint, :source, "
            ":workspace, :operation, 'allocated', :at, :at)"
        )
        connection.execute(insert, values)
        with pytest.raises(IntegrityError):
            connection.execute(insert, {**values, "id": str(uuid4())})
        with pytest.raises(IntegrityError):
            connection.execute(
                insert,
                {**values, "id": str(uuid4()), "fingerprint": "not-a-digest"},
            )
    engine.dispose()

    with pytest.raises(RuntimeError, match="workspace mutation state"):
        command.downgrade(config, "0039_mcp_development_inspection")
    engine = create_database_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM mcp_development_requests"))
        connection.execute(
            text("DELETE FROM mcp_clients WHERE id = :id"), {"id": client_id}
        )
    engine.dispose()
    command.downgrade(config, "0039_mcp_development_inspection")
    engine = create_database_engine(database_url)
    assert "mcp_development_requests" not in inspect(engine).get_table_names()
    with engine.connect() as connection:
        assert (
            connection.scalar(
                text(
                    "SELECT COUNT(*) FROM mcp_preset_permissions "
                    "WHERE permission = 'workspace.edit'"
                )
            )
            == 0
        )
    engine.dispose()


def test_0038_remote_receipt_constraints_and_downgrade(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0037_mcp_observation_permissions"
    )
    command.upgrade(config, "0038_mcp_remote_control")
    engine = create_database_engine(database_url)
    inspector = inspect(engine)
    assert {
        item["name"] for item in inspector.get_indexes("mcp_remote_action_receipts")
    } == {
        "ix_mcp_remote_receipt_client_created",
        "ix_mcp_remote_receipt_created",
    }
    assert inspector.get_foreign_keys("mcp_remote_action_receipts") == []
    with engine.begin() as connection:
        assert [
            tuple(row)
            for row in connection.execute(
                text(
                    "SELECT permission, COUNT(*) FROM mcp_preset_permissions "
                    "GROUP BY permission ORDER BY permission"
                )
            )
        ] == [
            ("target.control", 2),
            ("target.inspect", 3),
            ("target.visual", 3),
        ]
        assert (
            connection.scalar(
                text(
                    "SELECT COUNT(*) FROM mcp_preset_permissions "
                    "WHERE preset_id = '11000000-0000-4000-8000-000000000002' "
                    "AND permission = 'target.control'"
                )
            )
            == 0
        )
        receipt = {
            "id": str(uuid4()),
            "client": str(uuid4()),
            "credential": str(uuid4()),
            "target": str(uuid4()),
            "request": str(uuid4()),
            "at": "2026-09-19T00:00:00.000000Z",
        }
        connection.execute(
            text(
                "INSERT INTO mcp_remote_action_receipts "
                "(id, client_id, credential_id, target_id, request_id, action, "
                "created_at, state, final_result, completed_at, "
                "screenshot_refresh_result, screenshot_capture_id, "
                "screenshot_captured_at) VALUES "
                "(:id, :client, :credential, :target, :request, 'left', :at, "
                "'pending', NULL, NULL, NULL, NULL, NULL)"
            ),
            receipt,
        )
        with pytest.raises(IntegrityError):
            connection.execute(
                text(
                    "INSERT INTO mcp_remote_action_receipts "
                    "(id, client_id, credential_id, target_id, request_id, action, "
                    "created_at, state, final_result, completed_at, "
                    "screenshot_refresh_result, screenshot_capture_id, "
                    "screenshot_captured_at) VALUES "
                    "(:other, :client, :credential, :target, :request, 'right', :at, "
                    "'pending', NULL, NULL, NULL, NULL, NULL)"
                ),
                receipt | {"other": str(uuid4())},
            )
    engine.dispose()

    with pytest.raises(RuntimeError, match="remote-control authority or receipts"):
        command.downgrade(config, "0037_mcp_observation_permissions")

    engine = create_database_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM mcp_remote_action_receipts"))
    engine.dispose()
    command.downgrade(config, "0037_mcp_observation_permissions")
    engine = create_database_engine(database_url)
    assert "mcp_remote_action_receipts" not in inspect(engine).get_table_names()
    with engine.connect() as connection:
        assert (
            connection.scalar(
                text(
                    "SELECT COUNT(*) FROM mcp_preset_permissions "
                    "WHERE permission = 'target.control'"
                )
            )
            == 0
        )
    engine.dispose()
    command.upgrade(config, "0038_mcp_remote_control")


def test_explicit_0001_upgrades_to_head(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(monkeypatch, tmp_path, start_revision=None)
    command.upgrade(config, "0001_targets_instances")

    engine = create_database_engine(database_url)
    assert "runtime_revision" in {
        column["name"] for column in inspect(engine).get_columns("managed_instances")
    }
    engine.dispose()

    command.upgrade(config, "head")

    engine = create_database_engine(database_url)
    columns = {
        column["name"] for column in inspect(engine).get_columns("managed_instances")
    }
    assert "runtime_revision" not in columns
    assert "runtime_revision_id" in columns
    engine.dispose()


def test_explicit_0002_upgrades_to_artifact_head(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0002_runtime_catalog"
    )
    command.upgrade(config, "0002_runtime_catalog")
    engine = create_database_engine(database_url)
    assert "runtime_container_artifacts" not in inspect(engine).get_table_names()
    engine.dispose()

    command.upgrade(config, "0003_runtime_container_artifacts")
    engine = create_database_engine(database_url)
    assert "runtime_container_artifacts" in inspect(engine).get_table_names()
    engine.dispose()


def test_0007_upgrades_to_reset_operations_and_empty_round_trip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0007_fixture_definitions"
    )
    command.upgrade(config, "0007_fixture_definitions")
    engine = create_database_engine(database_url)
    assert "reset_operations" not in inspect(engine).get_table_names()
    engine.dispose()

    command.upgrade(config, "0008_reset_operations")
    engine = create_database_engine(database_url)
    assert "reset_operations" in inspect(engine).get_table_names()
    engine.dispose()
    command.downgrade(config, "0007_fixture_definitions")
    command.upgrade(config, "0008_reset_operations")

    engine = create_database_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO runtime_definitions "
                "(id, slug, kodi_version, support_tier) "
                "VALUES ('81000000-0000-4000-8000-000000000001', 'reset-test', "
                "'21.3', 'current')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO runtime_revisions (id, runtime_definition_id, "
                "revision_number, source_kind, source_reference, content_digest, "
                "manifest_reference, manifest_digest, built_at, approval_state, "
                "requires_trusted_build_confirmation) VALUES "
                "('81000000-0000-4000-8000-000000000002', "
                "'81000000-0000-4000-8000-000000000001', 1, 'test', 'test', "
                ":digest, 'test', :digest, '2026-09-15T00:00:00Z', "
                "'not_approved', 0)"
            ),
            {"digest": "sha256:" + "a" * 64},
        )
        connection.execute(
            text(
                "INSERT INTO targets (id, name, target_type, kodi_version, "
                "availability, health, connection_state) VALUES "
                "('81000000-0000-4000-8000-000000000003', 'reset', 'managed', "
                "'21.3', 'unavailable', 'warning', 'disconnected')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO managed_instances (target_id, instance_type, "
                "runtime_revision_id, runtime_state, start_on_host_boot) VALUES "
                "('81000000-0000-4000-8000-000000000003', 'disposable', "
                "'81000000-0000-4000-8000-000000000002', 'stopped', 0)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO reset_operations (id, managed_instance_id, requested_at, "
                "completed_at, state, phase, reconciliation_required) VALUES "
                "('81000000-0000-4000-8000-000000000004', "
                "'81000000-0000-4000-8000-000000000003', "
                "'2026-09-15T00:00:00.000000Z', "
                "'2026-09-15T00:01:00.000000Z', 'succeeded', 'succeeded', 0)"
            )
        )
    engine.dispose()
    with pytest.raises(RuntimeError, match="populated reset operations"):
        command.downgrade(config, "0007_fixture_definitions")


def test_0008_upgrades_to_fixture_taxonomy_and_empty_round_trip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0008_reset_operations"
    )
    command.upgrade(config, "0008_reset_operations")

    engine = create_database_engine(database_url)
    before = next(
        column
        for column in inspect(engine).get_columns("fixture_definitions")
        if column["name"] == "content_category"
    )
    assert isinstance(before["type"], String)
    assert before["type"].length == 5
    engine.dispose()

    command.upgrade(config, "0009_fixture_content_taxonomy")
    engine = create_database_engine(database_url)
    after = next(
        column
        for column in inspect(engine).get_columns("fixture_definitions")
        if column["name"] == "content_category"
    )
    assert isinstance(after["type"], String)
    assert after["type"].length == 16
    engine.dispose()

    command.downgrade(config, "0008_reset_operations")
    command.upgrade(config, "0009_fixture_content_taxonomy")


def test_populated_mixed_fixture_assignment_and_reset_survive_0009(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0008_reset_operations"
    )
    command.upgrade(config, "0008_reset_operations")
    engine = create_database_engine(database_url)
    timestamp = "2026-09-15T00:00:00.000000Z"
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO runtime_definitions "
                "(id, slug, kodi_version, support_tier) VALUES "
                "('82000000-0000-4000-8000-000000000001', 'taxonomy-test', "
                "'21.3', 'current')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO runtime_revisions (id, runtime_definition_id, "
                "revision_number, source_kind, source_reference, content_digest, "
                "manifest_reference, manifest_digest, built_at, approval_state, "
                "requires_trusted_build_confirmation) VALUES "
                "('82000000-0000-4000-8000-000000000002', "
                "'82000000-0000-4000-8000-000000000001', 1, 'test', 'test', "
                ":digest, 'test', :digest, :timestamp, 'not_approved', 0)"
            ),
            {"digest": "sha256:" + "a" * 64, "timestamp": timestamp},
        )
        connection.execute(
            text(
                "INSERT INTO targets (id, name, target_type, kodi_version, "
                "availability, health, connection_state) VALUES "
                "('82000000-0000-4000-8000-000000000003', 'taxonomy', 'managed', "
                "'21.3', 'unavailable', 'warning', 'disconnected')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO fixture_definitions "
                "(id, slug, fixture_version, size_category, content_category, "
                "content_digest, compatible_kodi_version, description) VALUES "
                "('82000000-0000-4000-8000-000000000004', 'retained-mixed', 1, "
                "'small', 'mixed', :digest, '21.3', 'retained mixed fixture')"
            ),
            {"digest": "sha256:" + "b" * 64},
        )
        connection.execute(
            text(
                "INSERT INTO managed_instances (target_id, instance_type, "
                "runtime_revision_id, runtime_state, start_on_host_boot, "
                "fixture_definition_id) VALUES "
                "('82000000-0000-4000-8000-000000000003', 'disposable', "
                "'82000000-0000-4000-8000-000000000002', 'stopped', 0, "
                "'82000000-0000-4000-8000-000000000004')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO reset_operations (id, managed_instance_id, requested_at, "
                "completed_at, state, phase, reconciliation_required) VALUES "
                "('82000000-0000-4000-8000-000000000005', "
                "'82000000-0000-4000-8000-000000000003', :timestamp, :timestamp, "
                "'succeeded', 'succeeded', 0)"
            ),
            {"timestamp": timestamp},
        )
        before = {
            table: connection.execute(
                text(f'SELECT * FROM "{table}" ORDER BY 1')
            ).fetchall()
            for table in (
                "fixture_definitions",
                "managed_instances",
                "reset_operations",
            )
        }
    engine.dispose()

    command.upgrade(config, "0009_fixture_content_taxonomy")
    engine = create_database_engine(database_url)
    with engine.connect() as connection:
        after = {
            table: connection.execute(
                text(f'SELECT * FROM "{table}" ORDER BY 1')
            ).fetchall()
            for table in before
        }
    assert after == before
    engine.dispose()


def test_fixture_taxonomy_constraint_accepts_only_mixed_and_movies(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0008_reset_operations"
    )
    command.upgrade(config, "0009_fixture_content_taxonomy")
    engine = create_database_engine(database_url)
    statement = text(
        "INSERT INTO fixture_definitions "
        "(id, slug, fixture_version, size_category, content_category, "
        "content_digest, compatible_kodi_version, description) VALUES "
        "(:id, :slug, 1, 'small', :category, :digest, '21.3', 'taxonomy')"
    )
    with engine.begin() as connection:
        for ordinal, category in enumerate(("mixed", "movies"), start=1):
            connection.execute(
                statement,
                {
                    "id": f"83000000-0000-4000-8000-{ordinal:012d}",
                    "slug": f"category-{category}",
                    "category": category,
                    "digest": "sha256:" + f"{ordinal:064x}",
                },
            )
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            statement,
            {
                "id": "83000000-0000-4000-8000-000000000003",
                "slug": "category-unknown",
                "category": "unknown",
                "digest": "sha256:" + "3" * 64,
            },
        )
    engine.dispose()


def test_movies_fixture_refuses_lossy_0009_downgrade(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0008_reset_operations"
    )
    command.upgrade(config, "0009_fixture_content_taxonomy")
    engine = create_database_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO fixture_definitions "
                "(id, slug, fixture_version, size_category, content_category, "
                "content_digest, compatible_kodi_version, description) VALUES "
                "('84000000-0000-4000-8000-000000000001', 'movies-retained', 1, "
                "'small', 'movies', :digest, '21.3', 'movies retained')"
            ),
            {"digest": "sha256:" + "d" * 64},
        )
    engine.dispose()

    with pytest.raises(RuntimeError, match="populated Movies definitions"):
        command.downgrade(config, "0008_reset_operations")

    engine = create_database_engine(database_url)
    with engine.connect() as connection:
        assert connection.execute(
            text(
                "SELECT slug, content_category FROM fixture_definitions "
                "WHERE id = '84000000-0000-4000-8000-000000000001'"
            )
        ).one() == ("movies-retained", "movies")
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0009_fixture_content_taxonomy"
        )
    engine.dispose()


def test_0009_upgrades_to_tv_taxonomy_and_empty_round_trip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, _database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0009_fixture_content_taxonomy"
    )
    command.upgrade(config, "0009_fixture_content_taxonomy")
    command.upgrade(config, "0010_fixture_tv_taxonomy")
    command.downgrade(config, "0009_fixture_content_taxonomy")
    command.upgrade(config, "0010_fixture_tv_taxonomy")


def test_existing_movies_definition_survives_0010_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0009_fixture_content_taxonomy"
    )
    command.upgrade(config, "0009_fixture_content_taxonomy")
    engine = create_database_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO fixture_definitions "
                "(id, slug, fixture_version, size_category, content_category, "
                "content_digest, compatible_kodi_version, description) VALUES "
                "('85500000-0000-4000-8000-000000000001', 'movies-existing', 1, "
                "'small', 'movies', :digest, '21.3', 'existing Movies fixture')"
            ),
            {"digest": "sha256:" + "5" * 64},
        )
        before = connection.execute(
            text(
                "SELECT * FROM fixture_definitions "
                "WHERE id = '85500000-0000-4000-8000-000000000001'"
            )
        ).one()
    engine.dispose()

    command.upgrade(config, "0010_fixture_tv_taxonomy")
    engine = create_database_engine(database_url)
    with engine.connect() as connection:
        after = connection.execute(
            text(
                "SELECT * FROM fixture_definitions "
                "WHERE id = '85500000-0000-4000-8000-000000000001'"
            )
        ).one()
    assert after == before
    engine.dispose()


def test_tv_taxonomy_constraint_accepts_only_three_supported_categories(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0009_fixture_content_taxonomy"
    )
    command.upgrade(config, "0010_fixture_tv_taxonomy")
    engine = create_database_engine(database_url)
    statement = text(
        "INSERT INTO fixture_definitions "
        "(id, slug, fixture_version, size_category, content_category, "
        "content_digest, compatible_kodi_version, description) VALUES "
        "(:id, :slug, 1, 'small', :category, :digest, '21.3', 'taxonomy')"
    )
    with engine.begin() as connection:
        for ordinal, category in enumerate(("mixed", "movies", "tv"), start=1):
            connection.execute(
                statement,
                {
                    "id": f"85000000-0000-4000-8000-{ordinal:012d}",
                    "slug": f"supported-{category}",
                    "category": category,
                    "digest": "sha256:" + f"{ordinal:064x}",
                },
            )
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            statement,
            {
                "id": "85000000-0000-4000-8000-000000000004",
                "slug": "unsupported-category",
                "category": "unknown",
                "digest": "sha256:" + "4" * 64,
            },
        )
    engine.dispose()


def test_tv_fixture_refuses_lossy_0010_downgrade(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0009_fixture_content_taxonomy"
    )
    command.upgrade(config, "0010_fixture_tv_taxonomy")
    engine = create_database_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO fixture_definitions "
                "(id, slug, fixture_version, size_category, content_category, "
                "content_digest, compatible_kodi_version, description) VALUES "
                "('86000000-0000-4000-8000-000000000001', 'tv-retained', 1, "
                "'small', 'tv', :digest, '21.3', 'TV retained')"
            ),
            {"digest": "sha256:" + "e" * 64},
        )
    engine.dispose()

    with pytest.raises(RuntimeError, match="populated TV definitions"):
        command.downgrade(config, "0009_fixture_content_taxonomy")

    engine = create_database_engine(database_url)
    with engine.connect() as connection:
        assert connection.execute(
            text(
                "SELECT slug, content_category FROM fixture_definitions "
                "WHERE id = '86000000-0000-4000-8000-000000000001'"
            )
        ).one() == ("tv-retained", "tv")
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0010_fixture_tv_taxonomy"
        )
    engine.dispose()


def test_0010_upgrades_to_medium_taxonomy_and_empty_round_trip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0010_fixture_tv_taxonomy"
    )
    command.upgrade(config, "0010_fixture_tv_taxonomy")
    engine = create_database_engine(database_url)
    before = next(
        column
        for column in inspect(engine).get_columns("fixture_definitions")
        if column["name"] == "size_category"
    )
    assert isinstance(before["type"], String)
    assert before["type"].length == 5
    engine.dispose()

    command.upgrade(config, "0011_fixture_medium_taxonomy")
    engine = create_database_engine(database_url)
    after = next(
        column
        for column in inspect(engine).get_columns("fixture_definitions")
        if column["name"] == "size_category"
    )
    assert isinstance(after["type"], String)
    assert after["type"].length == 16
    engine.dispose()
    command.downgrade(config, "0010_fixture_tv_taxonomy")
    command.upgrade(config, "0011_fixture_medium_taxonomy")


def test_medium_size_constraint_accepts_only_small_and_medium(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0010_fixture_tv_taxonomy"
    )
    command.upgrade(config, "0011_fixture_medium_taxonomy")
    engine = create_database_engine(database_url)
    statement = text(
        "INSERT INTO fixture_definitions "
        "(id, slug, fixture_version, size_category, content_category, "
        "content_digest, compatible_kodi_version, description) VALUES "
        "(:id, :slug, 1, :size, 'mixed', :digest, '21.3', 'size taxonomy')"
    )
    with engine.begin() as connection:
        for ordinal, size in enumerate(("small", "medium"), start=1):
            connection.execute(
                statement,
                {
                    "id": f"87000000-0000-4000-8000-{ordinal:012d}",
                    "slug": f"supported-{size}",
                    "size": size,
                    "digest": "sha256:" + f"{ordinal:064x}",
                },
            )
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            statement,
            {
                "id": "87000000-0000-4000-8000-000000000003",
                "slug": "unsupported-large",
                "size": "large",
                "digest": "sha256:" + "3" * 64,
            },
        )
    engine.dispose()


def test_medium_fixture_refuses_lossy_0011_downgrade_and_remains_intact(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0010_fixture_tv_taxonomy"
    )
    command.upgrade(config, "0011_fixture_medium_taxonomy")
    engine = create_database_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO fixture_definitions "
                "(id, slug, fixture_version, size_category, content_category, "
                "content_digest, compatible_kodi_version, description) VALUES "
                "('88000000-0000-4000-8000-000000000001', 'medium-retained', "
                "1, 'medium', 'mixed', :digest, '21.3', 'Medium retained')"
            ),
            {"digest": "sha256:" + "8" * 64},
        )
    engine.dispose()

    with pytest.raises(RuntimeError, match="populated Medium definitions"):
        command.downgrade(config, "0010_fixture_tv_taxonomy")

    engine = create_database_engine(database_url)
    with engine.connect() as connection:
        assert connection.execute(
            text(
                "SELECT slug, size_category FROM fixture_definitions "
                "WHERE id = '88000000-0000-4000-8000-000000000001'"
            )
        ).one() == ("medium-retained", "medium")
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0011_fixture_medium_taxonomy"
        )
    engine.dispose()


def test_0011_active_reset_is_seeded_into_phase7_mutation_lease(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0011_fixture_medium_taxonomy"
    )
    command.upgrade(config, "0011_fixture_medium_taxonomy")
    engine = create_database_engine(database_url)
    timestamp = "2026-09-15T00:00:00.000000Z"
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO runtime_definitions "
                "(id, slug, kodi_version, support_tier) VALUES "
                "('89000000-0000-4000-8000-000000000001', 'phase7-test', "
                "'21.3', 'current')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO runtime_revisions (id, runtime_definition_id, "
                "revision_number, source_kind, source_reference, content_digest, "
                "manifest_reference, manifest_digest, built_at, approval_state, "
                "requires_trusted_build_confirmation) VALUES "
                "('89000000-0000-4000-8000-000000000002', "
                "'89000000-0000-4000-8000-000000000001', 1, 'test', 'test', "
                ":digest, 'test', :digest, :timestamp, 'not_approved', 0)"
            ),
            {"digest": "sha256:" + "9" * 64, "timestamp": timestamp},
        )
        connection.execute(
            text(
                "INSERT INTO targets (id, name, target_type, kodi_version, "
                "availability, health, connection_state) VALUES "
                "('89000000-0000-4000-8000-000000000003', 'phase7', 'managed', "
                "'21.3', 'unavailable', 'warning', 'disconnected')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO managed_instances (target_id, instance_type, "
                "runtime_revision_id, runtime_state, start_on_host_boot) VALUES "
                "('89000000-0000-4000-8000-000000000003', 'disposable', "
                "'89000000-0000-4000-8000-000000000002', 'stopped', 0)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO reset_operations (id, managed_instance_id, "
                "requested_at, state, phase, reconciliation_required) VALUES "
                "('89000000-0000-4000-8000-000000000004', "
                "'89000000-0000-4000-8000-000000000003', :timestamp, "
                "'queued', 'queued', 0)"
            ),
            {"timestamp": timestamp},
        )
    engine.dispose()

    command.upgrade(config, "0012_disposable_snapshot_creation")
    engine = create_database_engine(database_url)
    with engine.connect() as connection:
        assert connection.execute(
            text(
                "SELECT operation_kind, operation_id, lease_token_digest "
                "FROM managed_instance_mutation_leases"
            )
        ).one() == (
            "reset",
            "89000000-0000-4000-8000-000000000004",
            None,
        )
    engine.dispose()


def test_populated_phase7_snapshot_state_refuses_downgrade(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0011_fixture_medium_taxonomy"
    )
    command.upgrade(config, "0012_disposable_snapshot_creation")
    engine = create_database_engine(database_url)
    timestamp = "2026-09-15T00:00:00.000000Z"
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO runtime_definitions "
                "(id, slug, kodi_version, support_tier) VALUES "
                "('89500000-0000-4000-8000-000000000001', 'snapshot-test', "
                "'21.3', 'current')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO runtime_revisions (id, runtime_definition_id, "
                "revision_number, source_kind, source_reference, content_digest, "
                "manifest_reference, manifest_digest, built_at, approval_state, "
                "requires_trusted_build_confirmation) VALUES "
                "('89500000-0000-4000-8000-000000000002', "
                "'89500000-0000-4000-8000-000000000001', 1, 'test', 'test', "
                ":digest, 'test', :digest, :timestamp, 'not_approved', 0)"
            ),
            {"digest": "sha256:" + "5" * 64, "timestamp": timestamp},
        )
        connection.execute(
            text(
                "INSERT INTO targets (id, name, target_type, kodi_version, "
                "availability, health, connection_state) VALUES "
                "('89500000-0000-4000-8000-000000000003', 'snapshot', 'managed', "
                "'21.3', 'unavailable', 'warning', 'disconnected')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO managed_instances (target_id, instance_type, "
                "runtime_revision_id, runtime_state, start_on_host_boot) VALUES "
                "('89500000-0000-4000-8000-000000000003', 'disposable', "
                "'89500000-0000-4000-8000-000000000002', 'stopped', 0)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO snapshots (id, source_managed_instance_id, "
                "runtime_revision_id, captured_at, manifest_version, state, "
                "validation_result) VALUES "
                "('89500000-0000-4000-8000-000000000004', "
                "'89500000-0000-4000-8000-000000000003', "
                "'89500000-0000-4000-8000-000000000002', :timestamp, 1, "
                "'creating', 'pending')"
            ),
            {"timestamp": timestamp},
        )
    engine.dispose()

    with pytest.raises(RuntimeError, match="populated Phase 7 snapshot state"):
        command.downgrade(config, "0011_fixture_medium_taxonomy")

    engine = create_database_engine(database_url)
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT COUNT(*) FROM snapshots")) == 1
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0012_disposable_snapshot_creation"
        )
    engine.dispose()


def test_populated_snapshot_restore_state_refuses_downgrade(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0013_disposable_snapshot_restore"
    )
    command.upgrade(config, "0013_disposable_snapshot_restore")
    engine = create_database_engine(database_url)
    timestamp = "2026-09-16T00:00:00.000000Z"
    digest = "sha256:" + "6" * 64
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO runtime_definitions "
                "(id, slug, kodi_version, support_tier) VALUES "
                "('89600000-0000-4000-8000-000000000001', 'restore-test', "
                "'21.3', 'current')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO runtime_revisions (id, runtime_definition_id, "
                "revision_number, source_kind, source_reference, content_digest, "
                "manifest_reference, manifest_digest, built_at, approval_state, "
                "requires_trusted_build_confirmation) VALUES "
                "('89600000-0000-4000-8000-000000000002', "
                "'89600000-0000-4000-8000-000000000001', 1, 'test', 'test', "
                ":digest, 'test', :digest, :timestamp, 'not_approved', 0)"
            ),
            {"digest": digest, "timestamp": timestamp},
        )
        connection.execute(
            text(
                "INSERT INTO targets (id, name, target_type, kodi_version, "
                "availability, health, connection_state) VALUES "
                "('89600000-0000-4000-8000-000000000003', 'restore', 'managed', "
                "'21.3', 'unavailable', 'warning', 'disconnected')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO managed_instances (target_id, instance_type, "
                "runtime_revision_id, runtime_state, start_on_host_boot) VALUES "
                "('89600000-0000-4000-8000-000000000003', 'disposable', "
                "'89600000-0000-4000-8000-000000000002', 'stopped', 0)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO snapshots (id, source_managed_instance_id, "
                "runtime_revision_id, captured_at, prior_runtime_state, "
                "manifest_version, archive_digest, manifest_digest, archive_size, "
                "member_count, state, validation_at, validation_result) VALUES "
                "('89600000-0000-4000-8000-000000000004', "
                "'89600000-0000-4000-8000-000000000003', "
                "'89600000-0000-4000-8000-000000000002', :timestamp, 'stopped', "
                "1, :digest, :digest, 10240, 1, 'available', :timestamp, 'passed')"
            ),
            {"digest": digest, "timestamp": timestamp},
        )
        connection.execute(
            text(
                "INSERT INTO snapshot_restore_operations "
                "(id, snapshot_id, managed_instance_id, requested_at, state, "
                "phase, reconciliation_required) VALUES "
                "('89600000-0000-4000-8000-000000000005', "
                "'89600000-0000-4000-8000-000000000004', "
                "'89600000-0000-4000-8000-000000000003', :timestamp, "
                "'queued', 'queued', 0)"
            ),
            {"timestamp": timestamp},
        )
    engine.dispose()

    with pytest.raises(RuntimeError, match="populated snapshot restore state"):
        command.downgrade(config, "0012_disposable_snapshot_creation")

    engine = create_database_engine(database_url)
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0013_disposable_snapshot_restore"
        )
        assert (
            connection.scalar(text("SELECT COUNT(*) FROM snapshot_restore_operations"))
            == 1
        )
    engine.dispose()


def test_0014_preserves_history_across_snapshot_delete_and_refuses_lossy_downgrade(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0013_disposable_snapshot_restore"
    )
    command.upgrade(config, "0013_disposable_snapshot_restore")
    engine = create_database_engine(database_url)
    timestamp = "2026-09-16T00:00:00.000000Z"
    digest = "sha256:" + "7" * 64
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO runtime_definitions "
                "(id, slug, kodi_version, support_tier) VALUES "
                "('89700000-0000-4000-8000-000000000001', 'delete-test', "
                "'21.3', 'current')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO runtime_revisions (id, runtime_definition_id, "
                "revision_number, source_kind, source_reference, content_digest, "
                "manifest_reference, manifest_digest, built_at, approval_state, "
                "requires_trusted_build_confirmation) VALUES "
                "('89700000-0000-4000-8000-000000000002', "
                "'89700000-0000-4000-8000-000000000001', 1, 'test', 'test', "
                ":digest, 'test', :digest, :timestamp, 'not_approved', 0)"
            ),
            {"digest": digest, "timestamp": timestamp},
        )
        connection.execute(
            text(
                "INSERT INTO targets (id, name, target_type, kodi_version, "
                "availability, health, connection_state) VALUES "
                "('89700000-0000-4000-8000-000000000003', 'delete', 'managed', "
                "'21.3', 'unavailable', 'warning', 'disconnected')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO managed_instances (target_id, instance_type, "
                "runtime_revision_id, runtime_state, start_on_host_boot) VALUES "
                "('89700000-0000-4000-8000-000000000003', 'disposable', "
                "'89700000-0000-4000-8000-000000000002', 'stopped', 0)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO snapshots (id, source_managed_instance_id, "
                "runtime_revision_id, captured_at, prior_runtime_state, "
                "manifest_version, archive_digest, manifest_digest, archive_size, "
                "member_count, state, validation_at, validation_result) VALUES "
                "('89700000-0000-4000-8000-000000000004', "
                "'89700000-0000-4000-8000-000000000003', "
                "'89700000-0000-4000-8000-000000000002', :timestamp, 'stopped', "
                "1, :digest, :digest, 10240, 1, 'available', :timestamp, 'passed')"
            ),
            {"digest": digest, "timestamp": timestamp},
        )
        for table, operation_id in (
            ("snapshot_create_operations", "89700000-0000-4000-8000-000000000005"),
            ("snapshot_restore_operations", "89700000-0000-4000-8000-000000000006"),
        ):
            connection.execute(
                text(
                    f"INSERT INTO {table} (id, snapshot_id, managed_instance_id, "
                    "requested_at, completed_at, state, phase, "
                    "reconciliation_required) VALUES (:id, "
                    "'89700000-0000-4000-8000-000000000004', "
                    "'89700000-0000-4000-8000-000000000003', :timestamp, "
                    ":timestamp, 'succeeded', 'succeeded', 0)"
                ),
                {"id": operation_id, "timestamp": timestamp},
            )
    engine.dispose()

    command.upgrade(config, "0014_snapshot_deletion")
    engine = create_database_engine(database_url)
    with engine.begin() as connection:
        assert not any(
            foreign_key["constrained_columns"] == ["snapshot_id"]
            for table in (
                "snapshot_create_operations",
                "snapshot_restore_operations",
            )
            for foreign_key in inspect(connection).get_foreign_keys(table)
        )
        connection.execute(
            text(
                "UPDATE snapshots SET state='deleting' WHERE "
                "id='89700000-0000-4000-8000-000000000004'"
            )
        )
        connection.execute(
            text(
                "INSERT INTO snapshot_delete_operations "
                "(id, snapshot_id, managed_instance_id, prior_snapshot_state, "
                "requested_at, completed_at, state, phase, reconciliation_required) "
                "VALUES ('89700000-0000-4000-8000-000000000007', "
                "'89700000-0000-4000-8000-000000000004', "
                "'89700000-0000-4000-8000-000000000003', 'available', :timestamp, "
                ":timestamp, 'succeeded', 'succeeded', 0)"
            ),
            {"timestamp": timestamp},
        )
        connection.execute(
            text(
                "DELETE FROM snapshots WHERE id='89700000-0000-4000-8000-000000000004'"
            )
        )
        assert connection.scalar(text("SELECT COUNT(*) FROM snapshots")) == 0
        assert (
            connection.scalar(text("SELECT COUNT(*) FROM snapshot_create_operations"))
            == 1
        )
        assert (
            connection.scalar(text("SELECT COUNT(*) FROM snapshot_restore_operations"))
            == 1
        )
        assert (
            connection.scalar(text("SELECT COUNT(*) FROM snapshot_delete_operations"))
            == 1
        )
    engine.dispose()

    with pytest.raises(RuntimeError, match="populated snapshot deletion history"):
        command.downgrade(config, "0013_disposable_snapshot_restore")
    engine = create_database_engine(database_url)
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0014_snapshot_deletion"
        )
    engine.dispose()


def test_prior_head_upgrades_to_execution_profile_head(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0003_runtime_container_artifacts"
    )
    command.upgrade(config, "0003_runtime_container_artifacts")
    engine = create_database_engine(database_url)
    assert "runtime_execution_profiles" not in inspect(engine).get_table_names()
    engine.dispose()

    command.upgrade(config, "0004_runtime_execution_profiles")
    engine = create_database_engine(database_url)
    assert "runtime_execution_profiles" in inspect(engine).get_table_names()
    engine.dispose()

    command.downgrade(config, "0003_runtime_container_artifacts")
    command.upgrade(config, "0004_runtime_execution_profiles")


def test_0004_upgrades_to_web_auth_head_and_empty_state_round_trips(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0004_runtime_execution_profiles"
    )
    command.upgrade(config, "0004_runtime_execution_profiles")
    engine = create_database_engine(database_url)
    assert "web_admin_credentials" not in inspect(engine).get_table_names()
    engine.dispose()

    command.upgrade(config, "0005_web_auth_sessions")
    engine = create_database_engine(database_url)
    inspector = inspect(engine)
    assert {"web_admin_credentials", "web_sessions"}.issubset(
        inspector.get_table_names()
    )
    session_foreign_key = inspector.get_foreign_keys("web_sessions")[0]
    assert session_foreign_key["referred_table"] == "web_admin_credentials"
    assert session_foreign_key["options"] == {"ondelete": "RESTRICT"}
    engine.dispose()

    command.downgrade(config, "0004_runtime_execution_profiles")
    command.upgrade(config, "0005_web_auth_sessions")


def test_populated_web_auth_state_refuses_downgrade(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0004_runtime_execution_profiles"
    )
    command.upgrade(config, "0005_web_auth_sessions")
    engine = create_database_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO web_admin_credentials "
                "(admin_key, password_hash, password_updated_at) VALUES "
                "('local-admin', '$argon2id$test', '2026-09-15T00:00:00.000000Z')"
            )
        )
    engine.dispose()

    with pytest.raises(RuntimeError, match="populated Web authentication"):
        command.downgrade(config, "0004_runtime_execution_profiles")


def test_populated_0001_migration_refuses_ambiguous_legacy_revision(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0001_targets_instances"
    )
    command.upgrade(config, "0001_targets_instances")
    engine = create_database_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO targets (
                    id, name, target_type, kodi_version,
                    availability, health, connection_state
                ) VALUES (
                    '00000000-0000-0000-0000-000000000001', 'legacy',
                    'managed', '21.3', 'unavailable', 'warning', 'disconnected'
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO managed_instances (
                    target_id, instance_type, runtime_revision,
                    runtime_state, start_on_host_boot
                ) VALUES (
                    '00000000-0000-0000-0000-000000000001', 'persistent',
                    'legacy-free-form', 'stopped', 0
                )
                """
            )
        )
    engine.dispose()

    with pytest.raises(RuntimeError, match="predates the runtime catalog"):
        command.upgrade(config, "0002_runtime_catalog")

    engine = create_database_engine(database_url)
    inspector = inspect(engine)
    assert "runtime_definitions" not in inspector.get_table_names()
    with engine.connect() as connection:
        assert (
            connection.scalar(text("SELECT runtime_revision FROM managed_instances"))
            == "legacy-free-form"
        )
    engine.dispose()


def test_head_downgrades_to_0001_and_upgrades_again(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(monkeypatch, tmp_path, start_revision="head")
    command.upgrade(config, "head")

    command.downgrade(config, "0001_targets_instances")

    engine = create_database_engine(database_url)
    inspector = inspect(engine)
    assert "runtime_definitions" not in inspector.get_table_names()
    assert "runtime_revision" in {
        column["name"] for column in inspector.get_columns("managed_instances")
    }
    engine.dispose()

    command.upgrade(config, "head")
    command.check(config)


def test_populated_artifact_mapping_refuses_downgrade(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0002_runtime_catalog"
    )
    command.upgrade(config, "0003_runtime_container_artifacts")
    engine = create_database_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO runtime_definitions (id, slug, kodi_version) "
                "VALUES ('00000000-0000-0000-0000-000000000001', 'kodi-21', '21')"
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO runtime_revisions (
                    id, runtime_definition_id, revision_number, source_kind,
                    source_reference, content_digest, manifest_reference,
                    manifest_digest, built_at, approval_state,
                    requires_trusted_build_confirmation
                ) VALUES (
                    '00000000-0000-0000-0000-000000000002',
                    '00000000-0000-0000-0000-000000000001', 1, 'test', 'test',
                    :digest, 'test', :digest, '2026-09-14T00:00:00Z',
                    'not_approved', 0
                )
                """
            ),
            {"digest": "sha256:" + ("a" * 64)},
        )
        connection.execute(
            text(
                "INSERT INTO runtime_container_artifacts "
                "(runtime_revision_id, image_id) VALUES "
                "('00000000-0000-0000-0000-000000000002', :image_id)"
            ),
            {"image_id": "sha256:" + ("b" * 64)},
        )
    engine.dispose()

    with pytest.raises(RuntimeError, match="populated runtime-container artifact"):
        command.downgrade(config, "0002_runtime_catalog")

    engine = create_database_engine(database_url)
    assert "runtime_container_artifacts" in inspect(engine).get_table_names()
    engine.dispose()


def test_0005_known_curated_state_upgrades_without_fabricated_validation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0005_web_auth_sessions"
    )
    command.upgrade(config, "0005_web_auth_sessions")
    engine = create_database_engine(database_url)
    timestamp = "2026-09-15T12:00:00.000000Z"
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO runtime_definitions (id, slug, kodi_version) VALUES "
                "('00000000-0000-4000-8000-000000000001', 'kodi-21-3', '21.3')"
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO runtime_revisions (
                    id, runtime_definition_id, revision_number, source_kind,
                    source_reference, content_digest, manifest_reference,
                    manifest_digest, built_at, approval_state, approved_at,
                    requires_trusted_build_confirmation
                ) VALUES (
                    '3a000000-0000-4000-8000-000000000004',
                        '00000000-0000-4000-8000-000000000001', 2,
                    'official-package', 'ubuntu', :digest, 'manifest', :digest,
                    :timestamp, 'approved', :timestamp, 0
                )
                """
            ),
            {"digest": "sha256:" + ("a" * 64), "timestamp": timestamp},
        )
        connection.execute(
            text(
                "INSERT INTO runtime_container_artifacts "
                "(runtime_revision_id, image_id) VALUES "
                "('3a000000-0000-4000-8000-000000000004', :digest)"
            ),
            {
                "digest": (
                    "sha256:70e0166fd79ea731c3f81056c2f0516f8fe5c9c8ec729b0c0f1c3e6bac66f254"
                )
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO runtime_execution_profiles (
                    runtime_revision_id, runtime_kind,
                    expected_application_name, expected_kodi_version,
                    expected_version_tag, jsonrpc_port, jsonrpc_path,
                    process_observation, readiness_probe,
                    readiness_timeout_seconds, poll_interval_seconds,
                    http_timeout_seconds
                ) VALUES (
                    '3a000000-0000-4000-8000-000000000004', 'kodi', 'Kodi',
                    '21.3', 'stable', 8080, '/jsonrpc', 'docker_healthcheck',
                    'kodi_jsonrpc', 90, 1.0, 3.0
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO targets (
                    id, name, target_type, kodi_version, availability, health,
                    connection_state
                ) VALUES (
                    '00000000-0000-4000-8000-000000000003', 'retained',
                    'managed', '21.3', 'unavailable', 'warning', 'disconnected'
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO managed_instances (
                    target_id, instance_type, runtime_revision_id,
                    runtime_state, start_on_host_boot
                ) VALUES (
                    '00000000-0000-4000-8000-000000000003', 'disposable',
                    '3a000000-0000-4000-8000-000000000004', 'stopped', 0
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO web_admin_credentials (
                    admin_key, password_hash, password_updated_at
                ) VALUES ('local-admin', 'argon2id-test-hash', :timestamp)
                """
            ),
            {"timestamp": timestamp},
        )
        connection.execute(
            text(
                """
                INSERT INTO web_sessions (
                    token_digest, admin_key, csrf_token, created_at,
                    last_used_at, absolute_expires_at
                ) VALUES (
                    :token_digest, 'local-admin', :csrf_token, :timestamp,
                    :timestamp, '2026-09-22T12:00:00.000000Z'
                )
                """
            ),
            {
                "token_digest": "c" * 64,
                "csrf_token": "d" * 43,
                "timestamp": timestamp,
            },
        )
    engine.dispose()

    command.upgrade(config, "0018_development_source_attach")
    engine = create_database_engine(database_url)
    with engine.connect() as connection:
        assert (
            connection.scalar(text("SELECT support_tier FROM runtime_definitions"))
            == "current"
        )
        assert (
            connection.scalar(
                text(
                    "SELECT expected_prerelease_ordinal FROM runtime_execution_profiles"
                )
            )
            is None
        )
        assert (
            connection.scalar(text("SELECT COUNT(*) FROM runtime_validation_evidence"))
            == 0
        )
        assert connection.scalar(text("SELECT COUNT(*) FROM managed_instances")) == 1
        assert (
            connection.scalar(text("SELECT COUNT(*) FROM runtime_container_artifacts"))
            == 1
        )
        assert connection.scalar(text("SELECT COUNT(*) FROM web_sessions")) == 1
        assert (
            connection.scalar(text("SELECT COUNT(*) FROM web_admin_credentials")) == 1
        )
    engine.dispose()
    command.downgrade(config, "0005_web_auth_sessions")
    engine = create_database_engine(database_url)
    assert "support_tier" not in {
        column["name"] for column in inspect(engine).get_columns("runtime_definitions")
    }
    assert "expected_prerelease_ordinal" not in {
        column["name"]
        for column in inspect(engine).get_columns("runtime_execution_profiles")
    }
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT COUNT(*) FROM managed_instances")) == 1
        assert connection.scalar(text("SELECT COUNT(*) FROM web_sessions")) == 1
        assert (
            connection.scalar(text("SELECT COUNT(*) FROM web_admin_credentials")) == 1
        )
    engine.dispose()
    command.upgrade(config, "head")
    engine = create_database_engine(database_url)
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT runtime_uid, runtime_gid FROM runtime_execution_profiles")
        ).one() == (1000, 1000)
    engine.dispose()
    command.check(config)


def test_phase5_history_and_support_policy_refuse_lossy_downgrade(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0005_web_auth_sessions"
    )
    command.upgrade(config, "0006_runtime_matrix_contract")
    engine = create_database_engine(database_url)
    timestamp = "2026-09-15T12:00:00.000000Z"
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO runtime_definitions "
                "(id, slug, kodi_version, support_tier) VALUES "
                "('00000000-0000-4000-8000-000000000001', "
                "'kodi-19-5', '19.5', 'legacy')"
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO runtime_revisions (
                    id, runtime_definition_id, revision_number, source_kind,
                    source_reference, content_digest, manifest_reference,
                    manifest_digest, built_at, approval_state,
                    requires_trusted_build_confirmation
                ) VALUES (
                    '00000000-0000-4000-8000-000000000002',
                    '00000000-0000-4000-8000-000000000001', 1, 'test', 'test',
                    :digest, 'test', :digest, :timestamp, 'not_approved', 0
                )
                """
            ),
            {"digest": "sha256:" + ("a" * 64), "timestamp": timestamp},
        )
        connection.execute(
            text(
                """
                INSERT INTO runtime_validation_evidence (
                    id, runtime_revision_id, contract_version, result,
                    started_at, completed_at, evidence_reference, recorded_at
                ) VALUES (
                    '00000000-0000-4000-8000-000000000003',
                    '00000000-0000-4000-8000-000000000002',
                    'kodi-runtime-v1', 'passed', :timestamp, :timestamp,
                    'validation://evidence', :timestamp
                )
                """
            ),
            {"timestamp": timestamp},
        )
    engine.dispose()

    with pytest.raises(RuntimeError, match="validation evidence"):
        command.downgrade(config, "0005_web_auth_sessions")

    engine = create_database_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM runtime_validation_evidence"))
    engine.dispose()
    with pytest.raises(RuntimeError, match="support policy"):
        command.downgrade(config, "0005_web_auth_sessions")


def test_0006_populated_state_upgrades_with_null_fixture_assignment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0006_runtime_matrix_contract"
    )
    command.upgrade(config, "0006_runtime_matrix_contract")
    engine = create_database_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO runtime_definitions "
                "(id, slug, kodi_version, support_tier) VALUES "
                "('10000000-0000-4000-8000-000000000001', "
                "'kodi-21-3-fixture-test', '21.3', 'current')"
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO runtime_revisions (
                    id, runtime_definition_id, revision_number, source_kind,
                    source_reference, content_digest, manifest_reference,
                    manifest_digest, built_at, approval_state,
                    requires_trusted_build_confirmation
                ) VALUES (
                    '10000000-0000-4000-8000-000000000002',
                    '10000000-0000-4000-8000-000000000001', 1, 'test', 'test',
                    :digest, 'manifest', :digest, '2026-09-15T00:00:00Z',
                    'not_approved', 0
                )
                """
            ),
            {"digest": "sha256:" + ("a" * 64)},
        )
        connection.execute(
            text(
                "INSERT INTO targets "
                "(id, name, target_type, kodi_version, availability, health, "
                "connection_state) VALUES "
                "('10000000-0000-4000-8000-000000000003', 'fixture migration', "
                "'managed', '21.3', 'unavailable', 'warning', 'disconnected')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO managed_instances "
                "(target_id, instance_type, runtime_revision_id, runtime_state, "
                "start_on_host_boot) VALUES "
                "('10000000-0000-4000-8000-000000000003', 'disposable', "
                "'10000000-0000-4000-8000-000000000002', 'stopped', 0)"
            )
        )
    engine.dispose()

    command.upgrade(config, "0007_fixture_definitions")
    engine = create_database_engine(database_url)
    with engine.connect() as connection:
        assert (
            connection.scalar(
                text("SELECT fixture_definition_id FROM managed_instances")
            )
            is None
        )
    engine.dispose()


def test_fixture_migration_empty_round_trip_and_populated_downgrade_refusal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0006_runtime_matrix_contract"
    )
    command.upgrade(config, "0006_runtime_matrix_contract")
    command.upgrade(config, "0007_fixture_definitions")
    command.downgrade(config, "0006_runtime_matrix_contract")
    command.upgrade(config, "0007_fixture_definitions")
    engine = create_database_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO fixture_definitions "
                "(id, slug, fixture_version, size_category, content_category, "
                "content_digest, compatible_kodi_version, description) VALUES "
                "('35d6854d-6cfc-5247-97df-7f2023c6ae9d', 'small-mixed-v1', "
                "1, 'small', 'mixed', :digest, '21.3', 'fixture')"
            ),
            {"digest": "sha256:" + ("a" * 64)},
        )
    engine.dispose()
    with pytest.raises(RuntimeError, match="populated fixture"):
        command.downgrade(config, "0006_runtime_matrix_contract")


def _seed_0014_instance_with_notes(connection: Connection, *, notes: str) -> None:
    execute = connection.execute
    timestamp = "2026-09-16T00:00:00.000000Z"
    digest = "sha256:" + "a" * 64
    execute(
        text(
            "INSERT INTO runtime_definitions "
            "(id, slug, kodi_version, support_tier) VALUES "
            "('89800000-0000-4000-8000-000000000001', 'instance-delete', "
            "'21.3', 'current')"
        )
    )
    execute(
        text(
            "INSERT INTO runtime_revisions (id, runtime_definition_id, "
            "revision_number, source_kind, source_reference, content_digest, "
            "manifest_reference, manifest_digest, built_at, approval_state, "
            "requires_trusted_build_confirmation) VALUES "
            "('89800000-0000-4000-8000-000000000002', "
            "'89800000-0000-4000-8000-000000000001', 1, 'test', 'test', "
            ":digest, 'test', :digest, :timestamp, 'not_approved', 0)"
        ),
        {"digest": digest, "timestamp": timestamp},
    )
    execute(
        text(
            "INSERT INTO targets (id, name, target_type, kodi_version, "
            "availability, health, connection_state) VALUES "
            "('89800000-0000-4000-8000-000000000003', 'delete source', "
            "'managed', '21.3', 'unavailable', 'warning', 'disconnected')"
        )
    )
    execute(
        text(
            "INSERT INTO managed_instances (target_id, instance_type, "
            "runtime_revision_id, runtime_state, start_on_host_boot, notes) VALUES "
            "('89800000-0000-4000-8000-000000000003', 'disposable', "
            "'89800000-0000-4000-8000-000000000002', 'stopped', 0, :notes)"
        ),
        {"notes": notes},
    )


@pytest.mark.parametrize("notes", ["short 🐍 note", "🐍" * 4096])
def test_0015_preserves_bounded_notes_exactly_and_backfills_snapshot_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, notes: str
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0014_snapshot_deletion"
    )
    command.upgrade(config, "0014_snapshot_deletion")
    engine = create_database_engine(database_url)
    timestamp = "2026-09-16T00:00:00.000000Z"
    digest = "sha256:" + "b" * 64
    with engine.begin() as connection:
        _seed_0014_instance_with_notes(connection, notes=notes)
        connection.execute(
            text(
                "INSERT INTO snapshots (id, source_managed_instance_id, "
                "runtime_revision_id, captured_at, prior_runtime_state, "
                "manifest_version, archive_digest, manifest_digest, archive_size, "
                "member_count, state, validation_at, validation_result) VALUES "
                "('89800000-0000-4000-8000-000000000004', "
                "'89800000-0000-4000-8000-000000000003', "
                "'89800000-0000-4000-8000-000000000002', :timestamp, 'stopped', "
                "1, :digest, :digest, 10, 1, 'available', :timestamp, 'passed')"
            ),
            {"digest": digest, "timestamp": timestamp},
        )
    engine.dispose()

    command.upgrade(config, "0015_instance_deletion")
    engine = create_database_engine(database_url)
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT notes FROM managed_instances")) == notes
        snapshot = connection.execute(
            text(
                "SELECT source_managed_instance_id, "
                "original_source_managed_instance_id, source_binding "
                "FROM snapshots"
            )
        ).one()
    assert snapshot == (
        "89800000-0000-4000-8000-000000000003",
        "89800000-0000-4000-8000-000000000003",
        "attached",
    )
    engine.dispose()


def test_0015_refuses_oversized_existing_notes_without_rewriting_database(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0014_snapshot_deletion"
    )
    command.upgrade(config, "0014_snapshot_deletion")
    oversized = "🐍" * 4097
    engine = create_database_engine(database_url)
    with engine.begin() as connection:
        _seed_0014_instance_with_notes(connection, notes=oversized)
    engine.dispose()

    with pytest.raises(RuntimeError, match="exceed 4096 Unicode code points"):
        command.upgrade(config, "0015_instance_deletion")

    engine = create_database_engine(database_url)
    with engine.connect() as connection:
        assert (
            connection.scalar(text("SELECT notes FROM managed_instances")) == oversized
        )
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0014_snapshot_deletion"
        )
        assert (
            "managed_instance_delete_operations"
            not in inspect(connection).get_table_names()
        )
    engine.dispose()


def test_0015_downgrade_refuses_detached_snapshot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0014_snapshot_deletion"
    )
    command.upgrade(config, "0014_snapshot_deletion")
    engine = create_database_engine(database_url)
    timestamp = "2026-09-16T00:00:00.000000Z"
    digest = "sha256:" + "b" * 64
    with engine.begin() as connection:
        _seed_0014_instance_with_notes(connection, notes="bounded")
        connection.execute(
            text(
                "INSERT INTO snapshots (id, source_managed_instance_id, "
                "runtime_revision_id, captured_at, prior_runtime_state, "
                "manifest_version, archive_digest, manifest_digest, archive_size, "
                "member_count, state, validation_at, validation_result) VALUES "
                "('89800000-0000-4000-8000-000000000004', "
                "'89800000-0000-4000-8000-000000000003', "
                "'89800000-0000-4000-8000-000000000002', :timestamp, 'stopped', 1, "
                ":digest, :digest, 10, 1, 'available', :timestamp, 'passed')"
            ),
            {"digest": digest, "timestamp": timestamp},
        )
    engine.dispose()
    command.upgrade(config, "0015_instance_deletion")
    engine = create_database_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE snapshots SET source_managed_instance_id=NULL, "
                "source_binding='deleted_source', source_instance_name='source', "
                "source_deleted_at=:timestamp"
            ),
            {"timestamp": timestamp},
        )
    engine.dispose()

    with pytest.raises(RuntimeError, match="detached snapshots"):
        command.downgrade(config, "0014_snapshot_deletion")


def test_0015_downgrade_refuses_populated_instance_delete_history(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0014_snapshot_deletion"
    )
    command.upgrade(config, "0014_snapshot_deletion")
    engine = create_database_engine(database_url)
    timestamp = "2026-09-16T00:00:00.000000Z"
    with engine.begin() as connection:
        _seed_0014_instance_with_notes(connection, notes="bounded")
    engine.dispose()
    command.upgrade(config, "0015_instance_deletion")
    engine = create_database_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO managed_instance_delete_operations "
                "(id, original_managed_instance_id, source_instance_name, "
                "runtime_revision_id, preserve_snapshots, snapshot_count, "
                "aggregate_snapshot_bytes, completed_snapshot_count, "
                "notes_at_deletion, "
                "requested_at, completed_at, state, phase, reconciliation_required) "
                "VALUES ('89800000-0000-4000-8000-000000000005', "
                "'89800000-0000-4000-8000-000000000003', 'delete source', "
                "'89800000-0000-4000-8000-000000000002', 1, 0, 0, 0, 'bounded', "
                ":timestamp, :timestamp, 'succeeded', 'succeeded', 0)"
            ),
            {"timestamp": timestamp},
        )
    engine.dispose()

    with pytest.raises(RuntimeError, match="populated instance deletion history"):
        command.downgrade(config, "0014_snapshot_deletion")


def test_0015_relaxed_terminal_history_retains_original_instance_uuid(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0014_snapshot_deletion"
    )
    command.upgrade(config, "0014_snapshot_deletion")
    engine = create_database_engine(database_url)
    timestamp = "2026-09-16T00:00:00.000000Z"
    with engine.begin() as connection:
        _seed_0014_instance_with_notes(connection, notes="bounded")
        connection.execute(
            text(
                "INSERT INTO reset_operations "
                "(id, managed_instance_id, requested_at, completed_at, state, phase, "
                "reconciliation_required) VALUES "
                "('89800000-0000-4000-8000-000000000006', "
                "'89800000-0000-4000-8000-000000000003', :timestamp, :timestamp, "
                "'succeeded', 'succeeded', 0)"
            ),
            {"timestamp": timestamp},
        )
    engine.dispose()
    command.upgrade(config, "0015_instance_deletion")
    engine = create_database_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "DELETE FROM managed_instances WHERE target_id="
                "'89800000-0000-4000-8000-000000000003'"
            )
        )
        connection.execute(
            text("DELETE FROM targets WHERE id='89800000-0000-4000-8000-000000000003'")
        )
        retained = connection.scalar(
            text("SELECT managed_instance_id FROM reset_operations")
        )
    assert retained == "89800000-0000-4000-8000-000000000003"
    engine.dispose()

    with pytest.raises(RuntimeError, match="operation history"):
        command.downgrade(config, "0014_snapshot_deletion")


def test_0016_lifecycle_history_schema_and_guarded_round_trip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0015_instance_deletion"
    )
    command.upgrade(config, "0015_instance_deletion")
    engine = create_database_engine(database_url)
    assert (
        "managed_instance_lifecycle_operations" not in inspect(engine).get_table_names()
    )
    engine.dispose()

    command.upgrade(config, "0016_lifecycle_history")
    engine = create_database_engine(database_url)
    inspector = inspect(engine)
    indexes = {
        index["name"]: index
        for index in inspector.get_indexes("managed_instance_lifecycle_operations")
    }
    assert indexes["uq_lifecycle_operation_running_instance"]["unique"] == 1
    assert indexes["ix_lifecycle_operation_original_requested"]["column_names"] == [
        "original_managed_instance_id",
        "requested_at",
    ]
    foreign_key = inspector.get_foreign_keys("managed_instance_lifecycle_operations")[0]
    assert foreign_key["referred_table"] == "managed_instances"
    assert foreign_key["options"] == {"ondelete": "SET NULL"}
    engine.dispose()
    command.downgrade(config, "0015_instance_deletion")
    command.upgrade(config, "0016_lifecycle_history")

    engine = create_database_engine(database_url)
    timestamp = "2026-09-16T00:00:00.000000Z"
    with engine.begin() as connection:
        _seed_0014_instance_with_notes(connection, notes="bounded")
        connection.execute(
            text(
                "INSERT INTO managed_instance_lifecycle_operations "
                "(id, original_managed_instance_id, managed_instance_id, "
                "instance_name, kind, requested_at, started_at, completed_at, "
                "prior_state, resulting_state, state, reconciliation_required) "
                "VALUES ('89900000-0000-4000-8000-000000000001', "
                "'89800000-0000-4000-8000-000000000003', "
                "'89800000-0000-4000-8000-000000000003', 'source', 'start', "
                ":timestamp, :timestamp, :timestamp, 'stopped', "
                "'running_not_ready', 'succeeded', 0)"
            ),
            {"timestamp": timestamp},
        )
    engine.dispose()
    with pytest.raises(RuntimeError, match="populated lifecycle operation history"):
        command.downgrade(config, "0015_instance_deletion")


def test_0017_development_sources_schema_constraints_and_guarded_round_trip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0016_lifecycle_history"
    )
    command.upgrade(config, "0016_lifecycle_history")
    engine = create_database_engine(database_url)
    assert "development_sources" not in inspect(engine).get_table_names()
    engine.dispose()

    command.upgrade(config, "0017_development_sources")
    engine = create_database_engine(database_url)
    inspector = inspect(engine)
    unique = {
        item["name"]: item["column_names"]
        for item in inspector.get_unique_constraints("development_sources")
    }
    assert unique == {
        "uq_development_source_addon_id": ["addon_id"],
        "uq_development_source_logical_name": ["logical_name"],
        "uq_development_source_relative_locator": ["relative_locator"],
    }
    engine.dispose()
    command.downgrade(config, "0016_lifecycle_history")
    command.upgrade(config, "0017_development_sources")

    engine = create_database_engine(database_url)
    insert = text(
        "INSERT INTO development_sources "
        "(id, logical_name, source_kind, relative_locator, addon_id, observed_name, "
        "observed_version, extension_types_json, registered_at, validated_at) VALUES "
        "(:id, :name, :kind, :locator, :addon_id, 'Example', '1.0.0', "
        "'[\"xbmc.python.pluginsource\"]', :timestamp, :timestamp)"
    )
    timestamp = "2026-09-16T20:00:00.000000Z"
    with engine.begin() as connection:
        connection.execute(
            insert,
            {
                "id": "94000000-0000-4000-8000-000000000001",
                "name": "Example",
                "kind": "local_directory",
                "locator": "sources/example",
                "addon_id": "plugin.video.example",
                "timestamp": timestamp,
            },
        )
    for ordinal, (field, value) in enumerate(
        (
            ("kind", "git"),
            ("locator", "/absolute/source"),
            ("addon_id", "Plugin.Video.Upper"),
        ),
        start=2,
    ):
        values = {
            "id": f"94000000-0000-4000-8000-{ordinal:012d}",
            "name": f"Invalid {field}",
            "kind": "local_directory",
            "locator": f"sources/invalid-{field}",
            "addon_id": f"plugin.video.invalid-{field}",
            "timestamp": timestamp,
        }
        values[field] = value
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(insert, values)
    engine.dispose()

    with pytest.raises(RuntimeError, match="populated development-source catalog"):
        command.downgrade(config, "0016_lifecycle_history")


def test_0018_source_attachments_schema_constraints_and_guarded_round_trip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0017_development_sources"
    )
    command.upgrade(config, "0017_development_sources")
    engine = create_database_engine(database_url)
    assert (
        "managed_instance_development_sources" not in inspect(engine).get_table_names()
    )
    engine.dispose()

    command.upgrade(config, "0018_development_source_attach")
    engine = create_database_engine(database_url)
    inspector = inspect(engine)
    association_fks = {
        item["referred_table"]: item["options"]
        for item in inspector.get_foreign_keys("managed_instance_development_sources")
    }
    assert association_fks == {
        "development_sources": {"ondelete": "RESTRICT"},
        "managed_instances": {"ondelete": "CASCADE"},
    }
    history_fks = {
        item["referred_table"]: item["options"]
        for item in inspector.get_foreign_keys(
            "development_source_attachment_operations"
        )
    }
    assert history_fks == {
        "development_sources": {"ondelete": "RESTRICT"},
        "managed_instances": {"ondelete": "SET NULL"},
    }
    unique = {
        item["name"]: item["column_names"]
        for item in inspector.get_unique_constraints(
            "managed_instance_development_sources"
        )
    }
    assert unique["uq_managed_instance_development_source"] == [
        "managed_instance_id",
        "development_source_id",
    ]
    mutation_kind = next(
        item
        for item in inspector.get_columns("managed_instance_mutation_leases")
        if item["name"] == "operation_kind"
    )
    assert isinstance(mutation_kind["type"], String)
    assert mutation_kind["type"].length == 32
    engine.dispose()
    command.downgrade(config, "0017_development_sources")
    command.upgrade(config, "0018_development_source_attach")

    engine = create_database_engine(database_url)
    timestamp = "2026-09-16T21:00:00.000000Z"
    with engine.begin() as connection:
        _seed_0014_instance_with_notes(connection, notes="attachment migration")
        connection.execute(
            text(
                "INSERT INTO development_sources "
                "(id, logical_name, source_kind, relative_locator, addon_id, "
                "observed_name, observed_version, extension_types_json, "
                "registered_at, validated_at) VALUES "
                "('98300000-0000-4000-8000-000000000001', 'Attached', "
                "'local_directory', 'sources/attached', 'plugin.video.attached', "
                "'Attached', '1.0.0', '[\"xbmc.python.pluginsource\"]', "
                ":timestamp, :timestamp)"
            ),
            {"timestamp": timestamp},
        )
        connection.execute(
            text(
                "INSERT INTO managed_instance_development_sources "
                "(managed_instance_id, development_source_id, attached_at) VALUES "
                "('89800000-0000-4000-8000-000000000003', "
                "'98300000-0000-4000-8000-000000000001', :timestamp)"
            ),
            {"timestamp": timestamp},
        )
    engine.dispose()
    with pytest.raises(RuntimeError, match="populated development-source attachment"):
        command.downgrade(config, "0017_development_sources")


CURATED_OWNERSHIP_ROWS = (
    (
        "51950000-0000-4000-8000-000000000002",
        1,
        "19.5",
        "stable",
        None,
        "sha256:73c221f1fc1fc14cbfdfd82e62fd4b87752ef71e0521227cb6790451fa51d2bd",
    ),
    (
        "52000000-0000-4000-8000-000000000002",
        1,
        "20.5",
        "stable",
        None,
        "sha256:be35d7cb8899dfe474c6a1eca62da99c4ada1280f4325933d7b6193e83537638",
    ),
    (
        "3a000000-0000-4000-8000-000000000002",
        1,
        "21.3",
        "stable",
        None,
        "sha256:1ac4521ef5f0f86eb497106c7fd3bdaff11ac989b34e803c3d105e5a24ff6247",
    ),
    (
        "3a000000-0000-4000-8000-000000000004",
        2,
        "21.3",
        "stable",
        None,
        "sha256:70e0166fd79ea731c3f81056c2f0516f8fe5c9c8ec729b0c0f1c3e6bac66f254",
    ),
    (
        "52200000-0000-4000-8000-000000000002",
        1,
        "22.0",
        "beta",
        2,
        "sha256:4fb611666a2637904d6edd0cf90aa8cfc12c8d89edaa7a7fa60c61ef26655918",
    ),
)


def _insert_0018_execution_profile(
    connection: Connection,
    *,
    revision_id: str,
    revision_number: int,
    version: str,
    channel: str,
    prerelease: int | None,
    image_id: str,
) -> None:
    definition_id = f"{revision_id[:-1]}1"
    canonical_release = (
        version if prerelease is None else f"{version}-{channel}.{prerelease}"
    )
    connection.execute(
        text(
            "INSERT OR IGNORE INTO runtime_definitions "
            "(id, slug, kodi_version, support_tier) "
            "VALUES (:id, :slug, :release, :tier)"
        ),
        {
            "id": definition_id,
            "slug": f"migration-{revision_id}",
            "release": canonical_release,
            "tier": "preview" if prerelease is not None else "current",
        },
    )
    connection.execute(
        text(
            "INSERT INTO runtime_revisions "
            "(id, runtime_definition_id, revision_number, source_kind, "
            "source_reference, content_digest, manifest_reference, "
            "manifest_digest, built_at, approval_state, "
            "requires_trusted_build_confirmation) VALUES "
            "(:id, :definition_id, :number, 'curated', 'curated', :digest, "
            "'manifest', :digest, :timestamp, 'not_approved', 0)"
        ),
        {
            "id": revision_id,
            "definition_id": definition_id,
            "number": revision_number,
            "digest": image_id,
            "timestamp": "2026-09-16T00:00:00.000000Z",
        },
    )
    connection.execute(
        text(
            "INSERT INTO runtime_container_artifacts "
            "(runtime_revision_id, image_id) VALUES (:id, :image_id)"
        ),
        {"id": revision_id, "image_id": image_id},
    )
    connection.execute(
        text(
            "INSERT INTO runtime_execution_profiles "
            "(runtime_revision_id, runtime_kind, expected_application_name, "
            "expected_kodi_version, expected_version_tag, "
            "expected_prerelease_ordinal, jsonrpc_port, jsonrpc_path, "
            "process_observation, readiness_probe, readiness_timeout_seconds, "
            "poll_interval_seconds, http_timeout_seconds) VALUES "
            "(:id, 'kodi', 'Kodi', :version, :channel, :prerelease, 8080, "
            "'/jsonrpc', 'docker_healthcheck', 'kodi_jsonrpc', 90, 1.0, 3.0)"
        ),
        {
            "id": revision_id,
            "version": version,
            "channel": channel,
            "prerelease": prerelease,
        },
    )


def test_0019_explicitly_backfills_all_curated_execution_profiles(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0018_development_source_attach"
    )
    command.upgrade(config, "0018_development_source_attach")
    engine = create_database_engine(database_url)
    with engine.begin() as connection:
        for row in CURATED_OWNERSHIP_ROWS:
            _insert_0018_execution_profile(
                connection,
                revision_id=row[0],
                revision_number=row[1],
                version=row[2],
                channel=row[3],
                prerelease=row[4],
                image_id=row[5],
            )
    engine.dispose()

    command.upgrade(config, "0019_runtime_execution_ownership")
    engine = create_database_engine(database_url)
    inspector = inspect(engine)
    ownership_columns = {
        item["name"]: item
        for item in inspector.get_columns("runtime_execution_profiles")
    }
    assert ownership_columns["runtime_uid"]["nullable"] is False
    assert ownership_columns["runtime_gid"]["nullable"] is False
    assert ownership_columns["runtime_uid"]["default"] is None
    assert ownership_columns["runtime_gid"]["default"] is None
    with engine.connect() as connection:
        rows = [
            tuple(row)
            for row in connection.execute(
                text(
                    "SELECT runtime_revision_id, runtime_uid, runtime_gid "
                    "FROM runtime_execution_profiles ORDER BY runtime_revision_id"
                )
            )
        ]
    assert rows == sorted((row[0], 1000, 1000) for row in CURATED_OWNERSHIP_ROWS)
    for field, value in (
        ("runtime_uid", -1),
        ("runtime_gid", -1),
        ("runtime_uid", 2**31),
        ("runtime_gid", 2**31),
    ):
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(
                text(
                    f"UPDATE runtime_execution_profiles SET {field} = :value "
                    "WHERE runtime_revision_id = :id"
                ),
                {"value": value, "id": CURATED_OWNERSHIP_ROWS[0][0]},
            )
    engine.dispose()

    with pytest.raises(RuntimeError, match="populated runtime execution ownership"):
        command.downgrade(config, "0018_development_source_attach")


def test_0019_unknown_profile_fails_without_default_backfill(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0018_development_source_attach"
    )
    command.upgrade(config, "0018_development_source_attach")
    engine = create_database_engine(database_url)
    with engine.begin() as connection:
        _insert_0018_execution_profile(
            connection,
            revision_id="99900000-0000-4000-8000-000000000002",
            revision_number=1,
            version="21.3",
            channel="stable",
            prerelease=None,
            image_id="sha256:" + "9" * 64,
        )
    engine.dispose()

    with pytest.raises(RuntimeError, match="unknown or mismatched execution profile"):
        command.upgrade(config, "0019_runtime_execution_ownership")
    engine = create_database_engine(database_url)
    assert {
        item["name"]
        for item in inspect(engine).get_columns("runtime_execution_profiles")
    }.isdisjoint({"runtime_uid", "runtime_gid"})
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0018_development_source_attach"
        )
    engine.dispose()


def test_0019_empty_downgrade_and_reupgrade(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0018_development_source_attach"
    )
    command.upgrade(config, "0019_runtime_execution_ownership")
    command.downgrade(config, "0018_development_source_attach")
    engine = create_database_engine(database_url)
    columns = {
        item["name"]
        for item in inspect(engine).get_columns("runtime_execution_profiles")
    }
    assert "runtime_uid" not in columns
    assert "runtime_gid" not in columns
    engine.dispose()
    command.upgrade(config, "0019_runtime_execution_ownership")


def test_0021_backfills_closed_python_bytecode_policy_without_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0018_development_source_attach"
    )
    command.upgrade(config, "0018_development_source_attach")
    engine = create_database_engine(database_url)
    with engine.begin() as connection:
        for row in CURATED_OWNERSHIP_ROWS:
            _insert_0018_execution_profile(
                connection,
                revision_id=row[0],
                revision_number=row[1],
                version=row[2],
                channel=row[3],
                prerelease=row[4],
                image_id=row[5],
            )
    engine.dispose()
    command.upgrade(config, "0019_runtime_execution_ownership")

    engine = create_database_engine(database_url)
    assert "python_bytecode_policy" not in {
        item["name"]
        for item in inspect(engine).get_columns("runtime_execution_profiles")
    }
    engine.dispose()
    command.upgrade(config, "0021_runtime_python_bytecode_policy")

    engine = create_database_engine(database_url)
    column = next(
        item
        for item in inspect(engine).get_columns("runtime_execution_profiles")
        if item["name"] == "python_bytecode_policy"
    )
    assert column["nullable"] is False
    assert column["default"] is None
    with engine.connect() as connection:
        policies = [
            tuple(row)
            for row in connection.execute(
                text(
                    "SELECT runtime_revision_id, python_bytecode_policy "
                    "FROM runtime_execution_profiles ORDER BY runtime_revision_id"
                )
            )
        ]
    assert policies == sorted(
        (row[0], "interpreter_default") for row in CURATED_OWNERSHIP_ROWS
    )
    for value in (None, "unknown", "SUPPRESS", "1"):
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE runtime_execution_profiles "
                    "SET python_bytecode_policy = :value "
                    "WHERE runtime_revision_id = :id"
                ),
                {"value": value, "id": CURATED_OWNERSHIP_ROWS[0][0]},
            )
    engine.dispose()

    with pytest.raises(RuntimeError, match="populated runtime Python bytecode"):
        command.downgrade(config, "0020_dev_source_copy_sync")


def test_0021_empty_downgrade_and_reupgrade(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0020_dev_source_copy_sync"
    )
    command.upgrade(config, "0021_runtime_python_bytecode_policy")
    command.downgrade(config, "0020_dev_source_copy_sync")
    engine = create_database_engine(database_url)
    assert "python_bytecode_policy" not in {
        item["name"]
        for item in inspect(engine).get_columns("runtime_execution_profiles")
    }
    engine.dispose()
    command.upgrade(config, "0021_runtime_python_bytecode_policy")


def test_0022_enablement_schema_empty_round_trip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0021_runtime_python_bytecode_policy"
    )
    command.upgrade(config, "0022_dev_source_enablement")
    engine = create_database_engine(database_url)
    inspector = inspect(engine)
    assert "development_source_enablement_operations" in inspector.get_table_names()
    with engine.connect() as connection:
        mutation_sql = connection.scalar(
            text(
                "SELECT sql FROM sqlite_master WHERE type = 'table' "
                "AND name = 'managed_instance_mutation_leases'"
            )
        )
    assert "development_source_enablement" in mutation_sql
    engine.dispose()
    command.downgrade(config, "0021_runtime_python_bytecode_policy")
    engine = create_database_engine(database_url)
    assert (
        "development_source_enablement_operations"
        not in inspect(engine).get_table_names()
    )
    engine.dispose()
    command.upgrade(config, "0022_dev_source_enablement")


def test_0022_populated_enablement_history_refuses_lossy_downgrade(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0018_development_source_attach"
    )
    command.upgrade(config, "0018_development_source_attach")
    engine = create_database_engine(database_url)
    revision_id = CURATED_OWNERSHIP_ROWS[0][0]
    with engine.begin() as connection:
        row = CURATED_OWNERSHIP_ROWS[0]
        _insert_0018_execution_profile(
            connection,
            revision_id=row[0],
            revision_number=row[1],
            version=row[2],
            channel=row[3],
            prerelease=row[4],
            image_id=row[5],
        )
    engine.dispose()
    command.upgrade(config, "0022_dev_source_enablement")
    engine = create_database_engine(database_url)
    timestamp = "2026-09-17T16:00:00.000000Z"
    instance_id = "aa220000-0000-4000-8000-000000000001"
    source_id = "aa220000-0000-4000-8000-000000000002"
    operation_id = "aa220000-0000-4000-8000-000000000003"
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO targets "
                "(id, name, target_type, kodi_version, availability, health, "
                "connection_state) VALUES "
                "(:id, 'Instance', 'managed', '21.3', 'available', 'healthy', "
                "'connected')"
            ),
            {"id": instance_id},
        )
        connection.execute(
            text(
                "INSERT INTO managed_instances "
                "(target_id, instance_type, runtime_revision_id, runtime_state, "
                "start_on_host_boot) VALUES "
                "(:id, 'disposable', :revision, 'running', 0)"
            ),
            {"id": instance_id, "revision": revision_id},
        )
        connection.execute(
            text(
                "INSERT INTO development_sources "
                "(id, logical_name, source_kind, relative_locator, addon_id, "
                "observed_name, observed_version, extension_types_json, "
                "registered_at, validated_at) VALUES "
                "(:id, 'Source', 'local_directory', 'source', "
                "'plugin.video.example', 'Example', '1.0.0', "
                "'[\"xbmc.python.pluginsource\"]', :at, :at)"
            ),
            {"id": source_id, "at": timestamp},
        )
        connection.execute(
            text(
                "INSERT INTO development_source_enablement_operations "
                "(id, original_managed_instance_id, managed_instance_id, "
                "development_source_id, source_logical_name, addon_id, "
                "addon_version, runtime_revision_id, requested_at, started_at, "
                "state, phase, result_classification, prior_enabled, "
                "result_enabled, enable_performed, completed_at, "
                "reconciliation_required) VALUES "
                "(:operation, :instance, :instance, :source, 'Source', "
                "'plugin.video.example', '1.0.0', :revision, :at, :at, "
                "'succeeded', 'finalize', 'enabled', 0, 1, 1, :at, 0)"
            ),
            {
                "operation": operation_id,
                "instance": instance_id,
                "source": source_id,
                "revision": revision_id,
                "at": timestamp,
            },
        )
    engine.dispose()
    with pytest.raises(RuntimeError, match="populated DevelopmentSource enablement"):
        command.downgrade(config, "0021_runtime_python_bytecode_policy")
    engine = create_database_engine(database_url)
    with engine.connect() as connection:
        assert (
            connection.scalar(
                text("SELECT COUNT(*) FROM development_source_enablement_operations")
            )
            == 1
        )
    engine.dispose()


def test_0020_deployment_schema_empty_lifecycle_and_metadata_check(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0019_runtime_execution_ownership"
    )
    command.upgrade(config, "0019_runtime_execution_ownership")
    engine = create_database_engine(database_url)
    assert "development_source_deployments" not in inspect(engine).get_table_names()
    engine.dispose()

    command.upgrade(config, "0020_dev_source_copy_sync")
    engine = create_database_engine(database_url)
    inspector = inspect(engine)
    assert {
        "development_source_deployments",
        "development_source_deployment_operations",
    } <= set(inspector.get_table_names())
    evidence_foreign_keys = {
        item["constrained_columns"][0]: item
        for item in inspector.get_foreign_keys("development_source_deployments")
    }
    assert (
        evidence_foreign_keys["last_successful_operation_id"]["referred_table"]
        == "development_source_deployment_operations"
    )
    assert evidence_foreign_keys["last_successful_operation_id"]["options"] == {
        "ondelete": "RESTRICT"
    }
    with engine.connect() as connection:
        mutation_sql = connection.scalar(
            text(
                "SELECT sql FROM sqlite_master "
                "WHERE type = 'table' AND name = 'managed_instance_mutation_leases'"
            )
        )
        heavy_sql = connection.scalar(
            text(
                "SELECT sql FROM sqlite_master "
                "WHERE type = 'table' AND name = 'global_heavy_operation_lease'"
            )
        )
    assert "development_source_deployment" in mutation_sql
    assert "development_source_deployment" in heavy_sql
    engine.dispose()

    command.downgrade(config, "0019_runtime_execution_ownership")
    engine = create_database_engine(database_url)
    assert {
        "development_source_deployments",
        "development_source_deployment_operations",
    }.isdisjoint(inspect(engine).get_table_names())
    engine.dispose()
    command.upgrade(config, "0020_dev_source_copy_sync")


def test_0020_populated_downgrade_refuses_without_data_loss(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0019_runtime_execution_ownership"
    )
    command.upgrade(config, "0020_dev_source_copy_sync")
    engine = create_database_engine(database_url)
    source_id = "aa200000-0000-4000-8000-000000000001"
    operation_id = "aa200000-0000-4000-8000-000000000002"
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO development_sources "
                "(id, logical_name, source_kind, relative_locator, addon_id, "
                "observed_name, observed_version, extension_types_json, "
                "registered_at, validated_at) VALUES "
                "(:id, 'Source', 'local_directory', 'source', "
                "'plugin.video.example', 'Example', '1.0.0', "
                "'[\"xbmc.python.pluginsource\"]', :at, :at)"
            ),
            {"id": source_id, "at": "2026-09-16T22:00:00.000000Z"},
        )
        connection.execute(
            text(
                "INSERT INTO development_source_deployment_operations "
                "(id, original_managed_instance_id, managed_instance_id, "
                "development_source_id, source_logical_name, addon_id, "
                "runtime_revision_id, request_kind, requested_at, state, phase, "
                "failure_reason, reconciliation_required) VALUES "
                "(:id, :instance, NULL, :source, 'Source', "
                "'plugin.video.example', :revision, 'copy_sync', :at, "
                "'failed', 'finalize', 'typed failure', 0)"
            ),
            {
                "id": operation_id,
                "instance": "aa200000-0000-4000-8000-000000000003",
                "source": source_id,
                "revision": "aa200000-0000-4000-8000-000000000004",
                "at": "2026-09-16T22:00:00.000000Z",
            },
        )
    engine.dispose()

    with pytest.raises(RuntimeError, match="populated development-source"):
        command.downgrade(config, "0019_runtime_execution_ownership")
    engine = create_database_engine(database_url)
    with engine.connect() as connection:
        assert (
            connection.scalar(
                text("SELECT COUNT(*) FROM development_source_deployment_operations")
            )
            == 1
        )
    engine.dispose()


def test_0023_smoke_schema_empty_round_trip_and_mutation_kind(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0022_dev_source_enablement"
    )
    command.upgrade(config, "0023_dev_source_smoke_test")
    engine = create_database_engine(database_url)
    inspector = inspect(engine)
    table = "development_source_smoke_test_operations"
    assert table in inspector.get_table_names()
    columns = {item["name"] for item in inspector.get_columns(table)}
    assert {
        "original_managed_instance_id",
        "managed_instance_id",
        "state",
        "phase",
        "overall_result",
        "execution_result",
        "post_execution_provenance",
        "returned_item_count",
    } <= columns
    foreign_keys = {
        item["constrained_columns"][0]: item
        for item in inspector.get_foreign_keys(table)
    }
    assert foreign_keys["managed_instance_id"]["options"] == {"ondelete": "SET NULL"}
    assert "original_managed_instance_id" not in foreign_keys
    with engine.connect() as connection:
        mutation_sql = connection.scalar(
            text(
                "SELECT sql FROM sqlite_master WHERE type = 'table' "
                "AND name = 'managed_instance_mutation_leases'"
            )
        )
    assert "development_source_smoke_test" in mutation_sql
    engine.dispose()
    command.downgrade(config, "0022_dev_source_enablement")
    engine = create_database_engine(database_url)
    assert table not in inspect(engine).get_table_names()
    engine.dispose()
    command.upgrade(config, "0023_dev_source_smoke_test")


def test_0023_populated_smoke_history_refuses_lossy_downgrade(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0022_dev_source_enablement"
    )
    command.upgrade(config, "0023_dev_source_smoke_test")
    engine = create_database_engine(database_url)
    source_id = "aa230000-0000-4000-8000-000000000001"
    operation_id = "aa230000-0000-4000-8000-000000000002"
    timestamp = "2026-09-17T18:00:00.000000Z"
    revision_id = CURATED_OWNERSHIP_ROWS[0][0]
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO runtime_definitions "
                "(id, slug, kodi_version, support_tier) VALUES "
                "('aa230000-0000-4000-8000-000000000004', "
                "'smoke-migration', '21.3', 'current')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO runtime_revisions "
                "(id, runtime_definition_id, revision_number, source_kind, "
                "source_reference, content_digest, manifest_reference, "
                "manifest_digest, built_at, approval_state, "
                "requires_trusted_build_confirmation) VALUES "
                "(:revision, 'aa230000-0000-4000-8000-000000000004', 1, "
                "'test', 'test', :digest, 'test', :digest, :at, "
                "'not_approved', 0)"
            ),
            {"revision": revision_id, "digest": "sha256:" + "a" * 64, "at": timestamp},
        )
        connection.execute(
            text(
                "INSERT INTO development_sources "
                "(id, logical_name, source_kind, relative_locator, addon_id, "
                "observed_name, observed_version, extension_types_json, "
                "registered_at, validated_at) VALUES "
                "(:id, 'Source', 'local_directory', 'source', "
                "'plugin.video.example', 'Example', '1.0.0', "
                "'[\"xbmc.python.pluginsource\"]', :at, :at)"
            ),
            {"id": source_id, "at": timestamp},
        )
        connection.execute(
            text(
                "INSERT INTO development_source_smoke_test_operations "
                "(id, original_managed_instance_id, managed_instance_id, "
                "development_source_id, source_logical_name, addon_id, "
                "addon_version, runtime_revision_id, requested_at, state, phase, "
                "overall_result, execution_result, post_execution_provenance, "
                "invocation_dispatched, reconciliation_required, completed_at) "
                "VALUES (:operation, :instance, NULL, :source, 'Source', "
                "'plugin.video.example', '1.0.0', :revision, :at, 'succeeded', "
                "'finalize', 'passed', 'directory_result', 'matched', 1, 0, :at)"
            ),
            {
                "operation": operation_id,
                "instance": "aa230000-0000-4000-8000-000000000003",
                "source": source_id,
                "revision": revision_id,
                "at": timestamp,
            },
        )
    engine.dispose()
    with pytest.raises(RuntimeError, match="populated DevelopmentSource smoke-test"):
        command.downgrade(config, "0022_dev_source_enablement")
    engine = create_database_engine(database_url)
    with engine.connect() as connection:
        assert (
            connection.scalar(
                text("SELECT COUNT(*) FROM development_source_smoke_test_operations")
            )
            == 1
        )
    engine.dispose()


def test_0024_uploaded_zip_schema_empty_round_trip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0024_addon_zip_installation"
    )
    command.upgrade(config, "0024_addon_zip_installation")
    engine = create_database_engine(database_url)
    with engine.begin() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0024_addon_zip_installation"
        )
    engine.dispose()

    command.downgrade(config, "0023_dev_source_smoke_test")
    command.upgrade(config, "0024_addon_zip_installation")


def test_0024_populated_uploaded_zip_state_refuses_lossy_downgrade(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0023_dev_source_smoke_test"
    )
    command.upgrade(config, "0024_addon_zip_installation")
    artifact_id = str(uuid4())
    engine = create_database_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO addon_zip_artifacts "
                "(id, logical_label, sha256_digest, compressed_size, "
                "total_uncompressed_bytes, member_count, addon_id, addon_name, "
                "addon_version, extension_classification, dependencies_json, "
                "payload_manifest_digest, imported_at, origin, validation_state) "
                "VALUES (:id, 'ZIP fixture', :digest, 100, 200, 2, "
                "'plugin.video.test', 'Test', '1.0.0', 'python_video_plugin', "
                "'[]', :manifest, :imported, 'uploaded_zip', 'accepted')"
            ),
            {
                "id": artifact_id,
                "digest": "sha256:" + "1" * 64,
                "manifest": "sha256:" + "2" * 64,
                "imported": "2026-09-17T12:00:00+00:00",
            },
        )
    engine.dispose()

    with pytest.raises(RuntimeError, match="populated uploaded-ZIP"):
        command.downgrade(config, "0023_dev_source_smoke_test")

    engine = create_database_engine(database_url)
    with engine.begin() as connection:
        assert (
            connection.scalar(text("SELECT id FROM addon_zip_artifacts")) == artifact_id
        )
    engine.dispose()


def test_0025_zip_enablement_schema_empty_round_trip_and_mutation_kind(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0025_addon_zip_enablement"
    )
    command.upgrade(config, "0025_addon_zip_enablement")
    engine = create_database_engine(database_url)
    inspector = inspect(engine)
    table = "addon_zip_enablement_operations"
    assert table in inspector.get_table_names()
    columns = {item["name"] for item in inspector.get_columns(table)}
    assert {
        "artifact_id",
        "installation_operation_id",
        "original_managed_instance_id",
        "managed_instance_id",
        "expected_container_id",
        "payload_verification",
        "enable_performed",
        "blocked_dependency_reason",
    } <= columns
    foreign_keys = {
        item["constrained_columns"][0]: item
        for item in inspector.get_foreign_keys(table)
    }
    assert foreign_keys["managed_instance_id"]["options"] == {"ondelete": "SET NULL"}
    assert "original_managed_instance_id" not in foreign_keys
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0025_addon_zip_enablement"
        )
        mutation_sql = connection.scalar(
            text(
                "SELECT sql FROM sqlite_master WHERE type='table' "
                "AND name='managed_instance_mutation_leases'"
            )
        )
    assert "addon_zip_enablement" in mutation_sql
    engine.dispose()
    command.downgrade(config, "0024_addon_zip_installation")
    engine = create_database_engine(database_url)
    assert table not in inspect(engine).get_table_names()
    engine.dispose()
    command.upgrade(config, "0025_addon_zip_enablement")


def test_0026_zip_smoke_schema_empty_round_trip_and_mutation_kind(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0025_addon_zip_enablement"
    )
    command.upgrade(config, "0026_addon_zip_smoke_test")
    engine = create_database_engine(database_url)
    inspector = inspect(engine)
    table = "addon_zip_smoke_test_operations"
    columns = {item["name"] for item in inspector.get_columns(table)}
    assert {
        "artifact_id",
        "installation_operation_id",
        "original_managed_instance_id",
        "managed_instance_id",
        "expected_container_id",
        "overall_result",
        "execution_result",
        "post_execution_provenance",
        "returned_item_count",
    } <= columns
    foreign_keys = {
        item["constrained_columns"][0]: item
        for item in inspector.get_foreign_keys(table)
    }
    assert foreign_keys["managed_instance_id"]["options"] == {"ondelete": "SET NULL"}
    assert foreign_keys["artifact_id"]["referred_table"] == "addon_zip_artifacts"
    assert (
        foreign_keys["installation_operation_id"]["referred_table"]
        == "addon_zip_installation_operations"
    )
    with engine.connect() as connection:
        mutation_sql = connection.scalar(
            text(
                "SELECT sql FROM sqlite_master WHERE type='table' "
                "AND name='managed_instance_mutation_leases'"
            )
        )
    assert "addon_zip_smoke_test" in mutation_sql
    engine.dispose()
    command.downgrade(config, "0025_addon_zip_enablement")
    engine = create_database_engine(database_url)
    assert table not in inspect(engine).get_table_names()
    engine.dispose()
    command.upgrade(config, "0026_addon_zip_smoke_test")


def test_0027_repository_schema_empty_round_trip_and_populated_guard(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0026_addon_zip_smoke_test"
    )
    command.upgrade(config, "0027_addon_repository_testing")
    engine = create_database_engine(database_url)
    inspector = inspect(engine)
    assert {
        "addon_repository_sources",
        "managed_instance_addon_repository_sources",
        "addon_repository_attachment_operations",
        "addon_repository_metadata_snapshots",
        "addon_repository_candidates",
        "addon_repository_refresh_operations",
        "addon_repository_fetch_operations",
    } <= set(inspector.get_table_names())
    assert {
        "repository_source_id",
        "repository_snapshot_id",
        "repository_candidate_id",
        "repository_fetched_at",
    } <= {item["name"] for item in inspector.get_columns("addon_zip_artifacts")}
    assert "artifact_origin" in {
        item["name"]
        for item in inspector.get_columns("addon_zip_installation_evidence")
    }
    with engine.connect() as connection:
        mutation_sql = connection.scalar(
            text(
                "SELECT sql FROM sqlite_master WHERE type='table' "
                "AND name='managed_instance_mutation_leases'"
            )
        )
        global_sql = connection.scalar(
            text(
                "SELECT sql FROM sqlite_master WHERE type='table' "
                "AND name='global_heavy_operation_lease'"
            )
        )
    assert "addon_repository_installation" in mutation_sql
    assert "addon_repository_refresh" in global_sql
    assert "addon_repository_fetch" in global_sql
    engine.dispose()

    command.downgrade(config, "0026_addon_zip_smoke_test")
    engine = create_database_engine(database_url)
    inspector = inspect(engine)
    assert "addon_repository_sources" not in inspector.get_table_names()
    assert "repository_source_id" not in {
        item["name"] for item in inspector.get_columns("addon_zip_artifacts")
    }
    engine.dispose()
    command.upgrade(config, "0027_addon_repository_testing")

    engine = create_database_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO addon_repository_sources "
                "(id, logical_name, canonical_index_url, origin, created_at) VALUES "
                "('a8270000-0000-4000-8000-000000000001', 'guarded', "
                "'https://example.com/addons.xml', 'external_https_repository', "
                "'2026-09-18T12:00:00+00:00')"
            )
        )
    engine.dispose()
    with pytest.raises(RuntimeError, match="populated repository"):
        command.downgrade(config, "0026_addon_zip_smoke_test")


def test_0029_agent_workspace_schema_round_trip_constraints_and_guard(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0029_agent_workspaces"
    )
    command.upgrade(config, "0029_agent_workspaces")
    engine = create_database_engine(database_url)
    inspector = inspect(engine)
    assert {"agent_workspaces", "agent_workspace_creation_operations"} <= set(
        inspector.get_table_names()
    )
    workspace_foreign_keys = {
        item["referred_table"]: item
        for item in inspector.get_foreign_keys("agent_workspaces")
    }
    assert workspace_foreign_keys["development_sources"]["options"] == {
        "ondelete": "RESTRICT"
    }
    with engine.connect() as connection:
        global_sql = connection.scalar(
            text(
                "SELECT sql FROM sqlite_master WHERE type='table' "
                "AND name='global_heavy_operation_lease'"
            )
        )
    assert "agent_workspace_creation" in global_sql
    engine.dispose()

    command.downgrade(config, "0028_repository_replacement_override")
    engine = create_database_engine(database_url)
    assert "agent_workspaces" not in inspect(engine).get_table_names()
    engine.dispose()
    command.upgrade(config, "0029_agent_workspaces")

    engine = create_database_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO development_sources "
                "(id,logical_name,source_kind,relative_locator,addon_id,observed_name,"
                "observed_version,extension_types_json,registered_at,validated_at) "
                "VALUES "
                "(:id,'Source','local_directory','source','plugin.video.source','Source',"
                "'1.0','[\"xbmc.python.pluginsource\"]',:time,:time)"
            ),
            {
                "id": "a9000000-0000-4000-8000-000000000001",
                "time": "2026-09-18T12:00:00+00:00",
            },
        )
        connection.execute(
            text(
                "INSERT INTO agent_workspaces "
                "(id,development_source_id,source_addon_id,source_logical_name,"
                "source_observed_name,source_observed_version,state,baseline_artifact_id,"
                "generation,capture_started_at,created_at,updated_at) VALUES "
                "(:id,:source,'plugin.video.source','Source','Source','1.0','creating',"
                ":baseline,0,:time,:time,:time)"
            ),
            {
                "id": "a9000000-0000-4000-8000-000000000002",
                "source": "a9000000-0000-4000-8000-000000000001",
                "baseline": "a9000000-0000-4000-8000-000000000003",
                "time": "2026-09-18T12:00:00+00:00",
            },
        )
    engine.dispose()
    with pytest.raises(RuntimeError, match="populated agent workspace"):
        command.downgrade(config, "0028_repository_replacement_override")


def test_0030_workspace_mutation_activity_round_trip_and_guard(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0029_agent_workspaces"
    )
    command.upgrade(config, "0029_agent_workspaces")
    engine = create_database_engine(database_url)
    assert "agent_workspace_mutation_events" not in inspect(engine).get_table_names()
    engine.dispose()

    command.upgrade(config, "0030_agent_workspace_mutations")
    engine = create_database_engine(database_url)
    inspector = inspect(engine)
    assert "agent_workspace_mutation_events" in inspector.get_table_names()
    assert inspector.get_foreign_keys("agent_workspace_mutation_events")[0][
        "options"
    ] == {"ondelete": "RESTRICT"}
    engine.dispose()
    command.downgrade(config, "0029_agent_workspaces")
    command.upgrade(config, "0030_agent_workspace_mutations")

    engine = create_database_engine(database_url)
    with engine.begin() as connection:
        timestamp = "2026-09-18T12:00:00+00:00"
        source_id = "a9300000-0000-4000-8000-000000000001"
        workspace_id = "a9300000-0000-4000-8000-000000000002"
        connection.execute(
            text(
                "INSERT INTO development_sources "
                "(id,logical_name,source_kind,relative_locator,addon_id,observed_name,"
                "observed_version,extension_types_json,registered_at,validated_at) "
                "VALUES (:id,'Source','local_directory','source','plugin.video.source',"
                "'Source','1.0','[\"xbmc.python.pluginsource\"]',:time,:time)"
            ),
            {"id": source_id, "time": timestamp},
        )
        connection.execute(
            text(
                "INSERT INTO agent_workspaces "
                "(id,development_source_id,source_addon_id,source_logical_name,"
                "source_observed_name,source_observed_version,state,baseline_artifact_id,"
                "generation,capture_started_at,created_at,updated_at) VALUES "
                "(:id,:source,'plugin.video.source','Source','Source','1.0','creating',"
                ":baseline,1,:time,:time,:time)"
            ),
            {
                "id": workspace_id,
                "source": source_id,
                "baseline": "a9300000-0000-4000-8000-000000000003",
                "time": timestamp,
            },
        )
        connection.execute(
            text(
                "INSERT INTO agent_workspace_mutation_events "
                "(id,workspace_id,generation,kind,relative_path,occurred_at) VALUES "
                "(:id,:workspace,1,'create','new.txt',:time)"
            ),
            {
                "id": "a9300000-0000-4000-8000-000000000004",
                "workspace": workspace_id,
                "time": timestamp,
            },
        )
    engine.dispose()
    with pytest.raises(RuntimeError, match="workspace mutation history"):
        command.downgrade(config, "0029_agent_workspaces")


def test_0031_workspace_promotion_schema_round_trip_constraints_and_guard(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0030_agent_workspace_mutations"
    )
    command.upgrade(config, "0030_agent_workspace_mutations")
    engine = create_database_engine(database_url)
    assert "agent_workspace_promotion_plans" not in inspect(engine).get_table_names()
    engine.dispose()

    command.upgrade(config, "0031_agent_workspace_promotions")
    engine = create_database_engine(database_url)
    inspector = inspect(engine)
    expected = {
        "agent_workspace_promotion_plans",
        "agent_workspace_promotion_plan_paths",
        "agent_workspace_promotion_operations",
        "agent_workspace_promotion_journal",
        "development_source_promotion_leases",
        "agent_workspace_promotion_handoffs",
    }
    assert expected <= set(inspector.get_table_names())
    plan_checks = " ".join(
        str(item["sqltext"])
        for item in inspector.get_check_constraints("agent_workspace_promotion_plans")
    )
    assert "changed_file_count BETWEEN 1 AND 100" in plan_checks
    assert "review_text_bytes BETWEEN 0 AND 4194304" in plan_checks
    workspace_checks = " ".join(
        str(item["sqltext"])
        for item in inspector.get_check_constraints("agent_workspaces")
    )
    assert "promoted" in workspace_checks
    mutation_checks = " ".join(
        str(item["sqltext"])
        for item in inspector.get_check_constraints("managed_instance_mutation_leases")
    )
    heavy_checks = " ".join(
        str(item["sqltext"])
        for item in inspector.get_check_constraints("global_heavy_operation_lease")
    )
    assert "agent_workspace_promotion" in mutation_checks
    assert "agent_workspace_promotion" in heavy_checks
    assert inspector.get_foreign_keys("agent_workspace_promotion_handoffs")
    engine.dispose()

    command.downgrade(config, "0030_agent_workspace_mutations")
    engine = create_database_engine(database_url)
    assert expected.isdisjoint(inspect(engine).get_table_names())
    engine.dispose()
    command.upgrade(config, "0031_agent_workspace_promotions")

    engine = create_database_engine(database_url)
    with engine.begin() as connection:
        timestamp = "2026-09-18T12:00:00+00:00"
        source_id = "a9310000-0000-4000-8000-000000000001"
        connection.execute(
            text(
                "INSERT INTO development_sources "
                "(id,logical_name,source_kind,relative_locator,addon_id,observed_name,"
                "observed_version,extension_types_json,registered_at,validated_at) "
                "VALUES (:id,'Source','local_directory','source','plugin.video.source',"
                "'Source','1.0','[\"xbmc.python.pluginsource\"]',:time,:time)"
            ),
            {"id": source_id, "time": timestamp},
        )
        connection.execute(
            text(
                "INSERT INTO agent_workspaces "
                "(id,development_source_id,source_addon_id,source_logical_name,"
                "source_observed_name,source_observed_version,state,baseline_artifact_id,"
                "generation,capture_started_at,created_at,updated_at) VALUES "
                "(:id,:source,'plugin.video.source','Source','Source','1.0','promoted',"
                ":baseline,1,:time,:time,:time)"
            ),
            {
                "id": "a9310000-0000-4000-8000-000000000002",
                "source": source_id,
                "baseline": "a9310000-0000-4000-8000-000000000003",
                "time": timestamp,
            },
        )
    engine.dispose()
    with pytest.raises(RuntimeError, match="populated agent workspace promotion"):
        command.downgrade(config, "0030_agent_workspace_mutations")


def test_0032_promotion_verification_schema_round_trip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0031_agent_workspace_promotions"
    )
    command.upgrade(config, "0031_agent_workspace_promotions")
    assert (
        "promotion_verification_operations"
        not in inspect(create_database_engine(database_url)).get_table_names()
    )

    command.upgrade(config, "0032_promotion_verification")
    engine = create_database_engine(database_url)
    inspector = inspect(engine)
    assert "promotion_verification_operations" in inspector.get_table_names()
    assert "promotion_verification_log_evidence" in inspector.get_table_names()
    mutation_checks = " ".join(
        str(item["sqltext"])
        for item in inspector.get_check_constraints("managed_instance_mutation_leases")
    )
    heavy_checks = " ".join(
        str(item["sqltext"])
        for item in inspector.get_check_constraints("global_heavy_operation_lease")
    )
    assert "promotion_verification" in mutation_checks
    assert "promotion_verification" in heavy_checks
    assert inspector.get_foreign_keys("promotion_verification_operations")

    command.downgrade(config, "0031_agent_workspace_promotions")
    engine = create_database_engine(database_url)
    assert "promotion_verification_operations" not in inspect(engine).get_table_names()
    assert (
        "promotion_verification_log_evidence" not in inspect(engine).get_table_names()
    )
    engine.dispose()
    command.upgrade(config, "0032_promotion_verification")


def test_0033_workspace_cleanup_schema_round_trip_and_populated_guard(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0032_promotion_verification"
    )
    command.upgrade(config, "0032_promotion_verification")
    engine = create_database_engine(database_url)
    assert "agent_workspace_cleanup_operations" not in inspect(engine).get_table_names()
    engine.dispose()

    command.upgrade(config, "0033_agent_workspace_cleanup")
    engine = create_database_engine(database_url)
    inspector = inspect(engine)
    assert {
        "agent_workspace_cleanup_operations",
        "agent_workspace_lifecycle_events",
    } <= set(inspector.get_table_names())
    assert {item["name"] for item in inspector.get_columns("agent_workspaces")} >= {
        "bytes_state",
        "promoted_at",
        "abandoned_at",
        "cleaned_at",
    }
    with engine.connect() as connection:
        heavy_sql = connection.scalar(
            text(
                "SELECT sql FROM sqlite_master WHERE type='table' "
                "AND name='global_heavy_operation_lease'"
            )
        )
    assert "agent_workspace_cleanup" in heavy_sql
    engine.dispose()
    command.downgrade(config, "0032_promotion_verification")
    command.upgrade(config, "0033_agent_workspace_cleanup")

    engine = create_database_engine(database_url)
    now = "2026-09-18T12:00:00+00:00"
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO development_sources "
                "(id,logical_name,source_kind,relative_locator,addon_id,observed_name,"
                "observed_version,extension_types_json,registered_at,validated_at) "
                "VALUES (:id,'Source','local_directory','source','plugin.video.source',"
                "'Source','1.0','[\"xbmc.python.pluginsource\"]',:time,:time)"
            ),
            {"id": "a9330000-0000-4000-8000-000000000001", "time": now},
        )
        connection.execute(
            text(
                "INSERT INTO agent_workspaces "
                "(id,development_source_id,source_addon_id,source_logical_name,"
                "source_observed_name,source_observed_version,state,bytes_state,"
                "baseline_artifact_id,generation,capture_started_at,created_at,"
                "updated_at,abandoned_at) VALUES (:id,:source,'plugin.video.source',"
                "'Source','Source','1.0','abandoned','present',:baseline,0,:time,:time,"
                ":time,:time)"
            ),
            {
                "id": "a9330000-0000-4000-8000-000000000002",
                "source": "a9330000-0000-4000-8000-000000000001",
                "baseline": "a9330000-0000-4000-8000-000000000003",
                "time": now,
            },
        )
        connection.execute(
            text(
                "INSERT INTO agent_workspace_lifecycle_events "
                "(id,workspace_id,kind,occurred_at) VALUES "
                "(:id,:workspace,'abandoned',:time)"
            ),
            {
                "id": "a9330000-0000-4000-8000-000000000004",
                "workspace": "a9330000-0000-4000-8000-000000000002",
                "time": now,
            },
        )
    engine.dispose()
    with pytest.raises(RuntimeError, match="populated agent workspace cleanup"):
        command.downgrade(config, "0032_promotion_verification")


def test_0034_phase9_retained_target_schema_round_trip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0033_agent_workspace_cleanup"
    )
    command.upgrade(config, "0033_agent_workspace_cleanup")
    command.upgrade(config, "0034_phase9_completion_remediation")
    engine = create_database_engine(database_url)
    inspector = inspect(engine)
    for table in (
        "agent_workspace_promotion_plans",
        "agent_workspace_promotion_operations",
        "agent_workspace_promotion_handoffs",
        "promotion_verification_operations",
    ):
        columns = {item["name"]: item for item in inspector.get_columns(table)}
        assert not columns["original_target_instance_id"]["nullable"]
        assert columns["target_instance_id"]["nullable"]
    assert "original_target_name" in {
        item["name"]
        for item in inspector.get_columns("agent_workspace_promotion_plans")
    }
    engine.dispose()
    command.downgrade(config, "0033_agent_workspace_cleanup")
    command.upgrade(config, "0034_phase9_completion_remediation")


def test_0034_upgrades_populated_0033_and_guards_detached_downgrade(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0033_agent_workspace_cleanup"
    )
    command.upgrade(config, "0033_agent_workspace_cleanup")
    engine = create_database_engine(database_url)
    timestamp = "2026-09-19T12:00:00.000000Z"
    instance_id = "89800000-0000-4000-8000-000000000003"
    revision_id = "89800000-0000-4000-8000-000000000002"
    source_id = "a9340000-0000-4000-8000-000000000001"
    workspace_id = "a9340000-0000-4000-8000-000000000002"
    deployment_id = "a9340000-0000-4000-8000-000000000003"
    plan_id = "a9340000-0000-4000-8000-000000000004"
    with engine.begin() as connection:
        _seed_0014_instance_with_notes(connection, notes="phase9 retained target")
        connection.execute(
            text(
                "INSERT INTO development_sources "
                "(id,logical_name,source_kind,relative_locator,addon_id,observed_name,"
                "observed_version,extension_types_json,registered_at,validated_at) "
                "VALUES (:id,'Source','local_directory','source','plugin.video.source',"
                "'Source','1.0','[\"xbmc.python.pluginsource\"]',:time,:time)"
            ),
            {"id": source_id, "time": timestamp},
        )
        connection.execute(
            text(
                "INSERT INTO agent_workspaces "
                "(id,development_source_id,source_addon_id,source_logical_name,"
                "source_observed_name,source_observed_version,state,bytes_state,"
                "baseline_artifact_id,baseline_manifest_digest,generation,"
                "current_tree_digest,capture_started_at,captured_at,created_at,"
                "updated_at) VALUES (:id,:source,'plugin.video.source','Source',"
                "'Source','1.0','ready','present',:baseline,:digest,0,:digest,:time,"
                ":time,:time,:time)"
            ),
            {
                "id": workspace_id,
                "source": source_id,
                "baseline": "a9340000-0000-4000-8000-000000000005",
                "digest": "sha256:" + "a" * 64,
                "time": timestamp,
            },
        )
        connection.execute(
            text(
                "INSERT INTO development_source_deployment_operations "
                "(id,original_managed_instance_id,managed_instance_id,"
                "development_source_id,source_logical_name,addon_id,"
                "runtime_revision_id,request_kind,requested_at,state,phase,"
                "reconciliation_required) VALUES (:id,:instance,:instance,:source,"
                "'Source','plugin.video.source',:revision,'copy_sync',:time,'queued',"
                "'acquire',0)"
            ),
            {
                "id": deployment_id,
                "instance": instance_id,
                "source": source_id,
                "revision": revision_id,
                "time": timestamp,
            },
        )
        connection.execute(
            text(
                "INSERT INTO agent_workspace_promotion_plans "
                "(id,workspace_id,development_source_id,target_instance_id,"
                "workspace_generation,workspace_digest,baseline_artifact_id,"
                "baseline_digest,source_capture_digest,addon_id,current_version,"
                "promoted_name,promoted_version,target_runtime_revision_id,"
                "target_container_id,target_deployment_operation_id,"
                "target_source_digest,target_destination_digest,expected_enabled,"
                "changed_file_count,review_text_bytes,created_at,reviewable_at,"
                "expires_at,state) VALUES (:id,:workspace,:source,:instance,0,"
                ":digest,:baseline,:digest,:digest,'plugin.video.source','1.0',"
                "'Source','1.1',:revision,:container,:deployment,:digest,:digest,1,"
                "1,10,:time,:time,:time,'reviewable')"
            ),
            {
                "id": plan_id,
                "workspace": workspace_id,
                "source": source_id,
                "instance": instance_id,
                "digest": "sha256:" + "a" * 64,
                "baseline": "a9340000-0000-4000-8000-000000000005",
                "revision": revision_id,
                "container": "b" * 64,
                "deployment": deployment_id,
                "time": timestamp,
            },
        )
    engine.dispose()

    command.upgrade(config, "0034_phase9_completion_remediation")
    engine = create_database_engine(database_url)
    with engine.begin() as connection:
        retained = connection.execute(
            text(
                "SELECT target_instance_id, original_target_instance_id, "
                "original_target_name FROM agent_workspace_promotion_plans "
                "WHERE id=:id"
            ),
            {"id": plan_id},
        ).one()
        assert retained == (instance_id, instance_id, "delete source")
        connection.execute(
            text(
                "UPDATE agent_workspace_promotion_plans "
                "SET target_instance_id=NULL WHERE id=:id"
            ),
            {"id": plan_id},
        )
    engine.dispose()
    with pytest.raises(RuntimeError, match="retained identity semantics"):
        command.downgrade(config, "0033_agent_workspace_cleanup")


def test_0041_workspace_retention_timestamp_and_guarded_downgrade(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0040_mcp_workspace_mutation"
    )
    client_id = str(uuid4())
    timestamp = "2026-09-20T14:00:00.000000Z"
    engine = create_database_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO mcp_clients "
                "(id,display_name,enabled,preset_id,created_at,last_used_at) "
                "VALUES (:id,'Disabled retention client',0,"
                "'11000000-0000-4000-8000-000000000003',:time,NULL)"
            ),
            {"id": client_id, "time": timestamp},
        )
    engine.dispose()

    migration_started_at = datetime.now(UTC)
    command.upgrade(config, "0041_mcp_workspace_validation_retention")
    migration_finished_at = datetime.now(UTC)
    engine = create_database_engine(database_url)
    with engine.begin() as connection:
        disabled_at = datetime.fromisoformat(
            connection.scalar(
                text("SELECT disabled_at FROM mcp_clients WHERE id=:id"),
                {"id": client_id},
            ).replace("Z", "+00:00")
        )
        assert migration_started_at <= disabled_at <= migration_finished_at
        connection.execute(
            text(
                "INSERT INTO mcp_security_events "
                "(id,event_kind,actor,client_id,credential_id,target_id,preset_id,"
                "development_source_id,workspace_id,occurred_at) VALUES "
                "(:id,'retention_cleanup_blocked','system',:client,NULL,NULL,NULL,"
                "NULL,NULL,:time)"
            ),
            {"id": str(uuid4()), "client": client_id, "time": timestamp},
        )
        with pytest.raises(IntegrityError):
            connection.execute(
                text("UPDATE mcp_clients SET enabled=1 WHERE id=:id"),
                {"id": client_id},
            )
    engine.dispose()
    with pytest.raises(RuntimeError, match="populated MCP workspace retention"):
        command.downgrade(config, "0040_mcp_workspace_mutation")
