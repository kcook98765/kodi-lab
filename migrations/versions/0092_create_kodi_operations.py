"""Durable fixed-stage Create Kodi intent and dispatch ledger."""

import sqlalchemy as sa
from alembic import op

revision = "0092_create_kodi_operations"
down_revision = "0091_managed_start_authorization"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "create_kodi_operations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("request_id", sa.String(36), nullable=False),
        sa.Column("target_id", sa.String(36), nullable=False),
        sa.Column("generation", sa.String(16), nullable=False),
        sa.Column("recipe_id", sa.String(128), nullable=False),
        sa.Column("recipe_revision", sa.Integer(), nullable=False),
        sa.Column("recipe_digest", sa.String(71), nullable=False),
        sa.Column("scope", sa.String(32), nullable=False),
        sa.Column("stage", sa.String(16), nullable=False),
        sa.Column("checkpoint", sa.String(24), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("approved_at", sa.String(27), nullable=False),
        sa.Column("updated_at", sa.String(27), nullable=False),
        sa.Column("dispatched_at", sa.String(27), nullable=True),
        sa.Column("completed_at", sa.String(27), nullable=True),
        sa.Column("outcome", sa.String(16), nullable=True),
        sa.Column("reason", sa.String(64), nullable=True),
        sa.Column("installation_operation_id", sa.String(36), nullable=True),
        sa.Column("provision_operation_id", sa.String(36), nullable=True),
        sa.Column("start_operation_id", sa.String(36), nullable=True),
        sa.Column("runtime_revision_id", sa.String(36), nullable=True),
        sa.Column("image_id", sa.String(71), nullable=True),
        sa.Column("container_id", sa.String(64), nullable=True),
        sa.Column("runtime_state_revision", sa.Integer(), nullable=True),
        sa.UniqueConstraint("installation_operation_id"),
        sa.UniqueConstraint("provision_operation_id"),
        sa.UniqueConstraint("request_id"),
        sa.UniqueConstraint("start_operation_id"),
        sa.UniqueConstraint("target_id"),
        sa.CheckConstraint(
            "(container_id IS NULL AND runtime_state_revision IS NULL) OR "
            "(container_id IS NOT NULL AND length(container_id)=64 AND "
            "container_id NOT GLOB '*[^0-9a-f]*' AND runtime_state_revision IS "
            "NOT NULL AND runtime_state_revision>=0 AND runtime_revision_id IS "
            "NOT NULL)",
            name="create_kodi_container_binding",
        ),
        sa.CheckConstraint(
            "(checkpoint IN ('accepted','planned') AND dispatched_at IS NULL) "
            "OR (checkpoint IN ('dispatched','confirmed','complete') AND "
            "dispatched_at IS NOT NULL) OR (checkpoint='needs_attention' AND "
            "(outcome='refused' OR dispatched_at IS NOT NULL))",
            name="create_kodi_dispatch_checkpoint",
        ),
        sa.CheckConstraint(
            "length(id)=36 AND length(request_id)=36 AND length(target_id)=36 "
            "AND length(recipe_digest)=71 AND "
            "substr(recipe_digest,1,7)='sha256:' AND substr(recipe_digest,8) "
            "NOT GLOB '*[^0-9a-f]*' AND (reason IS NULL OR (length(reason) "
            "BETWEEN 1 AND 64 AND reason NOT GLOB '*[^a-z0-9_]*'))",
            name="create_kodi_identifiers",
        ),
        sa.CheckConstraint(
            "generation IN ('matrix','nexus','omega') AND "
            "scope='create_native_v1' AND recipe_revision>=1 AND "
            "length(recipe_id) BETWEEN 1 AND 128 AND version>=0",
            name="create_kodi_intent",
        ),
        sa.CheckConstraint(
            "(checkpoint IN ('accepted','planned','dispatched') AND outcome IS "
            "NULL AND reason IS NULL AND completed_at IS NULL) OR "
            "(checkpoint='confirmed' AND outcome IS NOT NULL AND "
            "outcome='succeeded' AND reason IS NULL AND completed_at IS NULL) "
            "OR (checkpoint='complete' AND stage='readiness' AND outcome IS "
            "NOT NULL AND outcome='succeeded' AND reason IS NULL AND "
            "completed_at IS NOT NULL) OR (checkpoint='needs_attention' AND "
            "outcome IS NOT NULL AND outcome IN "
            "('refused','failed','uncertain') AND reason IS NOT NULL AND "
            "completed_at IS NOT NULL)",
            name="create_kodi_outcome",
        ),
        sa.CheckConstraint(
            "(runtime_revision_id IS NULL AND image_id IS NULL) OR "
            "(runtime_revision_id IS NOT NULL AND "
            "length(runtime_revision_id)=36 AND image_id IS NOT NULL AND "
            "length(image_id)=71 AND substr(image_id,1,7)='sha256:' AND "
            "substr(image_id,8) NOT GLOB '*[^0-9a-f]*')",
            name="create_kodi_runtime_binding",
        ),
        sa.CheckConstraint(
            "stage IN "
            "('installation','registration','provisioning','startup','readiness') "
            "AND checkpoint IN "
            "('accepted','planned','dispatched','confirmed','needs_attention','complete')",
            name="create_kodi_stage",
        ),
    )


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM create_kodi_operations")):
        raise RuntimeError("Create intent history cannot be discarded")
    op.drop_table("create_kodi_operations")
