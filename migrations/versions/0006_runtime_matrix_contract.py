"""Add the typed runtime matrix support and validation contract.

Revision ID: 0006_runtime_matrix_contract
Revises: 0005_web_auth_sessions
Create Date: 2026-09-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_runtime_matrix_contract"
down_revision: str | Sequence[str] | None = "0005_web_auth_sessions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "runtime_definitions",
        sa.Column(
            "support_tier",
            sa.String(16),
            server_default=sa.text("'current'"),
            nullable=False,
        ),
    )
    with op.batch_alter_table("runtime_definitions", recreate="always") as batch_op:
        batch_op.create_check_constraint(
            "runtime_support_tier",
            "support_tier IN ('current', 'legacy', 'preview', 'retired')",
        )

    with op.batch_alter_table(
        "runtime_execution_profiles", recreate="always"
    ) as batch_op:
        for constraint in (
            "runtime_profile_kodi_version",
            "runtime_profile_version_tag",
            "runtime_profile_jsonrpc_port",
            "runtime_profile_jsonrpc_path",
            "runtime_profile_total_timeout",
            "runtime_profile_poll_interval",
            "runtime_profile_http_timeout",
        ):
            batch_op.drop_constraint(constraint, type_="check")
        batch_op.add_column(
            sa.Column("expected_prerelease_ordinal", sa.Integer(), nullable=True)
        )
        batch_op.create_check_constraint(
            "runtime_profile_release_channel",
            "expected_version_tag IN ('stable', 'alpha', 'beta', 'rc')",
        )
        batch_op.create_check_constraint(
            "runtime_profile_release_ordinal",
            "(expected_version_tag = 'stable' AND "
            "expected_prerelease_ordinal IS NULL) OR "
            "(expected_version_tag != 'stable' AND "
            "expected_prerelease_ordinal > 0)",
        )
        batch_op.create_check_constraint(
            "runtime_profile_jsonrpc_port_range", "jsonrpc_port BETWEEN 1 AND 65535"
        )
        batch_op.create_check_constraint(
            "runtime_profile_total_timeout_range",
            "readiness_timeout_seconds BETWEEN 1 AND 600",
        )
        batch_op.create_check_constraint(
            "runtime_profile_poll_interval_range",
            "poll_interval_seconds >= 0.1 AND poll_interval_seconds <= 10.0",
        )
        batch_op.create_check_constraint(
            "runtime_profile_http_timeout_range",
            "http_timeout_seconds >= 0.1 AND http_timeout_seconds <= 5.0",
        )

    op.create_table(
        "runtime_validation_evidence",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("runtime_revision_id", sa.String(36), nullable=False),
        sa.Column("contract_version", sa.String(64), nullable=False),
        sa.Column(
            "result",
            sa.Enum(
                "passed",
                "failed",
                name="runtime_validation_result",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("started_at", sa.String(27), nullable=False),
        sa.Column("completed_at", sa.String(27), nullable=False),
        sa.Column("evidence_reference", sa.String(512), nullable=False),
        sa.Column("summary", sa.String(512), nullable=True),
        sa.Column("recorded_at", sa.String(27), nullable=False),
        sa.CheckConstraint(
            "length(contract_version) BETWEEN 1 AND 64",
            name="runtime_validation_contract_bounded",
        ),
        sa.CheckConstraint(
            "length(trim(evidence_reference)) BETWEEN 1 AND 512",
            name="runtime_validation_reference_bounded",
        ),
        sa.CheckConstraint(
            "summary IS NULL OR length(trim(summary)) BETWEEN 1 AND 512",
            name="runtime_validation_summary_bounded",
        ),
        sa.CheckConstraint(
            "completed_at >= started_at AND recorded_at >= completed_at",
            name="runtime_validation_timestamp_order",
        ),
        sa.ForeignKeyConstraint(
            ["runtime_revision_id"], ["runtime_revisions.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_runtime_validation_evidence_runtime_revision_id",
        "runtime_validation_evidence",
        ["runtime_revision_id"],
    )
    op.create_index(
        "ix_runtime_validation_effective",
        "runtime_validation_evidence",
        ["runtime_revision_id", "contract_version", "recorded_at"],
    )


def downgrade() -> None:
    connection = op.get_bind()
    evidence_count = connection.scalar(
        sa.text("SELECT COUNT(*) FROM runtime_validation_evidence")
    )
    if evidence_count:
        raise RuntimeError(
            "Cannot downgrade populated runtime validation evidence history."
        )
    unsupported_tiers = connection.scalar(
        sa.text(
            "SELECT COUNT(*) FROM runtime_definitions WHERE support_tier != 'current'"
        )
    )
    if unsupported_tiers:
        raise RuntimeError("Cannot downgrade non-current runtime support policy.")
    generalized_profiles = connection.scalar(
        sa.text(
            "SELECT COUNT(*) FROM runtime_execution_profiles "
            "WHERE expected_kodi_version != '21.3' "
            "OR expected_version_tag != 'stable' "
            "OR expected_prerelease_ordinal IS NOT NULL "
            "OR jsonrpc_port != 8080 OR jsonrpc_path != '/jsonrpc' "
            "OR readiness_timeout_seconds != 90 "
            "OR poll_interval_seconds != 1.0"
        )
    )
    if generalized_profiles:
        raise RuntimeError("Cannot downgrade generalized runtime execution profiles.")

    op.drop_index(
        "ix_runtime_validation_effective",
        table_name="runtime_validation_evidence",
    )
    op.drop_index(
        "ix_runtime_validation_evidence_runtime_revision_id",
        table_name="runtime_validation_evidence",
    )
    op.drop_table("runtime_validation_evidence")

    with op.batch_alter_table(
        "runtime_execution_profiles", recreate="always"
    ) as batch_op:
        for constraint in (
            "runtime_profile_release_channel",
            "runtime_profile_release_ordinal",
            "runtime_profile_jsonrpc_port_range",
            "runtime_profile_total_timeout_range",
            "runtime_profile_poll_interval_range",
            "runtime_profile_http_timeout_range",
        ):
            batch_op.drop_constraint(constraint, type_="check")
        batch_op.drop_column("expected_prerelease_ordinal")
        batch_op.create_check_constraint(
            "runtime_profile_kodi_version", "expected_kodi_version = '21.3'"
        )
        batch_op.create_check_constraint(
            "runtime_profile_version_tag", "expected_version_tag = 'stable'"
        )
        batch_op.create_check_constraint(
            "runtime_profile_jsonrpc_port", "jsonrpc_port = 8080"
        )
        batch_op.create_check_constraint(
            "runtime_profile_jsonrpc_path", "jsonrpc_path = '/jsonrpc'"
        )
        batch_op.create_check_constraint(
            "runtime_profile_total_timeout", "readiness_timeout_seconds = 90"
        )
        batch_op.create_check_constraint(
            "runtime_profile_poll_interval", "poll_interval_seconds = 1.0"
        )
        batch_op.create_check_constraint(
            "runtime_profile_http_timeout",
            "http_timeout_seconds >= 0.1 AND http_timeout_seconds <= 5.0",
        )

    with op.batch_alter_table("runtime_definitions", recreate="always") as batch_op:
        batch_op.drop_constraint("runtime_support_tier", type_="check")
        batch_op.drop_column("support_tier")
