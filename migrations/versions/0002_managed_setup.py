"""Separate installed profiles from managed running capacity and admit Piers setup."""

import sqlalchemy as sa
from alembic import op

revision = "0002_managed_setup"
down_revision = "0001_initial_release"
branch_labels = None
depends_on = None

OLD_INTENT = (
    "generation IN ('matrix','nexus','omega') "
    "AND scope='create_native_v1' AND recipe_revision>=1 "
    "AND length(recipe_id) BETWEEN 1 AND 128 AND version>=0"
)
NEW_INTENT = (
    OLD_INTENT.replace("'nexus','omega')", "'nexus','omega','piers')")
    + " AND (generation!='piers' OR preview_approved=1)"
)
OLD_OUTCOME = (
    "(checkpoint IN ('accepted','planned','dispatched') AND outcome IS NULL "
    "AND reason IS NULL AND completed_at IS NULL) OR "
    "(checkpoint='confirmed' AND outcome IS NOT NULL "
    "AND outcome='succeeded' AND reason IS NULL AND completed_at IS NULL) OR "
    "(checkpoint='complete' AND stage='readiness' AND outcome IS NOT NULL "
    "AND outcome='succeeded' AND reason IS NULL AND completed_at IS NOT NULL) OR "
    "(checkpoint='needs_attention' AND outcome IS NOT NULL "
    "AND outcome IN ('refused','failed','uncertain') AND reason IS NOT NULL "
    "AND completed_at IS NOT NULL)"
)
NEW_OUTCOME = OLD_OUTCOME.replace(
    "stage='readiness'",
    "(stage='readiness' OR (stage='provisioning' AND start_after_setup=0 "
    "AND container_id IS NOT NULL AND start_operation_id IS NULL))",
)


def upgrade() -> None:
    with op.batch_alter_table("create_kodi_operations", recreate="always") as batch:
        batch.add_column(
            sa.Column(
                "start_after_setup", sa.Boolean(), nullable=False, server_default="1"
            )
        )
        batch.add_column(
            sa.Column(
                "preview_approved", sa.Boolean(), nullable=False, server_default="0"
            )
        )
        batch.drop_constraint("create_kodi_intent", type_="check")
        batch.drop_constraint("create_kodi_outcome", type_="check")
        batch.create_check_constraint("create_kodi_intent", NEW_INTENT)
        batch.create_check_constraint("create_kodi_outcome", NEW_OUTCOME)
        batch.create_check_constraint(
            "create_start_after_setup", "start_after_setup IN (0, 1)"
        )
        batch.create_check_constraint(
            "create_preview_approved", "preview_approved IN (0, 1)"
        )


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.text(
            "SELECT count(*) FROM create_kodi_operations WHERE "
            "start_after_setup!=1 OR preview_approved!=0 OR generation='piers'"
        )
    ):
        raise RuntimeError(
            "Managed setup options have been used; restore a coordinated backup"
        )
    with op.batch_alter_table("create_kodi_operations", recreate="always") as batch:
        for name in (
            "create_kodi_intent",
            "create_kodi_outcome",
            "create_start_after_setup",
            "create_preview_approved",
        ):
            batch.drop_constraint(name, type_="check")
        batch.drop_column("start_after_setup")
        batch.drop_column("preview_approved")
        batch.create_check_constraint("create_kodi_intent", OLD_INTENT)
        batch.create_check_constraint("create_kodi_outcome", OLD_OUTCOME)
