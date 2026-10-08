"""Durable administrator household Connect operations."""

import sqlalchemy as sa
from alembic import op

from kodi_lab.storage.household_onboarding_sql import (
    EXTERNAL_SECRET_RETIREMENT_TRIGGERS,
    HOUSEHOLD_ONBOARDING_TRIGGERS,
)

revision = "0071_household_onboarding"
down_revision = "0070_external_screenshot_preference"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "external_secret_retirements",
        sa.Column("store_kind", sa.String(16), primary_key=True),
        sa.Column("reference", sa.String(36), primary_key=True),
        sa.Column("target_id", sa.String(36), nullable=False),
        sa.Column("created_at", sa.String(27), nullable=False),
        sa.CheckConstraint(
            "store_kind IN ('bridge','kodi')",
            name="external_secret_retirement_store_closed",
        ),
        sa.CheckConstraint(
            "length(reference)=36 AND length(target_id)=36 AND length(created_at)=27",
            name="external_secret_retirement_identity_bounded",
        ),
    )
    for statement in EXTERNAL_SECRET_RETIREMENT_TRIGGERS:
        op.execute(statement)
    op.create_table(
        "household_onboarding_operations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("target_id", sa.String(36), nullable=False, unique=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("host", sa.String(64), nullable=False),
        sa.Column("kodi_port", sa.Integer(), nullable=False),
        sa.Column("bridge_port", sa.Integer(), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("stage", sa.String(32), nullable=False),
        sa.Column("error_code", sa.String(64)),
        sa.Column("disclosure_token", sa.String(64)),
        sa.Column("native_binding_digest", sa.String(64)),
        sa.Column("created_by", sa.String(64), nullable=False),
        sa.Column("created_at", sa.String(27), nullable=False),
        sa.Column("updated_at", sa.String(27), nullable=False),
        sa.CheckConstraint(
            "stage IN ('reserved','native_verified','bootstrap_started',"
            "'paired','connected','blocked','abandoned')",
            name="household_onboarding_stage",
        ),
    )
    op.create_index(
        "uq_household_onboarding_endpoint",
        "household_onboarding_operations",
        ["host", "kodi_port"],
        unique=True,
        sqlite_where=sa.text("stage <> 'abandoned'"),
    )

    for statement in HOUSEHOLD_ONBOARDING_TRIGGERS:
        op.execute(statement)


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.text("SELECT count(*) FROM external_secret_retirements")
    ):
        raise RuntimeError("Pending external secret retirement cannot be downgraded")
    if op.get_bind().scalar(
        sa.text("SELECT count(*) FROM household_onboarding_operations")
    ):
        raise RuntimeError("Household onboarding history cannot be safely downgraded")
    for statement in reversed(HOUSEHOLD_ONBOARDING_TRIGGERS):
        op.execute("DROP TRIGGER " + statement.split()[2])
    op.drop_index("uq_household_onboarding_endpoint", "household_onboarding_operations")
    op.drop_table("household_onboarding_operations")
    for statement in reversed(EXTERNAL_SECRET_RETIREMENT_TRIGGERS):
        op.execute("DROP TRIGGER " + statement.split()[2])
    op.drop_table("external_secret_retirements")
