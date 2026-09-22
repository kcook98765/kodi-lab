"""Add constrained production navigation authority and durable evidence.

Revision ID: 0050_production_navigation
Revises: 0049_production_bridge_visual
"""

# Closed SQLite constraints and triggers are intentionally explicit.
# ruff: noqa: E501

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0050_production_navigation"
down_revision: str | Sequence[str] | None = "0049_production_bridge_visual"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PRODUCTION_INSPECTION_PRESET_ID = "11000000-0000-4000-8000-000000000005"
PRODUCTION_VISUAL_PRESET_ID = "11000000-0000-4000-8000-000000000006"
PRODUCTION_REMOTE_PRESET_ID = "11000000-0000-4000-8000-000000000004"


def _scope_triggers() -> None:
    op.execute(
        "CREATE TRIGGER trg_mcp_production_scope_binding_insert BEFORE INSERT ON mcp_production_target_scopes WHEN COALESCE((SELECT target_type FROM targets WHERE id=NEW.target_id),'')<>'external_production' OR COALESCE((SELECT identity_generation FROM external_production_policies WHERE target_id=NEW.target_id),0)<>NEW.identity_generation OR NEW.state<>'active' OR NEW.revoked_at IS NOT NULL OR NEW.revoked_by IS NOT NULL OR (NEW.scope IN ('visual','navigate') AND (COALESCE((SELECT production_bridge_trust_revision FROM external_bridge_pairings WHERE target_id=NEW.target_id),0)<>NEW.bridge_trust_revision OR COALESCE((SELECT production_identity_generation FROM external_bridge_pairings WHERE target_id=NEW.target_id),0)<>NEW.identity_generation OR COALESCE((SELECT pairing_state FROM external_bridge_pairings WHERE target_id=NEW.target_id),'')<>'paired' OR COALESCE((SELECT reachability_state FROM external_bridge_pairings WHERE target_id=NEW.target_id),'')<>'reachable' OR COALESCE((SELECT state FROM reviewed_bridge_builds WHERE id=(SELECT reviewed_build_id FROM external_bridge_pairings WHERE target_id=NEW.target_id)),'')<>'active' OR NOT EXISTS(SELECT 1 FROM mcp_production_target_scopes i WHERE i.client_id=NEW.client_id AND i.target_id=NEW.target_id AND i.identity_generation=NEW.identity_generation AND i.scope='inspect' AND i.state='active'))) BEGIN SELECT RAISE(ABORT, 'production_inspection_scope_binding_invalid'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_mcp_production_scope_immutable BEFORE UPDATE ON mcp_production_target_scopes WHEN NEW.id<>OLD.id OR NEW.client_id<>OLD.client_id OR NEW.target_id<>OLD.target_id OR NEW.identity_generation<>OLD.identity_generation OR NEW.scope<>OLD.scope OR NEW.bridge_trust_revision IS NOT OLD.bridge_trust_revision OR NEW.created_at<>OLD.created_at OR NEW.created_by<>OLD.created_by OR OLD.state<>'active' OR NEW.state<>'revoked' OR NEW.revoked_at IS NULL OR NEW.revoked_by IS NULL BEGIN SELECT RAISE(ABORT, 'production_inspection_scope_immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_mcp_production_scope_no_delete BEFORE DELETE ON mcp_production_target_scopes BEGIN SELECT RAISE(ABORT, 'production_inspection_scope_immutable'); END"
    )


def _preset_assignment_triggers(allowed: str) -> None:
    op.execute(
        f"CREATE TRIGGER trg_mcp_production_target_preset_insert BEFORE INSERT ON mcp_target_preset_assignments WHEN (COALESCE((SELECT target_type FROM targets WHERE id=NEW.target_id),'')='external_production' AND NEW.preset_id NOT IN {allowed}) OR (NEW.preset_id IN {allowed} AND COALESCE((SELECT target_type FROM targets WHERE id=NEW.target_id),'')<>'external_production') BEGIN SELECT RAISE(ABORT, 'production_inspection_preset_target_invalid'); END"
    )
    op.execute(
        f"CREATE TRIGGER trg_mcp_production_target_preset_update BEFORE UPDATE OF target_id,preset_id ON mcp_target_preset_assignments WHEN (COALESCE((SELECT target_type FROM targets WHERE id=NEW.target_id),'')='external_production' AND NEW.preset_id NOT IN {allowed}) OR (NEW.preset_id IN {allowed} AND COALESCE((SELECT target_type FROM targets WHERE id=NEW.target_id),'')<>'external_production') BEGIN SELECT RAISE(ABORT, 'production_inspection_preset_target_invalid'); END"
    )


def upgrade() -> None:
    op.execute("DROP TRIGGER trg_mcp_production_scope_no_delete")
    op.execute("DROP TRIGGER trg_mcp_production_scope_immutable")
    op.execute("DROP TRIGGER trg_mcp_production_scope_binding_insert")
    with op.batch_alter_table(
        "mcp_production_target_scopes", recreate="always"
    ) as batch:
        batch.drop_constraint("mcp_production_scope_bridge_binding", type_="check")
        batch.drop_constraint("mcp_production_scope_value_closed", type_="check")
        batch.create_check_constraint(
            "mcp_production_scope_value_closed",
            "scope IN ('inspect','visual','navigate')",
        )
        batch.create_check_constraint(
            "mcp_production_scope_bridge_binding",
            "(scope='inspect' AND bridge_trust_revision IS NULL) OR (scope IN ('visual','navigate') AND bridge_trust_revision>=1)",
        )
    _scope_triggers()

    op.execute(
        sa.text(
            "UPDATE mcp_presets SET assignable=1 WHERE id=:preset AND display_name='Production Remote Kodi' AND builtin=1 AND assignable=0"
        ).bindparams(preset=PRODUCTION_REMOTE_PRESET_ID)
    )
    connection = op.get_bind()
    preset = connection.execute(
        sa.text(
            "SELECT display_name,builtin,assignable FROM mcp_presets WHERE id=:preset"
        ),
        {"preset": PRODUCTION_REMOTE_PRESET_ID},
    ).fetchone()
    if preset != ("Production Remote Kodi", 1, 1):
        raise RuntimeError("Production Remote Kodi preset identity has drifted")
    permissions = sa.table(
        "mcp_preset_permissions",
        sa.column("preset_id", sa.String),
        sa.column("permission", sa.String),
    )
    op.bulk_insert(
        permissions,
        [
            {"preset_id": PRODUCTION_REMOTE_PRESET_ID, "permission": permission}
            for permission in ("target.inspect", "target.visual", "target.control")
        ],
    )
    op.execute("DROP TRIGGER trg_mcp_production_target_preset_update")
    op.execute("DROP TRIGGER trg_mcp_production_target_preset_insert")
    _preset_assignment_triggers(
        f"('{PRODUCTION_INSPECTION_PRESET_ID}','{PRODUCTION_VISUAL_PRESET_ID}','{PRODUCTION_REMOTE_PRESET_ID}')"
    )

    op.create_table(
        "production_native_control_http_acknowledgements",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("target_id", sa.String(36), nullable=False),
        sa.Column("identity_generation", sa.Integer(), nullable=False),
        sa.Column("protocol", sa.String(16), nullable=False),
        sa.Column("ip_literal", sa.String(64), nullable=False),
        sa.Column("native_port", sa.Integer(), nullable=False),
        sa.Column("endpoint_digest", sa.String(64), nullable=False),
        sa.Column("policy_revision", sa.Integer(), nullable=False),
        sa.Column("transport_policy_revision", sa.Integer(), nullable=False),
        sa.Column("acknowledged_by", sa.String(64), nullable=False),
        sa.Column("acknowledged_at", sa.String(27), nullable=False),
        sa.CheckConstraint(
            "length(id)=36 AND identity_generation>=1 AND policy_revision>=1 AND transport_policy_revision>=1",
            name="production_native_control_ack_generations_positive",
        ),
        sa.CheckConstraint(
            "protocol='jsonrpc_http' AND length(ip_literal) BETWEEN 2 AND 64 AND native_port BETWEEN 1 AND 65535",
            name="production_native_control_ack_endpoint_valid",
        ),
        sa.CheckConstraint(
            "length(endpoint_digest)=64 AND endpoint_digest NOT GLOB '*[^0-9a-f]*'",
            name="production_native_control_ack_digest_sha256",
        ),
        sa.CheckConstraint(
            "length(acknowledged_by) BETWEEN 1 AND 64 AND length(acknowledged_at)=27",
            name="production_native_control_ack_administrator_bounded",
        ),
        sa.ForeignKeyConstraint(
            ["target_id"],
            ["external_production_policies.target_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "target_id",
            "identity_generation",
            "policy_revision",
            "endpoint_digest",
            "transport_policy_revision",
            name="uq_production_native_control_http_ack_binding",
        ),
    )
    op.create_index(
        "ix_production_native_control_http_ack_target",
        "production_native_control_http_acknowledgements",
        ["target_id", "identity_generation", "policy_revision"],
    )
    op.execute(
        "CREATE TRIGGER trg_production_native_control_ack_immutable BEFORE UPDATE ON production_native_control_http_acknowledgements BEGIN SELECT RAISE(ABORT, 'production_native_control_ack_immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_production_native_control_ack_no_delete BEFORE DELETE ON production_native_control_http_acknowledgements BEGIN SELECT RAISE(ABORT, 'production_native_control_ack_immutable'); END"
    )

    op.create_table(
        "mcp_production_remote_receipt_bindings",
        sa.Column("receipt_id", sa.String(36), nullable=False),
        sa.Column("operation_class", sa.String(16), nullable=False),
        sa.Column("native_identity_generation", sa.Integer(), nullable=False),
        sa.Column("bridge_trust_revision", sa.Integer(), nullable=False),
        sa.Column("target_policy_revision", sa.Integer(), nullable=False),
        sa.Column("mutation_control_epoch", sa.Integer(), nullable=False),
        sa.Column("inspect_scope_id", sa.String(36), nullable=False),
        sa.Column("navigate_scope_id", sa.String(36), nullable=False),
        sa.Column("client_preset_id", sa.String(36), nullable=False),
        sa.Column("target_preset_id", sa.String(36), nullable=False),
        sa.Column("generic_grant_created_at", sa.String(27), nullable=False),
        sa.Column("native_http_acknowledgement_id", sa.String(36)),
        sa.CheckConstraint(
            "length(receipt_id)=36 AND operation_class='navigation'",
            name="mcp_production_remote_binding_identity_valid",
        ),
        sa.CheckConstraint(
            "native_identity_generation>=1 AND bridge_trust_revision>=1 AND target_policy_revision>=1 AND mutation_control_epoch>=1",
            name="mcp_production_remote_binding_revisions_positive",
        ),
        sa.CheckConstraint(
            "length(inspect_scope_id)=36 AND length(navigate_scope_id)=36 AND length(client_preset_id)=36 AND length(target_preset_id)=36",
            name="mcp_production_remote_binding_authority_shapes",
        ),
        sa.CheckConstraint(
            "length(generic_grant_created_at)=27 AND (native_http_acknowledgement_id IS NULL OR length(native_http_acknowledgement_id)=36)",
            name="mcp_production_remote_binding_optional_shapes",
        ),
        sa.ForeignKeyConstraint(
            ["receipt_id"], ["mcp_remote_action_receipts.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("receipt_id"),
    )
    op.create_index(
        "ix_mcp_production_remote_binding_scopes",
        "mcp_production_remote_receipt_bindings",
        ["inspect_scope_id", "navigate_scope_id"],
    )
    op.execute(
        "CREATE TRIGGER trg_mcp_production_remote_binding_immutable BEFORE UPDATE ON mcp_production_remote_receipt_bindings BEGIN SELECT RAISE(ABORT, 'production_remote_binding_immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_mcp_production_remote_binding_no_delete BEFORE DELETE ON mcp_production_remote_receipt_bindings WHEN NOT EXISTS(SELECT 1 FROM mcp_remote_action_receipts r JOIN mcp_production_navigation_audits a ON a.receipt_id=r.id WHERE r.id=OLD.receipt_id AND r.state='completed' AND r.final_result IS NOT NULL AND r.completed_at IS NOT NULL AND r.screenshot_refresh_result IS NOT NULL AND a.client_id=r.client_id AND a.credential_id=r.credential_id AND a.target_id=r.target_id AND a.request_id=r.request_id AND a.action=r.action AND a.operation_class=OLD.operation_class AND a.native_identity_generation=OLD.native_identity_generation AND a.bridge_trust_revision=OLD.bridge_trust_revision AND a.target_policy_revision=OLD.target_policy_revision AND a.mutation_control_epoch=OLD.mutation_control_epoch AND a.inspect_scope_id=OLD.inspect_scope_id AND a.navigate_scope_id=OLD.navigate_scope_id AND a.state='completed' AND a.outcome=r.final_result AND a.requested_at=r.created_at AND a.completed_at=r.completed_at AND ((r.final_result IN ('succeeded','authority_changed_after_dispatch','remote_kodi_rejected') AND a.dispatch_classification='acknowledged') OR (r.final_result NOT IN ('succeeded','authority_changed_after_dispatch','remote_kodi_rejected') AND a.dispatch_classification='not_dispatched'))) BEGIN SELECT RAISE(ABORT, 'production_remote_binding_protected'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_mcp_production_remote_receipt_protected BEFORE DELETE ON mcp_remote_action_receipts WHEN EXISTS(SELECT 1 FROM mcp_production_remote_receipt_bindings b WHERE b.receipt_id=OLD.id) BEGIN SELECT RAISE(ABORT, 'production_remote_receipt_protected'); END"
    )

    op.create_table(
        "mcp_production_navigation_audits",
        sa.Column("receipt_id", sa.String(36), nullable=False),
        sa.Column("client_id", sa.String(36), nullable=False),
        sa.Column("credential_id", sa.String(36), nullable=False),
        sa.Column("target_id", sa.String(36), nullable=False),
        sa.Column("request_id", sa.String(36), nullable=False),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("operation_class", sa.String(16), nullable=False),
        sa.Column("native_identity_generation", sa.Integer(), nullable=False),
        sa.Column("bridge_trust_revision", sa.Integer(), nullable=False),
        sa.Column("target_policy_revision", sa.Integer(), nullable=False),
        sa.Column("mutation_control_epoch", sa.Integer(), nullable=False),
        sa.Column("inspect_scope_id", sa.String(36), nullable=False),
        sa.Column("navigate_scope_id", sa.String(36), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("outcome", sa.String(64), nullable=False),
        sa.Column("dispatch_classification", sa.String(16), nullable=False),
        sa.Column("requested_at", sa.String(27), nullable=False),
        sa.Column("completed_at", sa.String(27)),
        sa.CheckConstraint(
            "length(receipt_id)=36 AND length(client_id)=36 AND length(credential_id)=36 AND length(target_id)=36 AND length(request_id)=36",
            name="mcp_production_navigation_audit_uuid_shapes",
        ),
        sa.CheckConstraint(
            "operation_class='navigation' AND action IN ('up','down','left','right','back','home','info')",
            name="mcp_production_navigation_audit_semantics_closed",
        ),
        sa.CheckConstraint(
            "native_identity_generation>=1 AND bridge_trust_revision>=1 AND target_policy_revision>=1 AND mutation_control_epoch>=1 AND length(inspect_scope_id)=36 AND length(navigate_scope_id)=36",
            name="mcp_production_navigation_audit_authority_valid",
        ),
        sa.CheckConstraint(
            "state IN ('pending','completed','ambiguous') AND dispatch_classification IN ('not_dispatched','acknowledged','ambiguous')",
            name="mcp_production_navigation_audit_outcome_closed",
        ),
        sa.CheckConstraint(
            "length(outcome) BETWEEN 1 AND 64 AND outcome=lower(outcome) AND outcome=trim(outcome)",
            name="mcp_production_navigation_audit_outcome_bounded",
        ),
        sa.CheckConstraint(
            "length(requested_at)=27 AND (completed_at IS NULL OR length(completed_at)=27) AND ((state='pending' AND completed_at IS NULL) OR (state IN ('completed','ambiguous') AND completed_at IS NOT NULL))",
            name="mcp_production_navigation_audit_timestamps_valid",
        ),
        sa.ForeignKeyConstraint(
            ["receipt_id"], ["mcp_remote_action_receipts.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("receipt_id"),
    )
    op.create_index(
        "ix_mcp_production_navigation_audit_target_time",
        "mcp_production_navigation_audits",
        ["target_id", "requested_at"],
    )
    op.execute(
        "CREATE TRIGGER trg_mcp_production_navigation_audit_terminal BEFORE UPDATE ON mcp_production_navigation_audits WHEN NEW.receipt_id<>OLD.receipt_id OR NEW.client_id<>OLD.client_id OR NEW.credential_id<>OLD.credential_id OR NEW.target_id<>OLD.target_id OR NEW.request_id<>OLD.request_id OR NEW.action<>OLD.action OR NEW.operation_class<>OLD.operation_class OR NEW.native_identity_generation<>OLD.native_identity_generation OR NEW.bridge_trust_revision<>OLD.bridge_trust_revision OR NEW.target_policy_revision<>OLD.target_policy_revision OR NEW.mutation_control_epoch<>OLD.mutation_control_epoch OR NEW.inspect_scope_id<>OLD.inspect_scope_id OR NEW.navigate_scope_id<>OLD.navigate_scope_id OR NEW.requested_at<>OLD.requested_at OR OLD.state<>'pending' OR NEW.state NOT IN ('completed','ambiguous') OR NEW.completed_at IS NULL BEGIN SELECT RAISE(ABORT, 'production_navigation_audit_immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_mcp_production_navigation_audit_no_delete BEFORE DELETE ON mcp_production_navigation_audits WHEN NOT EXISTS(SELECT 1 FROM mcp_remote_action_receipts r WHERE r.id=OLD.receipt_id AND r.state='completed' AND r.final_result IS NOT NULL AND r.completed_at IS NOT NULL AND r.screenshot_refresh_result IS NOT NULL AND OLD.client_id=r.client_id AND OLD.credential_id=r.credential_id AND OLD.target_id=r.target_id AND OLD.request_id=r.request_id AND OLD.action=r.action AND OLD.state='completed' AND OLD.outcome=r.final_result AND OLD.requested_at=r.created_at AND OLD.completed_at=r.completed_at AND ((r.final_result IN ('succeeded','authority_changed_after_dispatch','remote_kodi_rejected') AND OLD.dispatch_classification='acknowledged') OR (r.final_result NOT IN ('succeeded','authority_changed_after_dispatch','remote_kodi_rejected') AND OLD.dispatch_classification='not_dispatched'))) BEGIN SELECT RAISE(ABORT, 'production_navigation_audit_protected'); END"
    )


def downgrade() -> None:
    connection = op.get_bind()
    populated = connection.execute(
        sa.text(
            "SELECT EXISTS(SELECT 1 FROM mcp_production_target_scopes WHERE scope='navigate') OR EXISTS(SELECT 1 FROM production_native_control_http_acknowledgements) OR EXISTS(SELECT 1 FROM mcp_production_remote_receipt_bindings) OR EXISTS(SELECT 1 FROM mcp_production_navigation_audits) OR EXISTS(SELECT 1 FROM mcp_clients WHERE preset_id=:preset) OR EXISTS(SELECT 1 FROM mcp_target_preset_assignments WHERE preset_id=:preset)"
        ),
        {"preset": PRODUCTION_REMOTE_PRESET_ID},
    ).scalar_one()
    if populated:
        raise RuntimeError(
            "refusing destructive downgrade while production navigation authority or history exists"
        )
    op.execute("DROP TRIGGER trg_mcp_production_navigation_audit_no_delete")
    op.execute("DROP TRIGGER trg_mcp_production_navigation_audit_terminal")
    op.drop_index(
        "ix_mcp_production_navigation_audit_target_time",
        table_name="mcp_production_navigation_audits",
    )
    op.drop_table("mcp_production_navigation_audits")
    op.execute("DROP TRIGGER trg_mcp_production_remote_receipt_protected")
    op.execute("DROP TRIGGER trg_mcp_production_remote_binding_no_delete")
    op.execute("DROP TRIGGER trg_mcp_production_remote_binding_immutable")
    op.drop_index(
        "ix_mcp_production_remote_binding_scopes",
        table_name="mcp_production_remote_receipt_bindings",
    )
    op.drop_table("mcp_production_remote_receipt_bindings")
    op.execute("DROP TRIGGER trg_production_native_control_ack_no_delete")
    op.execute("DROP TRIGGER trg_production_native_control_ack_immutable")
    op.drop_index(
        "ix_production_native_control_http_ack_target",
        table_name="production_native_control_http_acknowledgements",
    )
    op.drop_table("production_native_control_http_acknowledgements")
    op.execute("DROP TRIGGER trg_mcp_production_target_preset_update")
    op.execute("DROP TRIGGER trg_mcp_production_target_preset_insert")
    _preset_assignment_triggers(
        f"('{PRODUCTION_INSPECTION_PRESET_ID}','{PRODUCTION_VISUAL_PRESET_ID}')"
    )
    op.execute(
        sa.text(
            "DELETE FROM mcp_preset_permissions WHERE preset_id=:preset"
        ).bindparams(preset=PRODUCTION_REMOTE_PRESET_ID)
    )
    op.execute(
        sa.text("UPDATE mcp_presets SET assignable=0 WHERE id=:preset").bindparams(
            preset=PRODUCTION_REMOTE_PRESET_ID
        )
    )
    op.execute("DROP TRIGGER trg_mcp_production_scope_no_delete")
    op.execute("DROP TRIGGER trg_mcp_production_scope_immutable")
    op.execute("DROP TRIGGER trg_mcp_production_scope_binding_insert")
    with op.batch_alter_table(
        "mcp_production_target_scopes", recreate="always"
    ) as batch:
        batch.drop_constraint("mcp_production_scope_bridge_binding", type_="check")
        batch.drop_constraint("mcp_production_scope_value_closed", type_="check")
        batch.create_check_constraint(
            "mcp_production_scope_value_closed", "scope IN ('inspect','visual')"
        )
        batch.create_check_constraint(
            "mcp_production_scope_bridge_binding",
            "(scope='inspect' AND bridge_trust_revision IS NULL) OR (scope='visual' AND bridge_trust_revision>=1)",
        )
    # Recreate the exact Slice 3 trigger set.
    op.execute(
        "CREATE TRIGGER trg_mcp_production_scope_binding_insert BEFORE INSERT ON mcp_production_target_scopes WHEN COALESCE((SELECT target_type FROM targets WHERE id=NEW.target_id),'')<>'external_production' OR COALESCE((SELECT identity_generation FROM external_production_policies WHERE target_id=NEW.target_id),0)<>NEW.identity_generation OR NEW.state<>'active' OR NEW.revoked_at IS NOT NULL OR NEW.revoked_by IS NOT NULL OR (NEW.scope='visual' AND (COALESCE((SELECT production_bridge_trust_revision FROM external_bridge_pairings WHERE target_id=NEW.target_id),0)<>NEW.bridge_trust_revision OR COALESCE((SELECT production_identity_generation FROM external_bridge_pairings WHERE target_id=NEW.target_id),0)<>NEW.identity_generation OR COALESCE((SELECT pairing_state FROM external_bridge_pairings WHERE target_id=NEW.target_id),'')<>'paired' OR COALESCE((SELECT reachability_state FROM external_bridge_pairings WHERE target_id=NEW.target_id),'')<>'reachable' OR COALESCE((SELECT state FROM reviewed_bridge_builds WHERE id=(SELECT reviewed_build_id FROM external_bridge_pairings WHERE target_id=NEW.target_id)),'')<>'active' OR NOT EXISTS(SELECT 1 FROM mcp_production_target_scopes i WHERE i.client_id=NEW.client_id AND i.target_id=NEW.target_id AND i.identity_generation=NEW.identity_generation AND i.scope='inspect' AND i.state='active'))) BEGIN SELECT RAISE(ABORT, 'production_inspection_scope_binding_invalid'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_mcp_production_scope_immutable BEFORE UPDATE ON mcp_production_target_scopes WHEN NEW.id<>OLD.id OR NEW.client_id<>OLD.client_id OR NEW.target_id<>OLD.target_id OR NEW.identity_generation<>OLD.identity_generation OR NEW.scope<>OLD.scope OR NEW.bridge_trust_revision IS NOT OLD.bridge_trust_revision OR NEW.created_at<>OLD.created_at OR NEW.created_by<>OLD.created_by OR OLD.state<>'active' OR NEW.state<>'revoked' OR NEW.revoked_at IS NULL OR NEW.revoked_by IS NULL BEGIN SELECT RAISE(ABORT, 'production_inspection_scope_immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_mcp_production_scope_no_delete BEFORE DELETE ON mcp_production_target_scopes BEGIN SELECT RAISE(ABORT, 'production_inspection_scope_immutable'); END"
    )
