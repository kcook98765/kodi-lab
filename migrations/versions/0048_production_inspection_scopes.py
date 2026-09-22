"""Add generation-bound production inspection scopes.

Revision ID: 0048_production_inspection_scopes
Revises: 0047_external_production_policy
"""

# Closed SQLite constraints and triggers are intentionally explicit.
# ruff: noqa: E501

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0048_production_inspection_scopes"
down_revision: str | Sequence[str] | None = "0047_external_production_policy"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PRODUCTION_INSPECTION_PRESET_ID = "11000000-0000-4000-8000-000000000005"


def upgrade() -> None:
    op.create_table(
        "mcp_production_target_scopes",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("client_id", sa.String(36), nullable=False),
        sa.Column("target_id", sa.String(36), nullable=False),
        sa.Column("identity_generation", sa.Integer(), nullable=False),
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("created_at", sa.String(27), nullable=False),
        sa.Column("created_by", sa.String(64), nullable=False),
        sa.Column("revoked_at", sa.String(27)),
        sa.Column("revoked_by", sa.String(64)),
        sa.CheckConstraint(
            "length(id)=36 AND identity_generation>=1",
            name="mcp_production_scope_identity_valid",
        ),
        sa.CheckConstraint("scope='inspect'", name="mcp_production_scope_value_closed"),
        sa.CheckConstraint(
            "state IN ('active','revoked')",
            name="mcp_production_scope_state_closed",
        ),
        sa.CheckConstraint(
            "(state='active' AND revoked_at IS NULL AND revoked_by IS NULL) OR "
            "(state='revoked' AND revoked_at IS NOT NULL AND revoked_by IS NOT NULL)",
            name="mcp_production_scope_revocation_consistent",
        ),
        sa.CheckConstraint(
            "length(created_at)=27 AND "
            "(revoked_at IS NULL OR (length(revoked_at)=27 AND revoked_at>=created_at))",
            name="mcp_production_scope_timestamps_valid",
        ),
        sa.CheckConstraint(
            "length(created_by) BETWEEN 1 AND 64 AND "
            "(revoked_by IS NULL OR length(revoked_by) BETWEEN 1 AND 64)",
            name="mcp_production_scope_administrators_bounded",
        ),
        sa.ForeignKeyConstraint(["client_id"], ["mcp_clients.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["target_id"],
            ["external_production_policies.target_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_mcp_production_scope_client",
        "mcp_production_target_scopes",
        ["client_id", "state"],
    )
    op.create_index(
        "ix_mcp_production_scope_target_generation",
        "mcp_production_target_scopes",
        ["target_id", "identity_generation", "state"],
    )
    op.create_index(
        "uq_mcp_production_scope_active",
        "mcp_production_target_scopes",
        ["client_id", "target_id", "identity_generation", "scope"],
        unique=True,
        sqlite_where=sa.text("state='active'"),
    )
    op.execute(
        "CREATE TRIGGER trg_mcp_production_scope_binding_insert "
        "BEFORE INSERT ON mcp_production_target_scopes "
        "WHEN COALESCE((SELECT target_type FROM targets WHERE id=NEW.target_id),'') <> 'external_production' "
        "OR COALESCE((SELECT identity_generation FROM external_production_policies WHERE target_id=NEW.target_id),0) <> NEW.identity_generation "
        "OR NEW.state<>'active' OR NEW.revoked_at IS NOT NULL OR NEW.revoked_by IS NOT NULL "
        "BEGIN SELECT RAISE(ABORT, 'production_inspection_scope_binding_invalid'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_mcp_production_scope_immutable "
        "BEFORE UPDATE ON mcp_production_target_scopes "
        "WHEN NEW.id<>OLD.id OR NEW.client_id<>OLD.client_id OR NEW.target_id<>OLD.target_id "
        "OR NEW.identity_generation<>OLD.identity_generation OR NEW.scope<>OLD.scope "
        "OR NEW.created_at<>OLD.created_at OR NEW.created_by<>OLD.created_by "
        "OR OLD.state<>'active' OR NEW.state<>'revoked' "
        "OR NEW.revoked_at IS NULL OR NEW.revoked_by IS NULL "
        "BEGIN SELECT RAISE(ABORT, 'production_inspection_scope_immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_mcp_production_scope_no_delete "
        "BEFORE DELETE ON mcp_production_target_scopes "
        "BEGIN SELECT RAISE(ABORT, 'production_inspection_scope_immutable'); END"
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
                "id": PRODUCTION_INSPECTION_PRESET_ID,
                "display_name": "Production Inspection",
                "builtin": True,
                "assignable": True,
            }
        ],
    )
    op.bulk_insert(
        permissions,
        [
            {
                "preset_id": PRODUCTION_INSPECTION_PRESET_ID,
                "permission": "target.inspect",
            }
        ],
    )
    op.execute(
        "CREATE TRIGGER trg_mcp_production_target_preset_insert "
        "BEFORE INSERT ON mcp_target_preset_assignments "
        f"WHEN (COALESCE((SELECT target_type FROM targets WHERE id=NEW.target_id),'')='external_production' AND NEW.preset_id<>'{PRODUCTION_INSPECTION_PRESET_ID}') "
        f"OR (NEW.preset_id='{PRODUCTION_INSPECTION_PRESET_ID}' AND COALESCE((SELECT target_type FROM targets WHERE id=NEW.target_id),'')<>'external_production') "
        "BEGIN SELECT RAISE(ABORT, 'production_inspection_preset_target_invalid'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_mcp_production_target_preset_update "
        "BEFORE UPDATE OF target_id,preset_id ON mcp_target_preset_assignments "
        f"WHEN (COALESCE((SELECT target_type FROM targets WHERE id=NEW.target_id),'')='external_production' AND NEW.preset_id<>'{PRODUCTION_INSPECTION_PRESET_ID}') "
        f"OR (NEW.preset_id='{PRODUCTION_INSPECTION_PRESET_ID}' AND COALESCE((SELECT target_type FROM targets WHERE id=NEW.target_id),'')<>'external_production') "
        "BEGIN SELECT RAISE(ABORT, 'production_inspection_preset_target_invalid'); END"
    )

    op.add_column(
        "mcp_security_events", sa.Column("production_scope_id", sa.String(36))
    )
    op.add_column("mcp_security_events", sa.Column("identity_generation", sa.Integer()))


def downgrade() -> None:
    connection = op.get_bind()
    populated = connection.execute(
        sa.text(
            "SELECT EXISTS(SELECT 1 FROM mcp_production_target_scopes) "
            "OR EXISTS(SELECT 1 FROM mcp_clients WHERE preset_id=:preset) "
            "OR EXISTS(SELECT 1 FROM mcp_target_preset_assignments WHERE preset_id=:preset) "
            "OR EXISTS(SELECT 1 FROM mcp_security_events WHERE event_kind IN "
            "('production_inspection_scope_granted','production_inspection_scope_revoked'))"
        ),
        {"preset": PRODUCTION_INSPECTION_PRESET_ID},
    ).scalar_one()
    if populated:
        raise RuntimeError(
            "refusing destructive downgrade while production inspection authority history exists"
        )

    op.drop_column("mcp_security_events", "identity_generation")
    op.drop_column("mcp_security_events", "production_scope_id")
    op.execute("DROP TRIGGER trg_mcp_production_target_preset_update")
    op.execute("DROP TRIGGER trg_mcp_production_target_preset_insert")
    op.execute(
        "DELETE FROM mcp_preset_permissions WHERE preset_id='"
        + PRODUCTION_INSPECTION_PRESET_ID
        + "'"
    )
    op.execute(
        "DELETE FROM mcp_presets WHERE id='" + PRODUCTION_INSPECTION_PRESET_ID + "'"
    )
    op.execute("DROP TRIGGER trg_mcp_production_scope_no_delete")
    op.execute("DROP TRIGGER trg_mcp_production_scope_immutable")
    op.execute("DROP TRIGGER trg_mcp_production_scope_binding_insert")
    op.drop_index(
        "uq_mcp_production_scope_active",
        table_name="mcp_production_target_scopes",
    )
    op.drop_index(
        "ix_mcp_production_scope_target_generation",
        table_name="mcp_production_target_scopes",
    )
    op.drop_index(
        "ix_mcp_production_scope_client",
        table_name="mcp_production_target_scopes",
    )
    op.drop_table("mcp_production_target_scopes")
