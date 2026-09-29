"""Approved, immutable development repository publications.

Revision ID: 0055_development_repository_publications
Revises: 0054_runtime_recipe_installations
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0055_development_repository_publications"
down_revision = "0054_runtime_recipe_installations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "development_repository_state",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("base_url", sa.String(512), nullable=False),
        sa.Column("bootstrap_sha256", sa.String(64), nullable=False),
        sa.Column("bootstrap_tree_sha256", sa.String(64), nullable=False),
        sa.Column("current_generation", sa.String(64), nullable=False),
        sa.Column("current_index_sha256", sa.String(64), nullable=False),
        sa.Column("pending_generation", sa.String(64)),
        sa.Column("pending_index_sha256", sa.String(64)),
        sa.Column("pending_retirements", sa.Text()),
        sa.Column("updated_at", sa.String(32), nullable=False),
        sa.CheckConstraint("id=1", name="development_repository_singleton"),
    )
    op.create_table(
        "development_publications",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("addon_id", sa.String(128), nullable=False),
        sa.Column("version", sa.String(64), nullable=False),
        sa.Column("addon_type", sa.String(48), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("source_kind", sa.String(16), nullable=False),
        sa.Column("source_id", sa.String(36), nullable=False),
        sa.Column(
            "workspace_id",
            sa.String(36),
            sa.ForeignKey("agent_workspaces.id", ondelete="RESTRICT"),
        ),
        sa.Column("workspace_generation", sa.Integer()),
        sa.Column("validation_receipt", sa.String(71)),
        sa.Column("source_tree_sha256", sa.String(71)),
        sa.Column("compatible_generations", sa.String(64), nullable=False),
        sa.Column("scope_kind", sa.String(32), nullable=False),
        sa.Column("intended_targets", sa.Text()),
        sa.Column("zip_sha256", sa.String(64), nullable=False),
        sa.Column("tree_sha256", sa.String(64), nullable=False),
        sa.Column("zip_bytes", sa.Integer(), nullable=False),
        sa.Column("feed_generation", sa.String(64)),
        sa.Column("index_sha256", sa.String(64)),
        sa.Column("approval_identity", sa.String(128)),
        sa.Column("approved_at", sa.String(32)),
        sa.Column("created_at", sa.String(32), nullable=False),
        sa.Column("published_at", sa.String(32)),
        sa.Column("superseded_at", sa.String(32)),
        sa.Column("retired_at", sa.String(32)),
        sa.Column("failure_reason", sa.String(128)),
        sa.UniqueConstraint(
            "addon_id", "version", name="uq_development_publication_identity"
        ),
        sa.CheckConstraint(
            "channel='development'", name="development_publication_channel"
        ),
        sa.CheckConstraint(
            "addon_type='plugin.video'", name="development_publication_type"
        ),
        sa.CheckConstraint(
            "scope_kind='development_only'", name="development_publication_scope"
        ),
        sa.CheckConstraint(
            "status IN ('prepared','publishing','current',"
            "'superseded','retired','failed')",
            name="development_publication_status",
        ),
        sa.CheckConstraint(
            "source_kind IN ('web_import','workspace')",
            name="development_publication_source_kind",
        ),
        sa.CheckConstraint("zip_bytes > 0", name="development_publication_nonempty"),
        sa.CheckConstraint(
            "(source_kind='web_import' AND workspace_id IS NULL "
            "AND workspace_generation IS NULL) OR "
            "(source_kind='workspace' AND workspace_id IS NOT NULL "
            "AND workspace_generation IS NOT NULL "
            "AND validation_receipt IS NOT NULL "
            "AND source_tree_sha256 IS NOT NULL)",
            name="development_publication_source_binding",
        ),
    )
    op.create_index(
        "uq_development_publication_current",
        "development_publications",
        ["addon_id"],
        unique=True,
        sqlite_where=sa.text("status='current'"),
    )
    op.create_index(
        "ix_development_publication_status", "development_publications", ["status"]
    )
    op.create_table(
        "development_publication_holds",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "publication_id",
            sa.String(36),
            sa.ForeignKey("development_publications.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("reason", sa.String(48), nullable=False),
        sa.Column("reference_id", sa.String(128), nullable=False),
        sa.Column("created_at", sa.String(32), nullable=False),
        sa.UniqueConstraint(
            "publication_id",
            "reason",
            "reference_id",
            name="uq_development_publication_hold",
        ),
    )
    immutable = (
        "id",
        "channel",
        "addon_id",
        "version",
        "addon_type",
        "source_kind",
        "source_id",
        "workspace_id",
        "workspace_generation",
        "validation_receipt",
        "source_tree_sha256",
        "compatible_generations",
        "scope_kind",
        "intended_targets",
        "zip_sha256",
        "tree_sha256",
        "zip_bytes",
    )
    condition = " OR ".join(f"OLD.{name} IS NOT NEW.{name}" for name in immutable)
    op.execute(
        "CREATE TRIGGER trg_development_publication_identity_immutable "
        "BEFORE UPDATE ON development_publications WHEN "
        + condition
        + " BEGIN SELECT RAISE(ABORT, "
        "'development_publication_identity_immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_development_publication_no_delete "
        "BEFORE DELETE ON development_publications BEGIN SELECT "
        "RAISE(ABORT, 'development_publication_no_delete'); END"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER trg_development_publication_no_delete")
    op.execute("DROP TRIGGER trg_development_publication_identity_immutable")
    op.drop_table("development_publication_holds")
    op.drop_index(
        "ix_development_publication_status", table_name="development_publications"
    )
    op.drop_index(
        "uq_development_publication_current", table_name="development_publications"
    )
    op.drop_table("development_publications")
    op.drop_table("development_repository_state")
