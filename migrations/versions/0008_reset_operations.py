"""Add reset-specific durable operation coordination.

Revision ID: 0008_reset_operations
Revises: 0007_fixture_definitions
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_reset_operations"
down_revision: str | None = "0007_fixture_definitions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_PHASES = (
    "queued",
    "preflight",
    "stopping",
    "deprovisioning",
    "provisioning",
    "applying_fixture",
    "starting_for_fixture",
    "waiting_readiness",
    "scanning_fixture",
    "verifying_fixture",
    "final_stopping",
    "final_verifying",
    "succeeded",
    "failed",
    "reconciliation_required",
)


def upgrade() -> None:
    op.create_table(
        "reset_operations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("managed_instance_id", sa.String(36), nullable=False),
        sa.Column("requested_at", sa.String(32), nullable=False),
        sa.Column("started_at", sa.String(32), nullable=True),
        sa.Column("heartbeat_at", sa.String(32), nullable=True),
        sa.Column("completed_at", sa.String(32), nullable=True),
        sa.Column("state", sa.String(23), nullable=False),
        sa.Column("phase", sa.String(23), nullable=False),
        sa.Column("failure_reason", sa.String(256), nullable=True),
        sa.Column(
            "reconciliation_required", sa.Boolean(), server_default="0", nullable=False
        ),
        sa.Column("lease_token_digest", sa.String(64), nullable=True),
        sa.Column("lease_expires_at", sa.String(32), nullable=True),
        sa.CheckConstraint(
            "state IN ('queued','running','succeeded','failed',"
            "'reconciliation_required')",
            name="reset_operation_state",
        ),
        sa.CheckConstraint(
            "phase IN (" + ",".join(repr(item) for item in _PHASES) + ")",
            name="reset_operation_phase",
        ),
        sa.CheckConstraint(
            "failure_reason IS NULL OR length(failure_reason) BETWEEN 1 AND 256",
            name="reset_operation_failure_reason_bounded",
        ),
        sa.CheckConstraint(
            "lease_token_digest IS NULL OR (length(lease_token_digest) = 64 "
            "AND lease_token_digest NOT GLOB '*[^0-9a-f]*')",
            name="reset_operation_lease_digest_sha256",
        ),
        sa.CheckConstraint(
            "(state = 'queued' AND started_at IS NULL AND completed_at IS NULL "
            "AND lease_token_digest IS NULL AND lease_expires_at IS NULL) OR "
            "(state = 'running' AND started_at IS NOT NULL "
            "AND completed_at IS NULL AND heartbeat_at IS NOT NULL "
            "AND lease_token_digest IS NOT NULL AND lease_expires_at IS NOT NULL) OR "
            "(state IN ('succeeded', 'failed', 'reconciliation_required') "
            "AND completed_at IS NOT NULL AND lease_token_digest IS NULL "
            "AND lease_expires_at IS NULL)",
            name="reset_operation_state_fields_consistent",
        ),
        sa.ForeignKeyConstraint(
            ["managed_instance_id"],
            ["managed_instances.target_id"],
            name="fk_reset_operation_managed_instance",
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "uq_reset_operations_active_instance",
        "reset_operations",
        ["managed_instance_id"],
        unique=True,
        sqlite_where=sa.text("state IN ('queued', 'running')"),
    )
    op.create_index(
        "ix_reset_operations_instance_requested",
        "reset_operations",
        ["managed_instance_id", "requested_at"],
        unique=False,
    )


def downgrade() -> None:
    count = op.get_bind().scalar(sa.text("SELECT COUNT(*) FROM reset_operations"))
    if count:
        raise RuntimeError("Cannot downgrade populated reset operations.")
    op.drop_index(
        "ix_reset_operations_instance_requested", table_name="reset_operations"
    )
    op.drop_index("uq_reset_operations_active_instance", table_name="reset_operations")
    op.drop_table("reset_operations")
