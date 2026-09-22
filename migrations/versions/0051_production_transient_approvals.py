"""Add one-shot production transient-control approvals.

Revision ID: 0051_production_transient_approvals
Revises: 0050_production_navigation
"""

# Closed SQLite constraints and triggers are intentionally explicit.
# ruff: noqa: E501

from collections.abc import Sequence
from uuid import UUID, uuid5

import sqlalchemy as sa
from alembic import op

revision: str = "0051_production_transient_approvals"
down_revision: str | Sequence[str] | None = "0050_production_navigation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_GRANT_NAMESPACE = UUID("51a30000-0000-4000-8000-000000000051")


def _grant_id(client_id: str, target_id: str, created_at: str) -> str:
    return str(uuid5(_GRANT_NAMESPACE, f"{client_id}:{target_id}:{created_at}"))


def _scope_triggers() -> None:
    op.execute(
        "CREATE TRIGGER trg_mcp_production_scope_binding_insert BEFORE INSERT ON mcp_production_target_scopes WHEN COALESCE((SELECT target_type FROM targets WHERE id=NEW.target_id),'')<>'external_production' OR COALESCE((SELECT identity_generation FROM external_production_policies WHERE target_id=NEW.target_id),0)<>NEW.identity_generation OR NEW.state<>'active' OR NEW.revoked_at IS NOT NULL OR NEW.revoked_by IS NOT NULL OR (NEW.scope IN ('visual','navigate','request_transient_control') AND (COALESCE((SELECT production_bridge_trust_revision FROM external_bridge_pairings WHERE target_id=NEW.target_id),0)<>NEW.bridge_trust_revision OR COALESCE((SELECT production_identity_generation FROM external_bridge_pairings WHERE target_id=NEW.target_id),0)<>NEW.identity_generation OR COALESCE((SELECT pairing_state FROM external_bridge_pairings WHERE target_id=NEW.target_id),'')<>'paired' OR COALESCE((SELECT reachability_state FROM external_bridge_pairings WHERE target_id=NEW.target_id),'')<>'reachable' OR COALESCE((SELECT state FROM reviewed_bridge_builds WHERE id=(SELECT reviewed_build_id FROM external_bridge_pairings WHERE target_id=NEW.target_id)),'')<>'active' OR NOT EXISTS(SELECT 1 FROM mcp_production_target_scopes i WHERE i.client_id=NEW.client_id AND i.target_id=NEW.target_id AND i.identity_generation=NEW.identity_generation AND i.scope='inspect' AND i.state='active'))) BEGIN SELECT RAISE(ABORT, 'production_inspection_scope_binding_invalid'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_mcp_production_scope_immutable BEFORE UPDATE ON mcp_production_target_scopes WHEN NEW.id<>OLD.id OR NEW.client_id<>OLD.client_id OR NEW.target_id<>OLD.target_id OR NEW.identity_generation<>OLD.identity_generation OR NEW.scope<>OLD.scope OR NEW.bridge_trust_revision IS NOT OLD.bridge_trust_revision OR NEW.created_at<>OLD.created_at OR NEW.created_by<>OLD.created_by OR OLD.state<>'active' OR NEW.state<>'revoked' OR NEW.revoked_at IS NULL OR NEW.revoked_by IS NULL BEGIN SELECT RAISE(ABORT, 'production_inspection_scope_immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_mcp_production_scope_no_delete BEFORE DELETE ON mcp_production_target_scopes BEGIN SELECT RAISE(ABORT, 'production_inspection_scope_immutable'); END"
    )


def _binding_triggers() -> None:
    op.execute(
        "CREATE TRIGGER trg_mcp_production_remote_binding_immutable BEFORE UPDATE ON mcp_production_remote_receipt_bindings BEGIN SELECT RAISE(ABORT, 'production_remote_binding_immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_mcp_production_remote_binding_no_delete BEFORE DELETE ON mcp_production_remote_receipt_bindings WHEN NOT ((OLD.operation_class='navigation' AND EXISTS(SELECT 1 FROM mcp_remote_action_receipts r JOIN mcp_production_navigation_audits a ON a.receipt_id=r.id WHERE r.id=OLD.receipt_id AND r.state='completed' AND r.final_result IS NOT NULL AND r.completed_at IS NOT NULL AND r.screenshot_refresh_result IS NOT NULL AND a.client_id=r.client_id AND a.credential_id=r.credential_id AND a.target_id=r.target_id AND a.request_id=r.request_id AND a.action=r.action AND a.operation_class=OLD.operation_class AND a.native_identity_generation=OLD.native_identity_generation AND a.bridge_trust_revision=OLD.bridge_trust_revision AND a.target_policy_revision=OLD.target_policy_revision AND a.mutation_control_epoch=OLD.mutation_control_epoch AND a.inspect_scope_id=OLD.inspect_scope_id AND a.navigate_scope_id=OLD.navigate_scope_id AND a.state='completed' AND a.outcome=r.final_result AND a.requested_at=r.created_at AND a.completed_at=r.completed_at AND ((r.final_result IN ('succeeded','authority_changed_after_dispatch','remote_kodi_rejected') AND a.dispatch_classification='acknowledged') OR (r.final_result NOT IN ('succeeded','authority_changed_after_dispatch','remote_kodi_rejected') AND a.dispatch_classification='not_dispatched')))) OR (OLD.operation_class='transient_control' AND EXISTS(SELECT 1 FROM mcp_remote_action_receipts r JOIN production_transient_dispatch_audits a ON a.receipt_id=r.id WHERE r.id=OLD.receipt_id AND r.state='completed' AND r.final_result IS NOT NULL AND r.completed_at IS NOT NULL AND r.screenshot_refresh_result IS NOT NULL AND a.approval_id=OLD.approval_id AND a.client_id=r.client_id AND a.credential_id=r.credential_id AND a.target_id=r.target_id AND a.request_id=r.request_id AND a.action=r.action AND a.state='completed' AND a.outcome=r.final_result AND a.requested_at=r.created_at AND a.completed_at=r.completed_at AND ((r.final_result IN ('succeeded','authority_changed_after_dispatch','remote_kodi_rejected') AND a.dispatch_classification='acknowledged') OR (r.final_result NOT IN ('succeeded','authority_changed_after_dispatch','remote_kodi_rejected') AND a.dispatch_classification='not_dispatched'))))) BEGIN SELECT RAISE(ABORT, 'production_remote_binding_protected'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_mcp_production_remote_receipt_protected BEFORE DELETE ON mcp_remote_action_receipts WHEN EXISTS(SELECT 1 FROM mcp_production_remote_receipt_bindings b WHERE b.receipt_id=OLD.id) BEGIN SELECT RAISE(ABORT, 'production_remote_receipt_protected'); END"
    )


def upgrade() -> None:
    op.add_column(
        "mcp_clients",
        sa.Column(
            "authority_revision", sa.Integer(), nullable=False, server_default="1"
        ),
    )
    with op.batch_alter_table("mcp_clients", recreate="always") as batch:
        batch.create_check_constraint(
            "mcp_client_authority_revision_positive", "authority_revision>=1"
        )
    op.execute(
        "CREATE TRIGGER trg_mcp_client_authority_revision BEFORE UPDATE ON mcp_clients WHEN (((NEW.enabled<>OLD.enabled OR NEW.preset_id<>OLD.preset_id) AND NEW.authority_revision<=OLD.authority_revision) OR ((NEW.enabled=OLD.enabled AND NEW.preset_id=OLD.preset_id) AND NEW.authority_revision<>OLD.authority_revision)) BEGIN SELECT RAISE(ABORT, 'mcp_client_authority_revision_invalid'); END"
    )

    op.add_column("mcp_client_target_grants", sa.Column("grant_id", sa.String(36)))
    connection = op.get_bind()
    for client_id, target_id, created_at in connection.execute(
        sa.text("SELECT client_id,target_id,created_at FROM mcp_client_target_grants")
    ):
        connection.execute(
            sa.text(
                "UPDATE mcp_client_target_grants SET grant_id=:grant_id "
                "WHERE client_id=:client_id AND target_id=:target_id"
            ),
            {
                "grant_id": _grant_id(client_id, target_id, created_at),
                "client_id": client_id,
                "target_id": target_id,
            },
        )
    with op.batch_alter_table("mcp_client_target_grants", recreate="always") as batch:
        batch.alter_column("grant_id", existing_type=sa.String(36), nullable=False)
        batch.create_check_constraint(
            "mcp_client_target_grant_id_shape", "length(grant_id)=36"
        )
        batch.create_unique_constraint("uq_mcp_client_target_grant_id", ["grant_id"])
    op.execute(
        "CREATE TRIGGER trg_mcp_client_target_grant_identity_immutable BEFORE UPDATE OF grant_id ON mcp_client_target_grants WHEN NEW.grant_id<>OLD.grant_id BEGIN SELECT RAISE(ABORT, 'mcp_client_target_grant_identity_immutable'); END"
    )

    op.execute("DROP TRIGGER trg_mcp_production_scope_no_delete")
    op.execute("DROP TRIGGER trg_mcp_production_scope_immutable")
    op.execute("DROP TRIGGER trg_mcp_production_scope_binding_insert")
    with op.batch_alter_table(
        "mcp_production_target_scopes", recreate="always"
    ) as batch:
        batch.drop_constraint("mcp_production_scope_bridge_binding", type_="check")
        batch.drop_constraint("mcp_production_scope_value_closed", type_="check")
        batch.alter_column(
            "scope", existing_type=sa.String(16), type_=sa.String(32), nullable=False
        )
        batch.create_check_constraint(
            "mcp_production_scope_value_closed",
            "scope IN ('inspect','visual','navigate','request_transient_control')",
        )
        batch.create_check_constraint(
            "mcp_production_scope_bridge_binding",
            "(scope='inspect' AND bridge_trust_revision IS NULL) OR (scope IN ('visual','navigate','request_transient_control') AND bridge_trust_revision>=1)",
        )
    _scope_triggers()

    op.execute("DROP TRIGGER trg_mcp_production_remote_binding_no_delete")
    op.execute("DROP TRIGGER trg_mcp_production_remote_binding_immutable")
    op.execute("DROP TRIGGER trg_mcp_production_remote_receipt_protected")
    op.add_column(
        "mcp_production_remote_receipt_bindings",
        sa.Column("transient_scope_id", sa.String(36)),
    )
    op.add_column(
        "mcp_production_remote_receipt_bindings",
        sa.Column("approval_id", sa.String(36)),
    )
    op.add_column(
        "mcp_production_remote_receipt_bindings",
        sa.Column("generic_grant_id", sa.String(36)),
    )
    for receipt_id, client_id, target_id, created_at in connection.execute(
        sa.text(
            "SELECT b.receipt_id,r.client_id,r.target_id,b.generic_grant_created_at "
            "FROM mcp_production_remote_receipt_bindings b "
            "JOIN mcp_remote_action_receipts r ON r.id=b.receipt_id"
        )
    ):
        connection.execute(
            sa.text(
                "UPDATE mcp_production_remote_receipt_bindings "
                "SET generic_grant_id=:grant_id WHERE receipt_id=:receipt_id"
            ),
            {
                "grant_id": _grant_id(client_id, target_id, created_at),
                "receipt_id": receipt_id,
            },
        )
    with op.batch_alter_table(
        "mcp_production_remote_receipt_bindings", recreate="always"
    ) as batch:
        batch.drop_constraint(
            "mcp_production_remote_binding_identity_valid", type_="check"
        )
        batch.drop_constraint(
            "mcp_production_remote_binding_authority_shapes", type_="check"
        )
        batch.alter_column(
            "navigate_scope_id", existing_type=sa.String(36), nullable=True
        )
        batch.alter_column(
            "generic_grant_id", existing_type=sa.String(36), nullable=False
        )
        batch.create_check_constraint(
            "mcp_production_remote_binding_identity_valid",
            "length(receipt_id)=36 AND operation_class IN ('navigation','transient_control')",
        )
        batch.create_check_constraint(
            "mcp_production_remote_binding_authority_shapes",
            "length(inspect_scope_id)=36 AND length(client_preset_id)=36 AND length(target_preset_id)=36 AND length(generic_grant_id)=36 AND ((operation_class='navigation' AND length(navigate_scope_id)=36 AND transient_scope_id IS NULL AND approval_id IS NULL) OR (operation_class='transient_control' AND navigate_scope_id IS NULL AND length(transient_scope_id)=36 AND length(approval_id)=36))",
        )
    op.drop_index(
        "ix_mcp_production_remote_binding_scopes",
        table_name="mcp_production_remote_receipt_bindings",
    )
    op.create_index(
        "ix_mcp_production_remote_binding_scopes",
        "mcp_production_remote_receipt_bindings",
        ["inspect_scope_id", "navigate_scope_id", "transient_scope_id"],
    )

    op.create_table(
        "production_operation_approvals",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("client_id", sa.String(36), nullable=False),
        sa.Column("credential_id", sa.String(36), nullable=False),
        sa.Column("target_id", sa.String(36), nullable=False),
        sa.Column("tool", sa.String(32), nullable=False),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("payload_sha256", sa.String(64), nullable=False),
        sa.Column("canonicalization_revision", sa.Integer(), nullable=False),
        sa.Column("request_id", sa.String(36), nullable=False),
        sa.Column("native_identity_generation", sa.Integer(), nullable=False),
        sa.Column("bridge_trust_revision", sa.Integer(), nullable=False),
        sa.Column("target_policy_revision", sa.Integer(), nullable=False),
        sa.Column("mutation_control_epoch", sa.Integer(), nullable=False),
        sa.Column("client_authority_revision", sa.Integer(), nullable=False),
        sa.Column("generic_grant_id", sa.String(36), nullable=False),
        sa.Column("client_preset_id", sa.String(36), nullable=False),
        sa.Column("target_preset_id", sa.String(36), nullable=False),
        sa.Column("inspect_scope_id", sa.String(36), nullable=False),
        sa.Column("transient_scope_id", sa.String(36), nullable=False),
        sa.Column("native_http_acknowledgement_id", sa.String(36)),
        sa.Column("reviewed_build_id", sa.String(36)),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("terminal_reason", sa.String(32)),
        sa.Column("created_at", sa.String(27), nullable=False),
        sa.Column("pending_expires_at", sa.String(27), nullable=False),
        sa.Column("approved_at", sa.String(27)),
        sa.Column("approved_dispatch_expires_at", sa.String(27)),
        sa.Column("approving_administrator", sa.String(64)),
        sa.Column("consumed_at", sa.String(27)),
        sa.Column("linked_remote_receipt_id", sa.String(36)),
        sa.CheckConstraint(
            "length(id)=36 AND length(client_id)=36 AND length(credential_id)=36 AND length(target_id)=36 AND length(request_id)=36",
            name="production_approval_primary_uuid_shapes",
        ),
        sa.CheckConstraint(
            "tool='remote_action' AND action IN ('play_pause','stop','previous','next','volume_up','volume_down','mute_toggle')",
            name="production_approval_semantics_closed",
        ),
        sa.CheckConstraint(
            "length(payload_sha256)=64 AND payload_sha256 NOT GLOB '*[^0-9a-f]*' AND canonicalization_revision>=1",
            name="production_approval_payload_digest_valid",
        ),
        sa.CheckConstraint(
            "native_identity_generation>=1 AND bridge_trust_revision>=1 AND target_policy_revision>=1 AND mutation_control_epoch>=1 AND client_authority_revision>=1",
            name="production_approval_revisions_positive",
        ),
        sa.CheckConstraint(
            "length(generic_grant_id)=36 AND length(client_preset_id)=36 AND length(target_preset_id)=36 AND length(inspect_scope_id)=36 AND length(transient_scope_id)=36",
            name="production_approval_authority_uuid_shapes",
        ),
        sa.CheckConstraint(
            "native_http_acknowledgement_id IS NULL OR length(native_http_acknowledgement_id)=36",
            name="production_approval_ack_shape",
        ),
        sa.CheckConstraint(
            "state IN ('pending','approved','denied','cancelled','expired','consumed')",
            name="production_approval_state_closed",
        ),
        sa.CheckConstraint(
            "terminal_reason IS NULL OR terminal_reason IN ('administrator_denied','administrator_cancelled','authority_changed','mutation_policy_changed','capability_lost','expired')",
            name="production_approval_terminal_reason_closed",
        ),
        sa.CheckConstraint(
            "length(created_at)=27 AND length(pending_expires_at)=27 AND pending_expires_at>created_at AND (approved_at IS NULL OR length(approved_at)=27) AND (approved_dispatch_expires_at IS NULL OR length(approved_dispatch_expires_at)=27) AND (consumed_at IS NULL OR length(consumed_at)=27)",
            name="production_approval_timestamps_valid",
        ),
        sa.CheckConstraint(
            "(state='pending' AND terminal_reason IS NULL AND approved_at IS NULL AND approved_dispatch_expires_at IS NULL AND approving_administrator IS NULL AND consumed_at IS NULL AND linked_remote_receipt_id IS NULL) OR (state='approved' AND terminal_reason IS NULL AND approved_at IS NOT NULL AND approved_dispatch_expires_at IS NOT NULL AND approving_administrator IS NOT NULL AND consumed_at IS NULL AND linked_remote_receipt_id IS NULL) OR (state IN ('denied','cancelled','expired') AND terminal_reason IS NOT NULL AND consumed_at IS NULL AND linked_remote_receipt_id IS NULL) OR (state='consumed' AND terminal_reason IS NULL AND approved_at IS NOT NULL AND approved_dispatch_expires_at IS NOT NULL AND approving_administrator IS NOT NULL AND consumed_at IS NOT NULL AND length(linked_remote_receipt_id)=36)",
            name="production_approval_state_shape",
        ),
        sa.CheckConstraint(
            "approving_administrator IS NULL OR length(approving_administrator) BETWEEN 1 AND 64",
            name="production_approval_administrator_bounded",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "client_id", "request_id", name="uq_production_approval_client_request"
        ),
    )
    op.create_index(
        "ix_production_approval_queue",
        "production_operation_approvals",
        ["state", "pending_expires_at"],
    )
    op.create_index(
        "ix_production_approval_client_history",
        "production_operation_approvals",
        ["client_id", "created_at", "id"],
    )
    op.create_index(
        "ix_production_approval_target_state",
        "production_operation_approvals",
        ["target_id", "state"],
    )

    op.create_table(
        "production_approval_transition_events",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("approval_id", sa.String(36), nullable=False),
        sa.Column("event_kind", sa.String(16), nullable=False),
        sa.Column("from_state", sa.String(16)),
        sa.Column("to_state", sa.String(16), nullable=False),
        sa.Column("terminal_reason", sa.String(32)),
        sa.Column("actor_kind", sa.String(16), nullable=False),
        sa.Column("actor_reference", sa.String(64), nullable=False),
        sa.Column("linked_remote_receipt_id", sa.String(36)),
        sa.Column("occurred_at", sa.String(27), nullable=False),
        sa.CheckConstraint(
            "length(id)=36 AND length(approval_id)=36",
            name="production_approval_event_uuid_shapes",
        ),
        sa.CheckConstraint(
            "from_state IS NULL OR from_state IN ('pending','approved')",
            name="production_approval_event_from_state_closed",
        ),
        sa.CheckConstraint(
            "to_state IN ('pending','approved','denied','cancelled','expired','consumed')",
            name="production_approval_event_to_state_closed",
        ),
        sa.CheckConstraint(
            "event_kind IN ('requested','approved','denied','cancelled','expired','consumed')",
            name="production_approval_event_kind_closed",
        ),
        sa.CheckConstraint(
            "actor_kind IN ('mcp_client','web_admin','system') AND length(actor_reference) BETWEEN 1 AND 64 AND length(occurred_at)=27",
            name="production_approval_event_actor_bounded",
        ),
        sa.CheckConstraint(
            "terminal_reason IS NULL OR terminal_reason IN ('administrator_denied','administrator_cancelled','authority_changed','mutation_policy_changed','capability_lost','expired')",
            name="production_approval_event_reason_closed",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_production_approval_event_approval_time",
        "production_approval_transition_events",
        ["approval_id", "occurred_at", "id"],
    )

    op.create_table(
        "production_transient_dispatch_audits",
        sa.Column("receipt_id", sa.String(36), nullable=False),
        sa.Column("approval_id", sa.String(36), nullable=False),
        sa.Column("client_id", sa.String(36), nullable=False),
        sa.Column("credential_id", sa.String(36), nullable=False),
        sa.Column("target_id", sa.String(36), nullable=False),
        sa.Column("request_id", sa.String(36), nullable=False),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("payload_sha256", sa.String(64), nullable=False),
        sa.Column("canonicalization_revision", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("outcome", sa.String(64), nullable=False),
        sa.Column("dispatch_classification", sa.String(16), nullable=False),
        sa.Column("requested_at", sa.String(27), nullable=False),
        sa.Column("completed_at", sa.String(27)),
        sa.CheckConstraint(
            "length(receipt_id)=36 AND length(approval_id)=36 AND length(client_id)=36 AND length(credential_id)=36 AND length(target_id)=36 AND length(request_id)=36",
            name="production_transient_audit_uuid_shapes",
        ),
        sa.CheckConstraint(
            "action IN ('play_pause','stop','previous','next','volume_up','volume_down','mute_toggle')",
            name="production_transient_audit_action_closed",
        ),
        sa.CheckConstraint(
            "length(payload_sha256)=64 AND payload_sha256 NOT GLOB '*[^0-9a-f]*' AND canonicalization_revision>=1",
            name="production_transient_audit_digest_valid",
        ),
        sa.CheckConstraint(
            "state IN ('pending','completed','ambiguous') AND dispatch_classification IN ('not_dispatched','acknowledged','ambiguous')",
            name="production_transient_audit_state_closed",
        ),
        sa.CheckConstraint(
            "length(outcome) BETWEEN 1 AND 64 AND outcome=lower(outcome) AND outcome=trim(outcome)",
            name="production_transient_audit_outcome_bounded",
        ),
        sa.CheckConstraint(
            "length(requested_at)=27 AND (completed_at IS NULL OR length(completed_at)=27)",
            name="production_transient_audit_timestamps_valid",
        ),
        sa.PrimaryKeyConstraint("receipt_id"),
    )
    op.create_index(
        "ix_production_transient_audit_target_time",
        "production_transient_dispatch_audits",
        ["target_id", "requested_at"],
    )

    op.create_table(
        "production_approval_creation_rates",
        sa.Column("client_id", sa.String(36), nullable=False),
        sa.Column("target_id", sa.String(36), nullable=False),
        sa.Column("last_created_at", sa.String(27), nullable=False),
        sa.PrimaryKeyConstraint("client_id", "target_id"),
    )
    op.create_table(
        "production_transient_dispatch_rates",
        sa.Column("client_id", sa.String(36), nullable=False),
        sa.Column("target_id", sa.String(36), nullable=False),
        sa.Column("last_dispatched_at", sa.String(27), nullable=False),
        sa.PrimaryKeyConstraint("client_id", "target_id"),
    )

    op.execute(
        "CREATE TRIGGER trg_production_approval_transition BEFORE UPDATE ON production_operation_approvals WHEN NEW.id<>OLD.id OR NEW.client_id<>OLD.client_id OR NEW.credential_id<>OLD.credential_id OR NEW.target_id<>OLD.target_id OR NEW.tool<>OLD.tool OR NEW.action<>OLD.action OR NEW.payload_sha256<>OLD.payload_sha256 OR NEW.canonicalization_revision<>OLD.canonicalization_revision OR NEW.request_id<>OLD.request_id OR NEW.native_identity_generation<>OLD.native_identity_generation OR NEW.bridge_trust_revision<>OLD.bridge_trust_revision OR NEW.target_policy_revision<>OLD.target_policy_revision OR NEW.mutation_control_epoch<>OLD.mutation_control_epoch OR NEW.client_authority_revision<>OLD.client_authority_revision OR NEW.generic_grant_id<>OLD.generic_grant_id OR NEW.client_preset_id<>OLD.client_preset_id OR NEW.target_preset_id<>OLD.target_preset_id OR NEW.inspect_scope_id<>OLD.inspect_scope_id OR NEW.transient_scope_id<>OLD.transient_scope_id OR NEW.native_http_acknowledgement_id IS NOT OLD.native_http_acknowledgement_id OR NEW.reviewed_build_id IS NOT OLD.reviewed_build_id OR NEW.created_at<>OLD.created_at OR NEW.pending_expires_at<>OLD.pending_expires_at OR NOT ((OLD.state='pending' AND NEW.state IN ('approved','denied','cancelled','expired')) OR (OLD.state='approved' AND NEW.state IN ('consumed','cancelled','expired'))) BEGIN SELECT RAISE(ABORT, 'production_approval_transition_invalid'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_production_approval_no_delete BEFORE DELETE ON production_operation_approvals WHEN NOT ((OLD.state IN ('denied','cancelled','expired') AND OLD.linked_remote_receipt_id IS NULL AND EXISTS(SELECT 1 FROM production_approval_transition_events e WHERE e.approval_id=OLD.id AND e.event_kind='requested' AND e.to_state='pending') AND EXISTS(SELECT 1 FROM production_approval_transition_events e WHERE e.approval_id=OLD.id AND e.to_state=OLD.state)) OR (OLD.state='consumed' AND EXISTS(SELECT 1 FROM mcp_remote_action_receipts r JOIN production_transient_dispatch_audits a ON a.receipt_id=r.id WHERE r.id=OLD.linked_remote_receipt_id AND r.state='completed' AND a.approval_id=OLD.id AND a.state='completed'))) BEGIN SELECT RAISE(ABORT, 'production_approval_protected'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_production_approval_event_immutable BEFORE UPDATE ON production_approval_transition_events BEGIN SELECT RAISE(ABORT, 'production_approval_event_immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_production_approval_event_no_delete BEFORE DELETE ON production_approval_transition_events WHEN EXISTS(SELECT 1 FROM production_operation_approvals a WHERE a.id=OLD.approval_id) BEGIN SELECT RAISE(ABORT, 'production_approval_event_protected'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_production_transient_audit_terminal BEFORE UPDATE ON production_transient_dispatch_audits WHEN NEW.receipt_id<>OLD.receipt_id OR NEW.approval_id<>OLD.approval_id OR NEW.client_id<>OLD.client_id OR NEW.credential_id<>OLD.credential_id OR NEW.target_id<>OLD.target_id OR NEW.request_id<>OLD.request_id OR NEW.action<>OLD.action OR NEW.payload_sha256<>OLD.payload_sha256 OR NEW.canonicalization_revision<>OLD.canonicalization_revision OR NEW.requested_at<>OLD.requested_at OR OLD.state<>'pending' OR NEW.state NOT IN ('completed','ambiguous') OR NEW.completed_at IS NULL BEGIN SELECT RAISE(ABORT, 'production_transient_audit_immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_production_transient_audit_no_delete BEFORE DELETE ON production_transient_dispatch_audits WHEN NOT EXISTS(SELECT 1 FROM mcp_remote_action_receipts r WHERE r.id=OLD.receipt_id AND r.state='completed' AND OLD.state='completed' AND OLD.outcome=r.final_result AND OLD.requested_at=r.created_at AND OLD.completed_at=r.completed_at) BEGIN SELECT RAISE(ABORT, 'production_transient_audit_protected'); END"
    )
    _binding_triggers()

    op.execute(
        "CREATE TRIGGER trg_external_production_policy_revision_monotonic BEFORE UPDATE OF policy_revision ON external_production_policies WHEN NEW.policy_revision<OLD.policy_revision BEGIN SELECT RAISE(ABORT, 'production_policy_revision_not_monotonic'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_external_production_policy_material_revision BEFORE UPDATE ON external_production_policies WHEN (NEW.designation_state<>OLD.designation_state OR NEW.confirmed_observation_id IS NOT OLD.confirmed_observation_id OR NEW.confirmed_observation_digest IS NOT OLD.confirmed_observation_digest OR NEW.identity_generation<>OLD.identity_generation OR NEW.mutations_suspended<>OLD.mutations_suspended) AND NEW.policy_revision<=OLD.policy_revision BEGIN SELECT RAISE(ABORT, 'production_policy_revision_required'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_production_mutation_epoch_monotonic BEFORE UPDATE ON production_mutation_control WHEN NEW.policy_epoch<OLD.policy_epoch OR (NEW.production_mutations_blocked<>OLD.production_mutations_blocked AND NEW.policy_epoch<=OLD.policy_epoch) OR (NEW.production_mutations_blocked=OLD.production_mutations_blocked AND NEW.policy_epoch<>OLD.policy_epoch) BEGIN SELECT RAISE(ABORT, 'production_mutation_epoch_invalid'); END"
    )


def downgrade() -> None:
    connection = op.get_bind()
    populated = connection.execute(
        sa.text(
            "SELECT EXISTS(SELECT 1 FROM mcp_production_target_scopes WHERE scope='request_transient_control') "
            "OR EXISTS(SELECT 1 FROM production_operation_approvals) "
            "OR EXISTS(SELECT 1 FROM production_approval_transition_events) "
            "OR EXISTS(SELECT 1 FROM production_transient_dispatch_audits) "
            "OR EXISTS(SELECT 1 FROM production_approval_creation_rates) "
            "OR EXISTS(SELECT 1 FROM production_transient_dispatch_rates) "
            "OR EXISTS(SELECT 1 FROM mcp_client_target_grants)"
        )
    ).scalar_one()
    if populated:
        raise RuntimeError(
            "refusing destructive downgrade while production transient authority or history exists"
        )

    for trigger in (
        "trg_production_mutation_epoch_monotonic",
        "trg_external_production_policy_material_revision",
        "trg_external_production_policy_revision_monotonic",
        "trg_mcp_production_remote_binding_no_delete",
        "trg_mcp_production_remote_binding_immutable",
        "trg_mcp_production_remote_receipt_protected",
        "trg_production_transient_audit_no_delete",
        "trg_production_transient_audit_terminal",
        "trg_production_approval_event_no_delete",
        "trg_production_approval_event_immutable",
        "trg_production_approval_no_delete",
        "trg_production_approval_transition",
    ):
        op.execute(f"DROP TRIGGER {trigger}")
    op.drop_table("production_transient_dispatch_rates")
    op.drop_table("production_approval_creation_rates")
    op.drop_index(
        "ix_production_transient_audit_target_time",
        table_name="production_transient_dispatch_audits",
    )
    op.drop_table("production_transient_dispatch_audits")
    op.drop_index(
        "ix_production_approval_event_approval_time",
        table_name="production_approval_transition_events",
    )
    op.drop_table("production_approval_transition_events")
    op.drop_index(
        "ix_production_approval_target_state",
        table_name="production_operation_approvals",
    )
    op.drop_index(
        "ix_production_approval_client_history",
        table_name="production_operation_approvals",
    )
    op.drop_index(
        "ix_production_approval_queue", table_name="production_operation_approvals"
    )
    op.drop_table("production_operation_approvals")

    op.drop_index(
        "ix_mcp_production_remote_binding_scopes",
        table_name="mcp_production_remote_receipt_bindings",
    )
    with op.batch_alter_table(
        "mcp_production_remote_receipt_bindings", recreate="always"
    ) as batch:
        batch.drop_constraint(
            "mcp_production_remote_binding_identity_valid", type_="check"
        )
        batch.drop_constraint(
            "mcp_production_remote_binding_authority_shapes", type_="check"
        )
        batch.drop_column("generic_grant_id")
        batch.drop_column("approval_id")
        batch.drop_column("transient_scope_id")
        batch.alter_column(
            "navigate_scope_id", existing_type=sa.String(36), nullable=False
        )
        batch.create_check_constraint(
            "mcp_production_remote_binding_identity_valid",
            "length(receipt_id)=36 AND operation_class='navigation'",
        )
        batch.create_check_constraint(
            "mcp_production_remote_binding_authority_shapes",
            "length(inspect_scope_id)=36 AND length(navigate_scope_id)=36 AND length(client_preset_id)=36 AND length(target_preset_id)=36",
        )
    op.create_index(
        "ix_mcp_production_remote_binding_scopes",
        "mcp_production_remote_receipt_bindings",
        ["inspect_scope_id", "navigate_scope_id"],
    )
    # Exact Slice 4A binding triggers are restored by reusing its migration text.
    op.execute(
        "CREATE TRIGGER trg_mcp_production_remote_binding_immutable BEFORE UPDATE ON mcp_production_remote_receipt_bindings BEGIN SELECT RAISE(ABORT, 'production_remote_binding_immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_mcp_production_remote_binding_no_delete BEFORE DELETE ON mcp_production_remote_receipt_bindings WHEN NOT EXISTS(SELECT 1 FROM mcp_remote_action_receipts r JOIN mcp_production_navigation_audits a ON a.receipt_id=r.id WHERE r.id=OLD.receipt_id AND r.state='completed' AND r.final_result IS NOT NULL AND r.completed_at IS NOT NULL AND r.screenshot_refresh_result IS NOT NULL AND a.client_id=r.client_id AND a.credential_id=r.credential_id AND a.target_id=r.target_id AND a.request_id=r.request_id AND a.action=r.action AND a.operation_class=OLD.operation_class AND a.native_identity_generation=OLD.native_identity_generation AND a.bridge_trust_revision=OLD.bridge_trust_revision AND a.target_policy_revision=OLD.target_policy_revision AND a.mutation_control_epoch=OLD.mutation_control_epoch AND a.inspect_scope_id=OLD.inspect_scope_id AND a.navigate_scope_id=OLD.navigate_scope_id AND a.state='completed' AND a.outcome=r.final_result AND a.requested_at=r.created_at AND a.completed_at=r.completed_at AND ((r.final_result IN ('succeeded','authority_changed_after_dispatch','remote_kodi_rejected') AND a.dispatch_classification='acknowledged') OR (r.final_result NOT IN ('succeeded','authority_changed_after_dispatch','remote_kodi_rejected') AND a.dispatch_classification='not_dispatched'))) BEGIN SELECT RAISE(ABORT, 'production_remote_binding_protected'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_mcp_production_remote_receipt_protected BEFORE DELETE ON mcp_remote_action_receipts WHEN EXISTS(SELECT 1 FROM mcp_production_remote_receipt_bindings b WHERE b.receipt_id=OLD.id) BEGIN SELECT RAISE(ABORT, 'production_remote_receipt_protected'); END"
    )

    op.execute("DROP TRIGGER trg_mcp_production_scope_no_delete")
    op.execute("DROP TRIGGER trg_mcp_production_scope_immutable")
    op.execute("DROP TRIGGER trg_mcp_production_scope_binding_insert")
    with op.batch_alter_table(
        "mcp_production_target_scopes", recreate="always"
    ) as batch:
        batch.drop_constraint("mcp_production_scope_bridge_binding", type_="check")
        batch.drop_constraint("mcp_production_scope_value_closed", type_="check")
        batch.alter_column(
            "scope", existing_type=sa.String(32), type_=sa.String(16), nullable=False
        )
        batch.create_check_constraint(
            "mcp_production_scope_value_closed",
            "scope IN ('inspect','visual','navigate')",
        )
        batch.create_check_constraint(
            "mcp_production_scope_bridge_binding",
            "(scope='inspect' AND bridge_trust_revision IS NULL) OR (scope IN ('visual','navigate') AND bridge_trust_revision>=1)",
        )
    # Restore the Slice 4A scope trigger set.
    op.execute(
        "CREATE TRIGGER trg_mcp_production_scope_binding_insert BEFORE INSERT ON mcp_production_target_scopes WHEN COALESCE((SELECT target_type FROM targets WHERE id=NEW.target_id),'')<>'external_production' OR COALESCE((SELECT identity_generation FROM external_production_policies WHERE target_id=NEW.target_id),0)<>NEW.identity_generation OR NEW.state<>'active' OR NEW.revoked_at IS NOT NULL OR NEW.revoked_by IS NOT NULL OR (NEW.scope IN ('visual','navigate') AND (COALESCE((SELECT production_bridge_trust_revision FROM external_bridge_pairings WHERE target_id=NEW.target_id),0)<>NEW.bridge_trust_revision OR COALESCE((SELECT production_identity_generation FROM external_bridge_pairings WHERE target_id=NEW.target_id),0)<>NEW.identity_generation OR COALESCE((SELECT pairing_state FROM external_bridge_pairings WHERE target_id=NEW.target_id),'')<>'paired' OR COALESCE((SELECT reachability_state FROM external_bridge_pairings WHERE target_id=NEW.target_id),'')<>'reachable' OR COALESCE((SELECT state FROM reviewed_bridge_builds WHERE id=(SELECT reviewed_build_id FROM external_bridge_pairings WHERE target_id=NEW.target_id)),'')<>'active' OR NOT EXISTS(SELECT 1 FROM mcp_production_target_scopes i WHERE i.client_id=NEW.client_id AND i.target_id=NEW.target_id AND i.identity_generation=NEW.identity_generation AND i.scope='inspect' AND i.state='active'))) BEGIN SELECT RAISE(ABORT, 'production_inspection_scope_binding_invalid'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_mcp_production_scope_immutable BEFORE UPDATE ON mcp_production_target_scopes WHEN NEW.id<>OLD.id OR NEW.client_id<>OLD.client_id OR NEW.target_id<>OLD.target_id OR NEW.identity_generation<>OLD.identity_generation OR NEW.scope<>OLD.scope OR NEW.bridge_trust_revision IS NOT OLD.bridge_trust_revision OR NEW.created_at<>OLD.created_at OR NEW.created_by<>OLD.created_by OR OLD.state<>'active' OR NEW.state<>'revoked' OR NEW.revoked_at IS NULL OR NEW.revoked_by IS NULL BEGIN SELECT RAISE(ABORT, 'production_inspection_scope_immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_mcp_production_scope_no_delete BEFORE DELETE ON mcp_production_target_scopes BEGIN SELECT RAISE(ABORT, 'production_inspection_scope_immutable'); END"
    )

    op.execute("DROP TRIGGER trg_mcp_client_target_grant_identity_immutable")
    with op.batch_alter_table("mcp_client_target_grants", recreate="always") as batch:
        batch.drop_constraint("uq_mcp_client_target_grant_id", type_="unique")
        batch.drop_constraint("mcp_client_target_grant_id_shape", type_="check")
        batch.drop_column("grant_id")
    op.execute("DROP TRIGGER trg_mcp_client_authority_revision")
    with op.batch_alter_table("mcp_clients", recreate="always") as batch:
        batch.drop_constraint("mcp_client_authority_revision_positive", type_="check")
        batch.drop_column("authority_revision")
