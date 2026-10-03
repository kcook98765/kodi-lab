"""Permit the fixed post-scan phases in durable reset history.

Revision ID: 0061_reset_phase_v5
Revises: 0060_repository_existing_verify
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0061_reset_phase_v5"
down_revision = "0060_repository_existing_verify"
branch_labels = None
depends_on = None

_OLD_PHASES = (
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
_PHASES = (
    *_OLD_PHASES[:10],
    "applying_post_scan_state",
    "verifying_post_scan_state",
    *_OLD_PHASES[10:],
)


def _closed_phases(phases: tuple[str, ...]) -> str:
    return "phase IN (" + ",".join(repr(phase) for phase in phases) + ")"


def upgrade() -> None:
    with op.batch_alter_table("reset_operations", recreate="always") as batch:
        batch.drop_constraint("reset_operation_phase", type_="check")
        batch.alter_column(
            "phase", existing_type=sa.String(23), type_=sa.String(25), nullable=False
        )
        batch.create_check_constraint("reset_operation_phase", _closed_phases(_PHASES))


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS(SELECT 1 FROM reset_operations WHERE phase IN "
            "('applying_post_scan_state','verifying_post_scan_state'))"
        )
    ):
        raise RuntimeError("post-scan reset history cannot be downgraded")
    with op.batch_alter_table("reset_operations", recreate="always") as batch:
        batch.drop_constraint("reset_operation_phase", type_="check")
        batch.alter_column(
            "phase", existing_type=sa.String(25), type_=sa.String(23), nullable=False
        )
        batch.create_check_constraint(
            "reset_operation_phase", _closed_phases(_OLD_PHASES)
        )
