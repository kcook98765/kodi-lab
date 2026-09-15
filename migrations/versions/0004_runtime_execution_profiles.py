"""Add immutable trusted runtime execution profiles.

Revision ID: 0004_runtime_execution_profiles
Revises: 0003_runtime_container_artifacts
Create Date: 2026-09-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_runtime_execution_profiles"
down_revision: str | Sequence[str] | None = "0003_runtime_container_artifacts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "runtime_execution_profiles",
        sa.Column("runtime_revision_id", sa.String(36), nullable=False),
        sa.Column("runtime_kind", sa.String(4), nullable=False),
        sa.Column("expected_application_name", sa.String(32), nullable=False),
        sa.Column("expected_kodi_version", sa.String(32), nullable=False),
        sa.Column("expected_version_tag", sa.String(16), nullable=False),
        sa.Column("jsonrpc_port", sa.Integer(), nullable=False),
        sa.Column("jsonrpc_path", sa.String(64), nullable=False),
        sa.Column("process_observation", sa.String(18), nullable=False),
        sa.Column("readiness_probe", sa.String(12), nullable=False),
        sa.Column("readiness_timeout_seconds", sa.Integer(), nullable=False),
        sa.Column("poll_interval_seconds", sa.Float(), nullable=False),
        sa.Column("http_timeout_seconds", sa.Float(), nullable=False),
        sa.CheckConstraint("runtime_kind = 'kodi'", name="runtime_profile_kind"),
        sa.CheckConstraint(
            "expected_application_name = 'Kodi'", name="runtime_profile_application"
        ),
        sa.CheckConstraint(
            "expected_kodi_version = '21.3'", name="runtime_profile_kodi_version"
        ),
        sa.CheckConstraint(
            "expected_version_tag = 'stable'", name="runtime_profile_version_tag"
        ),
        sa.CheckConstraint("jsonrpc_port = 8080", name="runtime_profile_jsonrpc_port"),
        sa.CheckConstraint(
            "jsonrpc_path = '/jsonrpc'", name="runtime_profile_jsonrpc_path"
        ),
        sa.CheckConstraint(
            "process_observation = 'docker_healthcheck'",
            name="runtime_profile_process_observation",
        ),
        sa.CheckConstraint(
            "readiness_probe = 'kodi_jsonrpc'", name="runtime_profile_readiness_probe"
        ),
        sa.CheckConstraint(
            "readiness_timeout_seconds = 90", name="runtime_profile_total_timeout"
        ),
        sa.CheckConstraint(
            "poll_interval_seconds = 1.0", name="runtime_profile_poll_interval"
        ),
        sa.CheckConstraint(
            "http_timeout_seconds >= 0.1 AND http_timeout_seconds <= 5.0",
            name="runtime_profile_http_timeout",
        ),
        sa.ForeignKeyConstraint(
            ["runtime_revision_id"], ["runtime_revisions.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("runtime_revision_id"),
    )


def downgrade() -> None:
    count = op.get_bind().scalar(
        sa.text("SELECT COUNT(*) FROM runtime_execution_profiles")
    )
    if count:
        raise RuntimeError(
            "Cannot downgrade a populated runtime execution profile mapping."
        )
    op.drop_table("runtime_execution_profiles")
