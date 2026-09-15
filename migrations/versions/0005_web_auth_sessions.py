"""Add the single Web administrator and opaque server sessions.

Revision ID: 0005_web_auth_sessions
Revises: 0004_runtime_execution_profiles
Create Date: 2026-09-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_web_auth_sessions"
down_revision: str | Sequence[str] | None = "0004_runtime_execution_profiles"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "web_admin_credentials",
        sa.Column("admin_key", sa.String(11), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("password_updated_at", sa.String(27), nullable=False),
        sa.CheckConstraint(
            "admin_key = 'local-admin'", name="web_admin_single_identity"
        ),
        sa.CheckConstraint(
            "length(trim(password_hash)) > 0", name="web_admin_password_hash_not_blank"
        ),
        sa.CheckConstraint(
            "length(password_updated_at) = 27",
            name="web_admin_password_updated_at_format",
        ),
        sa.PrimaryKeyConstraint("admin_key"),
    )
    op.create_table(
        "web_sessions",
        sa.Column("token_digest", sa.String(64), nullable=False),
        sa.Column("admin_key", sa.String(11), nullable=False),
        sa.Column("csrf_token", sa.String(43), nullable=False),
        sa.Column("created_at", sa.String(27), nullable=False),
        sa.Column("last_used_at", sa.String(27), nullable=False),
        sa.Column("absolute_expires_at", sa.String(27), nullable=False),
        sa.CheckConstraint(
            "length(token_digest) = 64 AND token_digest NOT GLOB '*[^0-9a-f]*'",
            name="web_session_token_digest_sha256",
        ),
        sa.CheckConstraint(
            "admin_key = 'local-admin'", name="web_session_single_admin"
        ),
        sa.CheckConstraint(
            "length(csrf_token) = 43", name="web_session_csrf_token_length"
        ),
        sa.CheckConstraint(
            "length(created_at) = 27 AND length(last_used_at) = 27 "
            "AND length(absolute_expires_at) = 27",
            name="web_session_timestamp_format",
        ),
        sa.ForeignKeyConstraint(
            ["admin_key"], ["web_admin_credentials.admin_key"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("token_digest"),
    )


def downgrade() -> None:
    connection = op.get_bind()
    session_count = connection.scalar(sa.text("SELECT COUNT(*) FROM web_sessions"))
    credential_count = connection.scalar(
        sa.text("SELECT COUNT(*) FROM web_admin_credentials")
    )
    if session_count or credential_count:
        raise RuntimeError("Cannot downgrade populated Web authentication state.")
    op.drop_table("web_sessions")
    op.drop_table("web_admin_credentials")
