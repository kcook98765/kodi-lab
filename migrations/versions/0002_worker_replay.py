"""Preserve terminal installer operation identity in application backups."""

from alembic import op
from sqlalchemy import text

revision = "0002_worker_replay"
down_revision = "0001_foundation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE TABLE runtime_worker_terminal_operations ("
        "operation_id BLOB NOT NULL PRIMARY KEY, "
        "CONSTRAINT runtime_worker_terminal_uuid "
        "CHECK (typeof(operation_id) = 'blob' AND length(operation_id) = 16)"
        ") WITHOUT ROWID"
    )
    op.execute(
        "CREATE TRIGGER trg_runtime_worker_terminal_no_update BEFORE UPDATE "
        "ON runtime_worker_terminal_operations BEGIN SELECT RAISE(ABORT, "
        "'runtime_worker_terminal_immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_runtime_worker_terminal_no_delete BEFORE DELETE "
        "ON runtime_worker_terminal_operations BEGIN SELECT RAISE(ABORT, "
        "'runtime_worker_terminal_immutable'); END"
    )


def downgrade() -> None:
    if (
        op.get_bind()
        .execute(text("SELECT 1 FROM runtime_worker_terminal_operations LIMIT 1"))
        .first()
        is not None
    ):
        raise RuntimeError("terminal worker replay history cannot be downgraded")
    op.drop_table("runtime_worker_terminal_operations")
