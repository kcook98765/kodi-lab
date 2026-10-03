"""Order managed runtime projection and represent unconfirmed replacement Stop.

Revision ID: 0063_managed_runtime_projection
Revises: 0062_workspace_kodi_error
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0063_managed_runtime_projection"
down_revision = "0062_workspace_kodi_error"
branch_labels = None
depends_on = None

_OLD_RESULT = (
    "(state='succeeded' AND application_started=1 AND "
    "stopped_readback_matched=1 AND final_readback_matched=1 AND "
    "recognition_enabled IS NOT NULL AND dependency_baseline_json IS NOT NULL "
    "AND dependency_post_json IS NOT NULL AND reconciliation_required=0) OR "
    "(state='failed' AND application_started=0 AND reconciliation_required=0) OR "
    "(state='reconciliation_required' AND application_started=1 AND "
    "reconciliation_required=1) OR state IN ('queued','running')"
)
_NEW_RESULT = _OLD_RESULT.replace(
    "(state='reconciliation_required' AND application_started=1 AND "
    "reconciliation_required=1)",
    "(state='reconciliation_required' AND reconciliation_required=1 AND "
    "(application_started=1 OR (application_started=0 AND "
    "phase IN ('stopping','revalidate_stopped') AND "
    "COALESCE(failure_reason,'')='broker_runtime_dispatch_unconfirmed')))",
)


def upgrade() -> None:
    op.add_column(
        "managed_instances",
        sa.Column(
            "runtime_state_revision",
            sa.Integer(),
            sa.CheckConstraint(
                "runtime_state_revision >= 0",
                name="managed_instance_runtime_state_revision_nonnegative",
            ),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    with op.batch_alter_table(
        "repository_replacement_operations", recreate="always"
    ) as batch:
        batch.drop_constraint(
            "repository_replacement_operation_result_consistent", type_="check"
        )
        batch.create_check_constraint(
            "repository_replacement_operation_result_consistent", _NEW_RESULT
        )


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS(SELECT 1 FROM repository_replacement_operations "
            "WHERE state='reconciliation_required' AND application_started=0)"
        )
    ):
        raise RuntimeError(
            "unconfirmed replacement dispatch history cannot be downgraded"
        )
    with op.batch_alter_table(
        "repository_replacement_operations", recreate="always"
    ) as batch:
        batch.drop_constraint(
            "repository_replacement_operation_result_consistent", type_="check"
        )
        batch.create_check_constraint(
            "repository_replacement_operation_result_consistent", _OLD_RESULT
        )
    with op.batch_alter_table("managed_instances", recreate="always") as batch:
        batch.drop_constraint(
            "managed_instance_runtime_state_revision_nonnegative", type_="check"
        )
        batch.drop_column("runtime_state_revision")
