"""Keep exact single-session replacement reviews and selected evidence holds."""

import sqlalchemy as sa
from alembic import op

revision = "0082_session_replacement_reviews"
down_revision = "0081_managed_diagnostic_captures"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "development_session_replacement_reviews",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("request_id", sa.String(36), nullable=False),
        sa.Column("session_generation", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("snapshot_json", sa.Text(), nullable=False),
        sa.Column("snapshot_sha256", sa.String(64), nullable=False),
        sa.Column("created_at", sa.String(27), nullable=False),
        sa.Column("cancelled_at", sa.String(27)),
        sa.ForeignKeyConstraint(
            ["session_generation"],
            ["development_sessions.generation"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("request_id", name="session_replacement_request"),
        sa.CheckConstraint(
            "length(id)=36 AND length(request_id)=36 AND session_generation>0",
            name="session_replacement_identity",
        ),
        sa.CheckConstraint(
            "state IN ('open','cancelled')", name="session_replacement_state"
        ),
        sa.CheckConstraint(
            "length(CAST(snapshot_json AS BLOB)) BETWEEN 2 AND 16384 AND "
            "length(snapshot_sha256)=64 AND snapshot_sha256 NOT GLOB '*[^0-9a-f]*'",
            name="session_replacement_snapshot",
        ),
        sa.CheckConstraint(
            "(state='open' AND cancelled_at IS NULL) OR "
            "(state='cancelled' AND cancelled_at IS NOT NULL)",
            name="session_replacement_cancellation",
        ),
    )
    op.create_index(
        "uq_session_replacement_open",
        "development_session_replacement_reviews",
        ["state"],
        unique=True,
        sqlite_where=sa.text("state='open'"),
    )
    op.create_table(
        "development_session_evidence_holds",
        sa.Column("review_id", sa.String(36), primary_key=True),
        sa.Column("capture_id", sa.String(36), primary_key=True),
        sa.Column("payload_sha256", sa.String(64), nullable=False),
        sa.ForeignKeyConstraint(
            ["review_id"],
            ["development_session_replacement_reviews.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["capture_id"],
            ["managed_diagnostic_captures.id"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "length(payload_sha256)=64 AND payload_sha256 NOT GLOB '*[^0-9a-f]*'",
            name="session_evidence_hold_hash",
        ),
    )


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.text("SELECT count(*) FROM development_session_replacement_reviews")
    ):
        raise RuntimeError(
            "Session replacement review/history cannot be safely downgraded"
        )
    op.drop_table("development_session_evidence_holds")
    op.drop_index(
        "uq_session_replacement_open",
        table_name="development_session_replacement_reviews",
    )
    op.drop_table("development_session_replacement_reviews")
