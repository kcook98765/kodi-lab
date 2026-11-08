"""External screenshots require an individual administrator opt-in."""

import sqlalchemy as sa
from alembic import op

revision = "0006_external_screenshots"
down_revision = "0005_managed_web"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP TRIGGER trg_external_screenshot_preference_initialize")
    op.execute("""
CREATE TRIGGER trg_external_screenshot_preference_initialize AFTER INSERT ON
external_kodi_targets BEGIN INSERT INTO external_screenshot_preferences
(target_id,enabled,revision,updated_by,updated_at)
VALUES (NEW.target_id,0,1,'system',NEW.created_at); END
""")
    op.execute(
        sa.text("""
UPDATE external_screenshot_preferences
SET enabled=0, revision=revision+1, updated_by='migration_0006',
updated_at=strftime('%Y-%m-%dT%H:%M:%f000Z','now')
WHERE enabled=1
""")
    )


def downgrade() -> None:
    # Restore the predecessor's creation rule, never its former enabled values.
    # Current OFF choices, revision history and replay invalidation survive rollback.
    op.execute("DROP TRIGGER trg_external_screenshot_preference_initialize")
    op.execute("""
CREATE TRIGGER trg_external_screenshot_preference_initialize AFTER INSERT ON
external_kodi_targets BEGIN INSERT INTO external_screenshot_preferences
(target_id,enabled,revision,updated_by,updated_at)
VALUES (NEW.target_id,EXISTS(SELECT 1 FROM
json_each(NEW.configured_capabilities_json) WHERE value='screenshot'),
1,'system',NEW.created_at); END
""")
