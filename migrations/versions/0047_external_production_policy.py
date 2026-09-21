"""Add the external-production policy foundation.

Revision ID: 0047_external_production_policy
Revises: 0046_external_bridge_development
"""

# Closed SQLite constraints are intentionally explicit and auditable.
# ruff: noqa: E501

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0047_external_production_policy"
down_revision: str | Sequence[str] | None = "0046_external_bridge_development"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("targets", recreate="always") as batch:
        batch.drop_constraint("target_type", type_="check")
        batch.drop_constraint("target_kodi_version_by_type", type_="check")
        batch.alter_column(
            "target_type",
            existing_type=sa.String(20),
            type_=sa.Enum(
                "managed",
                "external_development",
                "external_production",
                name="target_type",
                native_enum=False,
                create_constraint=True,
            ),
            existing_nullable=False,
        )
        batch.create_check_constraint(
            "target_kodi_version_by_type",
            "(target_type='managed' AND kodi_version IS NOT NULL AND length(trim(kodi_version))>0) OR (target_type IN ('external_development','external_production') AND (kodi_version IS NULL OR length(trim(kodi_version))>0))",
        )

    op.execute(
        "CREATE TRIGGER trg_targets_type_immutable "
        "BEFORE UPDATE OF target_type ON targets "
        "WHEN NEW.target_type <> OLD.target_type "
        "BEGIN SELECT RAISE(ABORT, 'target_type_immutable'); END"
    )

    op.create_table(
        "external_production_policies",
        sa.Column("target_id", sa.String(36), nullable=False),
        sa.Column(
            "designation_state",
            sa.Enum(
                "pending_native_identity",
                "pending_confirmation",
                "confirmed",
                "invalidated",
                "deactivated",
                name="production_designation_state",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("confirmed_observation_id", sa.String(36)),
        sa.Column("confirmed_observation_digest", sa.String(64)),
        sa.Column("identity_generation", sa.Integer(), nullable=False),
        sa.Column("policy_revision", sa.Integer(), nullable=False),
        sa.Column("mutations_suspended", sa.Boolean(), nullable=False),
        sa.Column("designation_confirmed_by", sa.String(64)),
        sa.Column("designation_confirmed_at", sa.String(27)),
        sa.Column("designation_invalidated_at", sa.String(27)),
        sa.Column("created_at", sa.String(27), nullable=False),
        sa.Column("updated_at", sa.String(27), nullable=False),
        sa.CheckConstraint(
            "identity_generation>=1 AND policy_revision>=1",
            name="external_production_generations_positive",
        ),
        sa.CheckConstraint(
            "confirmed_observation_digest IS NULL OR (length(confirmed_observation_digest)=64 AND confirmed_observation_digest NOT GLOB '*[^0-9a-f]*')",
            name="external_production_observation_digest_sha256",
        ),
        sa.CheckConstraint(
            "designation_state<>'confirmed' OR (confirmed_observation_id IS NOT NULL AND confirmed_observation_digest IS NOT NULL AND designation_confirmed_by IS NOT NULL AND designation_confirmed_at IS NOT NULL)",
            name="external_production_confirmation_complete",
        ),
        sa.CheckConstraint(
            "length(created_at)=27 AND length(updated_at)=27 AND (designation_confirmed_at IS NULL OR length(designation_confirmed_at)=27) AND (designation_invalidated_at IS NULL OR length(designation_invalidated_at)=27)",
            name="external_production_timestamps_format",
        ),
        sa.CheckConstraint(
            "designation_confirmed_by IS NULL OR length(designation_confirmed_by) BETWEEN 1 AND 64",
            name="external_production_administrator_bounded",
        ),
        sa.ForeignKeyConstraint(
            ["target_id"], ["external_kodi_targets.target_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["confirmed_observation_id"],
            ["external_kodi_target_observations.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("target_id"),
    )
    op.create_index(
        "ix_external_production_designation",
        "external_production_policies",
        ["designation_state"],
    )
    op.execute(
        "CREATE TRIGGER trg_external_production_policy_class_insert "
        "BEFORE INSERT ON external_production_policies "
        "WHEN COALESCE((SELECT target_type FROM targets WHERE id=NEW.target_id),'') <> 'external_production' "
        "BEGIN SELECT RAISE(ABORT, 'external_production_policy_class_required'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_external_production_policy_class_update "
        "BEFORE UPDATE OF target_id ON external_production_policies "
        "WHEN NEW.target_id <> OLD.target_id OR COALESCE((SELECT target_type FROM targets WHERE id=NEW.target_id),'') <> 'external_production' "
        "BEGIN SELECT RAISE(ABORT, 'external_production_policy_class_required'); END"
    )

    op.create_table(
        "production_mutation_control",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("production_mutations_blocked", sa.Boolean(), nullable=False),
        sa.Column("policy_epoch", sa.Integer(), nullable=False),
        sa.Column("updated_by", sa.String(64), nullable=False),
        sa.Column("updated_at", sa.String(27), nullable=False),
        sa.CheckConstraint("id=1", name="production_mutation_control_singleton"),
        sa.CheckConstraint("policy_epoch>=1", name="production_policy_epoch_positive"),
        sa.CheckConstraint(
            "length(updated_by) BETWEEN 1 AND 64 AND length(updated_at)=27",
            name="production_mutation_control_update_bounded",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.execute(
        "INSERT INTO production_mutation_control "
        "(id,production_mutations_blocked,policy_epoch,updated_by,updated_at) "
        "VALUES (1,1,1,'system',strftime('%Y-%m-%dT%H:%M:%f000Z','now'))"
    )

    with op.batch_alter_table(
        "external_kodi_target_events", recreate="always"
    ) as batch:
        batch.drop_constraint("external_target_event_kind_closed", type_="check")
        batch.create_check_constraint(
            "external_target_event_kind_closed",
            "event_kind IN ('registered','endpoint_changed','credential_rotated','probe_attempted','probe_succeeded','probe_failed','verification_confirmed','reverification_required','enabled','disabled','deactivated','production_target_registered','native_observation_obtained','production_native_identity_confirmed','production_designation_confirmed','production_designation_invalidated','production_mutations_suspended','production_mutations_resumed')",
        )


def downgrade() -> None:
    connection = op.get_bind()
    populated = connection.execute(
        sa.text(
            "SELECT EXISTS(SELECT 1 FROM targets WHERE target_type='external_production') "
            "OR EXISTS(SELECT 1 FROM mcp_security_events WHERE event_kind LIKE 'production_%') "
            "OR EXISTS(SELECT 1 FROM external_kodi_target_events WHERE event_kind LIKE 'production_%')"
        )
    ).scalar_one()
    if populated:
        raise RuntimeError(
            "refusing destructive downgrade while production target or policy history exists"
        )

    with op.batch_alter_table(
        "external_kodi_target_events", recreate="always"
    ) as batch:
        batch.drop_constraint("external_target_event_kind_closed", type_="check")
        batch.create_check_constraint(
            "external_target_event_kind_closed",
            "event_kind IN ('registered','endpoint_changed','credential_rotated','probe_attempted','probe_succeeded','probe_failed','verification_confirmed','reverification_required','enabled','disabled','deactivated')",
        )

    op.drop_table("production_mutation_control")
    op.execute("DROP TRIGGER trg_external_production_policy_class_update")
    op.execute("DROP TRIGGER trg_external_production_policy_class_insert")
    op.drop_index(
        "ix_external_production_designation",
        table_name="external_production_policies",
    )
    op.drop_table("external_production_policies")
    op.execute("DROP TRIGGER trg_targets_type_immutable")

    with op.batch_alter_table("targets", recreate="always") as batch:
        batch.drop_constraint("target_type", type_="check")
        batch.drop_constraint("target_kodi_version_by_type", type_="check")
        batch.alter_column(
            "target_type",
            existing_type=sa.String(20),
            type_=sa.Enum(
                "managed",
                "external_development",
                name="target_type",
                native_enum=False,
                create_constraint=True,
            ),
            existing_nullable=False,
        )
        batch.create_check_constraint(
            "target_kodi_version_by_type",
            "(target_type='managed' AND kodi_version IS NOT NULL AND length(trim(kodi_version))>0) OR (target_type='external_development' AND (kodi_version IS NULL OR length(trim(kodi_version))>0))",
        )
