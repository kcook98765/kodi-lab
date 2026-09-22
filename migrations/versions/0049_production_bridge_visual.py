"""Add reviewed production bridge visual inspection authority.

Revision ID: 0049_production_bridge_visual
Revises: 0048_production_inspection_scopes
"""

# Closed SQLite constraints and triggers are intentionally explicit.
# ruff: noqa: E501

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0049_production_bridge_visual"
down_revision: str | Sequence[str] | None = "0048_production_inspection_scopes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PRODUCTION_INSPECTION_PRESET_ID = "11000000-0000-4000-8000-000000000005"
PRODUCTION_VISUAL_PRESET_ID = "11000000-0000-4000-8000-000000000006"


def upgrade() -> None:
    op.create_table(
        "reviewed_bridge_builds",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("semantic_fingerprint", sa.String(64), nullable=False),
        sa.Column("zip_sha256", sa.String(64), nullable=False),
        sa.Column("addon_id", sa.String(128), nullable=False),
        sa.Column("addon_version", sa.String(64), nullable=False),
        sa.Column("protocol_version", sa.String(16), nullable=False),
        sa.Column("capabilities_json", sa.String(256), nullable=False),
        sa.Column("supported_kodi_versions_json", sa.String(256), nullable=False),
        sa.Column("source_reference", sa.String(512), nullable=False),
        sa.Column("reviewed_by", sa.String(64), nullable=False),
        sa.Column("reviewed_at", sa.String(27), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("retired_by", sa.String(64)),
        sa.Column("retired_at", sa.String(27)),
        sa.CheckConstraint("length(id)=36", name="reviewed_bridge_build_id_shape"),
        sa.CheckConstraint(
            "length(semantic_fingerprint)=64 AND semantic_fingerprint NOT GLOB '*[^0-9a-f]*' AND length(zip_sha256)=64 AND zip_sha256 NOT GLOB '*[^0-9a-f]*'",
            name="reviewed_bridge_build_digests_sha256",
        ),
        sa.CheckConstraint(
            "length(addon_id) BETWEEN 1 AND 128 AND length(addon_version) BETWEEN 1 AND 64 AND length(protocol_version) BETWEEN 1 AND 16",
            name="reviewed_bridge_build_identity_bounded",
        ),
        sa.CheckConstraint(
            'capabilities_json IN (\'["screenshot"]\',\'["screenshot","workspace_deployment"]\') AND length(supported_kodi_versions_json) BETWEEN 5 AND 256',
            name="reviewed_bridge_build_sets_bounded",
        ),
        sa.CheckConstraint(
            "length(source_reference) BETWEEN 1 AND 512 AND length(reviewed_by) BETWEEN 1 AND 64 AND length(reviewed_at)=27",
            name="reviewed_bridge_build_provenance_bounded",
        ),
        sa.CheckConstraint(
            "state IN ('active','retired') AND ((state='active' AND retired_by IS NULL AND retired_at IS NULL) OR (state='retired' AND retired_by IS NOT NULL AND retired_at IS NOT NULL))",
            name="reviewed_bridge_build_state_consistent",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "semantic_fingerprint",
            "addon_id",
            "addon_version",
            "protocol_version",
            name="uq_reviewed_bridge_semantic_identity",
        ),
        sa.UniqueConstraint("zip_sha256", name="uq_reviewed_bridge_zip_sha256"),
    )
    op.create_index(
        "ix_reviewed_bridge_build_state",
        "reviewed_bridge_builds",
        ["state", "semantic_fingerprint"],
    )
    op.execute(
        "CREATE TRIGGER trg_reviewed_bridge_build_immutable BEFORE UPDATE ON reviewed_bridge_builds WHEN NEW.id<>OLD.id OR NEW.semantic_fingerprint<>OLD.semantic_fingerprint OR NEW.zip_sha256<>OLD.zip_sha256 OR NEW.addon_id<>OLD.addon_id OR NEW.addon_version<>OLD.addon_version OR NEW.protocol_version<>OLD.protocol_version OR NEW.capabilities_json<>OLD.capabilities_json OR NEW.supported_kodi_versions_json<>OLD.supported_kodi_versions_json OR NEW.source_reference<>OLD.source_reference OR NEW.reviewed_by<>OLD.reviewed_by OR NEW.reviewed_at<>OLD.reviewed_at OR OLD.state<>'active' OR NEW.state<>'retired' OR NEW.retired_by IS NULL OR NEW.retired_at IS NULL BEGIN SELECT RAISE(ABORT, 'reviewed_bridge_build_immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_reviewed_bridge_build_no_delete BEFORE DELETE ON reviewed_bridge_builds BEGIN SELECT RAISE(ABORT, 'reviewed_bridge_build_immutable'); END"
    )

    with op.batch_alter_table("external_bridge_pairings", recreate="always") as batch:
        batch.add_column(sa.Column("production_identity_generation", sa.Integer()))
        batch.add_column(sa.Column("production_bridge_trust_revision", sa.Integer()))
        batch.add_column(sa.Column("reviewed_build_id", sa.String(36)))
        batch.add_column(sa.Column("production_confirmed_by", sa.String(64)))
        batch.add_column(sa.Column("production_confirmed_at", sa.String(27)))
        batch.create_foreign_key(
            "fk_external_bridge_reviewed_build",
            "reviewed_bridge_builds",
            ["reviewed_build_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_check_constraint(
            "external_bridge_production_binding_complete",
            "(production_identity_generation IS NULL AND reviewed_build_id IS NULL AND production_confirmed_by IS NULL AND production_confirmed_at IS NULL) OR (production_identity_generation>=1 AND production_bridge_trust_revision>=1 AND reviewed_build_id IS NOT NULL AND production_confirmed_by IS NOT NULL AND length(production_confirmed_by) BETWEEN 1 AND 64 AND length(production_confirmed_at)=27)",
        )
        batch.create_check_constraint(
            "external_bridge_production_revision_positive",
            "production_bridge_trust_revision IS NULL OR production_bridge_trust_revision>=1",
        )
    op.execute(
        "CREATE TRIGGER trg_production_bridge_revision_monotonic BEFORE UPDATE OF production_bridge_trust_revision ON external_bridge_pairings WHEN OLD.production_bridge_trust_revision IS NOT NULL AND (NEW.production_bridge_trust_revision IS NULL OR NEW.production_bridge_trust_revision < OLD.production_bridge_trust_revision) BEGIN SELECT RAISE(ABORT, 'production_bridge_trust_revision_not_monotonic'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_production_bridge_revision_no_delete BEFORE DELETE ON external_bridge_pairings WHEN OLD.production_bridge_trust_revision IS NOT NULL BEGIN SELECT RAISE(ABORT, 'production_bridge_trust_revision_not_monotonic'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_production_bridge_revision_no_replace BEFORE INSERT ON external_bridge_pairings WHEN EXISTS(SELECT 1 FROM external_bridge_pairings p WHERE p.target_id=NEW.target_id AND p.production_bridge_trust_revision IS NOT NULL) BEGIN SELECT RAISE(ABORT, 'production_bridge_trust_revision_not_monotonic'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_production_bridge_binding_insert BEFORE INSERT ON external_bridge_pairings WHEN NEW.production_identity_generation IS NOT NULL AND (COALESCE((SELECT target_type FROM targets WHERE id=NEW.target_id),'')<>'external_production' OR COALESCE((SELECT identity_generation FROM external_production_policies WHERE target_id=NEW.target_id),0)<>NEW.production_identity_generation OR COALESCE((SELECT state FROM reviewed_bridge_builds WHERE id=NEW.reviewed_build_id),'')<>'active') BEGIN SELECT RAISE(ABORT, 'production_bridge_binding_invalid'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_production_bridge_binding_update BEFORE UPDATE OF production_identity_generation,reviewed_build_id,production_confirmed_by,production_confirmed_at ON external_bridge_pairings WHEN NEW.production_identity_generation IS NOT NULL AND (COALESCE((SELECT target_type FROM targets WHERE id=NEW.target_id),'')<>'external_production' OR COALESCE((SELECT identity_generation FROM external_production_policies WHERE target_id=NEW.target_id),0)<>NEW.production_identity_generation OR COALESCE((SELECT state FROM reviewed_bridge_builds WHERE id=NEW.reviewed_build_id),'')<>'active') BEGIN SELECT RAISE(ABORT, 'production_bridge_binding_invalid'); END"
    )

    op.create_table(
        "production_bridge_http_acknowledgements",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("target_id", sa.String(36), nullable=False),
        sa.Column("identity_generation", sa.Integer(), nullable=False),
        sa.Column("bridge_trust_revision", sa.Integer(), nullable=False),
        sa.Column("protocol", sa.String(16), nullable=False),
        sa.Column("ip_literal", sa.String(64), nullable=False),
        sa.Column("native_port", sa.Integer(), nullable=False),
        sa.Column("bridge_port", sa.Integer(), nullable=False),
        sa.Column("endpoint_digest", sa.String(64), nullable=False),
        sa.Column("transport_policy_revision", sa.Integer(), nullable=False),
        sa.Column("acknowledged_by", sa.String(64), nullable=False),
        sa.Column("acknowledged_at", sa.String(27), nullable=False),
        sa.CheckConstraint(
            "length(id)=36 AND identity_generation>=1 AND bridge_trust_revision>=1 AND transport_policy_revision>=1",
            name="production_bridge_ack_generations_positive",
        ),
        sa.CheckConstraint(
            "protocol='jsonrpc_http' AND length(ip_literal) BETWEEN 2 AND 64 AND native_port BETWEEN 1 AND 65535 AND bridge_port BETWEEN 1 AND 65535",
            name="production_bridge_ack_endpoint_valid",
        ),
        sa.CheckConstraint(
            "length(endpoint_digest)=64 AND endpoint_digest NOT GLOB '*[^0-9a-f]*'",
            name="production_bridge_ack_digest_sha256",
        ),
        sa.CheckConstraint(
            "length(acknowledged_by) BETWEEN 1 AND 64 AND length(acknowledged_at)=27",
            name="production_bridge_ack_administrator_bounded",
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
            "bridge_trust_revision",
            "endpoint_digest",
            "transport_policy_revision",
            name="uq_production_bridge_http_ack_binding",
        ),
    )
    op.create_index(
        "ix_production_bridge_http_ack_target",
        "production_bridge_http_acknowledgements",
        ["target_id", "identity_generation", "bridge_trust_revision"],
    )
    op.execute(
        "CREATE TRIGGER trg_production_bridge_http_ack_immutable BEFORE UPDATE ON production_bridge_http_acknowledgements BEGIN SELECT RAISE(ABORT, 'production_bridge_http_ack_immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_production_bridge_http_ack_no_delete BEFORE DELETE ON production_bridge_http_acknowledgements BEGIN SELECT RAISE(ABORT, 'production_bridge_http_ack_immutable'); END"
    )

    with op.batch_alter_table(
        "mcp_production_target_scopes", recreate="always"
    ) as batch:
        batch.drop_constraint("mcp_production_scope_value_closed", type_="check")
        batch.add_column(sa.Column("bridge_trust_revision", sa.Integer()))
        batch.create_check_constraint(
            "mcp_production_scope_value_closed", "scope IN ('inspect','visual')"
        )
        batch.create_check_constraint(
            "mcp_production_scope_bridge_binding",
            "(scope='inspect' AND bridge_trust_revision IS NULL) OR (scope='visual' AND bridge_trust_revision>=1)",
        )
    # SQLite drops table-owned triggers during the batch recreation above.
    op.execute(
        "CREATE TRIGGER trg_mcp_production_scope_binding_insert BEFORE INSERT ON mcp_production_target_scopes WHEN COALESCE((SELECT target_type FROM targets WHERE id=NEW.target_id),'')<>'external_production' OR COALESCE((SELECT identity_generation FROM external_production_policies WHERE target_id=NEW.target_id),0)<>NEW.identity_generation OR NEW.state<>'active' OR NEW.revoked_at IS NOT NULL OR NEW.revoked_by IS NOT NULL OR (NEW.scope='visual' AND (COALESCE((SELECT production_bridge_trust_revision FROM external_bridge_pairings WHERE target_id=NEW.target_id),0)<>NEW.bridge_trust_revision OR COALESCE((SELECT production_identity_generation FROM external_bridge_pairings WHERE target_id=NEW.target_id),0)<>NEW.identity_generation OR COALESCE((SELECT pairing_state FROM external_bridge_pairings WHERE target_id=NEW.target_id),'')<>'paired' OR COALESCE((SELECT reachability_state FROM external_bridge_pairings WHERE target_id=NEW.target_id),'')<>'reachable' OR COALESCE((SELECT state FROM reviewed_bridge_builds WHERE id=(SELECT reviewed_build_id FROM external_bridge_pairings WHERE target_id=NEW.target_id)),'')<>'active' OR NOT EXISTS(SELECT 1 FROM mcp_production_target_scopes i WHERE i.client_id=NEW.client_id AND i.target_id=NEW.target_id AND i.identity_generation=NEW.identity_generation AND i.scope='inspect' AND i.state='active'))) BEGIN SELECT RAISE(ABORT, 'production_inspection_scope_binding_invalid'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_mcp_production_scope_immutable BEFORE UPDATE ON mcp_production_target_scopes WHEN NEW.id<>OLD.id OR NEW.client_id<>OLD.client_id OR NEW.target_id<>OLD.target_id OR NEW.identity_generation<>OLD.identity_generation OR NEW.scope<>OLD.scope OR NEW.bridge_trust_revision IS NOT OLD.bridge_trust_revision OR NEW.created_at<>OLD.created_at OR NEW.created_by<>OLD.created_by OR OLD.state<>'active' OR NEW.state<>'revoked' OR NEW.revoked_at IS NULL OR NEW.revoked_by IS NULL BEGIN SELECT RAISE(ABORT, 'production_inspection_scope_immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_mcp_production_scope_no_delete BEFORE DELETE ON mcp_production_target_scopes BEGIN SELECT RAISE(ABORT, 'production_inspection_scope_immutable'); END"
    )

    presets = sa.table(
        "mcp_presets",
        sa.column("id", sa.String),
        sa.column("display_name", sa.String),
        sa.column("builtin", sa.Boolean),
        sa.column("assignable", sa.Boolean),
    )
    permissions = sa.table(
        "mcp_preset_permissions",
        sa.column("preset_id", sa.String),
        sa.column("permission", sa.String),
    )
    op.bulk_insert(
        presets,
        [
            {
                "id": PRODUCTION_VISUAL_PRESET_ID,
                "display_name": "Production Visual Inspection",
                "builtin": True,
                "assignable": True,
            }
        ],
    )
    op.bulk_insert(
        permissions,
        [
            {"preset_id": PRODUCTION_VISUAL_PRESET_ID, "permission": "target.inspect"},
            {"preset_id": PRODUCTION_VISUAL_PRESET_ID, "permission": "target.visual"},
        ],
    )
    op.execute("DROP TRIGGER trg_mcp_production_target_preset_update")
    op.execute("DROP TRIGGER trg_mcp_production_target_preset_insert")
    allowed = f"('{PRODUCTION_INSPECTION_PRESET_ID}','{PRODUCTION_VISUAL_PRESET_ID}')"
    op.execute(
        f"CREATE TRIGGER trg_mcp_production_target_preset_insert BEFORE INSERT ON mcp_target_preset_assignments WHEN (COALESCE((SELECT target_type FROM targets WHERE id=NEW.target_id),'')='external_production' AND NEW.preset_id NOT IN {allowed}) OR (NEW.preset_id IN {allowed} AND COALESCE((SELECT target_type FROM targets WHERE id=NEW.target_id),'')<>'external_production') BEGIN SELECT RAISE(ABORT, 'production_inspection_preset_target_invalid'); END"
    )
    op.execute(
        f"CREATE TRIGGER trg_mcp_production_target_preset_update BEFORE UPDATE OF target_id,preset_id ON mcp_target_preset_assignments WHEN (COALESCE((SELECT target_type FROM targets WHERE id=NEW.target_id),'')='external_production' AND NEW.preset_id NOT IN {allowed}) OR (NEW.preset_id IN {allowed} AND COALESCE((SELECT target_type FROM targets WHERE id=NEW.target_id),'')<>'external_production') BEGIN SELECT RAISE(ABORT, 'production_inspection_preset_target_invalid'); END"
    )

    op.create_table(
        "mcp_production_screenshot_receipts",
        sa.Column("client_id", sa.String(36), nullable=False),
        sa.Column("request_id", sa.String(36), nullable=False),
        sa.Column("target_id", sa.String(36), nullable=False),
        sa.Column("parameter_digest", sa.String(64), nullable=False),
        sa.Column("identity_generation", sa.Integer(), nullable=False),
        sa.Column("bridge_trust_revision", sa.Integer(), nullable=False),
        sa.Column("bridge_request_id", sa.String(36), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("response_digest", sa.String(64)),
        sa.Column("byte_size", sa.Integer()),
        sa.Column("width", sa.Integer()),
        sa.Column("height", sa.Integer()),
        sa.Column("created_at", sa.String(27), nullable=False),
        sa.Column("completed_at", sa.String(27)),
        sa.Column("expires_at", sa.String(27), nullable=False),
        sa.CheckConstraint(
            "length(client_id)=36 AND length(request_id)=36 AND length(target_id)=36 AND length(bridge_request_id)=36",
            name="production_screenshot_receipt_uuid_lengths",
        ),
        sa.CheckConstraint(
            "length(parameter_digest)=64 AND parameter_digest NOT GLOB '*[^0-9a-f]*' AND identity_generation>=1 AND bridge_trust_revision>=1",
            name="production_screenshot_receipt_binding_valid",
        ),
        sa.CheckConstraint(
            "state IN ('pending','completed','ambiguous','rejected')",
            name="production_screenshot_receipt_state_closed",
        ),
        sa.CheckConstraint(
            "response_digest IS NULL OR (length(response_digest)=64 AND response_digest NOT GLOB '*[^0-9a-f]*')",
            name="production_screenshot_receipt_digest_sha256",
        ),
        sa.CheckConstraint(
            "byte_size IS NULL OR byte_size BETWEEN 1 AND 8388608",
            name="production_screenshot_receipt_size_bounded",
        ),
        sa.CheckConstraint(
            "(width IS NULL AND height IS NULL) OR (width BETWEEN 1 AND 4096 AND height BETWEEN 1 AND 2160)",
            name="production_screenshot_receipt_dimensions_bounded",
        ),
        sa.CheckConstraint(
            "length(created_at)=27 AND length(expires_at)=27 AND (completed_at IS NULL OR length(completed_at)=27)",
            name="production_screenshot_receipt_timestamps_format",
        ),
        sa.CheckConstraint(
            "(state='pending' AND completed_at IS NULL AND response_digest IS NULL AND byte_size IS NULL AND width IS NULL AND height IS NULL) OR (state='completed' AND completed_at IS NOT NULL AND response_digest IS NOT NULL AND byte_size IS NOT NULL AND width IS NOT NULL AND height IS NOT NULL) OR (state IN ('ambiguous','rejected') AND completed_at IS NOT NULL)",
            name="production_screenshot_receipt_terminal_coherent",
        ),
        sa.ForeignKeyConstraint(["client_id"], ["mcp_clients.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["target_id"],
            ["external_production_policies.target_id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "target_id",
            "bridge_request_id",
            name="uq_production_screenshot_bridge_request",
        ),
        sa.PrimaryKeyConstraint("client_id", "request_id"),
    )
    op.create_index(
        "ix_production_screenshot_receipt_capacity",
        "mcp_production_screenshot_receipts",
        ["expires_at", "state", "target_id"],
    )
    op.execute(
        "CREATE TRIGGER trg_production_screenshot_receipt_terminal BEFORE UPDATE ON mcp_production_screenshot_receipts WHEN NEW.client_id<>OLD.client_id OR NEW.request_id<>OLD.request_id OR NEW.target_id<>OLD.target_id OR NEW.parameter_digest<>OLD.parameter_digest OR NEW.identity_generation<>OLD.identity_generation OR NEW.bridge_trust_revision<>OLD.bridge_trust_revision OR NEW.bridge_request_id<>OLD.bridge_request_id OR NEW.created_at<>OLD.created_at OR NEW.expires_at<>OLD.expires_at OR OLD.state<>'pending' OR NEW.state NOT IN ('completed','ambiguous','rejected') BEGIN SELECT RAISE(ABORT, 'production_screenshot_receipt_immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_production_screenshot_receipt_protected BEFORE DELETE ON mcp_production_screenshot_receipts WHEN OLD.state='pending' OR OLD.expires_at>=strftime('%Y-%m-%dT%H:%M:%f000Z','now') BEGIN SELECT RAISE(ABORT, 'production_screenshot_receipt_protected'); END"
    )

    with op.batch_alter_table("external_bridge_events", recreate="always") as batch:
        batch.drop_constraint("external_bridge_event_kind_closed", type_="check")
        batch.create_check_constraint(
            "external_bridge_event_kind_closed",
            "event_kind IN ('pairing_token_generated','pairing_attempted','bridge_paired','pairing_failed','bridge_revoked','bridge_reverified','bridge_reverification_required','screenshot_requested','screenshot_completed','screenshot_failed','baseline_established','baseline_invalidated','deployment_requested','deployment_completed','deployment_failed','deployment_reconciled','execution_completed','execution_failed','production_build_reviewed','production_build_retired','production_transport_acknowledged','production_bridge_confirmed','production_bridge_revision_advanced')",
        )


def downgrade() -> None:
    connection = op.get_bind()
    populated = connection.execute(
        sa.text(
            "SELECT EXISTS(SELECT 1 FROM reviewed_bridge_builds) OR EXISTS(SELECT 1 FROM production_bridge_http_acknowledgements) OR EXISTS(SELECT 1 FROM mcp_production_target_scopes WHERE scope='visual') OR EXISTS(SELECT 1 FROM mcp_production_screenshot_receipts) OR EXISTS(SELECT 1 FROM mcp_clients WHERE preset_id=:preset) OR EXISTS(SELECT 1 FROM mcp_target_preset_assignments WHERE preset_id=:preset) OR EXISTS(SELECT 1 FROM external_bridge_pairings WHERE production_bridge_trust_revision IS NOT NULL)"
        ),
        {"preset": PRODUCTION_VISUAL_PRESET_ID},
    ).scalar_one()
    if populated:
        raise RuntimeError(
            "refusing destructive downgrade while production bridge or visual history exists"
        )
    with op.batch_alter_table("external_bridge_events", recreate="always") as batch:
        batch.drop_constraint("external_bridge_event_kind_closed", type_="check")
        batch.create_check_constraint(
            "external_bridge_event_kind_closed",
            "event_kind IN ('pairing_token_generated','pairing_attempted','bridge_paired','pairing_failed','bridge_revoked','bridge_reverified','bridge_reverification_required','screenshot_requested','screenshot_completed','screenshot_failed','baseline_established','baseline_invalidated','deployment_requested','deployment_completed','deployment_failed','deployment_reconciled','execution_completed','execution_failed')",
        )
    op.execute("DROP TRIGGER trg_production_screenshot_receipt_protected")
    op.execute("DROP TRIGGER trg_production_screenshot_receipt_terminal")
    op.drop_index(
        "ix_production_screenshot_receipt_capacity",
        table_name="mcp_production_screenshot_receipts",
    )
    op.drop_table("mcp_production_screenshot_receipts")
    op.execute("DROP TRIGGER trg_mcp_production_target_preset_update")
    op.execute("DROP TRIGGER trg_mcp_production_target_preset_insert")
    op.execute(
        f"DELETE FROM mcp_preset_permissions WHERE preset_id='{PRODUCTION_VISUAL_PRESET_ID}'"
    )
    op.execute(f"DELETE FROM mcp_presets WHERE id='{PRODUCTION_VISUAL_PRESET_ID}'")
    op.execute(
        f"CREATE TRIGGER trg_mcp_production_target_preset_insert BEFORE INSERT ON mcp_target_preset_assignments WHEN (COALESCE((SELECT target_type FROM targets WHERE id=NEW.target_id),'')='external_production' AND NEW.preset_id<>'{PRODUCTION_INSPECTION_PRESET_ID}') OR (NEW.preset_id='{PRODUCTION_INSPECTION_PRESET_ID}' AND COALESCE((SELECT target_type FROM targets WHERE id=NEW.target_id),'')<>'external_production') BEGIN SELECT RAISE(ABORT, 'production_inspection_preset_target_invalid'); END"
    )
    op.execute(
        f"CREATE TRIGGER trg_mcp_production_target_preset_update BEFORE UPDATE OF target_id,preset_id ON mcp_target_preset_assignments WHEN (COALESCE((SELECT target_type FROM targets WHERE id=NEW.target_id),'')='external_production' AND NEW.preset_id<>'{PRODUCTION_INSPECTION_PRESET_ID}') OR (NEW.preset_id='{PRODUCTION_INSPECTION_PRESET_ID}' AND COALESCE((SELECT target_type FROM targets WHERE id=NEW.target_id),'')<>'external_production') BEGIN SELECT RAISE(ABORT, 'production_inspection_preset_target_invalid'); END"
    )
    op.execute("DROP TRIGGER trg_mcp_production_scope_no_delete")
    op.execute("DROP TRIGGER trg_mcp_production_scope_immutable")
    op.execute("DROP TRIGGER trg_mcp_production_scope_binding_insert")
    with op.batch_alter_table(
        "mcp_production_target_scopes", recreate="always"
    ) as batch:
        batch.drop_constraint("mcp_production_scope_bridge_binding", type_="check")
        batch.drop_constraint("mcp_production_scope_value_closed", type_="check")
        batch.drop_column("bridge_trust_revision")
        batch.create_check_constraint(
            "mcp_production_scope_value_closed", "scope='inspect'"
        )
    op.execute(
        "CREATE TRIGGER trg_mcp_production_scope_binding_insert BEFORE INSERT ON mcp_production_target_scopes WHEN COALESCE((SELECT target_type FROM targets WHERE id=NEW.target_id),'')<>'external_production' OR COALESCE((SELECT identity_generation FROM external_production_policies WHERE target_id=NEW.target_id),0)<>NEW.identity_generation OR NEW.state<>'active' OR NEW.revoked_at IS NOT NULL OR NEW.revoked_by IS NOT NULL BEGIN SELECT RAISE(ABORT, 'production_inspection_scope_binding_invalid'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_mcp_production_scope_immutable BEFORE UPDATE ON mcp_production_target_scopes WHEN NEW.id<>OLD.id OR NEW.client_id<>OLD.client_id OR NEW.target_id<>OLD.target_id OR NEW.identity_generation<>OLD.identity_generation OR NEW.scope<>OLD.scope OR NEW.created_at<>OLD.created_at OR NEW.created_by<>OLD.created_by OR OLD.state<>'active' OR NEW.state<>'revoked' OR NEW.revoked_at IS NULL OR NEW.revoked_by IS NULL BEGIN SELECT RAISE(ABORT, 'production_inspection_scope_immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_mcp_production_scope_no_delete BEFORE DELETE ON mcp_production_target_scopes BEGIN SELECT RAISE(ABORT, 'production_inspection_scope_immutable'); END"
    )
    op.execute("DROP TRIGGER trg_production_bridge_http_ack_no_delete")
    op.execute("DROP TRIGGER trg_production_bridge_http_ack_immutable")
    op.drop_index(
        "ix_production_bridge_http_ack_target",
        table_name="production_bridge_http_acknowledgements",
    )
    op.drop_table("production_bridge_http_acknowledgements")
    op.execute("DROP TRIGGER trg_production_bridge_binding_update")
    op.execute("DROP TRIGGER trg_production_bridge_binding_insert")
    op.execute("DROP TRIGGER trg_production_bridge_revision_no_replace")
    op.execute("DROP TRIGGER trg_production_bridge_revision_no_delete")
    op.execute("DROP TRIGGER trg_production_bridge_revision_monotonic")
    with op.batch_alter_table("external_bridge_pairings", recreate="always") as batch:
        batch.drop_constraint(
            "external_bridge_production_revision_positive", type_="check"
        )
        batch.drop_constraint(
            "external_bridge_production_binding_complete", type_="check"
        )
        batch.drop_constraint("fk_external_bridge_reviewed_build", type_="foreignkey")
        batch.drop_column("production_confirmed_at")
        batch.drop_column("production_confirmed_by")
        batch.drop_column("reviewed_build_id")
        batch.drop_column("production_bridge_trust_revision")
        batch.drop_column("production_identity_generation")
    op.execute("DROP TRIGGER trg_reviewed_bridge_build_no_delete")
    op.execute("DROP TRIGGER trg_reviewed_bridge_build_immutable")
    op.drop_index("ix_reviewed_bridge_build_state", table_name="reviewed_bridge_builds")
    op.drop_table("reviewed_bridge_builds")
