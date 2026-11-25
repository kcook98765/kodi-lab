import sqlite3
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.util.exc import CommandError
from sqlalchemy import Connection, inspect, text
from sqlalchemy.exc import IntegrityError

from kodi_lab.application.external_bridges import ExternalBridgeService
from kodi_lab.application.external_targets import ExternalTargetAdministrationService
from kodi_lab.domain import (
    ExternalAuthenticationMode,
    ExternalProtocolKind,
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
        ).fetchone() == ("0001_foundation",)


def test_failed_schema_command_cannot_poison_later_clone(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    migration_templates: MigrationTemplateStore,
) -> None:
    config, _database_url = migration_setup(
        monkeypatch,
        tmp_path,
        start_revision="0001_foundation",
    )
    with pytest.raises(CommandError, match="Can't locate revision"):
        command.upgrade(config, "not_a_revision")

    later = tmp_path / "later.db"
    migration_templates.clone("0001_foundation", later)
    with sqlite3.connect(later) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == ("0001_foundation",)


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


def test_allows_atomic_cleanup_only_for_coherent_terminal_navigation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch, tmp_path, start_revision="0001_foundation"
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


def test_install_attempts_are_unique_active_and_terminal_immutable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(
        monkeypatch,
        tmp_path,
        start_revision="0001_foundation",
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
        monkeypatch, tmp_path, start_revision="0001_foundation"
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
        monkeypatch, tmp_path, start_revision="0001_foundation"
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
        monkeypatch, tmp_path, start_revision="0001_foundation"
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
