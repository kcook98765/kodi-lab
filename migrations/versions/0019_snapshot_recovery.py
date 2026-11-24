"""One-use legacy snapshot recovery; preserve original failure and attribution."""

import re

import sqlalchemy as sa
from alembic import op

revision = "0019_snapshot_recovery"
down_revision = "0018_source_addition_reviews"
branch_labels = None
depends_on = None

_TABLES = (
    "snapshot_recovery_epochs",
    "snapshot_recoveries",
    "snapshot_recovery_dispatches",
    "snapshot_recovery_events",
    "snapshot_recovery_resolutions",
)
_MUTATION_KINDS = (
    "reset",
    "snapshot_create",
    "snapshot_restore",
    "lifecycle",
    "fixture_assignment",
    "runtime_reassignment",
    "instance_delete",
    "development_source_attachment",
    "development_source_deployment",
    "development_source_enablement",
    "development_source_smoke_test",
    "addon_zip_installation",
    "addon_zip_enablement",
    "addon_zip_smoke_test",
    "addon_repository_attachment",
    "addon_repository_installation",
    "repository_replacement",
    "agent_workspace_promotion",
    "promotion_verification",
    "mcp_workspace_deployment",
    "mcp_workspace_execution",
    "workspace_execution_recovery",
    "mcp_workspace_launch",
    "mcp_addon_settings",
    "reconstruction",
)
_HEAVY_KINDS = (
    "snapshot_create",
    "snapshot_restore",
    "snapshot_delete",
    "instance_delete",
    "development_source_deployment",
    "addon_zip_import",
    "addon_zip_installation",
    "addon_repository_refresh",
    "addon_repository_fetch",
    "addon_repository_installation",
    "repository_replacement",
    "agent_workspace_creation",
    "agent_workspace_promotion",
    "promotion_verification",
    "agent_workspace_cleanup",
    "mcp_workspace_deployment",
)


def _lease_kinds(upgrading: bool) -> None:
    connection = op.get_bind()
    tables = (
        ("managed_instance_mutation_leases", "managed_instance_mutation_kind"),
        ("global_heavy_operation_lease", "global_heavy_operation_kind"),
    )
    checks: dict[str, str] = {}
    for (table, constraint), kinds in zip(
        tables, (_MUTATION_KINDS, _HEAVY_KINDS), strict=True
    ):
        old = next(
            c["sqltext"]
            for c in sa.inspect(connection).get_check_constraints(table)
            if c["name"] == constraint
        )
        expected_kinds = kinds if upgrading else ("snapshot_recovery", *kinds)
        expected = (
            "operation_kindIN(" + ",".join(f"'{k}'" for k in expected_kinds) + ")"
        )
        if re.sub(r"\s+", "", old) != expected:
            raise RuntimeError("Unexpected frozen lease vocabulary; database unchanged")
        checks[table] = old
    triggers = [
        (name, sql)
        for name, sql in connection.execute(
            sa.text(
                "SELECT name,sql FROM sqlite_master "
                "WHERE type='trigger' AND sql IS NOT NULL"
            )
        )
        if any(table in sql for table, _ in tables)
    ]
    for name, _ in triggers:
        op.execute('DROP TRIGGER "' + name.replace('"', '""') + '"')
    for table, constraint in tables:
        old = checks[table]
        new = (
            old.replace(" IN (", " IN ('snapshot_recovery', ")
            if upgrading
            else old.replace("'snapshot_recovery', ", "")
        )
        with op.batch_alter_table(table, recreate="always") as batch:
            batch.drop_constraint(constraint, type_="check")
            batch.create_check_constraint(constraint, new)
    for _, sql in triggers:
        op.execute(sql)


def upgrade() -> None:
    _lease_kinds(True)
    op.execute("""CREATE TABLE snapshot_recovery_epochs (
        id VARCHAR(36) PRIMARY KEY NOT NULL, incident_id VARCHAR(36) NOT NULL,
        receipt_digest VARCHAR(64) NOT NULL,
        evidence_json TEXT NOT NULL CHECK(length(evidence_json) BETWEEN 2 AND 262144),
        seal_json TEXT NOT NULL CHECK(length(seal_json) BETWEEN 2 AND 65536),
        state VARCHAR(16) NOT NULL CHECK(
            state IN ('sealed','serving','blocked','closed')),
        active INTEGER CHECK(active IS NULL OR active=1),
        created_at VARCHAR(32) NOT NULL,
        web_owner TEXT, broker_owner TEXT,
        CHECK((state='closed' AND active IS NULL) OR
              (state<>'closed' AND active IS 1)))""")
    op.execute(
        "CREATE UNIQUE INDEX uq_snapshot_recovery_epoch_active "
        "ON snapshot_recovery_epochs(active)"
    )
    op.execute("""CREATE TABLE snapshot_recoveries (
        id VARCHAR(36) PRIMARY KEY NOT NULL,
        epoch_id VARCHAR(36) NOT NULL REFERENCES snapshot_recovery_epochs(id)
            ON DELETE RESTRICT,
        incident_id VARCHAR(36) NOT NULL, target_id VARCHAR(36) NOT NULL,
        actor_digest VARCHAR(64) NOT NULL, fingerprint VARCHAR(64) NOT NULL,
        proof_json TEXT NOT NULL CHECK(length(proof_json) BETWEEN 2 AND 262144),
        created_at VARCHAR(32) NOT NULL, expires_at VARCHAR(32) NOT NULL,
        state VARCHAR(32) NOT NULL CHECK(state IN ('preview','revoked','claimed',
            'provisioned','applied','verified','succeeded','reconciliation_required')),
        request_id VARCHAR(36) UNIQUE, consumed_at VARCHAR(32),
        lease_digest VARCHAR(64), successor_generation INTEGER,
        container_id VARCHAR(64), failure_code VARCHAR(64))
    """)
    op.execute(
        "CREATE UNIQUE INDEX uq_snapshot_recovery_incident_claim "
        "ON snapshot_recoveries(incident_id) WHERE consumed_at IS NOT NULL"
    )
    op.execute("""CREATE TABLE snapshot_recovery_dispatches (
        id VARCHAR(36) PRIMARY KEY NOT NULL,
        recovery_id VARCHAR(36) NOT NULL REFERENCES snapshot_recoveries(id)
            ON DELETE RESTRICT,
        step VARCHAR(16) NOT NULL CHECK(step IN ('provision','apply','verify')),
        request_id VARCHAR(36) UNIQUE NOT NULL, intent_digest VARCHAR(64) NOT NULL,
        deadline VARCHAR(32) NOT NULL,
        state VARCHAR(16) NOT NULL CHECK(state IN ('prepared','consumed','completed',
            'uncertain')), result_json TEXT, UNIQUE(recovery_id,step))""")
    op.execute("""CREATE TABLE snapshot_recovery_events (
        id VARCHAR(36) PRIMARY KEY NOT NULL,
        recovery_id VARCHAR(36) NOT NULL REFERENCES snapshot_recoveries(id)
            ON DELETE RESTRICT,
        event VARCHAR(64) NOT NULL, recorded_at VARCHAR(32) NOT NULL,
        detail_json TEXT NOT NULL, UNIQUE(recovery_id,event))""")
    op.execute("""CREATE TABLE snapshot_recovery_resolutions (
        incident_id VARCHAR(36) PRIMARY KEY NOT NULL,
        recovery_id VARCHAR(36) UNIQUE NOT NULL REFERENCES snapshot_recoveries(id)
            ON DELETE RESTRICT,
        relation VARCHAR(64) NOT NULL CHECK(
            relation='verified_original_snapshot_stopped_successor'),
        receipt_digest VARCHAR(64) NOT NULL, proof_digest VARCHAR(64) NOT NULL,
        completed_at VARCHAR(32) NOT NULL)""")
    for table in _TABLES:
        op.execute(f"""CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table}
            BEGIN SELECT RAISE(ABORT,'snapshot recovery history is immutable'); END""")
        key = (
            "incident_id=NEW.incident_id"
            if table.endswith("resolutions")
            else "id=NEW.id"
        )
        if table == "snapshot_recovery_dispatches":
            key += " OR request_id=NEW.request_id OR "
            key += "(recovery_id=NEW.recovery_id AND step=NEW.step)"
        elif table == "snapshot_recoveries":
            key += " OR request_id=NEW.request_id OR "
            key += "(incident_id=NEW.incident_id AND consumed_at IS NOT NULL "
            key += "AND NEW.consumed_at IS NOT NULL)"
        elif table == "snapshot_recovery_epochs":
            key += " OR (active=1 AND NEW.active=1)"
        elif table == "snapshot_recovery_resolutions":
            key += " OR recovery_id=NEW.recovery_id"
        elif table == "snapshot_recovery_events":
            key += " OR (recovery_id=NEW.recovery_id AND event=NEW.event)"
        op.execute(f"""CREATE TRIGGER {table}_no_replace BEFORE INSERT ON {table}
            WHEN EXISTS(SELECT 1 FROM {table} WHERE
                {key})
            BEGIN SELECT RAISE(ABORT,'snapshot recovery replacement refused'); END""")
    immutable = {
        "snapshot_recovery_epochs": (
            "id",
            "incident_id",
            "receipt_digest",
            "evidence_json",
            "seal_json",
            "created_at",
        ),
        "snapshot_recoveries": (
            "id",
            "epoch_id",
            "incident_id",
            "target_id",
            "actor_digest",
            "fingerprint",
            "proof_json",
            "created_at",
            "expires_at",
        ),
        "snapshot_recovery_dispatches": (
            "id",
            "recovery_id",
            "step",
            "request_id",
            "intent_digest",
            "deadline",
        ),
        "snapshot_recovery_events": (
            "id",
            "recovery_id",
            "event",
            "recorded_at",
            "detail_json",
        ),
        "snapshot_recovery_resolutions": (
            "incident_id",
            "recovery_id",
            "relation",
            "receipt_digest",
            "proof_digest",
            "completed_at",
        ),
    }
    for table, columns in immutable.items():
        changed = " OR ".join(f"NEW.{c} IS NOT OLD.{c}" for c in columns)
        op.execute(f"""CREATE TRIGGER {table}_immutable BEFORE UPDATE ON {table}
            WHEN {changed}
            BEGIN SELECT RAISE(ABORT,'snapshot recovery intent is immutable'); END""")
    op.execute("""CREATE TRIGGER snapshot_recovery_claim_immutable
        BEFORE UPDATE ON snapshot_recoveries WHEN
        (OLD.consumed_at IS NOT NULL AND (NEW.consumed_at IS NOT OLD.consumed_at
            OR NEW.request_id IS NOT OLD.request_id
            OR NEW.lease_digest IS NOT OLD.lease_digest))
        OR (OLD.container_id IS NOT NULL AND NEW.container_id IS NOT OLD.container_id)
        OR (OLD.successor_generation IS NOT NULL AND
            NEW.successor_generation IS NOT OLD.successor_generation)
        OR (OLD.state IN ('revoked','succeeded','reconciliation_required') AND
            NEW.state<>OLD.state)
        BEGIN SELECT RAISE(ABORT,'snapshot recovery claim is immutable'); END""")
    op.execute("""CREATE TRIGGER snapshot_recovery_epoch_closed
        BEFORE UPDATE ON snapshot_recovery_epochs WHEN OLD.state='closed'
        OR (OLD.state='blocked' AND NEW.state<>'blocked')
        BEGIN SELECT RAISE(ABORT,'snapshot recovery epoch cannot be reopened'); END""")
    op.execute("""CREATE TRIGGER snapshot_recovery_claim_shape
        BEFORE UPDATE ON snapshot_recoveries WHEN
        (NEW.state IN ('preview','revoked') AND
         (NEW.consumed_at IS NOT NULL OR NEW.request_id IS NOT NULL
          OR NEW.lease_digest IS NOT NULL)) OR
        (NEW.state NOT IN ('preview','revoked') AND
         (NEW.consumed_at IS NULL OR NEW.request_id IS NULL
          OR NEW.lease_digest IS NULL OR length(NEW.request_id)<>36
          OR length(NEW.lease_digest)<>64)) OR
        EXISTS(SELECT 1 FROM snapshot_recoveries r WHERE r.id<>OLD.id AND
          (r.request_id=NEW.request_id OR (r.incident_id=NEW.incident_id
           AND r.consumed_at IS NOT NULL AND NEW.consumed_at IS NOT NULL)))
        OR (OLD.state='preview' AND NEW.state='claimed' AND
            (NEW.consumed_at<OLD.created_at OR NEW.consumed_at>=NEW.expires_at))
        BEGIN SELECT RAISE(ABORT,'invalid snapshot recovery one-use claim'); END""")
    op.execute("""CREATE TRIGGER snapshot_recovery_preview_insert
        BEFORE INSERT ON snapshot_recoveries WHEN
        NEW.state<>'preview' OR NEW.consumed_at IS NOT NULL
        OR NEW.request_id IS NOT NULL OR NEW.lease_digest IS NOT NULL
        OR NEW.container_id IS NOT NULL OR NEW.successor_generation IS NOT NULL
        BEGIN SELECT RAISE(ABORT,'snapshot recovery requires a fresh review'); END""")
    op.execute("""CREATE TRIGGER snapshot_recovery_state_transition
        BEFORE UPDATE ON snapshot_recoveries WHEN OLD.state<>NEW.state AND NOT (
            (OLD.state='preview' AND NEW.state IN ('claimed','revoked')) OR
            (OLD.state='claimed' AND NEW.state='provisioned') OR
            (OLD.state='provisioned' AND NEW.state='applied') OR
            (OLD.state='applied' AND NEW.state='verified') OR
            (OLD.state='verified' AND NEW.state='succeeded') OR
            (OLD.state IN ('claimed','provisioned','applied','verified') AND
                NEW.state='reconciliation_required'))
        BEGIN SELECT RAISE(ABORT,'invalid snapshot recovery transition'); END""")
    op.execute("""CREATE TRIGGER snapshot_recovery_success_verified
        BEFORE UPDATE ON snapshot_recoveries WHEN NEW.state='succeeded' AND
        (NEW.container_id IS NULL OR NEW.successor_generation IS NULL OR
         NEW.consumed_at IS NULL OR NEW.request_id IS NULL OR
         (SELECT count(*) FROM snapshot_recovery_dispatches
          WHERE recovery_id=OLD.id AND state='completed')<>3)
        BEGIN SELECT RAISE(ABORT,'snapshot recovery proof is incomplete'); END""")
    op.execute("""CREATE TRIGGER snapshot_recovery_dispatch_insert
        BEFORE INSERT ON snapshot_recovery_dispatches WHEN
        NEW.state<>'prepared' OR NEW.result_json IS NOT NULL OR NOT EXISTS(
            SELECT 1 FROM snapshot_recoveries r WHERE r.id=NEW.recovery_id
            AND r.consumed_at IS NOT NULL AND r.successor_generation IS NOT NULL
            AND ((NEW.step='provision' AND r.state='claimed') OR
                 (NEW.step='apply' AND r.state='provisioned') OR
                 (NEW.step='verify' AND r.state='applied')))
        BEGIN SELECT RAISE(ABORT,'snapshot recovery dispatch must be prepared'); END""")
    op.execute("""CREATE TRIGGER snapshot_recovery_dispatch_transition
        BEFORE UPDATE ON snapshot_recovery_dispatches WHEN
        NOT ((OLD.state='prepared' AND NEW.state IN ('consumed','uncertain')) OR
             (OLD.state='consumed' AND NEW.state IN ('completed','uncertain')))
        BEGIN SELECT RAISE(ABORT,'snapshot recovery dispatch is one use'); END""")
    op.execute("""CREATE TRIGGER snapshot_recovery_resolution_verified
        BEFORE INSERT ON snapshot_recovery_resolutions WHEN NOT EXISTS(
            SELECT 1 FROM snapshot_recoveries r WHERE r.id=NEW.recovery_id
            AND r.incident_id=NEW.incident_id AND r.state='succeeded'
            AND r.container_id IS NOT NULL AND r.successor_generation IS NOT NULL
            AND (SELECT count(*) FROM snapshot_recovery_dispatches d
                 WHERE d.recovery_id=r.id AND d.state='completed')=3)
        BEGIN SELECT RAISE(ABORT,'snapshot recovery is not verified'); END""")
    for action in ("UPDATE", "DELETE"):
        op.execute(f"""CREATE TRIGGER snapshot_recovery_original_{action.lower()}
            BEFORE {action} ON snapshot_restore_operations WHEN EXISTS(
                SELECT 1 FROM snapshot_recovery_epochs e WHERE e.incident_id=OLD.id)
            BEGIN SELECT RAISE(ABORT,'original restore receipt is immutable'); END""")
    op.execute("""CREATE TRIGGER snapshot_recovery_original_replace
        BEFORE INSERT ON snapshot_restore_operations WHEN EXISTS(
            SELECT 1 FROM snapshot_recovery_epochs e WHERE e.incident_id=NEW.id)
        BEGIN SELECT RAISE(ABORT,'original restore replacement refused'); END""")
    op.execute("""CREATE TRIGGER snapshot_recovery_session_attribution
        BEFORE UPDATE ON development_sessions WHEN EXISTS(
            SELECT 1 FROM snapshot_recoveries r WHERE r.consumed_at IS NOT NULL
            AND json_extract(r.proof_json,'$.selection.generation')=OLD.generation)
        AND (NEW.container_id IS NOT OLD.container_id OR NEW.target_id<>OLD.target_id
            OR NEW.runtime_revision_id<>OLD.runtime_revision_id
            OR NEW.workspace_id<>OLD.workspace_id OR NEW.source_id<>OLD.source_id
            OR NEW.request_id<>OLD.request_id OR NEW.created_at<>OLD.created_at
            OR (OLD.phase='closed' AND (NEW.phase<>'closed'
                OR NEW.updated_at IS NOT OLD.updated_at)))
        BEGIN SELECT RAISE(ABORT,'original session attribution is immutable'); END""")
    op.execute("""CREATE TRIGGER snapshot_recovery_session_retention
        BEFORE DELETE ON development_sessions WHEN EXISTS(
            SELECT 1 FROM snapshot_recoveries r WHERE r.consumed_at IS NOT NULL AND
            (json_extract(r.proof_json,'$.selection.generation')=OLD.generation
             OR r.successor_generation=OLD.generation))
        BEGIN SELECT RAISE(ABORT,'recovery session history is retained'); END""")
    op.execute("""CREATE TRIGGER snapshot_recovery_session_replace
        BEFORE INSERT ON development_sessions WHEN EXISTS(
            SELECT 1 FROM snapshot_recoveries r WHERE r.consumed_at IS NOT NULL AND
            (json_extract(r.proof_json,'$.selection.generation')=NEW.generation
             OR r.successor_generation=NEW.generation))
        BEGIN SELECT RAISE(ABORT,'original session replacement refused'); END""")
    # Retain the original profile's tested copy/review details even after a
    # later ordinary copy changes the current deployment pointer.
    for table, target_column, cid_column in (
        (
            "development_source_deployment_operations",
            "original_managed_instance_id",
            "expected_container_id",
        ),
        ("source_addition_reviews", "target_id", "container_id"),
    ):
        linked = (
            "EXISTS(SELECT 1 FROM snapshot_recovery_epochs e WHERE "
            "json_extract(e.evidence_json,'$.receipt.old_container_id')="
            f"OLD.{cid_column} AND "
            "json_extract(e.evidence_json,'$.receipt.managed_instance_id')="
            f"OLD.{target_column})"
        )
        for action in ("UPDATE", "DELETE"):
            changed = "1"
            if action == "UPDATE":
                changed = " OR ".join(
                    f"NEW.{c['name']} IS NOT OLD.{c['name']}"
                    for c in sa.inspect(op.get_bind()).get_columns(table)
                    if c["name"] != "managed_instance_id"
                )
            op.execute(f"""CREATE TRIGGER snapshot_recovery_{table}_{action.lower()}
                BEFORE {action} ON {table} WHEN {linked} AND ({changed})
                BEGIN SELECT RAISE(ABORT,'linked recovery history is immutable');
                END""")
        op.execute(f"""CREATE TRIGGER snapshot_recovery_{table}_replace
            BEFORE INSERT ON {table} WHEN EXISTS(
                SELECT 1 FROM {table} OLD WHERE OLD.id=NEW.id AND {linked})
            BEGIN SELECT RAISE(ABORT,'recovery linked replacement refused'); END""")


def downgrade() -> None:
    connection = op.get_bind()
    if any(
        connection.scalar(
            sa.text(
                f"SELECT EXISTS(SELECT 1 FROM {table} "
                "WHERE operation_kind='snapshot_recovery')"
            )
        )
        for table in (
            "managed_instance_mutation_leases",
            "global_heavy_operation_lease",
        )
    ):
        raise RuntimeError("Snapshot recovery lease exists; refusing lossy downgrade")
    if any(
        connection.scalar(sa.text(f"SELECT EXISTS(SELECT 1 FROM {t})")) for t in _TABLES
    ):
        raise RuntimeError("Snapshot recovery history exists; refusing lossy downgrade")
    triggers = list(
        connection.execute(
            sa.text(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND "
                "(name LIKE 'snapshot_recovery_%' OR name LIKE 'snapshot_recoveries_%')"
            )
        )
    )
    for (name,) in triggers:
        op.execute('DROP TRIGGER "' + name + '"')
    for table in reversed(_TABLES):
        op.drop_table(table)
    _lease_kinds(False)
