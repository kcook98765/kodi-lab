"""Bind every material production transition to one policy revision.

Revision ID: 0052_production_policy_revision_binding
Revises: 0051_production_transient_approvals
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0052_production_policy_revision_binding"
down_revision = "0051_production_transient_approvals"
branch_labels = None
depends_on = None


_MATERIAL = (
    "NEW.administrative_state<>OLD.administrative_state OR "
    "NEW.protocol_kind<>OLD.protocol_kind OR NEW.host<>OLD.host OR "
    "NEW.port<>OLD.port OR NEW.authentication_mode<>OLD.authentication_mode OR "
    "NEW.secret_reference IS NOT OLD.secret_reference OR "
    "NEW.configured_capabilities_json<>OLD.configured_capabilities_json OR "
    "NEW.verified_capabilities_json<>OLD.verified_capabilities_json OR "
    "NEW.verification_state<>OLD.verification_state OR "
    "NEW.verified_addresses_json<>OLD.verified_addresses_json OR "
    "NEW.verified_kodi_name IS NOT OLD.verified_kodi_name OR "
    "NEW.verified_kodi_version IS NOT OLD.verified_kodi_version OR "
    "NEW.observed_kodi_name IS NOT OLD.observed_kodi_name OR "
    "NEW.observed_kodi_version IS NOT OLD.observed_kodi_version OR "
    "NEW.observed_protocol_version IS NOT OLD.observed_protocol_version OR "
    "NEW.pending_observation_id IS NOT OLD.pending_observation_id"
)
_NATIVE_MATERIAL = _MATERIAL.replace(
    "NEW.administrative_state<>OLD.administrative_state OR ", ""
)


def _create_revision_guards() -> None:
    op.execute(
        "CREATE TRIGGER trg_external_production_policy_revision_monotonic "
        "BEFORE UPDATE OF policy_revision ON external_production_policies WHEN "
        "NEW.policy_revision<OLD.policy_revision BEGIN SELECT RAISE(ABORT, "
        "'production_policy_revision_not_monotonic'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_external_production_policy_material_revision BEFORE "
        "UPDATE ON external_production_policies WHEN (NEW.designation_state<>"
        "OLD.designation_state OR NEW.confirmed_observation_id IS NOT "
        "OLD.confirmed_observation_id OR NEW.confirmed_observation_digest IS NOT "
        "OLD.confirmed_observation_digest OR NEW.identity_generation<>"
        "OLD.identity_generation OR NEW.mutations_suspended<>OLD.mutations_suspended "
        "OR NEW.updated_at<>OLD.updated_at) AND "
        "NEW.policy_revision<>OLD.policy_revision+1 BEGIN SELECT RAISE(ABORT, "
        "'production_policy_revision_required'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_external_production_policy_guard_insert AFTER INSERT ON "
        "external_production_policies BEGIN INSERT INTO "
        "external_production_policy_revision_guards "
        "(target_id,maximum_policy_revision) VALUES "
        "(NEW.target_id,NEW.policy_revision); END"
    )
    op.execute(
        "CREATE TRIGGER trg_external_production_policy_guard_advance AFTER UPDATE OF "
        "policy_revision ON external_production_policies WHEN "
        "NEW.policy_revision>OLD.policy_revision BEGIN UPDATE "
        "external_production_policy_revision_guards SET maximum_policy_revision="
        "NEW.policy_revision WHERE target_id=NEW.target_id; END"
    )
    op.execute(
        "CREATE TRIGGER trg_external_production_policy_guard_immutable BEFORE UPDATE "
        "ON external_production_policy_revision_guards WHEN NEW.target_id<>"
        "OLD.target_id OR NEW.maximum_policy_revision<=OLD.maximum_policy_revision "
        "BEGIN SELECT RAISE(ABORT, 'production_policy_revision_not_monotonic'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_external_production_policy_guard_no_delete BEFORE DELETE "
        "ON external_production_policy_revision_guards BEGIN SELECT RAISE(ABORT, "
        "'production_policy_revision_not_monotonic'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_external_production_revision_binding_insert BEFORE "
        "INSERT ON external_kodi_targets WHEN (COALESCE((SELECT target_type FROM "
        "targets WHERE id=NEW.target_id),'')='external_production') <> "
        "(NEW.production_policy_revision IS NOT NULL) BEGIN SELECT RAISE(ABORT, "
        "'production_policy_revision_required'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_external_production_revision_binding_material BEFORE "
        "UPDATE ON external_kodi_targets WHEN COALESCE((SELECT target_type FROM "
        "targets WHERE id=NEW.target_id),'')='external_production' AND "
        f"({_MATERIAL}) AND (NEW.production_policy_revision IS NULL OR "
        "NEW.production_policy_revision<=COALESCE(OLD.production_policy_revision,0) "
        "OR NEW.production_policy_revision<>COALESCE((SELECT policy_revision FROM "
        "external_production_policies WHERE target_id=NEW.target_id),0)) BEGIN "
        "SELECT RAISE(ABORT, 'production_policy_revision_required'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_external_production_revision_binding_immutable BEFORE "
        "UPDATE ON external_kodi_targets WHEN (NEW.production_policy_revision IS NOT "
        f"OLD.production_policy_revision AND NOT ({_MATERIAL})) OR "
        "(COALESCE((SELECT target_type FROM targets WHERE id=NEW.target_id),'')<>"
        "'external_production' AND NEW.production_policy_revision IS NOT NULL) BEGIN "
        "SELECT RAISE(ABORT, 'production_policy_revision_required'); END"
    )


def upgrade() -> None:
    op.add_column(
        "external_kodi_targets",
        sa.Column(
            "production_policy_revision",
            sa.Integer(),
            sa.CheckConstraint(
                "production_policy_revision IS NULL OR production_policy_revision>=1",
                name="external_target_production_revision_positive",
            ),
            nullable=True,
        ),
    )
    op.execute(
        "UPDATE external_kodi_targets SET production_policy_revision="
        "(SELECT policy_revision FROM external_production_policies p WHERE "
        "p.target_id=external_kodi_targets.target_id) WHERE target_id IN "
        "(SELECT target_id FROM external_production_policies)"
    )
    op.create_table(
        "external_production_policy_revision_guards",
        sa.Column("target_id", sa.String(36), nullable=False),
        sa.Column("maximum_policy_revision", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "maximum_policy_revision>=1",
            name="external_production_policy_revision_guard_positive",
        ),
        sa.ForeignKeyConstraint(["target_id"], ["targets.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("target_id"),
    )
    op.execute(
        "INSERT INTO external_production_policy_revision_guards "
        "(target_id,maximum_policy_revision) SELECT target_id,policy_revision FROM "
        "external_production_policies"
    )
    for trigger in (
        "trg_external_production_policy_material_revision",
        "trg_external_production_policy_revision_monotonic",
        "trg_external_production_administrative_revision",
        "trg_external_production_native_material_revision",
    ):
        op.execute(f"DROP TRIGGER {trigger}")
    _create_revision_guards()


def downgrade() -> None:
    connection = op.get_bind()
    if connection.scalar(
        sa.text(
            "SELECT EXISTS(SELECT 1 FROM external_production_policy_revision_guards)"
        )
    ):
        raise RuntimeError(
            "refusing destructive downgrade while production policy revision "
            "history exists"
        )
    for trigger in (
        "trg_external_production_revision_binding_immutable",
        "trg_external_production_revision_binding_material",
        "trg_external_production_revision_binding_insert",
        "trg_external_production_policy_guard_no_delete",
        "trg_external_production_policy_guard_immutable",
        "trg_external_production_policy_guard_advance",
        "trg_external_production_policy_guard_insert",
        "trg_external_production_policy_material_revision",
        "trg_external_production_policy_revision_monotonic",
    ):
        op.execute(f"DROP TRIGGER {trigger}")
    op.drop_table("external_production_policy_revision_guards")
    op.drop_column("external_kodi_targets", "production_policy_revision")
    op.execute(
        "CREATE TRIGGER trg_external_production_policy_revision_monotonic BEFORE "
        "UPDATE OF policy_revision ON external_production_policies WHEN "
        "NEW.policy_revision<OLD.policy_revision BEGIN SELECT RAISE(ABORT, "
        "'production_policy_revision_not_monotonic'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_external_production_policy_material_revision BEFORE "
        "UPDATE ON external_production_policies WHEN (NEW.designation_state<>"
        "OLD.designation_state OR NEW.confirmed_observation_id IS NOT "
        "OLD.confirmed_observation_id OR NEW.confirmed_observation_digest IS NOT "
        "OLD.confirmed_observation_digest OR NEW.identity_generation<>"
        "OLD.identity_generation OR NEW.mutations_suspended<>OLD.mutations_suspended) "
        "AND NEW.policy_revision<=OLD.policy_revision BEGIN SELECT RAISE(ABORT, "
        "'production_policy_revision_required'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_external_production_administrative_revision AFTER UPDATE "
        "OF administrative_state ON external_kodi_targets WHEN "
        "NEW.administrative_state<>OLD.administrative_state AND COALESCE((SELECT "
        "target_type FROM targets WHERE id=NEW.target_id),'')='external_production' "
        "BEGIN UPDATE external_production_policies SET policy_revision="
        "policy_revision+1 WHERE target_id=NEW.target_id; END"
    )
    op.execute(
        "CREATE TRIGGER trg_external_production_native_material_revision AFTER "
        "UPDATE ON external_kodi_targets WHEN COALESCE((SELECT target_type FROM "
        "targets WHERE id=NEW.target_id),'')='external_production' AND "
        f"({_NATIVE_MATERIAL}) "
        "BEGIN UPDATE external_production_policies SET policy_revision="
        "policy_revision+1 WHERE target_id=NEW.target_id; END"
    )
