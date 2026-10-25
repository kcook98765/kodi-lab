"""Bind explicit replacement dispatch and consume reviews at atomic handoff."""

import sqlalchemy as sa
from alembic import op

revision = "0084_session_replacements"
down_revision = "0083_managed_runtime_retirements"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable REFERENCES plus a column-local CHECK permits SQLite ADD COLUMN
    # without rebuilding a referenced populated parent or disabling foreign keys.
    # The default immediate NO ACTION reference refuses parent deletion. Explicit
    # inline RESTRICT is not reflected by the installed SQLAlchemy dialect.
    # https://www.sqlite.org/lang_altertable.html#alter_table_add_column
    op.execute(
        "ALTER TABLE development_session_replacement_reviews ADD COLUMN "
        "successor_generation INTEGER REFERENCES development_sessions(generation) "
        "CONSTRAINT session_replacement_successor CHECK "
        "(successor_generation IS NULL OR (state='open' AND "
        "successor_generation>session_generation))"
    )
    op.drop_index(
        "uq_session_replacement_open",
        table_name="development_session_replacement_reviews",
    )
    op.create_index(
        "uq_session_replacement_open",
        "development_session_replacement_reviews",
        ["state"],
        unique=True,
        sqlite_where=sa.text("state='open' AND successor_generation IS NULL"),
    )
    op.create_table(
        "development_session_replacements",
        sa.Column("review_id", sa.String(36), primary_key=True),
        sa.Column("request_id", sa.String(36), nullable=False),
        sa.Column("review_snapshot_sha256", sa.String(64), nullable=False),
        sa.Column("retirement_operation_id", sa.String(36), nullable=False),
        sa.Column("prepare_operation_id", sa.String(36)),
        sa.Column("requested_at", sa.String(27), nullable=False),
        sa.Column("profile_disposal_confirmed", sa.Boolean(), nullable=False),
        sa.Column("released_at", sa.String(27)),
        sa.UniqueConstraint("request_id", name="session_replacement_execution_request"),
        sa.UniqueConstraint(
            "retirement_operation_id", name="session_replacement_retirement"
        ),
        sa.UniqueConstraint(
            "prepare_operation_id", name="session_replacement_preparation"
        ),
        sa.ForeignKeyConstraint(
            ["review_id"],
            ["development_session_replacement_reviews.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["retirement_operation_id"],
            ["managed_runtime_retirements.operation_id"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "length(request_id)=36 AND length(retirement_operation_id)=36",
            name="session_replacement_execution_identity",
        ),
        sa.CheckConstraint(
            "length(review_snapshot_sha256)=64 AND "
            "review_snapshot_sha256 NOT GLOB '*[^0-9a-f]*'",
            name="session_replacement_execution_hash",
        ),
        sa.CheckConstraint(
            "profile_disposal_confirmed=1",
            name="session_replacement_disposal_confirmation",
        ),
        sa.CheckConstraint(
            "prepare_operation_id IS NULL OR length(prepare_operation_id)=36",
            name="session_replacement_prepare_identity",
        ),
        sa.CheckConstraint(
            "length(requested_at)=27 AND "
            "(released_at IS NULL OR length(released_at)=27)",
            name="session_replacement_execution_timestamps",
        ),
    )


def downgrade() -> None:
    connection = op.get_bind()
    if connection.scalar(
        sa.text("SELECT count(*) FROM development_session_replacements")
    ) or connection.scalar(
        sa.text(
            "SELECT count(*) FROM development_session_replacement_reviews "
            "WHERE successor_generation IS NOT NULL"
        )
    ):
        raise RuntimeError(
            "Session replacement execution/history cannot be safely downgraded"
        )
    op.drop_table("development_session_replacements")
    op.drop_index(
        "uq_session_replacement_open",
        table_name="development_session_replacement_reviews",
    )
    op.execute(
        "ALTER TABLE development_session_replacement_reviews "
        "DROP COLUMN successor_generation"
    )
    op.create_index(
        "uq_session_replacement_open",
        "development_session_replacement_reviews",
        ["state"],
        unique=True,
        sqlite_where=sa.text("state='open'"),
    )
