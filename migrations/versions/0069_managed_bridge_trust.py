"""Lifecycle-owned managed Bridge trust; no credential or authority backfill."""

import sqlalchemy as sa
from alembic import op

revision = "0069_managed_bridge_trust"
down_revision = "0068_ui_observation_capability"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "managed_bridge_trust",
        sa.Column(
            "target_id",
            sa.String(36),
            sa.ForeignKey("managed_instances.target_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "runtime_revision_id",
            sa.String(36),
            sa.ForeignKey("runtime_revisions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("container_id", sa.String(64), nullable=False),
        sa.Column("bridge_id", sa.String(36)),
        sa.Column("bridge_port", sa.Integer(), nullable=False),
        sa.Column("secret_reference", sa.String(36)),
        sa.Column("identity_json", sa.String(4096)),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("prepared_at", sa.String(27), nullable=False),
        sa.Column("established_at", sa.String(27)),
        sa.Column("invalidated_at", sa.String(27)),
        sa.CheckConstraint(
            "state IN ('prepared','active','invalidated','revoked')",
            name="managed_bridge_trust_state",
        ),
        sa.CheckConstraint("bridge_port=8765", name="managed_bridge_trust_port"),
        sa.CheckConstraint(
            "length(container_id)=64 AND container_id NOT GLOB '*[^0-9a-f]*' "
            "AND (bridge_id IS NULL OR length(bridge_id)=36) "
            "AND (secret_reference IS NULL OR length(secret_reference)=36)",
            name="managed_bridge_trust_identity_shape",
        ),
        sa.CheckConstraint(
            "identity_json IS NULL OR length(identity_json) BETWEEN 2 AND 4096",
            name="managed_bridge_trust_identity_bounded",
        ),
        sa.CheckConstraint("generation>=1", name="managed_bridge_trust_generation"),
        sa.CheckConstraint(
            "(state IN ('prepared','active') AND secret_reference IS NOT NULL) OR "
            "(state IN ('invalidated','revoked') AND secret_reference IS NULL)",
            name="managed_bridge_trust_credential_state",
        ),
        sa.CheckConstraint(
            "state<>'active' OR (bridge_id IS NOT NULL AND identity_json IS NOT NULL "
            "AND established_at IS NOT NULL)",
            name="managed_bridge_trust_activation_complete",
        ),
    )


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM managed_bridge_trust")):
        raise RuntimeError(
            "Managed Bridge trust records must be removed after credential retirement "
            "before downgrade"
        )
    op.drop_table("managed_bridge_trust")
