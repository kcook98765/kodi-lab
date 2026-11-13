import re
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.util.exc import CommandError
from sqlalchemy import Connection, String, inspect, text
from sqlalchemy.exc import IntegrityError

from kodi_lab.application.external_bridges import ExternalBridgeService
from kodi_lab.application.external_targets import ExternalTargetAdministrationService
from kodi_lab.application.production_capability_amendments import (
    ProductionCapabilityAmendmentService,
)
from kodi_lab.application.reset_activity import (
    ResetActivityRegistry,
    ResetActivityState,
)
from kodi_lab.application.reset_operations import (
    DurableResetActivityObserver,
    ResetExecutor,
    ResetOperationCoordinator,
)
from kodi_lab.domain import (
    ExternalAuthenticationMode,
    ExternalProtocolKind,
    ResetOperationPhase,
    ResetOperationState,
    TargetType,
)
from kodi_lab.kodi.external import (
    ExternalEndpointPolicy,
    ExternalKodiProbeResult,
    ValidatedExternalEndpoint,
)
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.storage.secrets import ExternalBridgeSecretStore, ExternalCredentialStore
from tests.migration_support import MigrationTemplateStore, migration_setup
from tests.migration_support import migration_templates as migration_templates

pytestmark = pytest.mark.slow


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
        ).fetchone() == ("0014_human_reset_bridge",)


def test_workspace_execution_recovery_schema_retains_historical_ineligibility(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0001_initial_release"
    )
    execution_id = str(uuid4())
    values = {
        key: str(uuid4())
        for key in ("client", "request", "workspace", "source", "target", "deployment")
    }
    values["id"] = execution_id
    values["now"] = "2026-10-03T00:00:00.000000Z"
    engine = create_database_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO mcp_workspace_execution_operations "
                "(id,client_id,request_id,workspace_id,development_source_id,"
                "target_id,deployment_id,generation,addon_id,addon_version,state,"
                "phase,invocation_dispatched,execution_classification,"
                "failure_code,requested_at,started_at,completed_at) VALUES "
                "(:id,:client,:request,:workspace,:source,:target,:deployment,1,"
                "'plugin.video.example','1.0.0','ambiguous','finalize',1,'timeout',"
                "'execution_ambiguous',:now,:now,:now)"
            ),
            values,
        )
    engine.dispose()
    command.upgrade(config, "head")
    engine = create_database_engine(database_url)
    with engine.begin() as connection:
        historical = connection.execute(
            text(
                "SELECT execution_kind,original_container_id,"
                "original_runtime_state_revision FROM "
                "mcp_workspace_execution_operations WHERE id=:id"
            ),
            {"id": execution_id},
        ).one()
        assert historical == (None, None, None)
        columns = {
            column["name"]
            for column in inspect(engine).get_columns(
                "workspace_execution_recovery_operations"
            )
        }
        assert {"execution_operation_id", "outcome", "containment_confirmed"} <= columns
        lease_schema = connection.scalar(
            text(
                "SELECT sql FROM sqlite_master WHERE type='table' "
                "AND name='managed_instance_mutation_leases'"
            )
        )
        assert lease_schema is not None
        assert "workspace_execution_recovery" in lease_schema
    for assignment in (
        "execution_kind='other'",
        "original_container_id='" + "a" * 64 + "'",
        "execution_kind='smoke',original_container_id='" + "g" * 64 + "',"
        "original_runtime_state_revision=0",
    ):
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE mcp_workspace_execution_operations SET "
                    f"{assignment} WHERE id=:id"
                ),
                {"id": execution_id},
            )
    recovery_id = str(uuid4())
    recovery_values = {
        "id": recovery_id,
        "client": values["client"],
        "request": str(uuid4()),
        "execution": execution_id,
        "now": values["now"],
    }
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspace_execution_recovery_operations "
                "(id,client_id,request_id,execution_operation_id,state,phase,outcome,"
                "owned_runtime_state_revision,containment_confirmed,"
                "readiness_confirmed,provenance_confirmed,requested_at,completed_at) "
                "VALUES (:id,:client,:request,:execution,'completed','finalized',"
                "'still_requires_admin',0,0,0,0,:now,:now)"
            ),
            recovery_values,
        )
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspace_execution_recovery_operations "
                "(id,client_id,request_id,execution_operation_id,state,phase,outcome,"
                "owned_runtime_state_revision,containment_confirmed,"
                "readiness_confirmed,provenance_confirmed,requested_at,completed_at) "
                "VALUES (:id,:client,:request,:execution,'completed','finalized',"
                "'still_requires_admin',0,0,0,0,:now,:now)"
            ),
            {**recovery_values, "id": str(uuid4()), "request": str(uuid4())},
        )
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE workspace_execution_recovery_operations "
                "SET outcome='recovered' WHERE id=:id"
            ),
            {"id": recovery_id},
        )
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspace_execution_recovery_operations "
                "(id,client_id,request_id,execution_operation_id,state,phase,outcome,"
                "owned_runtime_state_revision,containment_confirmed,"
                "readiness_confirmed,provenance_confirmed,requested_at,completed_at) "
                "VALUES (:id,:client,:request,:execution,'completed','finalized',"
                "'still_requires_admin',0,0,0,0,:now,:now)"
            ),
            {
                **recovery_values,
                "id": str(uuid4()),
                "request": str(uuid4()),
                "execution": str(uuid4()),
            },
        )
    engine.dispose()
    engine = create_database_engine(database_url)
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0014_human_reset_bridge"
        )
    engine.dispose()


def test_historical_containment_schema_and_reinitialization(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch,
        tmp_path,
        start_revision="0001_initial_release",
    )
    command.upgrade(config, "head")
    engine = create_database_engine(database_url)
    table = "historical_workspace_execution_containment_resolutions"
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0014_human_reset_bridge"
        )
        assert table in inspect(connection).get_table_names()
        foreign_keys = {
            key["referred_table"] for key in inspect(connection).get_foreign_keys(table)
        }
        assert {
            "mcp_workspace_execution_operations",
            "managed_instances",
            "managed_instance_lifecycle_operations",
        } <= foreign_keys
        triggers = {
            row[0]
            for row in connection.execute(
                text("SELECT name FROM sqlite_master WHERE type='trigger'")
            )
        }
        assert {
            "trg_historical_containment_eligible",
            "trg_historical_containment_immutable",
            "trg_historical_containment_no_delete",
        } <= triggers
    engine.dispose()
    engine = create_database_engine(database_url)
    engine.dispose()


def test_failed_schema_command_cannot_poison_later_clone(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    migration_templates: MigrationTemplateStore,
) -> None:
    config, _database_url = migration_setup(
        monkeypatch,
        tmp_path,
        start_revision="0001_initial_release",
    )
    with pytest.raises(CommandError, match="Can't locate revision"):
        command.upgrade(config, "not_a_revision")

    later = tmp_path / "later.db"
    migration_templates.clone("0001_initial_release", later)
    with sqlite3.connect(later) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == ("0001_initial_release",)


def test_empty_database_initializes_runtime_catalog_head(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(monkeypatch, tmp_path, start_revision=None)

    command.upgrade(config, "head")

    engine = create_database_engine(database_url)
    inspector = inspect(engine)
    assert set(inspector.get_table_names()) == {
        "agent_projects",
        "agent_packages",
        "managed_version_operations",
        "managed_clean_resets",
        "managed_web_intents",
        "managed_runtime_reconstruction_operations",
        "external_development_repository_readiness",
        "repository_workspace_deployments",
        "repository_workspace_validation_snapshots",
        "development_publications",
        "development_publication_holds",
        "development_repository_state",
        "addon_repository_candidates",
        "addon_repository_attachment_operations",
        "addon_repository_fetch_operations",
        "addon_repository_metadata_snapshots",
        "addon_repository_refresh_operations",
        "addon_repository_sources",
        "addon_zip_artifacts",
        "native_zip_installations",
        "addon_zip_enablement_operations",
        "addon_zip_import_operations",
        "addon_zip_installation_evidence",
        "addon_zip_installation_operations",
        "addon_zip_smoke_test_operations",
        "agent_workspace_creation_operations",
        "agent_workspace_capture_operations",
        "workspace_import_operations",
        "mcp_remote_observations",
        "external_reconnect_operations",
        "mcp_workspace_launches",
        "mcp_addon_settings_operations",
        "managed_resource_policy",
        "managed_remote_guards",
        "novnc_sessions",
        "managed_idle_states",
        "managed_instance_uses",
        "managed_start_reservations",
        "managed_start_authorizations",
        "create_kodi_operations",
        "managed_bridge_preparations",
        "managed_bridge_preparation_children",
        "managed_provision_reservations",
        "development_sessions",
        "development_selections",
        "development_session_policy",
        "managed_diagnostic_captures",
        "development_session_replacement_reviews",
        "development_session_evidence_holds",
        "managed_runtime_retirements",
        "development_session_replacements",
        "session_source_copy_admissions",
        "managed_agent_lifecycle_requests",
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
        "external_kodi_target_events",
        "external_development_capability_amendments",
        "external_kodi_target_observations",
        "external_kodi_targets",
        "external_bridge_events",
        "external_bridge_deployment_receipts",
        "external_bridge_pairings",
        "external_bridge_request_receipts",
        "external_screenshot_preferences",
        "reviewed_bridge_builds",
        "external_development_source_baselines",
        "external_production_policies",
        "external_production_policy_revision_guards",
        "production_capability_amendments",
        "production_bridge_http_acknowledgements",
        "production_native_control_http_acknowledgements",
        "production_mutation_control",
        "global_heavy_operation_lease",
        "managed_instances",
        "managed_bridge_trust",
        "managed_screenshots",
        "mcp_clients",
        "mcp_client_target_grants",
        "mcp_client_target_grant_identities",
        "mcp_client_development_source_grants",
        "mcp_agent_workspace_bindings",
        "mcp_credentials",
        "mcp_development_requests",
        "mcp_presets",
        "mcp_production_screenshot_receipts",
        "mcp_production_navigation_audits",
        "mcp_production_remote_receipt_bindings",
        "mcp_production_target_scopes",
        "production_operation_approvals",
        "production_approval_transition_events",
        "production_transient_dispatch_audits",
        "production_approval_creation_rates",
        "production_transient_dispatch_rates",
        "mcp_preset_permissions",
        "mcp_remote_action_receipts",
        "mcp_security_events",
        "mcp_target_preset_assignments",
        "mcp_workspace_deployment_operations",
        "mcp_workspace_execution_operations",
        "workspace_execution_recovery_operations",
        "historical_workspace_execution_containment_resolutions",
        "household_onboarding_operations",
        "external_secret_retirements",
        "mcp_workspace_validations",
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
        "runtime_recipe_installations",
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


def test_smoke_exception_upgrade_constraints_and_safe_downgrade(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0001_initial_release"
    )
    table = "mcp_workspace_execution_operations"
    operation_id = str(uuid4())
    now = "2026-10-03T00:00:00.000000Z"
    engine = create_database_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                f"INSERT INTO {table} (id,client_id,request_id,workspace_id,"
                "development_source_id,target_id,deployment_id,generation,"
                "addon_id,addon_version,state,phase,invocation_dispatched,"
                "execution_classification,item_count,result_classification,"
                "failure_code,kodi_error_code,kodi_error_message,requested_at,"
                "started_at,completed_at) VALUES "
                "(:id,:client,:request,:workspace,:source,:target,:deployment,1,"
                "'plugin.video.example','1.0.0','failed','finalize',1,"
                "'jsonrpc_error',NULL,'smoke_failed','execution_failed',"
                "-32602,'Invalid params.',:now,:now,:now)"
            ),
            {
                "id": operation_id,
                "client": str(uuid4()),
                "request": str(uuid4()),
                "workspace": str(uuid4()),
                "source": str(uuid4()),
                "target": str(uuid4()),
                "deployment": str(uuid4()),
                "now": now,
            },
        )
    engine.dispose()
    command.upgrade(config, "head")
    engine = create_database_engine(database_url)
    columns = {column["name"] for column in inspect(engine).get_columns(table)}
    assert {
        "addon_exception_status",
        "addon_exception_class",
        "addon_exception_file",
        "addon_exception_line",
        "addon_exception_detail",
    } <= columns
    with engine.begin() as connection:
        assert connection.execute(
            text(
                f"SELECT addon_exception_status,addon_exception_class FROM {table} "
                "WHERE id=:id"
            ),
            {"id": operation_id},
        ).one() == (None, None)
        connection.execute(
            text(
                f"UPDATE {table} SET addon_exception_status='matched',"
                "addon_exception_class='AttributeError',"
                "addon_exception_file='default.py',addon_exception_line=23 "
                "WHERE id=:id"
            ),
            {"id": operation_id},
        )
    for invalid in (
        "addon_exception_status='other'",
        "addon_exception_line=0",
        "addon_exception_line=1000001",
        "addon_exception_class='Bad.Class'",
        "addon_exception_class='" + "x" * 65 + "'",
        "addon_exception_file='/tmp/default.py'",
        "addon_exception_file='https:secret.py'",
        "addon_exception_file='../default.py'",
        "addon_exception_file='lib//default.py'",
        "addon_exception_file='./default.py'",
        "addon_exception_file='default.txt'",
        "addon_exception_file='" + "x" * 257 + "'",
        "addon_exception_detail='" + "x" * 129 + "'",
        "addon_exception_file='default.py' || char(0)",
        "addon_exception_status='no_match'",
        "addon_exception_class=NULL",
        "kodi_error_code=NULL",
        "failure_code=NULL",
        "result_classification=NULL",
    ):
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(
                text(f"UPDATE {table} SET {invalid} WHERE id=:id"),
                {"id": operation_id},
            )
    engine.dispose()
    engine = create_database_engine(database_url)
    with engine.begin() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0014_human_reset_bridge"
        )
        connection.execute(
            text(
                f"UPDATE {table} SET addon_exception_status=NULL,"
                "addon_exception_class=NULL,addon_exception_file=NULL,"
                "addon_exception_line=NULL,addon_exception_detail=NULL WHERE id=:id"
            ),
            {"id": operation_id},
        )
    engine.dispose()
    engine = create_database_engine(database_url)
    with engine.connect() as connection:
        assert connection.scalar(text("PRAGMA integrity_check")) == "ok"
        assert connection.scalar(text(f"SELECT count(*) FROM {table}")) == 1
    engine.dispose()


_RESET_PHASE_INSTANCE = UUID("61000000-0000-4000-8000-000000000003")
_RESET_PHASE_HISTORY = "61000000-0000-4000-8000-000000000004"


def _seed_reset_phase_history(database_url: str) -> None:
    """Populate the migrated predecessor with a real parent and reset history."""
    engine = create_database_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO runtime_definitions (id,slug,kodi_version,support_tier) "
                "VALUES ('61000000-0000-4000-8000-000000000001',"
                "'reset-phase-test','20.5','current')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO runtime_revisions "
                "(id,runtime_definition_id,revision_number,source_kind,"
                "source_reference,content_digest,manifest_reference,"
                "manifest_digest,built_at,approval_state,"
                "requires_trusted_build_confirmation) VALUES "
                "('61000000-0000-4000-8000-000000000002',"
                "'61000000-0000-4000-8000-000000000001',1,'test','test',"
                ":digest,'test',:digest,'2026-10-02T00:00:00.000000Z',"
                "'not_approved',0)"
            ),
            {"digest": "sha256:" + "a" * 64},
        )
        connection.execute(
            text(
                "INSERT INTO targets "
                "(id,name,target_type,kodi_version,availability,health,"
                "connection_state) VALUES (:instance,'reset-phase-test','managed',"
                "'20.5','unavailable','warning','disconnected')"
            ),
            {"instance": str(_RESET_PHASE_INSTANCE)},
        )
        connection.execute(
            text(
                "INSERT INTO managed_instances "
                "(target_id,instance_type,runtime_revision_id,runtime_state,"
                "start_on_host_boot) VALUES (:instance,'disposable',"
                "'61000000-0000-4000-8000-000000000002','stopped',0)"
            ),
            {"instance": str(_RESET_PHASE_INSTANCE)},
        )
        connection.execute(
            text(
                "INSERT INTO reset_operations "
                "(id,managed_instance_id,requested_at,started_at,heartbeat_at,"
                "completed_at,state,phase,failure_reason,reconciliation_required) "
                "VALUES (:history,:instance,:requested,:started,:started,"
                ":completed,'failed','final_stopping',:reason,0)"
            ),
            {
                "history": _RESET_PHASE_HISTORY,
                "instance": str(_RESET_PHASE_INSTANCE),
                "requested": "2026-10-02T23:56:26.836031Z",
                "started": "2026-10-02T23:56:26.865671Z",
                "completed": "2026-10-02T23:56:36.485608Z",
                "reason": "fixture scan or semantic verification failed",
            },
        )
    engine.dispose()


def test_migrates_populated_reset_phase_history_and_keeps_contract(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0001_initial_release"
    )
    _seed_reset_phase_history(database_url)
    engine = create_database_engine(database_url)
    before = inspect(engine)
    old_checks = {
        check["name"]: check["sqltext"]
        for check in before.get_check_constraints("reset_operations")
        if check["name"] != "reset_operation_phase"
    }
    old_indexes = {
        index["name"]: (
            index["column_names"],
            index["unique"],
            str(index["dialect_options"].get("sqlite_where", "")),
        )
        for index in before.get_indexes("reset_operations")
    }
    old_foreign_keys = before.get_foreign_keys("reset_operations")
    with engine.connect() as connection:
        old_schema = connection.scalar(
            text(
                "SELECT sql FROM sqlite_master WHERE type='table' "
                "AND name='reset_operations'"
            )
        )
    assert isinstance(old_schema, str)
    old_phase_type = next(
        column["type"]
        for column in before.get_columns("reset_operations")
        if column["name"] == "phase"
    )
    assert isinstance(old_phase_type, String)
    engine.dispose()

    command.upgrade(config, "head")
    engine = create_database_engine(database_url)
    after = inspect(engine)
    assert {
        check["name"]: check["sqltext"]
        for check in after.get_check_constraints("reset_operations")
        if check["name"] != "reset_operation_phase"
    } == old_checks
    assert {
        index["name"]: (
            index["column_names"],
            index["unique"],
            str(index["dialect_options"].get("sqlite_where", "")),
        )
        for index in after.get_indexes("reset_operations")
    } == old_indexes
    assert after.get_foreign_keys("reset_operations") == old_foreign_keys
    new_phase_type = next(
        column["type"]
        for column in after.get_columns("reset_operations")
        if column["name"] == "phase"
    )
    assert isinstance(new_phase_type, String)
    assert new_phase_type.length == 25
    phase_check = next(
        check["sqltext"]
        for check in after.get_check_constraints("reset_operations")
        if check["name"] == "reset_operation_phase"
    )
    assert set(re.findall(r"'([^']+)'", phase_check)) == {
        phase.value for phase in ResetOperationPhase
    }
    with engine.begin() as connection:
        assert connection.execute(
            text(
                "SELECT state,phase,failure_reason FROM reset_operations "
                "WHERE id=:history"
            ),
            {"history": _RESET_PHASE_HISTORY},
        ).one() == (
            "failed",
            "final_stopping",
            "fixture scan or semantic verification failed",
        )
        for phase in ResetOperationPhase:
            connection.execute(
                text("UPDATE reset_operations SET phase=:phase WHERE id=:history"),
                {"phase": phase.value, "history": _RESET_PHASE_HISTORY},
            )
        connection.execute(
            text(
                "UPDATE reset_operations SET phase='applying_post_scan_state' "
                "WHERE id=:history"
            ),
            {"history": _RESET_PHASE_HISTORY},
        )
        connection.execute(
            text(
                "UPDATE reset_operations SET phase='verifying_post_scan_state' "
                "WHERE id=:history"
            ),
            {"history": _RESET_PHASE_HISTORY},
        )
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE reset_operations SET phase='not_a_reset_phase' "
                "WHERE id=:history"
            ),
            {"history": _RESET_PHASE_HISTORY},
        )
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE reset_operations SET state='not_a_reset_state' "
                "WHERE id=:history"
            ),
            {"history": _RESET_PHASE_HISTORY},
        )
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO reset_operations "
                "(id,managed_instance_id,requested_at,state,phase) "
                "VALUES ('61000000-0000-4000-8000-000000000005',:instance,"
                "'2026-10-03T00:00:00.000000Z','queued','queued')"
            ),
            {"instance": str(_RESET_PHASE_INSTANCE)},
        )
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO reset_operations "
                "(id,managed_instance_id,requested_at,state,phase) "
                "VALUES ('61000000-0000-4000-8000-000000000006',:instance,"
                "'2026-10-03T00:00:01.000000Z','queued','queued')"
            ),
            {"instance": str(_RESET_PHASE_INSTANCE)},
        )
    with engine.connect() as connection:
        assert connection.scalar(text("PRAGMA integrity_check")) == "ok"
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
        assert (
            connection.scalar(
                text("SELECT phase FROM reset_operations WHERE id=:history"),
                {"history": _RESET_PHASE_HISTORY},
            )
            == "verifying_post_scan_state"
        )
    engine.dispose()

    engine = create_database_engine(database_url)
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0014_human_reset_bridge"
        )
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE reset_operations SET phase='final_stopping' WHERE id=:history"
            ),
            {"history": _RESET_PHASE_HISTORY},
        )
    engine.dispose()
    engine = create_database_engine(database_url)
    with engine.connect() as connection:
        assert connection.scalar(text("PRAGMA integrity_check")) == "ok"
        downgraded_phase_type = next(
            column["type"]
            for column in inspect(engine).get_columns("reset_operations")
            if column["name"] == "phase"
        )
        assert isinstance(downgraded_phase_type, String)
        assert (
            connection.scalar(
                text("SELECT phase FROM reset_operations WHERE id=:history"),
                {"history": _RESET_PHASE_HISTORY},
            )
            == "final_stopping"
        )
    engine.dispose()


def test_durable_reset_flow_records_both_post_scan_phases(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0001_initial_release"
    )
    command.upgrade(config, "head")
    # The current coordinator maps the current ORM model, including later
    # nullable execution diagnostics; exercise it on the complete schema.
    command.upgrade(config, "head")
    _seed_reset_phase_history(database_url)
    engine = create_database_engine(database_url)
    factory = create_session_factory(engine)
    coordinator = ResetOperationCoordinator(factory)
    operation = coordinator.queue(_RESET_PHASE_INSTANCE).operation
    seen: list[ResetOperationPhase] = []

    class PostScanResetService:
        def __init__(self, observer: DurableResetActivityObserver) -> None:
            self.observer = observer

        def reset(self, instance_id: UUID) -> object:
            activity = self.observer.begin(instance_id)
            for state in (
                ResetActivityState.SCANNING_FIXTURE,
                ResetActivityState.VERIFYING_FIXTURE,
                ResetActivityState.APPLYING_POST_SCAN_STATE,
                ResetActivityState.VERIFYING_POST_SCAN_STATE,
                ResetActivityState.FINAL_STOPPING,
                ResetActivityState.FINAL_VERIFYING,
            ):
                self.observer.transition(instance_id, activity.generation, state)
                current = coordinator.latest_for_instance(instance_id)
                assert current is not None
                seen.append(current.phase)
            self.observer.succeed(instance_id, activity.generation)
            return object()

    executor = ResetExecutor(
        coordinator,
        ResetActivityRegistry(),
        PostScanResetService,
        max_workers=1,
    )
    executor.submit(operation.operation_id)
    for _ in range(100):
        latest = coordinator.latest_for_instance(_RESET_PHASE_INSTANCE)
        if latest is not None and latest.state is ResetOperationState.SUCCEEDED:
            break
        time.sleep(0.01)
    executor.close()
    assert latest is not None and latest.state is ResetOperationState.SUCCEEDED
    assert seen == [
        ResetOperationPhase.SCANNING_FIXTURE,
        ResetOperationPhase.VERIFYING_FIXTURE,
        ResetOperationPhase.APPLYING_POST_SCAN_STATE,
        ResetOperationPhase.VERIFYING_POST_SCAN_STATE,
        ResetOperationPhase.FINAL_STOPPING,
        ResetOperationPhase.FINAL_VERIFYING,
    ]
    engine.dispose()


def test_mcp_foundation_seeds_builtins_and_guards_populated_downgrade(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0001_initial_release"
    )
    command.upgrade(config, "head")
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
            ("Production Remote Kodi", 1, 1),
            ("Production Inspection", 1, 1),
            ("Production Visual Inspection", 1, 1),
        ]
        permission_rows = connection.execute(
            text(
                "SELECT permission, COUNT(*) FROM mcp_preset_permissions "
                "GROUP BY permission"
            )
        ).fetchall()
        assert [tuple(row) for row in permission_rows] == [
            ("development.execute", 2),
            ("development.request_promotion", 2),
            ("target.control", 3),
            ("target.inspect", 6),
            ("target.visual", 5),
            ("workspace.deploy", 2),
            ("workspace.edit", 2),
            ("workspace.inspect", 2),
        ]
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

    engine = create_database_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM mcp_clients"))
    engine.dispose()
    command.upgrade(config, "head")


def test_development_inspection_constraints_and_history(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0001_initial_release"
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
    command.upgrade(config, "head")
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

    engine = create_database_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM mcp_agent_workspace_bindings"))
        connection.execute(text("DELETE FROM mcp_client_development_source_grants"))
        connection.execute(
            text("DELETE FROM mcp_clients WHERE id = :id"), {"id": client_id}
        )
    engine.dispose()
    engine = create_database_engine(database_url)
    engine.dispose()
    command.upgrade(config, "head")
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


def test_workspace_mutation_constraints_and_history(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0001_initial_release"
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
    command.upgrade(config, "head")
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

    engine = create_database_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM mcp_development_requests"))
        connection.execute(
            text("DELETE FROM mcp_clients WHERE id = :id"), {"id": client_id}
        )
    engine.dispose()
    engine = create_database_engine(database_url)
    engine.dispose()


def test_remote_receipt_constraints_and_downgrade(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0001_initial_release"
    )
    command.upgrade(config, "head")
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
            ("development.execute", 2),
            ("development.request_promotion", 2),
            ("target.control", 3),
            ("target.inspect", 6),
            ("target.visual", 5),
            ("workspace.deploy", 2),
            ("workspace.edit", 2),
            ("workspace.inspect", 2),
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

    engine = create_database_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM mcp_remote_action_receipts"))
    engine.dispose()
    engine = create_database_engine(database_url)
    engine.dispose()
    command.upgrade(config, "head")


def test_initializes_reset_operations_and_reinitialization(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0001_initial_release"
    )
    command.upgrade(config, "head")
    engine = create_database_engine(database_url)
    engine.dispose()

    command.upgrade(config, "head")
    engine = create_database_engine(database_url)
    assert "reset_operations" in inspect(engine).get_table_names()
    engine.dispose()
    command.upgrade(config, "head")

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


def test_tv_taxonomy_constraint_accepts_only_three_supported_categories(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0001_initial_release"
    )
    command.upgrade(config, "head")
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


def test_medium_size_constraint_accepts_only_small_and_medium(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0001_initial_release"
    )
    command.upgrade(config, "head")
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


def test_relaxed_terminal_history_retains_original_instance_uuid(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0001_initial_release"
    )
    command.upgrade(config, "head")
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
    command.upgrade(config, "head")
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


def test_lifecycle_history_schema_and_guarded_reinitialization(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0001_initial_release"
    )
    command.upgrade(config, "head")
    engine = create_database_engine(database_url)
    engine.dispose()

    command.upgrade(config, "head")
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
    command.upgrade(config, "head")

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


def test_development_sources_schema_constraints_and_guarded_reinitialization(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0001_initial_release"
    )
    command.upgrade(config, "head")
    engine = create_database_engine(database_url)
    engine.dispose()

    command.upgrade(config, "head")
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
    command.upgrade(config, "head")

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


def test_source_attachments_schema_constraints_and_guarded_reinitialization(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0001_initial_release"
    )
    command.upgrade(config, "head")
    engine = create_database_engine(database_url)
    engine.dispose()

    command.upgrade(config, "head")
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
    command.upgrade(config, "head")

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


def test_enablement_schema_reinitialization(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0001_initial_release"
    )
    command.upgrade(config, "head")
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
    engine = create_database_engine(database_url)
    engine.dispose()
    command.upgrade(config, "head")


def test_deployment_schema_empty_lifecycle_and_metadata_check(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0001_initial_release"
    )
    command.upgrade(config, "head")
    engine = create_database_engine(database_url)
    engine.dispose()

    command.upgrade(config, "head")
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

    engine = create_database_engine(database_url)
    engine.dispose()
    command.upgrade(config, "head")


def test_smoke_schema_reinitialization_and_mutation_kind(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0001_initial_release"
    )
    command.upgrade(config, "head")
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
    engine = create_database_engine(database_url)
    engine.dispose()
    command.upgrade(config, "head")


def test_uploaded_zip_schema_reinitialization(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0001_initial_release"
    )
    command.upgrade(config, "head")
    engine = create_database_engine(database_url)
    with engine.begin() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0014_human_reset_bridge"
        )
    engine.dispose()

    command.upgrade(config, "head")


def test_zip_enablement_schema_reinitialization_and_mutation_kind(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0001_initial_release"
    )
    command.upgrade(config, "head")
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
            "0014_human_reset_bridge"
        )
        mutation_sql = connection.scalar(
            text(
                "SELECT sql FROM sqlite_master WHERE type='table' "
                "AND name='managed_instance_mutation_leases'"
            )
        )
    assert "addon_zip_enablement" in mutation_sql
    engine.dispose()
    engine = create_database_engine(database_url)
    engine.dispose()
    command.upgrade(config, "head")


def test_zip_smoke_schema_reinitialization_and_mutation_kind(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0001_initial_release"
    )
    command.upgrade(config, "head")
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
    engine = create_database_engine(database_url)
    engine.dispose()
    command.upgrade(config, "head")


def test_repository_schema_reinitialization_and_populated_guard(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0001_initial_release"
    )
    command.upgrade(config, "head")
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

    engine = create_database_engine(database_url)
    inspector = inspect(engine)
    engine.dispose()
    command.upgrade(config, "head")

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


def test_agent_workspace_schema_reinitialization_constraints_and_guard(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0001_initial_release"
    )
    command.upgrade(config, "head")
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

    engine = create_database_engine(database_url)
    engine.dispose()
    command.upgrade(config, "head")

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


def test_workspace_mutation_activity_reinitialization_and_guard(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0001_initial_release"
    )
    command.upgrade(config, "head")
    engine = create_database_engine(database_url)
    engine.dispose()

    command.upgrade(config, "head")
    engine = create_database_engine(database_url)
    inspector = inspect(engine)
    assert "agent_workspace_mutation_events" in inspector.get_table_names()
    assert inspector.get_foreign_keys("agent_workspace_mutation_events")[0][
        "options"
    ] == {"ondelete": "RESTRICT"}
    engine.dispose()
    command.upgrade(config, "head")

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


def test_workspace_promotion_schema_reinitialization_constraints_and_guard(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0001_initial_release"
    )
    command.upgrade(config, "head")
    engine = create_database_engine(database_url)
    engine.dispose()

    command.upgrade(config, "head")
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

    engine = create_database_engine(database_url)
    engine.dispose()
    command.upgrade(config, "head")

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


def test_promotion_verification_schema_reinitialization(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0001_initial_release"
    )
    command.upgrade(config, "head")

    command.upgrade(config, "head")
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

    engine = create_database_engine(database_url)
    engine.dispose()
    command.upgrade(config, "head")


def test_workspace_cleanup_schema_reinitialization_and_populated_guard(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0001_initial_release"
    )
    command.upgrade(config, "head")
    engine = create_database_engine(database_url)
    engine.dispose()

    command.upgrade(config, "head")
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
    command.upgrade(config, "head")

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


def test_phase9_retained_target_schema_reinitialization(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0001_initial_release"
    )
    command.upgrade(config, "head")
    command.upgrade(config, "head")
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
    command.upgrade(config, "head")


def test_workspace_retention_timestamp_and_history(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0001_initial_release"
    )
    client_id = str(uuid4())
    timestamp = "2026-09-20T14:00:00.000000Z"
    engine = create_database_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO mcp_clients "
                "(id,display_name,enabled,preset_id,created_at,last_used_at,d"
                "isabled_at) "
                "VALUES (:id,'Disabled retention client',0,"
                "'11000000-0000-4000-8000-000000000003',:time,NULL,:time)"
            ),
            {"id": client_id, "time": timestamp},
        )
    engine.dispose()

    _migration_started_at = datetime.now(UTC)
    command.upgrade(config, "head")
    _migration_finished_at = datetime.now(UTC)
    engine = create_database_engine(database_url)
    with engine.begin() as connection:
        disabled_at = datetime.fromisoformat(
            connection.scalar(
                text("SELECT disabled_at FROM mcp_clients WHERE id=:id"),
                {"id": client_id},
            ).replace("Z", "+00:00")
        )
        assert disabled_at == datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
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


def test_workspace_deployment_execution_presets_and_history(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch,
        tmp_path,
        start_revision="0001_initial_release",
    )
    custom_id = str(uuid4())
    engine = create_database_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO mcp_presets (id,display_name,builtin,assignable) "
                "VALUES (:id,'Pre-Slice-4 snapshot',0,1)"
            ),
            {"id": custom_id},
        )
        connection.execute(
            text(
                "INSERT INTO mcp_preset_permissions (preset_id,permission) "
                "VALUES (:id,'workspace.edit')"
            ),
            {"id": custom_id},
        )
    engine.dispose()

    command.upgrade(config, "head")
    engine = create_database_engine(database_url)
    with engine.begin() as connection:
        seeded = [
            tuple(row)
            for row in connection.execute(
                text(
                    "SELECT preset_id,permission FROM mcp_preset_permissions "
                    "WHERE permission IN ('workspace.deploy','development.execute') "
                    "ORDER BY preset_id,permission"
                )
            )
        ]
        assert seeded == [
            ("11000000-0000-4000-8000-000000000001", "development.execute"),
            ("11000000-0000-4000-8000-000000000001", "workspace.deploy"),
            ("11000000-0000-4000-8000-000000000003", "development.execute"),
            ("11000000-0000-4000-8000-000000000003", "workspace.deploy"),
        ]
        assert [
            tuple(row)
            for row in connection.execute(
                text(
                    "SELECT permission FROM mcp_preset_permissions "
                    "WHERE preset_id=:id ORDER BY permission"
                ),
                {"id": custom_id},
            )
        ] == [("workspace.edit",)]
        tables = {
            row[0]
            for row in connection.execute(
                text(
                    "SELECT name FROM sqlite_master WHERE type='table' AND "
                    "name LIKE 'mcp_workspace_%'"
                )
            )
        }
        assert {
            "mcp_workspace_validations",
            "mcp_workspace_deployment_operations",
            "mcp_workspace_execution_operations",
        }.issubset(tables)
        connection.execute(
            text(
                "INSERT INTO mcp_preset_permissions (preset_id,permission) "
                "VALUES (:id,'workspace.deploy')"
            ),
            {"id": custom_id},
        )
    engine.dispose()

    engine = create_database_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "DELETE FROM mcp_preset_permissions "
                "WHERE preset_id=:id AND permission='workspace.deploy'"
            ),
            {"id": custom_id},
        )
    engine.dispose()


def test_promotion_request_reuses_plans_and_preserves_custom_snapshots(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch,
        tmp_path,
        start_revision="0001_initial_release",
    )
    custom_id = str(uuid4())
    engine = create_database_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO mcp_presets (id,display_name,builtin,assignable) "
                "VALUES (:id,'Pre-Slice-5 snapshot',0,1)"
            ),
            {"id": custom_id},
        )
        connection.execute(
            text(
                "INSERT INTO mcp_preset_permissions (preset_id,permission) "
                "VALUES (:id,'development.execute')"
            ),
            {"id": custom_id},
        )
    engine.dispose()

    command.upgrade(config, "head")
    engine = create_database_engine(database_url)
    inspector = inspect(engine)
    assert {
        "originating_mcp_client_id",
        "originating_mcp_request_id",
        "originating_mcp_deployment_id",
        "originating_mcp_execution_id",
    }.issubset(
        {
            item["name"]
            for item in inspector.get_columns("agent_workspace_promotion_plans")
        }
    )
    assert any(
        item["name"] == "workspace_promotion_mcp_origin_complete"
        for item in inspector.get_check_constraints("agent_workspace_promotion_plans")
    )
    assert any(
        item["name"] == "uq_workspace_promotion_mcp_request" and item["unique"]
        for item in inspector.get_indexes("agent_workspace_promotion_plans")
    )
    with engine.begin() as connection:
        assert [
            tuple(row)
            for row in connection.execute(
                text(
                    "SELECT preset_id,permission FROM mcp_preset_permissions "
                    "WHERE permission='development.request_promotion' "
                    "ORDER BY preset_id"
                )
            )
        ] == [
            (
                "11000000-0000-4000-8000-000000000001",
                "development.request_promotion",
            ),
            (
                "11000000-0000-4000-8000-000000000003",
                "development.request_promotion",
            ),
        ]
        assert [
            tuple(row)
            for row in connection.execute(
                text(
                    "SELECT permission FROM mcp_preset_permissions "
                    "WHERE preset_id=:id ORDER BY permission"
                ),
                {"id": custom_id},
            )
        ] == [("development.execute",)]
    engine.dispose()

    engine = create_database_engine(database_url)
    engine.dispose()

    command.upgrade(config, "head")
    engine = create_database_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO mcp_preset_permissions (preset_id,permission) "
                "VALUES (:id,'development.request_promotion')"
            ),
            {"id": custom_id},
        )
    engine.dispose()


def test_preserves_managed_target_grants_and_policies_reinitialization(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0001_initial_release"
    )
    target_id = "44000000-0000-4000-8000-000000000001"
    client_id = "44000000-0000-4000-8000-000000000002"
    definition_id = "44000000-0000-4000-8000-000000000003"
    revision_id = "44000000-0000-4000-8000-000000000004"
    preset_id = "11000000-0000-4000-8000-000000000002"
    timestamp = "2026-09-21T00:00:00.000000Z"
    engine = create_database_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO runtime_definitions "
                "(id,slug,kodi_version,support_tier) VALUES "
                "(:id,'phase13-preserve','21.3','current')"
            ),
            {"id": definition_id},
        )
        connection.execute(
            text(
                "INSERT INTO runtime_revisions "
                "(id,runtime_definition_id,revision_number,source_kind,"
                "source_reference,content_digest,manifest_reference,"
                "manifest_digest,built_at,approval_state,"
                "requires_trusted_build_confirmation) VALUES "
                "(:id,:definition,1,'test','test',:digest,'test',:digest,"
                "'2026-09-21T00:00:00Z','not_approved',0)"
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
                "(id,name,target_type,kodi_version,availability,health,"
                "connection_state) VALUES "
                "(:id,'preserved','managed','21.3','available','healthy',"
                "'connected')"
            ),
            {"id": target_id},
        )
        connection.execute(
            text(
                "INSERT INTO managed_instances "
                "(target_id,instance_type,runtime_revision_id,runtime_state,"
                "start_on_host_boot) VALUES "
                "(:id,'disposable',:revision,'stopped',0)"
            ),
            {"id": target_id, "revision": revision_id},
        )
        connection.execute(
            text(
                "INSERT INTO mcp_clients "
                "(id,display_name,enabled,preset_id,created_at,last_used_at,"
                "disabled_at) VALUES (:id,'preserved',1,:preset,:at,NULL,NULL)"
            ),
            {"id": client_id, "preset": preset_id, "at": timestamp},
        )
        connection.execute(
            text(
                "INSERT INTO mcp_client_target_grants "
                "(client_id,target_id,created_at,grant_id) VALUES (:client,:target,:at"
                ",'a0000000-0000-4000-8000-000000000001')"
            ),
            {"client": client_id, "target": target_id, "at": timestamp},
        )
        connection.execute(
            text(
                "INSERT INTO mcp_target_preset_assignments "
                "(target_id,preset_id,assigned_at) VALUES (:target,:preset,:at)"
            ),
            {"target": target_id, "preset": preset_id, "at": timestamp},
        )
    engine.dispose()

    command.upgrade(config, "head")
    engine = create_database_engine(database_url)
    inspector = inspect(engine)
    assert any(
        item["referred_table"] == "targets"
        for item in inspector.get_foreign_keys("mcp_client_target_grants")
    )
    with engine.begin() as connection:
        assert connection.execute(
            text("SELECT client_id,target_id,created_at FROM mcp_client_target_grants")
        ).one() == (client_id, target_id, timestamp)
        assert connection.execute(
            text(
                "SELECT target_id,preset_id,assigned_at "
                "FROM mcp_target_preset_assignments"
            )
        ).one() == (target_id, preset_id, timestamp)
    engine.dispose()


def test_external_registration_orders_parent_before_subtype_on_migrated_head(
    migration_templates: MigrationTemplateStore, tmp_path: Path
) -> None:
    database_path = tmp_path / "migrated-head.db"
    migration_templates.clone("head", database_path)
    engine = create_database_engine(f"sqlite:///{database_path}")
    factory = create_session_factory(engine)
    service = ExternalTargetAdministrationService(
        factory,
        endpoint_policy=ExternalEndpointPolicy(allowed_ports=(8080,)),
        credential_store=ExternalCredentialStore(
            tmp_path / "external-secrets", master_key=None, key_version=1
        ),
    )

    created = service.register(
        logical_name="Migrated external target",
        protocol_kind=ExternalProtocolKind.JSONRPC_HTTP,
        host="192.168.50.12",
        port=8080,
        authentication_mode=ExternalAuthenticationMode.NONE,
        configured_capabilities={"target_inspect"},
    )

    assert created.target_class == "external_development"
    with engine.connect() as connection:
        assert (
            connection.scalar(
                text("SELECT COUNT(*) FROM targets WHERE id=:id"),
                {"id": str(created.target_id)},
            )
            == 1
        )
        assert (
            connection.scalar(
                text("SELECT COUNT(*) FROM external_kodi_targets WHERE target_id=:id"),
                {"id": str(created.target_id)},
            )
            == 1
        )
    engine.dispose()


def test_bridge_token_orders_pairing_parent_before_event_on_migrated_head(
    migration_templates: MigrationTemplateStore, tmp_path: Path
) -> None:
    database_path = tmp_path / "migrated-head.db"
    migration_templates.clone("head", database_path)
    engine = create_database_engine(f"sqlite:///{database_path}")
    factory = create_session_factory(engine)
    target_service = ExternalTargetAdministrationService(
        factory,
        endpoint_policy=ExternalEndpointPolicy(allowed_ports=(8080,)),
        credential_store=ExternalCredentialStore(
            tmp_path / "external-secrets", master_key=None, key_version=1
        ),
    )
    target = target_service.register(
        logical_name="Migrated bridge target",
        protocol_kind=ExternalProtocolKind.JSONRPC_HTTP,
        host="192.168.50.12",
        port=8080,
        authentication_mode=ExternalAuthenticationMode.NONE,
        configured_capabilities={"screenshot", "target_inspect"},
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE external_kodi_targets SET "
                "administrative_state='enabled', verification_state='verified', "
                "observation_state='reachable', "
                "verified_addresses_json='[\"192.168.50.12\"]', "
                "verified_kodi_name='Kodi', verified_kodi_version='20.5', "
                "verified_at='2026-09-21T00:00:00.000000Z' "
                "WHERE target_id=:id"
            ),
            {"id": str(target.target_id)},
        )

    bridge_secrets = tmp_path / "bridge-secrets"
    service = ExternalBridgeService(
        factory,
        endpoint_policy=ExternalEndpointPolicy(allowed_ports=(8765,)),
        secret_store=ExternalBridgeSecretStore(
            bridge_secrets, master_key=b"b" * 32, key_version=1
        ),
    )
    generated = service.generate_pairing_token(target.target_id, bridge_port=8765)

    assert len(generated.token) == 43
    assert service.status(target.target_id).pairing_token_pending is True
    with engine.connect() as connection:
        assert (
            connection.scalar(
                text(
                    "SELECT COUNT(*) FROM external_bridge_pairings WHERE target_id=:id"
                ),
                {"id": str(target.target_id)},
            )
            == 1
        )
        assert (
            connection.scalar(
                text(
                    "SELECT COUNT(*) FROM external_bridge_events "
                    "WHERE target_id=:id AND event_kind='pairing_token_generated'"
                ),
                {"id": str(target.target_id)},
            )
            == 1
        )
    assert len(list(bridge_secrets.glob("*.json"))) == 1
    engine.dispose()


def test_external_bridge_schema_reinitialization(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0001_initial_release"
    )
    command.upgrade(config, "head")
    engine = create_database_engine(database_url)
    inspector = inspect(engine)
    assert {
        "external_bridge_pairings",
        "external_bridge_request_receipts",
        "external_bridge_events",
    } <= set(inspector.get_table_names())
    assert {
        item["name"]
        for item in inspector.get_unique_constraints("external_bridge_request_receipts")
    } == {"uq_external_bridge_nonce"}
    engine.dispose()

    engine = create_database_engine(database_url)
    engine.dispose()


def test_external_bridge_development_reinitialization(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0001_initial_release"
    )
    command.upgrade(config, "head")
    engine = create_database_engine(database_url)
    inspector = inspect(engine)
    assert {
        "external_development_source_baselines",
        "external_bridge_deployment_receipts",
    } <= set(inspector.get_table_names())
    deployment_columns = {
        item["name"]: item
        for item in inspector.get_columns("mcp_workspace_deployment_operations")
    }
    assert deployment_columns["runtime_revision_id"]["nullable"] is True
    assert deployment_columns["target_class"]["nullable"] is False
    promotion_columns = {
        item["name"]: item
        for item in inspector.get_columns("agent_workspace_promotion_plans")
    }
    assert promotion_columns["target_runtime_revision_id"]["nullable"] is True
    assert promotion_columns["target_bridge_id"]["nullable"] is True
    engine.dispose()

    engine = create_database_engine(database_url)
    inspector = inspect(engine)
    engine.dispose()


def test_external_production_foundation_upgrade_and_reinitialization(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0001_initial_release"
    )
    engine = create_database_engine(database_url)
    preserved_id = "47000000-0000-4000-8000-000000000001"
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO targets (id,name,target_type,kodi_version,availability,"
                "health,connection_state) VALUES "
                "(:id,'preserved development','external_development',NULL,"
                "'unavailable','warning','disconnected')"
            ),
            {"id": preserved_id},
        )
    engine.dispose()

    command.upgrade(config, "head")
    engine = create_database_engine(database_url)
    inspector = inspect(engine)
    assert {
        "external_production_policies",
        "production_mutation_control",
    } <= set(inspector.get_table_names())
    with engine.begin() as connection:
        assert connection.execute(
            text("SELECT name,target_type,kodi_version FROM targets WHERE id=:id"),
            {"id": preserved_id},
        ).one() == ("preserved development", "external_development", None)
        assert connection.execute(
            text(
                "SELECT production_mutations_blocked,policy_epoch,updated_by "
                "FROM production_mutation_control WHERE id=1"
            )
        ).one() == (1, 1, "system")
        with pytest.raises(IntegrityError, match="target_type_immutable"):
            connection.execute(
                text(
                    "UPDATE targets SET target_type='external_production' WHERE id=:id"
                ),
                {"id": preserved_id},
            )
    development_id = str(uuid4())
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO targets (id,name,target_type,kodi_version,availability,"
                "health,connection_state) VALUES (:id,'Development policy binding "
                "proof','external_development',NULL,'unavailable','warning',"
                "'disconnected')"
            ),
            {"id": development_id},
        )
        connection.execute(
            text(
                "INSERT INTO external_kodi_targets (target_id,protocol_kind,host,port,"
                "authentication_mode,secret_reference,configured_capabilities_json,"
                "verified_capabilities_json,administrative_state,verification_state,"
                "observation_state,verified_addresses_json,verified_kodi_name,"
                "verified_kodi_version,observed_kodi_name,observed_kodi_version,"
                "observed_protocol_version,pending_observation_id,production_policy_revision,created_at,"
                "verified_at,deactivated_at,last_observed_at) VALUES "
                "(:id,'jsonrpc_http','192.168.50.19',8080,'none',NULL,"
                "'[\"target_inspect\"]','[]','disabled','unverified',NULL,'[]',"
                "NULL,NULL,NULL,NULL,NULL,NULL,NULL,'2026-09-21T00:00:00.000000Z',"
                "NULL,NULL,NULL)"
            ),
            {"id": development_id},
        )
    with (
        engine.begin() as connection,
        pytest.raises(
            IntegrityError, match="external_production_policy_class_required"
        ),
    ):
        connection.execute(
            text(
                "INSERT INTO external_production_policies "
                "(target_id,designation_state,confirmed_observation_id,"
                "confirmed_observation_digest,identity_generation,"
                "policy_revision,mutations_suspended,designation_confirmed_by,"
                "designation_confirmed_at,designation_invalidated_at,created_at,"
                "updated_at) VALUES (:id,'pending_native_identity',NULL,NULL,"
                "1,1,1,NULL,NULL,NULL,'2026-09-21T00:00:00.000000Z',"
                "'2026-09-21T00:00:00.000000Z')"
            ),
            {"id": development_id},
        )
    engine.dispose()

    engine = create_database_engine(database_url)
    engine.dispose()


def test_orders_production_parent_subtype_policy_on_initial_schema(
    migration_templates: MigrationTemplateStore, tmp_path: Path
) -> None:
    database_path = tmp_path / "production-head.db"
    migration_templates.clone("head", database_path)
    engine = create_database_engine(f"sqlite:///{database_path}")
    factory = create_session_factory(engine)
    service = ExternalTargetAdministrationService(
        factory,
        endpoint_policy=ExternalEndpointPolicy(allowed_ports=(8080,)),
        credential_store=ExternalCredentialStore(
            tmp_path / "production-secrets", master_key=None, key_version=1
        ),
        target_type=TargetType.EXTERNAL_PRODUCTION,
    )
    created = service.register(
        logical_name="Production ordering proof",
        protocol_kind=ExternalProtocolKind.JSONRPC_HTTP,
        host="192.168.50.20",
        port=8080,
        authentication_mode=ExternalAuthenticationMode.NONE,
        configured_capabilities={"target_inspect"},
    )
    assert created.target_class == "external_production"
    assert created.production_policy is not None
    with engine.connect() as connection:
        assert connection.execute(
            text(
                "SELECT t.target_type,e.target_id,p.identity_generation "
                "FROM targets t JOIN external_kodi_targets e ON e.target_id=t.id "
                "JOIN external_production_policies p ON p.target_id=t.id "
                "WHERE t.id=:id"
            ),
            {"id": str(created.target_id)},
        ).one() == ("external_production", str(created.target_id), 1)
    engine.dispose()


def test_production_inspection_scope_upgrade_and_reinitialization(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0001_initial_release"
    )
    command.upgrade(config, "head")
    engine = create_database_engine(database_url)
    inspector = inspect(engine)
    assert "mcp_production_target_scopes" in inspector.get_table_names()
    assert {"production_scope_id", "identity_generation"} <= {
        item["name"] for item in inspector.get_columns("mcp_security_events")
    }
    with engine.connect() as connection:
        assert connection.execute(
            text(
                "SELECT display_name,builtin,assignable FROM mcp_presets "
                "WHERE id='11000000-0000-4000-8000-000000000005'"
            )
        ).one() == ("Production Inspection", 1, 1)
        assert connection.execute(
            text(
                "SELECT permission FROM mcp_preset_permissions "
                "WHERE preset_id='11000000-0000-4000-8000-000000000005'"
            )
        ).one() == ("target.inspect",)
    engine.dispose()

    engine = create_database_engine(database_url)
    inspector = inspect(engine)
    engine.dispose()


def test_scope_constraints_are_generation_bound_and_history_is_immutable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="head"
    )
    engine = create_database_engine(database_url)
    target_id = "48000000-0000-4000-8000-000000000010"
    client_id = "48000000-0000-4000-8000-000000000011"
    first_scope = "48000000-0000-4000-8000-000000000012"
    second_scope = "48000000-0000-4000-8000-000000000013"
    timestamp = "2026-09-21T00:00:00.000000Z"
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO targets "
                "(id,name,target_type,kodi_version,availability,health,"
                "connection_state) "
                "VALUES (:id,'production','external_production',NULL,"
                "'unavailable','warning','disconnected')"
            ),
            {"id": target_id},
        )
        connection.execute(
            text(
                "INSERT INTO external_kodi_targets "
                "(target_id,protocol_kind,host,port,authentication_mode,"
                "secret_reference,configured_capabilities_json,"
                "verified_capabilities_json,administrative_state,"
                "verification_state,observation_state,verified_addresses_json,"
                "verified_kodi_name,verified_kodi_version,observed_kodi_name,"
                "observed_kodi_version,observed_protocol_version,"
                "pending_observation_id,production_policy_revision,created_at,"
                "verified_at,deactivated_at,"
                "last_observed_at) VALUES (:id,'jsonrpc_http','192.168.50.10',"
                "8080,'none',NULL,'[\"target_inspect\"]','[]','disabled',"
                "'unverified',NULL,'[]',NULL,NULL,NULL,NULL,NULL,NULL,1,:at,NULL,NULL,NULL)"
            ),
            {"id": target_id, "at": timestamp},
        )
        connection.execute(
            text(
                "INSERT INTO external_production_policies "
                "(target_id,designation_state,confirmed_observation_id,"
                "confirmed_observation_digest,identity_generation,policy_revision,"
                "mutations_suspended,designation_confirmed_by,"
                "designation_confirmed_at,designation_invalidated_at,created_at,"
                "updated_at) VALUES (:id,'pending_native_identity',NULL,NULL,1,1,"
                "1,NULL,NULL,NULL,:at,:at)"
            ),
            {"id": target_id, "at": timestamp},
        )
        connection.execute(
            text(
                "INSERT INTO mcp_clients "
                "(id,display_name,enabled,preset_id,created_at,"
                "last_used_at,disabled_at) "
                "VALUES (:id,'reader',1,"
                "'11000000-0000-4000-8000-000000000005',:at,NULL,NULL)"
            ),
            {"id": client_id, "at": timestamp},
        )
        scope_sql = text(
            "INSERT INTO mcp_production_target_scopes "
            "(id,client_id,target_id,identity_generation,scope,state,created_at,"
            "created_by,revoked_at,revoked_by) VALUES "
            "(:id,:client,:target,:generation,'inspect','active',:at,'admin',NULL,NULL)"
        )
        connection.execute(
            scope_sql,
            {
                "id": first_scope,
                "client": client_id,
                "target": target_id,
                "generation": 1,
                "at": timestamp,
            },
        )
        with pytest.raises(IntegrityError):
            connection.execute(
                scope_sql,
                {
                    "id": second_scope,
                    "client": client_id,
                    "target": target_id,
                    "generation": 1,
                    "at": timestamp,
                },
            )
        with pytest.raises(
            IntegrityError, match="production_inspection_scope_immutable"
        ):
            connection.execute(
                text(
                    "UPDATE mcp_production_target_scopes SET identity_generation=2 "
                    "WHERE id=:id"
                ),
                {"id": first_scope},
            )
        connection.execute(
            text(
                "UPDATE mcp_production_target_scopes SET state='revoked',"
                "revoked_at=:at,revoked_by='admin' WHERE id=:id"
            ),
            {"id": first_scope, "at": timestamp},
        )
        connection.execute(
            scope_sql,
            {
                "id": second_scope,
                "client": client_id,
                "target": target_id,
                "generation": 1,
                "at": timestamp,
            },
        )
        with pytest.raises(
            IntegrityError, match="production_inspection_scope_binding_invalid"
        ):
            connection.execute(
                scope_sql,
                {
                    "id": "48000000-0000-4000-8000-000000000014",
                    "client": client_id,
                    "target": target_id,
                    "generation": 2,
                    "at": timestamp,
                },
            )
        connection.execute(
            text(
                "UPDATE external_production_policies SET identity_generation=2,"
                "policy_revision=2 WHERE target_id=:id"
            ),
            {"id": target_id},
        )
        with pytest.raises(
            IntegrityError, match="production_identity_generation_not_monotonic"
        ):
            connection.execute(
                text(
                    "UPDATE external_production_policies SET identity_generation=1,"
                    "policy_revision=3 WHERE target_id=:id"
                ),
                {"id": target_id},
            )
        with pytest.raises(
            IntegrityError, match="production_inspection_scope_immutable"
        ):
            connection.execute(
                text("DELETE FROM mcp_production_target_scopes WHERE id=:id"),
                {"id": second_scope},
            )
    engine.dispose()


def test_production_bridge_visual_fresh_upgrade_and_history(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0001_initial_release"
    )
    command.upgrade(config, "head")
    engine = create_database_engine(database_url)
    inspector = inspect(engine)
    assert {
        "reviewed_bridge_builds",
        "production_bridge_http_acknowledgements",
        "mcp_production_screenshot_receipts",
    } <= set(inspector.get_table_names())
    assert "bridge_trust_revision" in {
        item["name"] for item in inspector.get_columns("mcp_production_target_scopes")
    }
    with engine.connect() as connection:
        assert (
            connection.scalar(text("SELECT COUNT(*) FROM reviewed_bridge_builds")) == 0
        )
        assert connection.execute(
            text(
                "SELECT display_name,builtin,assignable FROM mcp_presets "
                "WHERE id='11000000-0000-4000-8000-000000000006'"
            )
        ).one() == ("Production Visual Inspection", 1, 1)
        assert [
            row[0]
            for row in connection.execute(
                text(
                    "SELECT permission FROM mcp_preset_permissions "
                    "WHERE preset_id='11000000-0000-4000-8000-000000000006' "
                    "ORDER BY permission"
                )
            ).all()
        ] == ["target.inspect", "target.visual"]
    engine.dispose()


def test_bridge_revision_is_monotonic_and_visual_history_blocks_downgrade(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="head"
    )
    engine = create_database_engine(database_url)
    factory = create_session_factory(engine)
    service = ExternalTargetAdministrationService(
        factory,
        endpoint_policy=ExternalEndpointPolicy(allowed_ports=(8080,)),
        credential_store=ExternalCredentialStore(
            tmp_path / "visual-secrets", master_key=None, key_version=1
        ),
        target_type=TargetType.EXTERNAL_PRODUCTION,
    )
    target = service.register(
        logical_name="Production visual migration proof",
        protocol_kind=ExternalProtocolKind.JSONRPC_HTTP,
        host="192.168.50.30",
        port=8080,
        authentication_mode=ExternalAuthenticationMode.NONE,
        configured_capabilities={"target_inspect", "screenshot"},
    )
    target_id = str(target.target_id)
    client_id = "49000000-0000-4000-8000-000000000001"
    build_id = "49000000-0000-4000-8000-000000000002"
    timestamp = "2026-09-21T12:00:00.000000Z"
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO reviewed_bridge_builds "
                "(id,semantic_fingerprint,zip_sha256,addon_id,addon_version,"
                "protocol_version,capabilities_json,supported_kodi_versions_json,"
                "source_reference,reviewed_by,reviewed_at,state,retired_by,retired_at) "
                "VALUES (:id,:fingerprint,:zip,'service.kodi_lab_bridge','1.1.7','1',"
                "'[\"screenshot\"]','[\"21.3\"]','local review','admin',:at,"
                "'active',NULL,NULL)"
            ),
            {"id": build_id, "fingerprint": "a" * 64, "zip": "b" * 64, "at": timestamp},
        )
        connection.execute(
            text(
                "INSERT INTO external_bridge_pairings "
                "(target_id,bridge_port,pairing_state,reachability_state,"
                "pairing_token_reference,bridge_secret_reference,pairing_token_expires_at,"
                "observed_bridge_id,observed_protocol_version,observed_addon_id,"
                "observed_addon_version,observed_kodi_version,observed_build_fingerprint,"
                "observed_capabilities_json,pinned_bridge_id,pinned_protocol_version,"
                "pinned_addon_id,pinned_addon_version,pinned_kodi_version,"
                "pinned_build_fingerprint,verified_capabilities_json,endpoint_revision_digest,"
                "token_generated_at,pairing_attempted_at,paired_at,verified_at,revoked_at,"
                "production_identity_generation,production_bridge_trust_revision,"
                "reviewed_build_id,production_confirmed_by,production_confirmed_at) "
                "VALUES (:target,8090,'paired','reachable',NULL,:secret,NULL,"
                ":bridge,'1',"
                "'service.kodi_lab_bridge','1.1.7','21.3',:fingerprint,'[\"screenshot\"]',"
                ":bridge,'1','service.kodi_lab_bridge','1.1.7','21.3',:fingerprint,"
                "'[\"screenshot\"]',:digest,NULL,NULL,:at,:at,NULL,1,2,:build,'admin',:at)"
            ),
            {
                "target": target_id,
                "secret": "49000000-0000-4000-8000-000000000003",
                "bridge": "49000000-0000-4000-8000-000000000004",
                "fingerprint": "a" * 64,
                "digest": "c" * 64,
                "build": build_id,
                "at": timestamp,
            },
        )
        with pytest.raises(
            IntegrityError, match="production_bridge_trust_revision_not_monotonic"
        ):
            connection.execute(
                text(
                    "UPDATE external_bridge_pairings "
                    "SET production_bridge_trust_revision=1 WHERE target_id=:target"
                ),
                {"target": target_id},
            )
        with pytest.raises(
            IntegrityError, match="production_bridge_trust_revision_not_monotonic"
        ):
            connection.execute(
                text("DELETE FROM external_bridge_pairings WHERE target_id=:target"),
                {"target": target_id},
            )
        with pytest.raises(
            IntegrityError, match="production_bridge_trust_revision_not_monotonic"
        ):
            connection.execute(
                text(
                    "INSERT OR REPLACE INTO external_bridge_pairings "
                    "SELECT * FROM external_bridge_pairings WHERE target_id=:target"
                ),
                {"target": target_id},
            )
        with pytest.raises(IntegrityError):
            connection.execute(
                text(
                    "UPDATE external_bridge_pairings "
                    "SET production_bridge_trust_revision=0 WHERE target_id=:target"
                ),
                {"target": target_id},
            )
        connection.execute(
            text(
                "INSERT INTO mcp_clients "
                "(id,display_name,enabled,preset_id,created_at,last_used_at,"
                "disabled_at) "
                "VALUES (:id,'visual',1,'11000000-0000-4000-8000-000000000006',"
                ":at,NULL,NULL)"
            ),
            {"id": client_id, "at": timestamp},
        )
        connection.execute(
            text(
                "INSERT INTO mcp_production_target_scopes "
                "(id,client_id,target_id,identity_generation,bridge_trust_revision,"
                "scope,state,created_at,created_by,revoked_at,revoked_by) VALUES "
                "('49000000-0000-4000-8000-000000000005',:client,:target,1,NULL,"
                "'inspect','active',:at,'admin',NULL,NULL)"
            ),
            {"client": client_id, "target": target_id, "at": timestamp},
        )
        connection.execute(
            text(
                "INSERT INTO mcp_production_target_scopes "
                "(id,client_id,target_id,identity_generation,bridge_trust_revision,"
                "scope,state,created_at,created_by,revoked_at,revoked_by) VALUES "
                "('49000000-0000-4000-8000-000000000006',:client,:target,1,2,"
                "'visual','active',:at,'admin',NULL,NULL)"
            ),
            {"client": client_id, "target": target_id, "at": timestamp},
        )
        screenshot_request_id = "49000000-0000-4000-8000-000000000007"
        connection.execute(
            text(
                "INSERT INTO mcp_production_screenshot_receipts "
                "(client_id,request_id,target_id,parameter_digest,"
                "identity_generation,bridge_trust_revision,bridge_request_id,"
                "state,response_digest,byte_size,width,height,created_at,"
                "completed_at,expires_at) VALUES "
                "(:client,:request,:target,:digest,1,2,:request,'pending',"
                "NULL,NULL,NULL,NULL,:at,NULL,'2099-09-21T12:00:00.000000Z')"
            ),
            {
                "client": client_id,
                "request": screenshot_request_id,
                "target": target_id,
                "digest": "d" * 64,
                "at": timestamp,
            },
        )
        with pytest.raises(
            IntegrityError, match="production_screenshot_receipt_protected"
        ):
            connection.execute(
                text(
                    "DELETE FROM mcp_production_screenshot_receipts "
                    "WHERE client_id=:client AND request_id=:request"
                ),
                {"client": client_id, "request": screenshot_request_id},
            )
    engine.dispose()


def test_navigation_schema_preset_and_protected_history(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0001_initial_release"
    )
    command.upgrade(config, "head")
    engine = create_database_engine(database_url)
    assert {
        "production_native_control_http_acknowledgements",
        "mcp_production_remote_receipt_bindings",
        "mcp_production_navigation_audits",
    } <= set(inspect(engine).get_table_names())
    timestamp = "2026-09-21T12:00:00.000000Z"
    receipt_id = "50000000-0000-4000-8000-000000000001"
    ids = {
        "receipt": receipt_id,
        "client": "50000000-0000-4000-8000-000000000002",
        "credential": "50000000-0000-4000-8000-000000000003",
        "target": "50000000-0000-4000-8000-000000000004",
        "request": "50000000-0000-4000-8000-000000000005",
        "inspect": "50000000-0000-4000-8000-000000000006",
        "navigate": "50000000-0000-4000-8000-000000000007",
        "at": timestamp,
    }
    with engine.begin() as connection:
        assert connection.execute(
            text(
                "SELECT display_name,builtin,assignable FROM mcp_presets "
                "WHERE id='11000000-0000-4000-8000-000000000004'"
            )
        ).one() == ("Production Remote Kodi", 1, 1)
        assert [
            row[0]
            for row in connection.execute(
                text(
                    "SELECT permission FROM mcp_preset_permissions "
                    "WHERE preset_id='11000000-0000-4000-8000-000000000004' "
                    "ORDER BY permission"
                )
            ).all()
        ] == ["target.control", "target.inspect", "target.visual"]
        connection.execute(
            text(
                "INSERT INTO mcp_remote_action_receipts "
                "(id,client_id,credential_id,target_id,request_id,action,created_at,"
                "state,final_result,completed_at,screenshot_refresh_result,"
                "screenshot_capture_id,screenshot_captured_at) VALUES "
                "(:receipt,:client,:credential,:target,:request,'home',:at,'pending',"
                "NULL,NULL,NULL,NULL,NULL)"
            ),
            ids,
        )
        connection.execute(
            text(
                "INSERT INTO mcp_production_remote_receipt_bindings (receipt_"
                "id,operation_class,native_identity_generation,bridge_trust_r"
                "evision,target_policy_revision,mutation_control_epoch,inspec"
                "t_scope_id,navigate_scope_id,client_preset_id,target_preset_"
                "id,generic_grant_created_at,native_http_acknowledgement_id,g"
                "eneric_grant_id) VALUES (:receipt,'navigation',1,1,1,1,:insp"
                "ect,:navigate,'11000000-0000-4000-8000-000000000004','110000"
                "00-0000-4000-8000-000000000004',:at,NULL,'a0000000-0000-4000"
                "-8000-000000000001')"
            ),
            ids,
        )
        connection.execute(
            text(
                "INSERT INTO mcp_production_navigation_audits "
                "(receipt_id,client_id,credential_id,target_id,request_id,action,"
                "operation_class,native_identity_generation,bridge_trust_revision,"
                "target_policy_revision,mutation_control_epoch,inspect_scope_id,"
                "navigate_scope_id,state,outcome,dispatch_classification,requested_at,"
                "completed_at) VALUES (:receipt,:client,:credential,:target,:request,"
                "'home','navigation',1,1,1,1,:inspect,:navigate,'pending','pending',"
                "'not_dispatched',:at,NULL)"
            ),
            ids,
        )
        with pytest.raises(IntegrityError, match="production_remote_binding_protected"):
            connection.execute(
                text(
                    "DELETE FROM mcp_production_remote_receipt_bindings "
                    "WHERE receipt_id=:receipt"
                ),
                ids,
            )
        with pytest.raises(IntegrityError, match="production_remote_receipt_protected"):
            connection.execute(
                text("DELETE FROM mcp_remote_action_receipts WHERE id=:receipt"), ids
            )
    engine.dispose()


def test_allows_atomic_cleanup_only_for_coherent_terminal_navigation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0001_initial_release"
    )
    command.upgrade(config, "head")
    engine = create_database_engine(database_url)
    ids = {
        "receipt": "51000000-0000-4000-8000-000000000001",
        "client": "51000000-0000-4000-8000-000000000002",
        "credential": "51000000-0000-4000-8000-000000000003",
        "target": "51000000-0000-4000-8000-000000000004",
        "request": "51000000-0000-4000-8000-000000000005",
        "inspect": "51000000-0000-4000-8000-000000000006",
        "navigate": "51000000-0000-4000-8000-000000000007",
        "at": "2026-08-01T12:00:00.000000Z",
        "done": "2026-08-01T12:00:01.000000Z",
    }
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO mcp_remote_action_receipts "
                "(id,client_id,credential_id,target_id,request_id,action,created_at,"
                "state,final_result,completed_at,screenshot_refresh_result,"
                "screenshot_capture_id,screenshot_captured_at) VALUES "
                "(:receipt,:client,:credential,:target,:request,'home',:at,'completed',"
                "'succeeded',:done,'not_requested',NULL,NULL)"
            ),
            ids,
        )
        connection.execute(
            text(
                "INSERT INTO mcp_production_remote_receipt_bindings (receipt_"
                "id,operation_class,native_identity_generation,bridge_trust_r"
                "evision,target_policy_revision,mutation_control_epoch,inspec"
                "t_scope_id,navigate_scope_id,client_preset_id,target_preset_"
                "id,generic_grant_created_at,native_http_acknowledgement_id,g"
                "eneric_grant_id) VALUES (:receipt,'navigation',1,1,1,1,:insp"
                "ect,:navigate,'11000000-0000-4000-8000-000000000004','110000"
                "00-0000-4000-8000-000000000004',:at,NULL,'a0000000-0000-4000"
                "-8000-000000000001')"
            ),
            ids,
        )
        connection.execute(
            text(
                "INSERT INTO mcp_production_navigation_audits "
                "(receipt_id,client_id,credential_id,target_id,request_id,action,"
                "operation_class,native_identity_generation,bridge_trust_revision,"
                "target_policy_revision,mutation_control_epoch,inspect_scope_id,"
                "navigate_scope_id,state,outcome,dispatch_classification,requested_at,"
                "completed_at) VALUES (:receipt,:client,:credential,:target,:request,"
                "'home','navigation',1,1,1,1,:inspect,:navigate,'completed','succeeded',"
                "'acknowledged',:at,:done)"
            ),
            ids,
        )
        with pytest.raises(IntegrityError, match="production_remote_receipt_protected"):
            connection.execute(
                text("DELETE FROM mcp_remote_action_receipts WHERE id=:receipt"), ids
            )
        connection.execute(
            text(
                "DELETE FROM mcp_production_remote_receipt_bindings "
                "WHERE receipt_id=:receipt"
            ),
            ids,
        )
        connection.execute(
            text(
                "DELETE FROM mcp_production_navigation_audits WHERE receipt_id=:receipt"
            ),
            ids,
        )
        connection.execute(
            text("DELETE FROM mcp_remote_action_receipts WHERE id=:receipt"), ids
        )
        assert (
            connection.scalar(
                text(
                    "SELECT COUNT(*) FROM mcp_remote_action_receipts WHERE id=:receipt"
                ),
                ids,
            )
            == 0
        )
    engine.dispose()


def test_preserves_navigation_and_assigns_safe_grant_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0001_initial_release"
    )
    engine = create_database_engine(database_url)
    ids = {
        "client": "52000000-0000-4000-8000-000000000001",
        "target": "52000000-0000-4000-8000-000000000002",
        "credential": "52000000-0000-4000-8000-000000000003",
        "request": "52000000-0000-4000-8000-000000000004",
        "receipt": "52000000-0000-4000-8000-000000000005",
        "inspect": "52000000-0000-4000-8000-000000000006",
        "navigate": "52000000-0000-4000-8000-000000000007",
        "approval": "52000000-0000-4000-8000-000000000008",
        "transient_receipt": "52000000-0000-4000-8000-000000000009",
        "transient_request": "52000000-0000-4000-8000-000000000010",
        "transient": "52000000-0000-4000-8000-000000000011",
        "requested_event": "52000000-0000-4000-8000-000000000012",
        "approved_event": "52000000-0000-4000-8000-000000000013",
        "consumed_event": "52000000-0000-4000-8000-000000000014",
        "reviewed_build": "52000000-0000-4000-8000-000000000015",
        "at": "2026-09-21T12:00:00.000000Z",
        "pending_expiry": "2026-09-21T12:15:00.000000Z",
        "dispatch_expiry": "2026-09-21T12:05:00.000000Z",
    }
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO targets (id,name,target_type,kodi_version,availability,"
                "health,connection_state) VALUES (:target,'legacy navigation',"
                "'external_production','21.3','available','healthy','connected')"
            ),
            ids,
        )
        connection.execute(
            text(
                "INSERT INTO mcp_clients (id,display_name,enabled,preset_id,created_at,"
                "last_used_at,disabled_at) VALUES (:client,'legacy client',1,"
                "'11000000-0000-4000-8000-000000000004',:at,NULL,NULL)"
            ),
            ids,
        )
        connection.execute(
            text(
                "INSERT INTO external_kodi_targets (target_id,protocol_kind,host,port,"
                "authentication_mode,secret_reference,configured_capabilities_json,"
                "verified_capabilities_json,administrative_state,verification_state,"
                "observation_state,verified_addresses_json,verified_kodi_name,"
                "verified_kodi_version,observed_kodi_name,observed_kodi_version,"
                "observed_protocol_version,pending_observation_id,production_policy_revision,created_at,"
                "verified_at,deactivated_at,last_observed_at) VALUES "
                "(:target,'jsonrpc_http','192.168.50.52',8080,'none',NULL,"
                "'[\"target_inspect\",\"semantic_remote\"]','[]','disabled',"
                "'unverified',NULL,'[]',NULL,NULL,NULL,NULL,NULL,NULL,1,:at,NULL,NULL,NULL)"
            ),
            ids,
        )
        connection.execute(
            text(
                "INSERT INTO external_production_policies (target_id,"
                "designation_state,confirmed_observation_id,"
                "confirmed_observation_digest,identity_generation,policy_revision,"
                "mutations_suspended,designation_confirmed_by,"
                "designation_confirmed_at,designation_invalidated_at,created_at,"
                "updated_at) VALUES (:target,'pending_native_identity',NULL,NULL,"
                "1,1,1,NULL,NULL,NULL,:at,:at)"
            ),
            ids,
        )
        connection.execute(
            text(
                "INSERT INTO mcp_client_target_grants (client_id,target_id,cr"
                "eated_at,grant_id) "
                "VALUES (:client,:target,:at,'a0000000-0000-4000-8000-000000000001')"
            ),
            ids,
        )
        connection.execute(
            text(
                "INSERT INTO mcp_remote_action_receipts (id,client_id,credential_id,"
                "target_id,request_id,action,created_at,state,final_result,completed_at,"
                "screenshot_refresh_result,screenshot_capture_id,"
                "screenshot_captured_at) "
                "VALUES (:receipt,:client,:credential,:target,:request,'home',:at,"
                "'pending',NULL,NULL,NULL,NULL,NULL)"
            ),
            ids,
        )
        connection.execute(
            text(
                "INSERT INTO mcp_production_remote_receipt_bindings (receipt_"
                "id,operation_class,native_identity_generation,bridge_trust_r"
                "evision,target_policy_revision,mutation_control_epoch,inspec"
                "t_scope_id,navigate_scope_id,client_preset_id,target_preset_"
                "id,generic_grant_created_at,native_http_acknowledgement_id,g"
                "eneric_grant_id) VALUES (:receipt,'navigation',1,1,1,1,:insp"
                "ect,:navigate,'11000000-0000-4000-8000-000000000004','110000"
                "00-0000-4000-8000-000000000004',:at,NULL,'a0000000-0000-4000"
                "-8000-000000000001')"
            ),
            ids,
        )
    engine.dispose()

    command.upgrade(config, "head")
    engine = create_database_engine(database_url)
    with engine.begin() as connection:
        grant_id = connection.scalar(
            text(
                "SELECT grant_id FROM mcp_client_target_grants "
                "WHERE client_id=:client AND target_id=:target"
            ),
            ids,
        )
        assert isinstance(grant_id, str) and len(grant_id) == 36
        assert connection.execute(
            text("SELECT authority_revision FROM mcp_clients WHERE id=:client"),
            ids,
        ).one() == (1,)
        assert connection.execute(
            text(
                "SELECT operation_class,navigate_scope_id,transient_scope_id,"
                "approval_id,generic_grant_created_at,generic_grant_id "
                "FROM mcp_production_remote_receipt_bindings WHERE receipt_id=:receipt"
            ),
            ids,
        ).one() == ("navigation", ids["navigate"], None, None, ids["at"], grant_id)
        assert connection.execute(
            text(
                "SELECT action,state,created_at FROM mcp_remote_action_receipts "
                "WHERE id=:receipt"
            ),
            ids,
        ).one() == ("home", "pending", ids["at"])
        with pytest.raises(
            IntegrityError, match="mcp_client_authority_revision_invalid"
        ):
            connection.execute(
                text(
                    "UPDATE mcp_clients SET enabled=0,disabled_at=:at WHERE id=:client"
                ),
                ids,
            )
        with pytest.raises(IntegrityError, match="production_policy_revision_required"):
            connection.execute(
                text(
                    "UPDATE external_production_policies SET mutations_suspended=0 "
                    "WHERE target_id=:target"
                ),
                ids,
            )
        connection.execute(
            text(
                "UPDATE external_production_policies SET mutations_suspended=0,"
                "policy_revision=2 WHERE target_id=:target"
            ),
            ids,
        )
        with pytest.raises(
            IntegrityError, match="production_policy_revision_not_monotonic"
        ):
            connection.execute(
                text(
                    "UPDATE external_production_policies SET policy_revision=1 "
                    "WHERE target_id=:target"
                ),
                ids,
            )
        with pytest.raises(IntegrityError, match="production_mutation_epoch_invalid"):
            connection.execute(
                text(
                    "UPDATE production_mutation_control "
                    "SET production_mutations_blocked=0 WHERE id=1"
                )
            )
        connection.execute(
            text(
                "UPDATE production_mutation_control SET production_mutations_blocked=0,"
                "policy_epoch=2 WHERE id=1"
            )
        )
        with pytest.raises(IntegrityError, match="production_mutation_epoch_invalid"):
            connection.execute(
                text(
                    "UPDATE production_mutation_control "
                    "SET production_mutations_blocked=1,policy_epoch=1 WHERE id=1"
                )
            )
        connection.execute(
            text(
                "UPDATE mcp_clients SET enabled=0,disabled_at=:at,authority_revision=2 "
                "WHERE id=:client"
            ),
            ids,
        )
        with pytest.raises(
            IntegrityError, match="mcp_client_authority_revision_invalid"
        ):
            connection.execute(
                text(
                    "UPDATE mcp_clients SET enabled=1,disabled_at=NULL,"
                    "authority_revision=1 "
                    "WHERE id=:client"
                ),
                ids,
            )
        assert connection.execute(
            text(
                "SELECT client_id,target_id,created_at FROM "
                "mcp_client_target_grant_identities WHERE grant_id=:grant_id"
            ),
            {**ids, "grant_id": grant_id},
        ).one() == (ids["client"], ids["target"], ids["at"])
        connection.execute(
            text(
                "INSERT INTO production_operation_approvals "
                "(id,client_id,credential_id,target_id,tool,action,payload_sha256,"
                "canonicalization_revision,request_id,native_identity_generation,"
                "bridge_trust_revision,target_policy_revision,mutation_control_epoch,"
                "client_authority_revision,generic_grant_id,client_preset_id,"
                "target_preset_id,inspect_scope_id,transient_scope_id,"
                "native_http_acknowledgement_id,reviewed_build_id,state,"
                "terminal_reason,created_at,pending_expires_at,approved_at,"
                "approved_dispatch_expires_at,approving_administrator,consumed_at,"
                "linked_remote_receipt_id) VALUES "
                "(:approval,:client,:credential,:target,'remote_action','play_pause',"
                "'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',"
                "1,:transient_request,1,1,2,2,2,:grant_id,"
                "'11000000-0000-4000-8000-000000000004',"
                "'11000000-0000-4000-8000-000000000004',:inspect,:transient,NULL,"
                ":reviewed_build,'approved',NULL,:at,:pending_expiry,:at,"
                ":dispatch_expiry,'auditor',NULL,NULL)"
            ),
            {**ids, "grant_id": grant_id},
        )
        connection.execute(
            text(
                "INSERT INTO production_approval_transition_events "
                "(id,approval_id,event_kind,from_state,to_state,terminal_reason,"
                "actor_kind,actor_reference,linked_remote_receipt_id,occurred_at) "
                "VALUES (:requested_event,:approval,'requested',NULL,'pending',NULL,"
                "'mcp_client',:client,NULL,:at),"
                "(:approved_event,:approval,'approved','pending','approved',NULL,"
                "'web_admin','auditor',NULL,:at)"
            ),
            ids,
        )
        connection.execute(
            text(
                "INSERT INTO mcp_remote_action_receipts "
                "(id,client_id,credential_id,target_id,request_id,action,created_at,"
                "state,final_result,completed_at,screenshot_refresh_result,"
                "screenshot_capture_id,screenshot_captured_at) VALUES "
                "(:transient_receipt,:client,:credential,:target,:transient_request,"
                "'play_pause',:at,'pending',NULL,NULL,NULL,NULL,NULL)"
            ),
            ids,
        )
        connection.execute(
            text(
                "INSERT INTO mcp_production_remote_receipt_bindings "
                "(receipt_id,operation_class,native_identity_generation,"
                "bridge_trust_revision,target_policy_revision,mutation_control_epoch,"
                "inspect_scope_id,navigate_scope_id,transient_scope_id,approval_id,"
                "client_preset_id,target_preset_id,generic_grant_created_at,"
                "generic_grant_id,native_http_acknowledgement_id) VALUES "
                "(:transient_receipt,'transient_control',1,1,2,2,:inspect,NULL,"
                ":transient,:approval,'11000000-0000-4000-8000-000000000004',"
                "'11000000-0000-4000-8000-000000000004',:at,:grant_id,NULL)"
            ),
            {**ids, "grant_id": grant_id},
        )
        connection.execute(
            text(
                "INSERT INTO production_transient_dispatch_audits "
                "(receipt_id,approval_id,client_id,credential_id,target_id,request_id,"
                "action,payload_sha256,canonicalization_revision,state,outcome,"
                "dispatch_classification,requested_at,completed_at) VALUES "
                "(:transient_receipt,:approval,:client,:credential,:target,"
                ":transient_request,'play_pause',"
                "'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',"
                "1,'pending','pending','not_dispatched',:at,NULL)"
            ),
            ids,
        )
        connection.execute(
            text(
                "UPDATE production_operation_approvals SET state='consumed',"
                "consumed_at=:at,linked_remote_receipt_id=:transient_receipt "
                "WHERE id=:approval"
            ),
            ids,
        )
        connection.execute(
            text(
                "INSERT INTO production_approval_transition_events "
                "(id,approval_id,event_kind,from_state,to_state,terminal_reason,"
                "actor_kind,actor_reference,linked_remote_receipt_id,occurred_at) "
                "VALUES (:consumed_event,:approval,'consumed','approved','consumed',"
                "NULL,'system','atomic_consumer',:transient_receipt,:at)"
            ),
            ids,
        )
        for statement, error in (
            (
                "DELETE FROM production_operation_approvals WHERE id=:approval",
                "production_approval_protected",
            ),
            (
                "UPDATE production_operation_approvals SET id=:reviewed_build "
                "WHERE id=:approval",
                "production_approval_transition_invalid",
            ),
            (
                "UPDATE production_operation_approvals "
                "SET linked_remote_receipt_id=:reviewed_build WHERE id=:approval",
                "production_approval_transition_invalid",
            ),
            (
                "DELETE FROM mcp_remote_action_receipts WHERE id=:transient_receipt",
                "production_remote_receipt_protected",
            ),
            (
                "UPDATE mcp_production_remote_receipt_bindings "
                "SET approval_id=:reviewed_build WHERE receipt_id=:transient_receipt",
                "production_remote_binding_immutable",
            ),
        ):
            with pytest.raises(IntegrityError, match=error):
                connection.execute(text(statement), ids)
    with engine.begin() as connection:
        connection.execute(
            text(
                "DELETE FROM mcp_client_target_grants "
                "WHERE client_id=:client AND target_id=:target"
            ),
            ids,
        )
    with (
        pytest.raises(IntegrityError, match="UNIQUE constraint failed"),
        engine.begin() as connection,
    ):
        connection.execute(
            text(
                "INSERT INTO mcp_client_target_grants "
                "(client_id,target_id,grant_id,created_at) "
                "VALUES (:client,:target,:grant_id,:at)"
            ),
            {**ids, "grant_id": grant_id},
        )
    with (
        pytest.raises(IntegrityError, match="production_mutation_epoch_invalid"),
        engine.begin() as connection,
    ):
        connection.execute(text("DELETE FROM production_mutation_control WHERE id=1"))
    with engine.begin() as connection:
        before = connection.scalar(
            text(
                "SELECT policy_revision FROM external_production_policies "
                "WHERE target_id=:target"
            ),
            ids,
        )
        assert isinstance(before, int)
        connection.execute(
            text(
                "UPDATE external_production_policies SET policy_revision=poli"
                "cy_revision+1 WHERE target_id=:target"
            ),
            ids,
        )
        connection.execute(
            text(
                "UPDATE external_kodi_targets SET administrative_state='enabl"
                "ed',production_policy_revision=(SELECT policy_revision FROM "
                "external_production_policies WHERE target_id=:target) "
                "WHERE target_id=:target"
            ),
            ids,
        )
        after_enable = connection.scalar(
            text(
                "SELECT policy_revision FROM external_production_policies "
                "WHERE target_id=:target"
            ),
            ids,
        )
        assert after_enable == before + 1
        connection.execute(
            text(
                "INSERT INTO mcp_target_preset_assignments "
                "(target_id,preset_id,assigned_at) VALUES "
                "(:target,'11000000-0000-4000-8000-000000000004',:at)"
            ),
            ids,
        )
        after_assignment = connection.scalar(
            text(
                "SELECT policy_revision FROM external_production_policies "
                "WHERE target_id=:target"
            ),
            ids,
        )
        assert after_assignment == after_enable + 1
        connection.execute(
            text("DELETE FROM mcp_target_preset_assignments WHERE target_id=:target"),
            ids,
        )
        after_removal = connection.scalar(
            text(
                "SELECT policy_revision FROM external_production_policies "
                "WHERE target_id=:target"
            ),
            ids,
        )
        assert after_removal == after_assignment + 1
    engine.dispose()


def test_stage_a_reverification_publication_is_atomic_and_exact(
    migration_templates: MigrationTemplateStore, tmp_path: Path
) -> None:
    database_path = tmp_path / "stage-a-copy.db"
    migration_templates.clone("head", database_path)
    engine = create_database_engine(f"sqlite:///{database_path}")
    factory = create_session_factory(engine)
    target_id = UUID("de8cefbd-1179-4978-a687-108e82f5b893")
    at = "2026-09-22T20:00:00.000000Z"
    values = {"target": str(target_id), "at": at}
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO targets (id,name,target_type,kodi_version,availability,"
                "health,connection_state) VALUES (:target,'Production Kodi 19.5',"
                "'external_production','19.5','unavailable','warning','disconnected')"
            ),
            values,
        )
        connection.execute(
            text(
                "INSERT INTO external_kodi_targets (target_id,protocol_kind,host,port,"
                "authentication_mode,secret_reference,configured_capabilities_json,"
                "verified_capabilities_json,administrative_state,verification_state,"
                "observation_state,verified_addresses_json,verified_kodi_name,"
                "verified_kodi_version,observed_kodi_name,observed_kodi_version,"
                "observed_protocol_version,pending_observation_id,"
                "production_policy_revision,created_at,verified_at,deactivated_at,"
                "last_observed_at) VALUES (:target,'jsonrpc_http','192.168.50.135',"
                "8080,'none',NULL,'[\"target_inspect\"]','[]','disabled',"
                "'needs_reverification',NULL,'[]','Kodi','19.5','Kodi','19.5',"
                "'13.0.0',NULL,3,:at,NULL,NULL,:at)"
            ),
            values,
        )
        connection.execute(
            text(
                "INSERT INTO external_production_policies (target_id,"
                "designation_state,confirmed_observation_id,"
                "confirmed_observation_digest,identity_generation,policy_revision,"
                "mutations_suspended,designation_confirmed_by,"
                "designation_confirmed_at,designation_invalidated_at,created_at,"
                "updated_at) VALUES (:target,'invalidated',NULL,NULL,2,3,1,NULL,NULL,"
                ":at,:at,:at)"
            ),
            values,
        )

    class SuccessfulObservation:
        def probe(
            self,
            endpoint: ValidatedExternalEndpoint,
            credential: object,
            configured: frozenset[str],
        ) -> ExternalKodiProbeResult:
            return ExternalKodiProbeResult(
                addresses=(endpoint.address,),
                kodi_name="Kodi",
                kodi_version="19.5",
                protocol_version="13.0.0",
                verified_capabilities=tuple(sorted(configured)),
            )

    service = ExternalTargetAdministrationService(
        factory,
        endpoint_policy=ExternalEndpointPolicy(allowed_ports=(8080,)),
        credential_store=ExternalCredentialStore(
            tmp_path / "stage-a-secrets", master_key=None, key_version=1
        ),
        verifier=SuccessfulObservation(),  # type: ignore[arg-type]
        target_type=TargetType.EXTERNAL_PRODUCTION,
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TRIGGER inject_stage_a_publication_failure BEFORE UPDATE ON "
                "external_kodi_targets WHEN NEW.target_id='"
                "de8cefbd-1179-4978-a687-108e82f5b893' BEGIN SELECT "
                "RAISE(ABORT,'injected_publication_failure'); END"
            )
        )
    with pytest.raises(IntegrityError, match="injected_publication_failure"):
        service.probe(target_id)
    with engine.connect() as connection:
        assert connection.execute(
            text(
                "SELECT p.identity_generation,p.policy_revision,p.designation_state,"
                "e.verification_state,e.administrative_state,e.pending_observation_id "
                "FROM external_production_policies p JOIN external_kodi_targets e "
                "ON e.target_id=p.target_id WHERE p.target_id=:target"
            ),
            values,
        ).one() == (2, 3, "invalidated", "needs_reverification", "disabled", None)
        assert (
            connection.scalar(
                text(
                    "SELECT COUNT(*) FROM external_kodi_target_observations "
                    "WHERE target_id=:target"
                ),
                values,
            )
            == 0
        )
        assert (
            connection.scalar(
                text(
                    "SELECT COUNT(*) FROM external_kodi_target_events WHERE "
                    "target_id=:target AND event_kind='probe_attempted'"
                ),
                values,
            )
            == 1
        )
    with engine.begin() as connection:
        connection.execute(text("DROP TRIGGER inject_stage_a_publication_failure"))

    observed = service.probe(target_id)
    assert observed.pending_observation_id is not None
    assert observed.administrative_state.value == "disabled"
    assert observed.verification_state.value == "needs_reverification"
    assert observed.production_policy is not None
    assert (
        observed.production_policy.designation_state.value == "pending_native_identity"
    )
    assert observed.production_policy.identity_generation == 2
    assert observed.production_policy.policy_revision == 4
    assert observed.production_policy.mutations_suspended is True
    with engine.connect() as connection:
        assert (
            connection.scalar(
                text(
                    "SELECT COUNT(*) FROM external_kodi_target_observations "
                    "WHERE target_id=:target"
                ),
                values,
            )
            == 1
        )
        for table in (
            "mcp_client_target_grants",
            "mcp_target_preset_assignments",
            "mcp_production_target_scopes",
            "external_bridge_pairings",
        ):
            assert (
                connection.scalar(
                    text(f"SELECT COUNT(*) FROM {table} WHERE target_id=:target"),
                    values,
                )
                == 0
            )
        assert connection.execute(
            text(
                "SELECT production_mutations_blocked,policy_epoch FROM "
                "production_mutation_control WHERE id=1"
            )
        ).one() == (1, 1)
    engine.dispose()


def test_direct_state_guards_reject_revision_bypass_and_recreation(
    migration_templates: MigrationTemplateStore, tmp_path: Path
) -> None:
    database_path = tmp_path / "revision-guards.db"
    migration_templates.clone("head", database_path)
    engine = create_database_engine(f"sqlite:///{database_path}")
    values = {
        "target": "54000000-0000-4000-8000-000000000001",
        "at": "2026-09-22T20:00:00.000000Z",
    }
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO targets (id,name,target_type,kodi_version,availability,"
                "health,connection_state) VALUES (:target,'guarded production',"
                "'external_production',NULL,'unavailable','warning','disconnected')"
            ),
            values,
        )
        connection.execute(
            text(
                "INSERT INTO external_kodi_targets (target_id,protocol_kind,host,port,"
                "authentication_mode,secret_reference,configured_capabilities_json,"
                "verified_capabilities_json,administrative_state,verification_state,"
                "observation_state,verified_addresses_json,verified_kodi_name,"
                "verified_kodi_version,observed_kodi_name,observed_kodi_version,"
                "observed_protocol_version,pending_observation_id,"
                "production_policy_revision,created_at,verified_at,deactivated_at,"
                "last_observed_at) VALUES (:target,'jsonrpc_http','192.168.50.54',"
                "8080,'none',NULL,'[\"target_inspect\"]','[]','disabled',"
                "'unverified',NULL,'[]',NULL,NULL,NULL,NULL,NULL,NULL,3,:at,NULL,NULL,NULL)"
            ),
            values,
        )
        connection.execute(
            text(
                "INSERT INTO external_production_policies (target_id,"
                "designation_state,confirmed_observation_id,"
                "confirmed_observation_digest,identity_generation,policy_revision,"
                "mutations_suspended,designation_confirmed_by,"
                "designation_confirmed_at,designation_invalidated_at,created_at,"
                "updated_at) VALUES (:target,'invalidated',NULL,NULL,2,3,1,NULL,NULL,"
                ":at,:at,:at)"
            ),
            values,
        )
        with pytest.raises(IntegrityError, match="production_policy_revision_required"):
            connection.execute(
                text(
                    "UPDATE external_production_policies SET designation_state="
                    "'pending_native_identity' WHERE target_id=:target"
                ),
                values,
            )
        with pytest.raises(IntegrityError, match="production_policy_revision_required"):
            connection.execute(
                text(
                    "UPDATE external_production_policies SET designation_state="
                    "'pending_native_identity',policy_revision=5 "
                    "WHERE target_id=:target"
                ),
                values,
            )
        with pytest.raises(
            IntegrityError, match="production_policy_revision_not_monotonic"
        ):
            connection.execute(
                text(
                    "UPDATE external_production_policies SET policy_revision=2 "
                    "WHERE target_id=:target"
                ),
                values,
            )
        with pytest.raises(IntegrityError, match="production_policy_revision_required"):
            connection.execute(
                text(
                    "UPDATE external_kodi_targets SET administrative_state='enabled' "
                    "WHERE target_id=:target"
                ),
                values,
            )
        with pytest.raises(
            IntegrityError, match="production_policy_revision_not_monotonic"
        ):
            connection.execute(
                text(
                    "INSERT OR REPLACE INTO external_kodi_targets (target_id,"
                    "protocol_kind,host,port,authentication_mode,secret_reference,"
                    "configured_capabilities_json,verified_capabilities_json,"
                    "administrative_state,verification_state,observation_state,"
                    "verified_addresses_json,verified_kodi_name,verified_kodi_version,"
                    "observed_kodi_name,observed_kodi_version,"
                    "observed_protocol_version,pending_observation_id,"
                    "production_policy_revision,created_at,verified_at,deactivated_at,"
                    "last_observed_at) SELECT target_id,"
                    "protocol_kind,host,port,authentication_mode,secret_reference,"
                    "configured_capabilities_json,verified_capabilities_json,"
                    "administrative_state,verification_state,observation_state,"
                    "verified_addresses_json,verified_kodi_name,verified_kodi_version,"
                    "observed_kodi_name,observed_kodi_version,"
                    "observed_protocol_version,pending_observation_id,2,created_at,"
                    "verified_at,deactivated_at,last_observed_at FROM "
                    "external_kodi_targets WHERE target_id=:target"
                ),
                values,
            )
        with pytest.raises(
            IntegrityError, match="production_policy_revision_not_monotonic"
        ):
            connection.execute(
                text(
                    "INSERT OR REPLACE INTO external_production_policies "
                    "(target_id,designation_state,confirmed_observation_id,"
                    "confirmed_observation_digest,identity_generation,policy_revision,"
                    "mutations_suspended,designation_confirmed_by,"
                    "designation_confirmed_at,designation_invalidated_at,created_at,"
                    "updated_at) SELECT "
                    "target_id,designation_state,confirmed_observation_id,"
                    "confirmed_observation_digest,identity_generation,2,"
                    "mutations_suspended,designation_confirmed_by,"
                    "designation_confirmed_at,designation_invalidated_at,created_at,"
                    "updated_at FROM external_production_policies WHERE "
                    "target_id=:target"
                ),
                values,
            )
        with pytest.raises(
            IntegrityError, match="production_policy_revision_not_monotonic"
        ):
            connection.execute(
                text(
                    "INSERT OR REPLACE INTO "
                    "external_production_policy_revision_guards "
                    "(target_id,maximum_policy_revision) VALUES (:target,2)"
                ),
                values,
            )
        assert connection.execute(
            text(
                "SELECT p.policy_revision,g.maximum_policy_revision FROM "
                "external_production_policies p JOIN "
                "external_production_policy_revision_guards g "
                "ON g.target_id=p.target_id WHERE p.target_id=:target"
            ),
            values,
        ).one() == (3, 3)
        with connection.begin_nested() as nested:
            connection.execute(
                text(
                    "UPDATE external_production_policies SET designation_state="
                    "'pending_native_identity',policy_revision=4 "
                    "WHERE target_id=:target"
                ),
                values,
            )
            assert connection.execute(
                text(
                    "SELECT p.policy_revision,e.production_policy_revision,"
                    "g.maximum_policy_revision FROM external_production_policies p "
                    "JOIN external_kodi_targets e ON e.target_id=p.target_id "
                    "JOIN external_production_policy_revision_guards g "
                    "ON g.target_id=p.target_id WHERE p.target_id=:target"
                ),
                values,
            ).one() == (4, 3, 4)
            with pytest.raises(
                IntegrityError, match="production_policy_revision_required"
            ):
                connection.execute(
                    text(
                        "UPDATE external_kodi_targets SET "
                        "production_policy_revision=4 WHERE target_id=:target"
                    ),
                    values,
                )
            nested.rollback()
        with pytest.raises(IntegrityError):
            connection.execute(
                text("DELETE FROM targets WHERE id=:target"),
                values,
            )
        with pytest.raises(IntegrityError):
            connection.execute(
                text(
                    "INSERT OR REPLACE INTO targets SELECT id,name,target_type,"
                    "kodi_version,availability,health,connection_state FROM targets "
                    "WHERE id=:target"
                ),
                values,
            )
        with connection.begin_nested() as nested:
            connection.execute(
                text(
                    "INSERT INTO mcp_target_preset_assignments "
                    "(target_id,preset_id,assigned_at) VALUES "
                    "(:target,'11000000-0000-4000-8000-000000000005',:at)"
                ),
                values,
            )
            assert (
                connection.scalar(
                    text(
                        "SELECT policy_revision FROM external_production_policies "
                        "WHERE target_id=:target"
                    ),
                    values,
                )
                == 4
            )
            connection.execute(
                text(
                    "DELETE FROM mcp_target_preset_assignments WHERE target_id=:target"
                ),
                values,
            )
            assert (
                connection.scalar(
                    text(
                        "SELECT policy_revision FROM external_production_policies "
                        "WHERE target_id=:target"
                    ),
                    values,
                )
                == 5
            )
            nested.rollback()
        with connection.begin_nested(), pytest.raises(IntegrityError):
            connection.execute(
                text(
                    "DELETE FROM external_production_policies WHERE target_id=:target"
                ),
                values,
            )
            connection.execute(
                text(
                    "INSERT INTO external_production_policies (target_id,"
                    "designation_state,confirmed_observation_id,"
                    "confirmed_observation_digest,identity_generation,policy_revision,"
                    "mutations_suspended,designation_confirmed_by,"
                    "designation_confirmed_at,designation_invalidated_at,created_at,"
                    "updated_at) VALUES (:target,'invalidated',NULL,NULL,2,1,1,NULL,"
                    "NULL,:at,:at,:at)"
                ),
                values,
            )
    engine.dispose()


def test_current_stage_a_6_5_6_state_reads_and_rebinds_on_enablement(
    migration_templates: MigrationTemplateStore, tmp_path: Path
) -> None:
    database_path = tmp_path / "stage-a-6-5-6.db"
    migration_templates.clone("head", database_path)
    engine = create_database_engine(f"sqlite:///{database_path}")
    factory = create_session_factory(engine)
    target_id = UUID("56000000-0000-4000-8000-000000000001")
    observation_id = UUID("56000000-0000-4000-8000-000000000002")
    values = {
        "target": str(target_id),
        "observation": str(observation_id),
        "at": "2026-09-22T21:00:00.000000Z",
    }
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO targets (id,name,target_type,kodi_version,availability,"
                "health,connection_state) VALUES (:target,'Stage A production',"
                "'external_production','21.3','unavailable','healthy','connected')"
            ),
            values,
        )
        connection.execute(
            text(
                "INSERT INTO external_kodi_targets (target_id,protocol_kind,host,port,"
                "authentication_mode,secret_reference,configured_capabilities_json,"
                "verified_capabilities_json,administrative_state,verification_state,"
                "observation_state,verified_addresses_json,verified_kodi_name,"
                "verified_kodi_version,observed_kodi_name,observed_kodi_version,"
                "observed_protocol_version,pending_observation_id,"
                "production_policy_revision,created_at,verified_at,deactivated_at,"
                "last_observed_at) VALUES (:target,'jsonrpc_http','192.0.2.6',8080,"
                "'none',NULL,'[\"target_inspect\"]','[\"target_inspect\"]',"
                "'disabled','verified','reachable','[\"192.0.2.6\"]','Kodi','21.3',"
                "'Kodi','21.3','13.0.0',:observation,5,:at,:at,NULL,:at)"
            ),
            values,
        )
        connection.execute(
            text(
                "INSERT INTO external_kodi_target_observations (id,target_id,outcome,"
                "addresses_json,kodi_name,kodi_version,protocol_version,"
                "verified_capabilities_json,error_code,attempted_at,completed_at) "
                "VALUES (:observation,:target,'reachable','[\"192.0.2.6\"]','Kodi',"
                "'21.3','13.0.0','[\"target_inspect\"]',NULL,:at,:at)"
            ),
            values,
        )
        connection.execute(
            text(
                "INSERT INTO external_production_policies (target_id,"
                "designation_state,confirmed_observation_id,"
                "confirmed_observation_digest,identity_generation,policy_revision,"
                "mutations_suspended,designation_confirmed_by,"
                "designation_confirmed_at,designation_invalidated_at,created_at,"
                "updated_at) VALUES (:target,'confirmed',:observation,'"
                + "a" * 64
                + "',2,6,1,'local_administrator',:at,NULL,:at,:at)"
            ),
            values,
        )

    class NoContactVerifier:
        def probe(
            self,
            endpoint: ValidatedExternalEndpoint,
            credential: object,
            configured: frozenset[str],
        ) -> ExternalKodiProbeResult:
            raise AssertionError("current-state compatibility must not probe")

    service = ExternalTargetAdministrationService(
        factory,
        endpoint_policy=ExternalEndpointPolicy(allowed_ports=(8080,)),
        credential_store=ExternalCredentialStore(
            tmp_path / "stage-a-6-5-6-secrets", master_key=None, key_version=1
        ),
        verifier=NoContactVerifier(),  # type: ignore[arg-type]
        target_type=TargetType.EXTERNAL_PRODUCTION,
    )
    current = service.get(target_id)
    assert current.production_policy is not None
    assert current.production_policy.designation_state.value == "confirmed"
    assert current.production_policy.policy_revision == 6
    assert current.verification_state.value == "verified"
    assert current.administrative_state.value == "disabled"
    with engine.connect() as connection:
        assert connection.execute(
            text(
                "SELECT p.policy_revision,e.production_policy_revision,"
                "g.maximum_policy_revision FROM external_production_policies p "
                "JOIN external_kodi_targets e ON e.target_id=p.target_id "
                "JOIN external_production_policy_revision_guards g "
                "ON g.target_id=p.target_id WHERE p.target_id=:target"
            ),
            values,
        ).one() == (6, 5, 6)

    enabled = service.set_enabled(target_id, enabled=True)
    assert enabled.production_policy is not None
    assert enabled.production_policy.policy_revision == 7
    assert enabled.verification_state.value == "verified"
    with engine.connect() as connection:
        assert connection.execute(
            text(
                "SELECT p.policy_revision,e.production_policy_revision,"
                "g.maximum_policy_revision FROM external_production_policies p "
                "JOIN external_kodi_targets e ON e.target_id=p.target_id "
                "JOIN external_production_policy_revision_guards g "
                "ON g.target_id=p.target_id WHERE p.target_id=:target"
            ),
            values,
        ).one() == (7, 7, 7)
    engine.dispose()


def test_populated_upgrade_preserves_target_without_seeding_authority(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch,
        tmp_path,
        start_revision="head",
    )
    engine = create_database_engine(database_url)
    factory = create_session_factory(engine)
    service = ExternalTargetAdministrationService(
        factory,
        endpoint_policy=ExternalEndpointPolicy(allowed_ports=(8080,)),
        credential_store=ExternalCredentialStore(
            tmp_path / "0053-populated-secrets", master_key=None, key_version=1
        ),
        target_type=TargetType.EXTERNAL_PRODUCTION,
    )
    target = service.register(
        logical_name="Preserved production target",
        protocol_kind=ExternalProtocolKind.JSONRPC_HTTP,
        host="192.168.50.90",
        port=8080,
        authentication_mode=ExternalAuthenticationMode.NONE,
        configured_capabilities={"target_inspect", "addon_inventory"},
    )
    engine.dispose()

    # Current services seed a policy; the actual old schema is then restored
    # before exercising the populated forward transition.
    command.downgrade(config, "0001_initial_release")
    command.upgrade(config, "head")
    engine = create_database_engine(database_url)
    with engine.connect() as connection:
        assert connection.execute(
            text(
                "SELECT configured_capabilities_json,verified_capabilities_json,"
                "production_policy_revision FROM external_kodi_targets "
                "WHERE target_id=:target"
            ),
            {"target": str(target.target_id)},
        ).one() == ('["addon_inventory","target_inspect"]', "[]", 1)
        assert (
            connection.scalar(
                text("SELECT COUNT(*) FROM production_capability_amendments")
            )
            == 0
        )
    with (
        pytest.raises(IntegrityError, match="production_capability_amendment_required"),
        engine.begin() as connection,
    ):
        connection.execute(
            text(
                "UPDATE external_production_policies SET policy_revision=2 "
                "WHERE target_id=:target"
            ),
            {"target": str(target.target_id)},
        )
        connection.execute(
            text(
                "UPDATE external_kodi_targets SET "
                "configured_capabilities_json="
                '\'["addon_inventory","target_inspect",'
                '"workspace_deployment"]\',production_policy_revision=2 '
                "WHERE target_id=:target"
            ),
            {"target": str(target.target_id)},
        )
    engine.dispose()


def test_refuses_downgrade_after_meaningful_amendment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch,
        tmp_path,
        start_revision="0001_initial_release",
    )
    command.upgrade(config, "head")
    engine = create_database_engine(database_url)
    factory = create_session_factory(engine)

    class Verifier:
        def probe(
            self,
            endpoint: ValidatedExternalEndpoint,
            credential: object,
            configured: frozenset[str],
        ) -> ExternalKodiProbeResult:
            return ExternalKodiProbeResult(
                addresses=(endpoint.address,),
                kodi_name="Kodi",
                kodi_version="21.3",
                protocol_version="13.0.0",
                verified_capabilities=("target_inspect", "addon_inventory"),
            )

    targets = ExternalTargetAdministrationService(
        factory,
        endpoint_policy=ExternalEndpointPolicy(allowed_ports=(8080,)),
        credential_store=ExternalCredentialStore(
            tmp_path / "0053-downgrade-native-secrets",
            master_key=None,
            key_version=1,
        ),
        verifier=Verifier(),  # type: ignore[arg-type]
        target_type=TargetType.EXTERNAL_PRODUCTION,
    )
    target = targets.register(
        logical_name="Amended production target",
        protocol_kind=ExternalProtocolKind.JSONRPC_HTTP,
        host="192.168.50.91",
        port=8080,
        authentication_mode=ExternalAuthenticationMode.NONE,
        configured_capabilities={"target_inspect", "addon_inventory"},
    )
    observed = targets.probe(target.target_id)
    assert observed.pending_observation_id is not None
    targets.confirm(target.target_id, observed.pending_observation_id)
    targets.confirm_production_designation(
        target.target_id, observed.pending_observation_id
    )
    enabled = targets.set_enabled(target.target_id, enabled=True)
    assert enabled.production_policy is not None
    amendments = ProductionCapabilityAmendmentService(
        factory,
        bridge_secrets=ExternalBridgeSecretStore(
            tmp_path / "0053-downgrade-bridge-secrets",
            master_key=b"b" * 32,
            key_version=1,
        ),
    )
    amendments.amend_production_capability_maximum(
        target.target_id,
        operation="add",
        capability="screenshot",
        expected_configured_capabilities=frozenset(
            {"target_inspect", "addon_inventory"}
        ),
        expected_policy_revision=enabled.production_policy.policy_revision,
    )
    engine.dispose()


def test_install_attempts_are_unique_active_and_terminal_immutable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch,
        tmp_path,
        start_revision="0001_initial_release",
    )
    command.upgrade(config, "head")
    engine = create_database_engine(database_url)
    stamp = "2026-09-28T12:00:00+00:00"
    row = {
        "id": str(uuid4()),
        "recipe_id": "ubuntu-jammy-matrix-19.4",
        "recipe_revision": 1,
        "recipe_digest": "sha256:" + "a" * 64,
        "generation": "matrix",
        "state": "pending",
        "created_at": stamp,
        "updated_at": stamp,
    }
    statement = text(
        "INSERT INTO runtime_recipe_installations "
        "(id,recipe_id,recipe_revision,recipe_digest,generation,state,"
        "created_at,updated_at) VALUES "
        "(:id,:recipe_id,:recipe_revision,:recipe_digest,:generation,:state,"
        ":created_at,:updated_at)"
    )
    with engine.begin() as connection:
        connection.execute(statement, row)
        with pytest.raises(IntegrityError):
            connection.execute(statement, {**row, "id": str(uuid4())})
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE runtime_recipe_installations SET state='failed', "
                "failure_code='interrupted', completed_at=:stamp, updated_at=:stamp "
                "WHERE id=:id"
            ),
            {"id": row["id"], "stamp": stamp},
        )
    with engine.begin() as connection, pytest.raises(IntegrityError):
        connection.execute(
            text(
                "UPDATE runtime_recipe_installations SET failure_code='other' "
                "WHERE id=:id"
            ),
            {"id": row["id"]},
        )
    with engine.begin() as connection, pytest.raises(IntegrityError):
        connection.execute(
            text("DELETE FROM runtime_recipe_installations WHERE id=:id"),
            {"id": row["id"]},
        )
    engine.dispose()


def test_development_publication_identity_is_durable_and_single_current(
    migration_templates: MigrationTemplateStore,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0001_initial_release"
    )
    command.upgrade(config, "head")
    engine = create_database_engine(database_url)
    first = str(uuid4())
    values = {
        "id": first,
        "channel": "development",
        "addon_id": "plugin.video.kodi-lab-test",
        "version": "1.0.0",
        "addon_type": "plugin.video",
        "status": "current",
        "source_kind": "web_import",
        "source_id": first,
        "compatible_generations": "matrix,nexus,omega",
        "scope_kind": "development_only",
        "zip_sha256": "a" * 64,
        "tree_sha256": "b" * 64,
        "zip_bytes": 100,
        "created_at": "2026-09-29T00:00:00+00:00",
    }
    insert = text(
        "INSERT INTO development_publications "
        "(id,channel,addon_id,version,addon_type,status,source_kind,source_id,"
        "compatible_generations,scope_kind,zip_sha256,tree_sha256,"
        "zip_bytes,created_at) "
        "VALUES (:id,:channel,:addon_id,:version,:addon_type,:status,:source_kind,"
        ":source_id,:compatible_generations,:scope_kind,:zip_sha256,:tree_sha256,"
        ":zip_bytes,:created_at)"
    )
    with engine.begin() as connection:
        connection.execute(insert, values)
    with engine.begin() as connection, pytest.raises(IntegrityError):
        connection.execute(insert, {**values, "id": str(uuid4()), "version": "1.0.1"})
    with engine.begin() as connection, pytest.raises(IntegrityError):
        connection.execute(
            text("UPDATE development_publications SET zip_sha256=:hash WHERE id=:id"),
            {"hash": "c" * 64, "id": first},
        )
    with engine.begin() as connection, pytest.raises(IntegrityError):
        connection.execute(
            text("DELETE FROM development_publications WHERE id=:id"), {"id": first}
        )
    engine.dispose()


def test_repository_workspace_deployment_reinitialization(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0001_initial_release"
    )
    command.upgrade(config, "head")
    engine = create_database_engine(database_url)
    inspector = inspect(engine)
    assert {
        "external_development_repository_readiness",
        "repository_workspace_deployments",
        "repository_workspace_validation_snapshots",
    } <= set(inspector.get_table_names())
    columns = {
        item["name"]: item
        for item in inspector.get_columns("mcp_workspace_deployment_operations")
    }
    assert columns["deployment_mode"]["nullable"] is False
    engine.dispose()
    engine = create_database_engine(database_url)
    inspector = inspect(engine)
    engine.dispose()


def test_development_capability_amendments_reinitialization(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0001_initial_release"
    )
    baseline_engine = create_database_engine(database_url)
    baseline_tables = [
        name
        for name in inspect(baseline_engine).get_table_names()
        if name != "alembic_version"
    ]
    with baseline_engine.connect() as connection:
        baseline_rows = {
            name: sorted(
                (
                    tuple(row)
                    for row in connection.execute(text(f"SELECT * FROM {name}"))
                ),
                key=repr,
            )
            for name in baseline_tables
        }
    assert baseline_rows["mcp_presets"]
    baseline_engine.dispose()
    command.upgrade(config, "head")
    engine = create_database_engine(database_url)
    inspector = inspect(engine)
    assert "external_development_capability_amendments" in inspector.get_table_names()
    columns = {
        item["name"]
        for item in inspector.get_columns("external_development_capability_amendments")
    }
    assert {
        "id",
        "target_id",
        "target_classification",
        "bridge_id",
        "bridge_fingerprint",
        "before_json",
        "requested_json",
        "resulting_json",
        "native_verified_json",
        "bridge_verified_json",
        "actor",
        "requested_at",
        "completed_at",
    } <= columns
    with engine.connect() as connection:
        for name, rows in baseline_rows.items():
            assert (
                sorted(
                    (
                        tuple(row)
                        for row in connection.execute(text(f"SELECT * FROM {name}"))
                    ),
                    key=repr,
                )
                == rows
            )
        trigger = connection.scalar(
            text(
                "SELECT sql FROM sqlite_master WHERE type='trigger' AND "
                "name='trg_production_screenshot_removal_safe'"
            )
        )
        assert isinstance(trigger, str)
        dev_trigger = connection.scalar(
            text(
                "SELECT sql FROM sqlite_master WHERE type='trigger' AND "
                "name='trg_development_configured_capability_amendment_required'"
            )
        )
        assert isinstance(dev_trigger, str)
        assert "'external_development' AND EXISTS" in dev_trigger
    engine.dispose()
    engine = create_database_engine(database_url)
    with engine.connect() as connection:
        trigger = connection.scalar(
            text(
                "SELECT sql FROM sqlite_master WHERE type='trigger' AND "
                "name='trg_production_screenshot_removal_safe'"
            )
        )
        assert isinstance(trigger, str)
    engine.dispose()


def test_workspace_kodi_error_preserves_history_and_closed_shape(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0001_initial_release"
    )
    engine = create_database_engine(database_url)
    table = "mcp_workspace_execution_operations"
    before_indexes = {entry["name"] for entry in inspect(engine).get_indexes(table)}
    timestamp = "2026-10-02T00:00:00.000000Z"
    ids = (str(uuid4()), str(uuid4()))
    with engine.begin() as connection:
        for operation_id, state in zip(ids, ("succeeded", "failed"), strict=True):
            connection.execute(
                text(
                    f"INSERT INTO {table} (id,client_id,request_id,workspace_id,"
                    "development_source_id,target_id,deployment_id,generation,"
                    "addon_id,addon_version,state,phase,invocation_dispatched,"
                    "execution_classification,item_count,result_classification,"
                    "failure_code,requested_at,started_at,completed_at) VALUES "
                    "(:id,:client,:request,:workspace,:source,:target,:deployment,1,"
                    "'plugin.video.example','1.0.0',:state,'finalize',1,:classification,"
                    ":item_count,:result,:failure,:timestamp,:timestamp,:timestamp)"
                ),
                {
                    "id": operation_id,
                    "client": str(uuid4()),
                    "request": str(uuid4()),
                    "workspace": str(uuid4()),
                    "source": str(uuid4()),
                    "target": str(uuid4()),
                    "deployment": str(uuid4()),
                    "state": state,
                    "classification": (
                        "directory_result" if state == "succeeded" else "jsonrpc_error"
                    ),
                    "item_count": 1 if state == "succeeded" else None,
                    "result": "smoke_passed"
                    if state == "succeeded"
                    else "smoke_failed",
                    "failure": None if state == "succeeded" else "execution_failed",
                    "timestamp": timestamp,
                },
            )
    engine.dispose()

    command.upgrade(config, "head")
    engine = create_database_engine(database_url)
    with engine.connect() as connection:
        assert [
            tuple(row)
            for row in connection.execute(
                text(
                    f"SELECT id,state,kodi_error_code,kodi_error_message FROM {table} "
                    "ORDER BY state"
                )
            ).all()
        ] == [
            (ids[1], "failed", None, None),
            (ids[0], "succeeded", None, None),
        ]
        assert connection.scalar(text("PRAGMA integrity_check")) == "ok"
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
    assert {
        entry["name"] for entry in inspect(engine).get_indexes(table)
    } == before_indexes
    with engine.begin() as connection:
        connection.execute(
            text(
                f"UPDATE {table} SET kodi_error_code=-32602, "
                "kodi_error_message='Invalid params.' WHERE id=:id"
            ),
            {"id": ids[1]},
        )
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            text(f"UPDATE {table} SET kodi_error_message=:message WHERE id=:id"),
            {"id": ids[1], "message": "x" * 193},
        )
    with engine.begin() as connection:
        connection.execute(
            text(f"UPDATE {table} SET kodi_error_message=NULL WHERE id=:id"),
            {"id": ids[1]},
        )
        assert (
            connection.scalar(
                text(f"SELECT kodi_error_code FROM {table} WHERE id=:id"),
                {"id": ids[1]},
            )
            == -32602
        )
        connection.execute(
            text(
                f"UPDATE {table} SET kodi_error_message='Invalid params.' WHERE id=:id"
            ),
            {"id": ids[1]},
        )
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            text(f"UPDATE {table} SET kodi_error_code=NULL WHERE id=:id"),
            {"id": ids[1]},
        )
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            text(f"UPDATE {table} SET kodi_error_code=-32601 WHERE id=:id"),
            {"id": ids[0]},
        )
    engine.dispose()
    engine = create_database_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                f"UPDATE {table} SET kodi_error_code=NULL,kodi_error_message=NULL "
                "WHERE id=:id"
            ),
            {"id": ids[1]},
        )
    engine.dispose()
    engine = create_database_engine(database_url)
    with engine.connect() as connection:
        assert connection.scalar(text("PRAGMA integrity_check")) == "ok"
        assert connection.scalar(text(f"SELECT COUNT(*) FROM {table}")) == 2
    engine.dispose()


def test_managed_projection_and_exact_preapplication_ambiguity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0001_initial_release"
    )
    engine = create_database_engine(database_url)
    ids = {
        name: str(uuid4())
        for name in (
            "definition",
            "revision",
            "target",
            "source",
            "repository",
            "snapshot",
            "candidate",
            "artifact",
            "plan",
            "operation",
        )
    }
    ids.update(at="2026-10-02T00:00:00.000000Z", digest="sha256:" + "a" * 64)
    with engine.begin() as connection:
        for statement in (
            "INSERT INTO runtime_definitions (id,slug,kodi_version,support_tier) "
            "VALUES (:definition,'kodi-21-3-projection','21.3','current')",
            "INSERT INTO runtime_revisions (id,runtime_definition_id,revision_number,"
            "source_kind,source_reference,content_digest,manifest_reference,"
            "manifest_digest,built_at,approval_state,"
            "requires_trusted_build_confirmation) VALUES "
            "(:revision,:definition,1,'test','test',:digest,'test',:digest,"
            ":at,'not_approved',0)",
            "INSERT INTO targets (id,name,target_type,kodi_version,availability,"
            "health,connection_state) VALUES "
            "(:target,'projection migration','managed','21.3','unavailable',"
            "'warning','disconnected')",
            "INSERT INTO managed_instances (target_id,instance_type,"
            "runtime_revision_id,runtime_state,start_on_host_boot) VALUES "
            "(:target,'disposable',:revision,'stopped',0)",
            "INSERT INTO development_sources (id,logical_name,source_kind,"
            "relative_locator,addon_id,observed_name,observed_version,"
            "extension_types_json,registered_at,validated_at) VALUES "
            "(:source,'migration source','local_directory','addons/source',"
            "'plugin.video.example','Example','1.0.0','[]',:at,:at)",
            "INSERT INTO addon_repository_sources (id,logical_name,"
            "canonical_index_url,origin,created_at) VALUES "
            "(:repository,'migration repository','https://example.test/addons.xml',"
            "'external_https_repository',:at)",
            "INSERT INTO addon_repository_metadata_snapshots (id,"
            "repository_source_id,fetched_at,transfer_digest,"
            "canonical_metadata_digest,transfer_size,expanded_size,entry_count) "
            "VALUES (:snapshot,:repository,:at,:digest,:digest,1,1,1)",
            "INSERT INTO addon_repository_candidates (id,snapshot_id,"
            "repository_source_id,addon_id,name,version,classification,"
            "dependencies_json,support) VALUES (:candidate,:snapshot,:repository,"
            "'plugin.video.example','Example','1.0.0','python_video_plugin','[]',"
            "'supported')",
            "INSERT INTO addon_zip_artifacts (id,logical_label,sha256_digest,"
            "compressed_size,total_uncompressed_bytes,member_count,addon_id,"
            "addon_name,addon_version,extension_classification,dependencies_json,"
            "payload_manifest_digest,imported_at,origin,validation_state,"
            "repository_source_id,repository_snapshot_id,repository_candidate_id,"
            "repository_fetched_at) VALUES (:artifact,'migration artifact',:digest,"
            "1,1,1,'plugin.video.example','Example','1.0.0',"
            "'python_video_plugin','[]',:digest,:at,'repository_download',"
            "'accepted',:repository,:snapshot,:candidate,:at)",
            "INSERT INTO repository_replacement_plans (id,"
            "original_managed_instance_id,managed_instance_id,repository_source_id,"
            "repository_attachment_at,repository_snapshot_id,"
            "repository_candidate_id,incoming_artifact_id,addon_id,incumbent_class,"
            "development_source_id,development_source_attachment_at,"
            "runtime_revision_id,expected_container_id,incoming_version,"
            "incoming_payload_manifest_digest,created_at,expires_at,state) VALUES "
            "(:plan,:target,:target,:repository,:at,:snapshot,:candidate,:artifact,"
            "'plugin.video.example','attached_unmaterialized',:source,:at,"
            ":revision,'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',"
            "'1.0.0',:digest,:at,:at,'consumed')",
            "INSERT INTO repository_replacement_operations (id,plan_id,"
            "original_managed_instance_id,managed_instance_id,incoming_artifact_id,"
            "development_source_id,addon_id,incoming_version,incumbent_class,"
            "runtime_revision_id,expected_container_id,requested_at,state,phase,"
            "application_started,completed_at,failure_reason,"
            "reconciliation_required) VALUES (:operation,:plan,:target,:target,"
            ":artifact,:source,'plugin.video.example','1.0.0',"
            "'attached_unmaterialized',:revision,"
            "'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',"
            ":at,'failed','finalize',0,:at,'preflight_failed',0)",
        ):
            connection.execute(text(statement), ids)
    engine.dispose()

    command.upgrade(config, "head")
    engine = create_database_engine(database_url)
    with engine.connect() as connection:
        assert (
            connection.scalar(
                text(
                    "SELECT runtime_state_revision FROM managed_instances "
                    "WHERE target_id=:target"
                ),
                ids,
            )
            == 0
        )
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
        assert connection.scalar(text("PRAGMA integrity_check")) == "ok"
        assert (
            connection.scalar(
                text(
                    "SELECT state FROM repository_replacement_operations "
                    "WHERE id=:operation"
                ),
                ids,
            )
            == "failed"
        )
    second_target = str(uuid4())
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO targets (id,name,target_type,kodi_version,availability,"
                "health,connection_state) VALUES (:id,'new managed','managed',"
                "'21.3','unavailable','warning','disconnected')"
            ),
            {"id": second_target},
        )
        connection.execute(
            text(
                "INSERT INTO managed_instances (target_id,instance_type,"
                "runtime_revision_id,runtime_state,start_on_host_boot) VALUES "
                "(:id,'disposable',:revision,'stopped',0)"
            ),
            {"id": second_target, "revision": ids["revision"]},
        )
        assert (
            connection.scalar(
                text(
                    "SELECT runtime_state_revision FROM managed_instances "
                    "WHERE target_id=:id"
                ),
                {"id": second_target},
            )
            == 0
        )
        connection.execute(
            text(
                "UPDATE repository_replacement_operations SET "
                "state='reconciliation_required',application_started=1,"
                "reconciliation_required=1,failure_reason='ordinary_reconciliation' "
                "WHERE id=:operation"
            ),
            ids,
        )
        connection.execute(
            text(
                "UPDATE repository_replacement_operations SET state='failed',"
                "application_started=0,reconciliation_required=0,"
                "failure_reason='preflight_failed' WHERE id=:operation"
            ),
            ids,
        )
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE managed_instances SET runtime_state_revision=-1 "
                "WHERE target_id=:target"
            ),
            ids,
        )
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE managed_instances SET runtime_state_revision=NULL "
                "WHERE target_id=:target"
            ),
            ids,
        )
    for reason in (
        "preflight_failed",
        "broker_runtime_dispatch_unconfirmed_extra",
        "",
        None,
    ):
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE repository_replacement_operations SET "
                    "state='reconciliation_required',phase='stopping',"
                    "reconciliation_required=1,failure_reason=:reason "
                    "WHERE id=:operation"
                ),
                {**ids, "reason": reason},
            )
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE repository_replacement_operations SET "
                "state='reconciliation_required',phase='stopping',"
                "reconciliation_required=1,"
                "failure_reason='broker_runtime_dispatch_unconfirmed' "
                "WHERE id=:operation"
            ),
            ids,
        )
    for inconsistent in (
        "phase='finalize'",
        "reconciliation_required=0",
        "state='succeeded'",
    ):
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE repository_replacement_operations SET "
                    + inconsistent
                    + " WHERE id=:operation"
                ),
                ids,
            )
    engine.dispose()
    engine = create_database_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE repository_replacement_operations SET state='failed',"
                "phase='finalize',reconciliation_required=0,"
                "failure_reason='preflight_failed' WHERE id=:operation"
            ),
            ids,
        )
    engine.dispose()
    engine = create_database_engine(database_url)
    engine.dispose()
