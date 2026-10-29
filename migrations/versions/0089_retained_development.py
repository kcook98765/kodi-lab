"""Opt-in retained profiles and requester-bound selection receipts."""

import sqlalchemy as sa
from alembic import op

from kodi_lab.storage.retained_policy_sql import RETAINED_MUTATION_TRIGGER

revision = "0089_retained_development"
down_revision = "0088_service_enable_start"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "development_session_policy",
        sa.Column(
            "retain_profiles",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "development_session_policy",
        sa.Column(
            "retained_omega_target_id",
            sa.String(36),
            nullable=True,
        ),
    )
    with op.batch_alter_table("development_session_policy") as batch:
        batch.create_check_constraint(
            "development_retained_policy",
            "(retain_profiles=0 AND retained_omega_target_id IS NULL) OR "
            "(retain_profiles=1 AND retained_omega_target_id IS NOT NULL "
            "AND retained_omega_target_id IN "
            "('908822fe-132a-5283-ac40-04e26d387db3','d968c23e-67e7-5cb2-99be-472b13ba3536'))",
        )
    op.create_table(
        "development_selections",
        sa.Column("actor", sa.String(48), primary_key=True),
        sa.Column("request_id", sa.String(36), primary_key=True),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column(
            "session_generation",
            sa.Integer(),
            sa.ForeignKey("development_sessions.generation", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(fingerprint)=64", name="development_selection_digest"
        ),
        sa.CheckConstraint(
            "length(request_id)=36", name="development_selection_request"
        ),
    )
    op.execute(RETAINED_MUTATION_TRIGGER)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.scalar(
        sa.text("SELECT count(*) FROM development_selections")
    ) or bind.scalar(
        sa.text(
            "SELECT count(*) FROM development_session_policy WHERE retain_profiles != 0"
        )
    ):
        raise RuntimeError("Retained profile policy/history cannot be discarded")
    op.execute("DROP TRIGGER retained_mutation_insert")
    op.drop_table("development_selections")
    with op.batch_alter_table("development_session_policy") as batch:
        batch.drop_constraint("development_retained_policy", type_="check")
        batch.drop_column("retained_omega_target_id")
        batch.drop_column("retain_profiles")
