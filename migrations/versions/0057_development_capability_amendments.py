"""Durable external-development capability amendments and scoped screenshot guard.

Revision ID: 0057_development_capability_amendments
Revises: 0056_repository_workspace_deployment
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from kodi_lab.storage.development_capability_sql import (
    DEVELOPMENT_CAPABILITY_TRIGGERS,
    PRODUCTION_SCREENSHOT_TARGET_GUARD,
)

revision = "0057_development_capability_amendments"
down_revision = "0056_repository_workspace_deployment"
branch_labels = None
depends_on = None


def _rewrite_production_guard(*, add: bool) -> None:
    connection = op.get_bind()
    original = connection.scalar(
        sa.text(
            "SELECT sql FROM sqlite_master WHERE type='trigger' "
            "AND name='trg_production_screenshot_removal_safe'"
        )
    )
    if not isinstance(original, str):
        raise RuntimeError("production screenshot guard is missing")
    old_marker = "WHEN EXISTS(SELECT 1 FROM json_each(OLD.configured_capabilities_json)"
    new_marker = "WHEN " + PRODUCTION_SCREENSHOT_TARGET_GUARD + old_marker[5:]
    source, destination = (old_marker, new_marker) if add else (new_marker, old_marker)
    if original.count(source) != 1 or destination in original:
        raise RuntimeError("production screenshot guard has drifted")
    op.execute("DROP TRIGGER trg_production_screenshot_removal_safe")
    op.execute(original.replace(source, destination, 1))


def upgrade() -> None:
    op.create_table(
        "external_development_capability_amendments",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("target_id", sa.String(36), nullable=False),
        sa.Column("target_classification", sa.String(32), nullable=False),
        sa.Column("bridge_id", sa.String(36), nullable=False),
        sa.Column("bridge_addon_id", sa.String(128), nullable=False),
        sa.Column("bridge_version", sa.String(64), nullable=False),
        sa.Column("bridge_protocol", sa.String(16), nullable=False),
        sa.Column("bridge_fingerprint", sa.String(64), nullable=False),
        sa.Column("before_json", sa.String(512), nullable=False),
        sa.Column("requested_json", sa.String(512), nullable=False),
        sa.Column("resulting_json", sa.String(512), nullable=False),
        sa.Column("native_verified_json", sa.String(512), nullable=False),
        sa.Column("bridge_verified_json", sa.String(256), nullable=False),
        sa.Column("actor", sa.String(64), nullable=False),
        sa.Column("requested_at", sa.String(27), nullable=False),
        sa.Column("completed_at", sa.String(27), nullable=False),
        sa.ForeignKeyConstraint(["target_id"], ["targets.id"], ondelete="RESTRICT"),
        sa.CheckConstraint(
            "length(id)=36 AND length(target_id)=36 AND length(bridge_id)=36 "
            "AND target_classification='external_development' "
            "AND bridge_addon_id='service.kodi_lab_bridge' "
            "AND length(bridge_version) BETWEEN 1 AND 64 "
            "AND bridge_protocol='1' AND length(bridge_fingerprint)=64",
            name="development_capability_amendment_identity_bounded",
        ),
        sa.CheckConstraint(
            "length(before_json) BETWEEN 2 AND 512 AND "
            "length(requested_json) BETWEEN 2 AND 512 AND "
            "resulting_json=requested_json AND before_json<>requested_json AND "
            "length(native_verified_json) BETWEEN 2 AND 512 AND "
            "length(bridge_verified_json) BETWEEN 2 AND 256",
            name="development_capability_amendment_sets_bounded",
        ),
        sa.CheckConstraint(
            "length(actor) BETWEEN 1 AND 64 AND length(requested_at)=27 "
            "AND length(completed_at)=27 AND completed_at>=requested_at",
            name="development_capability_amendment_audit_bounded",
        ),
    )
    op.create_index(
        "ix_development_capability_amendment_target_time",
        "external_development_capability_amendments",
        ["target_id", "completed_at", "id"],
    )
    for statement in DEVELOPMENT_CAPABILITY_TRIGGERS:
        op.execute(statement)
    _rewrite_production_guard(add=True)


def downgrade() -> None:
    connection = op.get_bind()
    if connection.scalar(
        sa.text(
            "SELECT EXISTS(SELECT 1 FROM external_development_capability_amendments)"
        )
    ):
        raise RuntimeError(
            "refusing to discard development capability amendment evidence"
        )
    _rewrite_production_guard(add=False)
    for name in (
        "trg_development_configured_capability_amendment_required",
        "trg_development_capability_amendment_no_delete",
        "trg_development_capability_amendment_immutable",
        "trg_development_capability_amendment_insert",
    ):
        op.execute(f"DROP TRIGGER {name}")
    op.drop_index(
        "ix_development_capability_amendment_target_time",
        table_name="external_development_capability_amendments",
    )
    op.drop_table("external_development_capability_amendments")
