"""Keep exact scoped retirement intent and confirmed lifecycle receipts."""

import sqlalchemy as sa
from alembic import op

revision = "0083_managed_runtime_retirements"
down_revision = "0082_session_replacement_reviews"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "managed_runtime_retirements",
        sa.Column("operation_id", sa.String(36), primary_key=True),
        sa.Column("original_managed_instance_id", sa.String(36), nullable=False),
        sa.Column("runtime_revision_id", sa.String(36), nullable=False),
        sa.Column("container_id", sa.String(64), nullable=False),
        sa.Column("image_id", sa.String(71), nullable=False),
        sa.Column("prepared_at", sa.String(27), nullable=False),
        sa.Column("confirmed_at", sa.String(27)),
        sa.ForeignKeyConstraint(
            ["operation_id"],
            ["managed_instance_lifecycle_operations.id"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "length(operation_id)=36 AND length(original_managed_instance_id)=36 "
            "AND length(runtime_revision_id)=36",
            name="runtime_retirement_identity",
        ),
        sa.CheckConstraint(
            "length(container_id)=64 AND container_id NOT GLOB '*[^0-9a-f]*' "
            "AND length(image_id)=71 AND substr(image_id,1,7)='sha256:' "
            "AND substr(image_id,8) NOT GLOB '*[^0-9a-f]*'",
            name="runtime_retirement_binding",
        ),
        sa.CheckConstraint(
            "length(prepared_at)=27 AND (confirmed_at IS NULL OR "
            "(length(confirmed_at)=27 AND confirmed_at>=prepared_at))",
            name="runtime_retirement_confirmation",
        ),
    )


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.text("SELECT count(*) FROM managed_runtime_retirements")
    ):
        raise RuntimeError(
            "Scoped runtime retirement history cannot be safely downgraded"
        )
    op.drop_table("managed_runtime_retirements")
