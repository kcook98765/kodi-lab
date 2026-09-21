"""Add Web-managed external-development targets.

Revision ID: 0044_external_development_targets
Revises: 0043_mcp_promotion_request
"""

# Closed SQLite constraints are intentionally explicit and auditable.
# ruff: noqa: E501

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0044_external_development_targets"
down_revision: str | Sequence[str] | None = "0043_mcp_promotion_request"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NAMING = {
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
}


def _rebuild_mcp_target_foreign_keys(*, generic: bool) -> None:
    referred_table = "targets" if generic else "managed_instances"
    referred_column = "id" if generic else "target_id"
    current_referred_table = "managed_instances" if generic else "targets"
    for table in ("mcp_client_target_grants", "mcp_target_preset_assignments"):
        constraint = f"fk_{table}_target_id_{current_referred_table}"
        with op.batch_alter_table(
            table, recreate="always", naming_convention=_NAMING
        ) as batch:
            batch.drop_constraint(constraint, type_="foreignkey")
            batch.create_foreign_key(
                f"fk_{table}_target_id_{referred_table}",
                referred_table,
                ["target_id"],
                [referred_column],
                ondelete="CASCADE",
            )


def upgrade() -> None:
    with op.batch_alter_table("targets", recreate="always") as batch:
        batch.drop_constraint("target_type", type_="check")
        batch.drop_constraint("target_kodi_version_not_blank", type_="check")
        batch.alter_column(
            "target_type",
            existing_type=sa.String(7),
            type_=sa.Enum(
                "managed",
                "external_development",
                name="target_type",
                native_enum=False,
                create_constraint=True,
            ),
            existing_nullable=False,
        )
        batch.alter_column("kodi_version", existing_type=sa.Text(), nullable=True)
        batch.create_check_constraint(
            "target_kodi_version_by_type",
            "(target_type = 'managed' AND kodi_version IS NOT NULL AND length(trim(kodi_version)) > 0) OR (target_type = 'external_development' AND (kodi_version IS NULL OR length(trim(kodi_version)) > 0))",
        )

    _rebuild_mcp_target_foreign_keys(generic=True)

    op.create_table(
        "external_kodi_targets",
        sa.Column("target_id", sa.String(36), nullable=False),
        sa.Column(
            "protocol_kind",
            sa.Enum(
                "jsonrpc_http",
                "jsonrpc_https",
                name="external_protocol_kind",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("host", sa.String(64), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column(
            "authentication_mode",
            sa.Enum(
                "none",
                "username_password",
                name="external_authentication_mode",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("secret_reference", sa.String(36)),
        sa.Column("configured_capabilities_json", sa.String(512), nullable=False),
        sa.Column("verified_capabilities_json", sa.String(512), nullable=False),
        sa.Column(
            "administrative_state",
            sa.Enum(
                "disabled",
                "enabled",
                "deactivated",
                name="external_administrative_state",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "verification_state",
            sa.Enum(
                "unverified",
                "verified",
                "needs_reverification",
                "identity_mismatch",
                name="external_verification_state",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "observation_state",
            sa.Enum(
                "reachable",
                "unreachable",
                "authentication_failed",
                "capability_missing",
                name="external_observation_state",
                native_enum=False,
                create_constraint=True,
            ),
        ),
        sa.Column("verified_addresses_json", sa.String(512), nullable=False),
        sa.Column("verified_kodi_name", sa.String(64)),
        sa.Column("verified_kodi_version", sa.String(64)),
        sa.Column("observed_kodi_name", sa.String(64)),
        sa.Column("observed_kodi_version", sa.String(64)),
        sa.Column("observed_protocol_version", sa.String(32)),
        sa.Column("pending_observation_id", sa.String(36)),
        sa.Column("created_at", sa.String(27), nullable=False),
        sa.Column("verified_at", sa.String(27)),
        sa.Column("deactivated_at", sa.String(27)),
        sa.Column("last_observed_at", sa.String(27)),
        sa.CheckConstraint(
            "length(host) BETWEEN 2 AND 64 AND host=trim(host)",
            name="external_target_host_bounded",
        ),
        sa.CheckConstraint(
            "port BETWEEN 1 AND 65535", name="external_target_port_valid"
        ),
        sa.CheckConstraint(
            "(authentication_mode='none' AND secret_reference IS NULL) OR (authentication_mode='username_password' AND length(secret_reference)=36)",
            name="external_target_secret_shape",
        ),
        sa.CheckConstraint(
            "length(configured_capabilities_json) BETWEEN 2 AND 512 AND length(verified_capabilities_json) BETWEEN 2 AND 512",
            name="external_target_capabilities_bounded",
        ),
        sa.CheckConstraint(
            "length(verified_addresses_json) BETWEEN 2 AND 512",
            name="external_target_addresses_bounded",
        ),
        sa.CheckConstraint(
            "length(created_at)=27 AND (verified_at IS NULL OR length(verified_at)=27) AND (deactivated_at IS NULL OR length(deactivated_at)=27) AND (last_observed_at IS NULL OR length(last_observed_at)=27)",
            name="external_target_timestamps_format",
        ),
        sa.CheckConstraint(
            "pending_observation_id IS NULL OR length(pending_observation_id)=36",
            name="external_target_pending_observation_shape",
        ),
        sa.CheckConstraint(
            "(administrative_state='deactivated' AND deactivated_at IS NOT NULL AND secret_reference IS NULL) OR (administrative_state<>'deactivated' AND deactivated_at IS NULL)",
            name="external_target_deactivation_consistent",
        ),
        sa.ForeignKeyConstraint(["target_id"], ["targets.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("target_id"),
    )
    op.create_index(
        "ix_external_target_admin_verification",
        "external_kodi_targets",
        ["administrative_state", "verification_state"],
    )

    op.create_table(
        "external_kodi_target_observations",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("target_id", sa.String(36), nullable=False),
        sa.Column("outcome", sa.String(32), nullable=False),
        sa.Column("addresses_json", sa.String(512), nullable=False),
        sa.Column("kodi_name", sa.String(64)),
        sa.Column("kodi_version", sa.String(64)),
        sa.Column("protocol_version", sa.String(32)),
        sa.Column("verified_capabilities_json", sa.String(512), nullable=False),
        sa.Column("error_code", sa.String(64)),
        sa.Column("attempted_at", sa.String(27), nullable=False),
        sa.Column("completed_at", sa.String(27), nullable=False),
        sa.CheckConstraint(
            "outcome IN ('reachable','unreachable','authentication_failed','capability_missing')",
            name="external_observation_outcome_closed",
        ),
        sa.CheckConstraint(
            "error_code IS NULL OR error_code IN ('endpoint_not_allowed','target_unreachable','authentication_failed','invalid_kodi_response','unsupported_kodi_version','identity_mismatch','secret_unavailable')",
            name="external_observation_error_closed",
        ),
        sa.CheckConstraint(
            "length(addresses_json) BETWEEN 2 AND 512 AND length(verified_capabilities_json) BETWEEN 2 AND 512",
            name="external_observation_json_bounded",
        ),
        sa.CheckConstraint(
            "length(attempted_at)=27 AND length(completed_at)=27",
            name="external_observation_timestamps_format",
        ),
        sa.ForeignKeyConstraint(
            ["target_id"], ["external_kodi_targets.target_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_external_observation_target_time",
        "external_kodi_target_observations",
        ["target_id", "attempted_at", "id"],
    )
    op.create_table(
        "external_kodi_target_events",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("target_id", sa.String(36), nullable=False),
        sa.Column("event_kind", sa.String(64), nullable=False),
        sa.Column("classification", sa.String(64)),
        sa.Column("observation_id", sa.String(36)),
        sa.Column("occurred_at", sa.String(27), nullable=False),
        sa.CheckConstraint(
            "event_kind IN ('registered','endpoint_changed','credential_rotated','probe_attempted','probe_succeeded','probe_failed','verification_confirmed','reverification_required','enabled','disabled','deactivated')",
            name="external_target_event_kind_closed",
        ),
        sa.CheckConstraint(
            "classification IS NULL OR length(classification) BETWEEN 1 AND 64",
            name="external_target_event_classification_bounded",
        ),
        sa.CheckConstraint(
            "length(occurred_at)=27", name="external_target_event_timestamp_format"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_external_target_event_time",
        "external_kodi_target_events",
        ["target_id", "occurred_at", "id"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    for table in (
        "external_kodi_targets",
        "external_kodi_target_observations",
        "external_kodi_target_events",
    ):
        if bind.scalar(sa.text(f"SELECT COUNT(*) FROM {table}")):
            raise RuntimeError(
                "Cannot downgrade populated external-development target state."
            )
    external_grants = bind.scalar(
        sa.text(
            "SELECT COUNT(*) FROM mcp_client_target_grants g JOIN targets t ON t.id=g.target_id WHERE t.target_type<>'managed'"
        )
    )
    external_policies = bind.scalar(
        sa.text(
            "SELECT COUNT(*) FROM mcp_target_preset_assignments a JOIN targets t ON t.id=a.target_id WHERE t.target_type<>'managed'"
        )
    )
    if external_grants or external_policies:
        raise RuntimeError("Cannot downgrade generic external target authority state.")

    op.drop_index(
        "ix_external_target_event_time", table_name="external_kodi_target_events"
    )
    op.drop_table("external_kodi_target_events")
    op.drop_index(
        "ix_external_observation_target_time",
        table_name="external_kodi_target_observations",
    )
    op.drop_table("external_kodi_target_observations")
    op.drop_index(
        "ix_external_target_admin_verification", table_name="external_kodi_targets"
    )
    op.drop_table("external_kodi_targets")

    _rebuild_mcp_target_foreign_keys(generic=False)
    with op.batch_alter_table("targets", recreate="always") as batch:
        batch.drop_constraint("target_type", type_="check")
        batch.drop_constraint("target_kodi_version_by_type", type_="check")
        batch.alter_column(
            "target_type",
            existing_type=sa.String(32),
            type_=sa.Enum(
                "managed", name="target_type", native_enum=False, create_constraint=True
            ),
            existing_nullable=False,
        )
        batch.alter_column("kodi_version", existing_type=sa.Text(), nullable=False)
        batch.create_check_constraint(
            "target_kodi_version_not_blank", "length(trim(kodi_version)) > 0"
        )
