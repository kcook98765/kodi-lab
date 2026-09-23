"""Add screenshot-only production capability amendment durability.

Revision ID: 0053_production_screenshot_capability_amendment
Revises: 0052_production_policy_revision_binding
"""

# Trigger SQL is kept as exact adjacent fragments for migration review.
# ruff: noqa: E501

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0053_production_screenshot_capability_amendment"
down_revision = "0052_production_policy_revision_binding"
branch_labels = None
depends_on = None


def _create_guards() -> None:
    op.execute(
        "CREATE TRIGGER trg_production_capability_amendment_insert BEFORE INSERT ON "
        "production_capability_amendments WHEN "
        "COALESCE((SELECT target_type FROM targets WHERE id=NEW.target_id),'')<>"
        "'external_production' OR NOT json_valid(NEW.before_configured_json) OR NOT "
        "json_valid(NEW.after_configured_json) OR json_type(NEW.before_configured_json)"
        "<>'array' OR json_type(NEW.after_configured_json)<>'array' OR EXISTS(SELECT 1 "
        "FROM json_each(NEW.before_configured_json) WHERE type<>'text' OR value NOT IN "
        "('target_inspect','addon_inventory','semantic_remote','screenshot')) OR "
        "EXISTS(SELECT 1 FROM json_each(NEW.after_configured_json) WHERE type<>'text' "
        "OR value NOT IN ('target_inspect','addon_inventory','semantic_remote',"
        "'screenshot')) OR (SELECT count(*) FROM json_each(NEW.before_configured_json))"
        "<>(SELECT count(DISTINCT value) FROM json_each(NEW.before_configured_json)) OR "
        "(SELECT count(*) FROM json_each(NEW.after_configured_json))<>(SELECT "
        "count(DISTINCT value) FROM json_each(NEW.after_configured_json)) OR "
        "COALESCE((SELECT configured_capabilities_json FROM external_kodi_targets WHERE "
        "target_id=NEW.target_id),'')<>NEW.before_configured_json OR COALESCE((SELECT "
        "policy_revision FROM external_production_policies WHERE target_id=NEW.target_id),"
        "0)<>NEW.resulting_policy_revision OR NOT (((NEW.operation='add') AND NOT "
        "EXISTS(SELECT 1 FROM json_each(NEW.before_configured_json) WHERE "
        "value='screenshot') AND EXISTS(SELECT 1 FROM "
        "json_each(NEW.after_configured_json) WHERE value='screenshot') AND "
        "json_array_length(NEW.after_configured_json)=json_array_length("
        "NEW.before_configured_json)+1 AND NOT EXISTS(SELECT 1 FROM "
        "json_each(NEW.before_configured_json) b WHERE NOT EXISTS(SELECT 1 FROM "
        "json_each(NEW.after_configured_json) a WHERE a.value=b.value)) AND NOT "
        "EXISTS(SELECT 1 FROM json_each(NEW.after_configured_json) a WHERE "
        "a.value<>'screenshot' AND NOT EXISTS(SELECT 1 FROM "
        "json_each(NEW.before_configured_json) b WHERE b.value=a.value))) OR "
        "((NEW.operation='remove') AND EXISTS(SELECT 1 FROM "
        "json_each(NEW.before_configured_json) WHERE value='screenshot') AND NOT "
        "EXISTS(SELECT 1 FROM json_each(NEW.after_configured_json) WHERE "
        "value='screenshot') AND json_array_length(NEW.after_configured_json)+1="
        "json_array_length(NEW.before_configured_json) AND NOT EXISTS(SELECT 1 FROM "
        "json_each(NEW.after_configured_json) a WHERE NOT EXISTS(SELECT 1 FROM "
        "json_each(NEW.before_configured_json) b WHERE b.value=a.value)) AND NOT "
        "EXISTS(SELECT 1 FROM json_each(NEW.before_configured_json) b WHERE "
        "b.value<>'screenshot' AND NOT EXISTS(SELECT 1 FROM "
        "json_each(NEW.after_configured_json) a WHERE a.value=b.value)))) BEGIN SELECT "
        "RAISE(ABORT, 'production_capability_amendment_invalid'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_production_capability_amendment_immutable BEFORE UPDATE ON "
        "production_capability_amendments BEGIN SELECT RAISE(ABORT, "
        "'production_capability_amendment_immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_production_capability_amendment_no_delete BEFORE DELETE ON "
        "production_capability_amendments BEGIN SELECT RAISE(ABORT, "
        "'production_capability_amendment_immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_production_configured_capability_amendment_required "
        "BEFORE UPDATE OF configured_capabilities_json ON external_kodi_targets WHEN "
        "NEW.configured_capabilities_json<>OLD.configured_capabilities_json AND "
        "COALESCE((SELECT target_type FROM targets WHERE id=NEW.target_id),'')="
        "'external_production' AND NOT EXISTS(SELECT 1 FROM "
        "production_capability_amendments a WHERE a.target_id=NEW.target_id AND "
        "a.before_configured_json=OLD.configured_capabilities_json AND "
        "a.after_configured_json=NEW.configured_capabilities_json AND "
        "a.resulting_policy_revision=NEW.production_policy_revision) BEGIN SELECT "
        "RAISE(ABORT, 'production_capability_amendment_required'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_production_screenshot_removal_safe BEFORE UPDATE OF "
        "configured_capabilities_json ON external_kodi_targets WHEN "
        "EXISTS(SELECT 1 FROM json_each(OLD.configured_capabilities_json) WHERE "
        "value='screenshot') AND NOT EXISTS(SELECT 1 FROM "
        "json_each(NEW.configured_capabilities_json) WHERE value='screenshot') AND "
        "(NOT EXISTS(SELECT 1 FROM production_capability_amendments a WHERE "
        "a.target_id=NEW.target_id AND a.operation='remove' AND "
        "a.before_configured_json=OLD.configured_capabilities_json AND "
        "a.after_configured_json=NEW.configured_capabilities_json AND "
        "a.resulting_policy_revision=NEW.production_policy_revision) OR EXISTS(SELECT 1 "
        "FROM external_bridge_pairings p WHERE p.target_id=NEW.target_id AND "
        "(p.pairing_state<>'revoked' OR p.pairing_token_reference IS NOT NULL OR "
        "p.bridge_secret_reference IS NOT NULL OR p.verified_capabilities_json<>'[]' OR "
        "p.production_bridge_trust_revision IS NULL OR "
        "p.production_identity_generation IS NOT NULL OR p.reviewed_build_id IS NOT "
        "NULL OR p.production_confirmed_by IS NOT NULL OR p.production_confirmed_at "
        "IS NOT NULL)) OR EXISTS(SELECT 1 FROM mcp_production_target_scopes s WHERE "
        "s.target_id=NEW.target_id AND s.scope IN "
        "('visual','navigate','request_transient_control') AND s.state='active') OR "
        "EXISTS(SELECT 1 FROM production_operation_approvals a WHERE "
        "a.target_id=NEW.target_id AND a.state IN ('pending','approved'))) BEGIN SELECT "
        "RAISE(ABORT, 'production_screenshot_removal_unsafe'); END"
    )


def upgrade() -> None:
    op.create_table(
        "production_capability_amendments",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("target_id", sa.String(36), nullable=False),
        sa.Column("operation", sa.String(8), nullable=False),
        sa.Column("capability", sa.String(32), nullable=False),
        sa.Column("event_kind", sa.String(48), nullable=False),
        sa.Column("before_configured_json", sa.String(512), nullable=False),
        sa.Column("after_configured_json", sa.String(512), nullable=False),
        sa.Column("resulting_policy_revision", sa.Integer(), nullable=False),
        sa.Column("actor", sa.String(64), nullable=False),
        sa.Column("occurred_at", sa.String(27), nullable=False),
        sa.CheckConstraint(
            "length(id)=36 AND operation IN ('add','remove') AND "
            "capability='screenshot' AND ((operation='add' AND "
            "event_kind='production_capability_screenshot_added') OR "
            "(operation='remove' AND "
            "event_kind='production_capability_screenshot_removed'))",
            name="production_capability_amendment_semantics_closed",
        ),
        sa.CheckConstraint(
            "length(before_configured_json) BETWEEN 2 AND 512 AND "
            "length(after_configured_json) BETWEEN 2 AND 512",
            name="production_capability_amendment_sets_bounded",
        ),
        sa.CheckConstraint(
            "resulting_policy_revision>=1 AND length(actor) BETWEEN 1 AND 64 "
            "AND length(occurred_at)=27",
            name="production_capability_amendment_audit_bounded",
        ),
        sa.ForeignKeyConstraint(["target_id"], ["targets.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "target_id",
            "resulting_policy_revision",
            name="uq_production_capability_amendment_revision",
        ),
    )
    op.create_index(
        "ix_production_capability_amendment_target_time",
        "production_capability_amendments",
        ["target_id", "occurred_at", "id"],
    )
    _create_guards()


def downgrade() -> None:
    connection = op.get_bind()
    if connection.scalar(
        sa.text("SELECT EXISTS(SELECT 1 FROM production_capability_amendments)")
    ):
        raise RuntimeError(
            "refusing destructive downgrade while production capability amendment "
            "or tombstone history exists"
        )
    for trigger in (
        "trg_production_screenshot_removal_safe",
        "trg_production_configured_capability_amendment_required",
        "trg_production_capability_amendment_no_delete",
        "trg_production_capability_amendment_immutable",
        "trg_production_capability_amendment_insert",
    ):
        op.execute(f"DROP TRIGGER {trigger}")
    op.drop_index(
        "ix_production_capability_amendment_target_time",
        table_name="production_capability_amendments",
    )
    op.drop_table("production_capability_amendments")
