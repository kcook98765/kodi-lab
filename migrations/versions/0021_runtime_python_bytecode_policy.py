"""Add immutable runtime Python bytecode policy.

Revision ID: 0021_runtime_python_bytecode_policy
Revises: 0020_dev_source_copy_sync
Create Date: 2026-09-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021_runtime_python_bytecode_policy"
down_revision: str | Sequence[str] | None = "0020_dev_source_copy_sync"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    connection = op.get_bind()
    op.add_column(
        "runtime_execution_profiles",
        sa.Column("python_bytecode_policy", sa.String(length=32), nullable=True),
    )
    connection.execute(
        sa.text(
            "UPDATE runtime_execution_profiles "
            "SET python_bytecode_policy = 'interpreter_default'"
        )
    )
    unresolved = connection.scalar(
        sa.text(
            "SELECT COUNT(*) FROM runtime_execution_profiles "
            "WHERE python_bytecode_policy IS NULL"
        )
    )
    if unresolved:
        raise RuntimeError("Cannot leave runtime Python bytecode policy unresolved")

    with op.batch_alter_table(
        "runtime_execution_profiles", recreate="always"
    ) as batch_op:
        batch_op.alter_column(
            "python_bytecode_policy",
            existing_type=sa.String(length=32),
            nullable=False,
        )
        batch_op.create_check_constraint(
            "runtime_profile_python_bytecode_policy",
            "python_bytecode_policy IN ('interpreter_default', 'suppress')",
        )


def downgrade() -> None:
    count = op.get_bind().scalar(
        sa.text("SELECT COUNT(*) FROM runtime_execution_profiles")
    )
    if count:
        raise RuntimeError(
            "Cannot downgrade populated runtime Python bytecode policy metadata."
        )
    with op.batch_alter_table(
        "runtime_execution_profiles", recreate="always"
    ) as batch_op:
        batch_op.drop_constraint(
            "runtime_profile_python_bytecode_policy", type_="check"
        )
        batch_op.drop_column("python_bytecode_policy")
