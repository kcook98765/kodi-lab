"""Independent external capture preference; no trust or authority changes."""

import sqlalchemy as sa
from alembic import op

from kodi_lab.storage.screenshot_preference_sql import SCREENSHOT_PREFERENCE_TRIGGERS

revision = "0070_external_screenshot_preference"
down_revision = "0069_managed_bridge_trust"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "external_screenshot_preferences",
        sa.Column(
            "target_id",
            sa.String(36),
            sa.ForeignKey("external_kodi_targets.target_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("updated_by", sa.String(64), nullable=False),
        sa.Column("updated_at", sa.String(27), nullable=False),
        sa.CheckConstraint("enabled IN (0,1)", name="screenshot_preference_boolean"),
        sa.CheckConstraint(
            "revision>=1 AND typeof(revision)='integer'",
            name="screenshot_preference_revision_positive",
        ),
        sa.CheckConstraint(
            "length(updated_by) BETWEEN 1 AND 64 AND length(updated_at)=27",
            name="screenshot_preference_audit_bounded",
        ),
    )
    # Preserve the old maximum. All existing authentication, trust, grants,
    # administrative state and technical capability checks still apply.
    op.execute(
        "INSERT INTO external_screenshot_preferences "
        "(target_id,enabled,revision,updated_by,updated_at) SELECT target_id,"
        "EXISTS(SELECT 1 FROM json_each(configured_capabilities_json) WHERE "
        "value='screenshot'),1,'migration',created_at FROM external_kodi_targets"
    )
    op.add_column(
        "external_bridge_request_receipts",
        sa.Column(
            "capture_preference_revision",
            sa.Integer(),
            sa.CheckConstraint(
                "capture_preference_revision>=1 AND "
                "typeof(capture_preference_revision)='integer'",
                name="bridge_capture_preference_revision_positive",
            ),
            nullable=False,
            server_default=sa.text("1"),
        ),
    )
    for statement in SCREENSHOT_PREFERENCE_TRIGGERS:
        op.execute(statement)


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.text(
            "SELECT count(*) FROM external_screenshot_preferences p JOIN "
            "external_kodi_targets e ON e.target_id=p.target_id WHERE p.revision<>1 "
            "OR p.enabled<>EXISTS(SELECT 1 FROM "
            "json_each(e.configured_capabilities_json) WHERE value='screenshot')"
        )
    ):
        raise RuntimeError("Changed screenshot preferences cannot be safely downgraded")
    for statement in reversed(SCREENSHOT_PREFERENCE_TRIGGERS):
        op.execute("DROP TRIGGER " + statement.split()[2])
    op.drop_column("external_bridge_request_receipts", "capture_preference_revision")
    op.drop_table("external_screenshot_preferences")
