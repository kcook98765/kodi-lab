"""One-use authority for ordinary managed Kodi Start."""

import sqlalchemy as sa
from alembic import op

from kodi_lab.storage.retained_policy_sql import (
    MANAGED_START_MUTATION_TRIGGER,
    RETAINED_MUTATION_TRIGGER,
)

revision = "0091_managed_start_authorization"
down_revision = "0090_verified_noop_deployment"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "managed_start_authorizations",
        sa.Column(
            "operation_id",
            sa.String(36),
            sa.ForeignKey(
                "managed_instance_lifecycle_operations.id", ondelete="CASCADE"
            ),
            primary_key=True,
        ),
        sa.Column("target_id", sa.String(36), nullable=False),
        sa.Column("runtime_revision_id", sa.String(36), nullable=False),
        sa.Column("container_id", sa.String(64), nullable=False),
        sa.Column("image_id", sa.String(71), nullable=False),
        sa.Column("runtime_state_revision", sa.Integer(), nullable=False),
        sa.Column("lease_digest", sa.String(64), nullable=False),
        sa.Column("proof_digest", sa.String(64), nullable=False, unique=True),
        sa.Column("created_at", sa.String(27), nullable=False),
        sa.Column("consumed_at", sa.String(27)),
        sa.CheckConstraint(
            "length(target_id)=36 AND length(runtime_revision_id)=36 "
            "AND length(container_id)=64 AND length(image_id)=71 "
            "AND substr(image_id,1,7)='sha256:' AND runtime_state_revision>=0 "
            "AND length(lease_digest)=64 AND length(proof_digest)=64",
            name="managed_start_authorization_binding",
        ),
    )

    op.execute("DROP TRIGGER retained_mutation_insert")
    op.execute(MANAGED_START_MUTATION_TRIGGER)


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.text("SELECT count(*) FROM managed_start_authorizations")
    ):
        raise RuntimeError("ordinary Start authorization history cannot be discarded")
    op.execute("DROP TRIGGER retained_mutation_insert")
    op.execute(RETAINED_MUTATION_TRIGGER)
    op.drop_table("managed_start_authorizations")
