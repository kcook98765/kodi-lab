"""Extend closed Bridge capability sets for read-only UI observation.

Revision ID: 0068_ui_observation_capability
Revises: 0067_managed_runtime_reconstruction
"""

import sqlalchemy as sa
from alembic import op

revision = "0068_ui_observation_capability"
down_revision = "0067_managed_runtime_reconstruction"
branch_labels = None
depends_on = None

_OLD = (
    "('[]','[\"screenshot\"]','[\"workspace_deployment\"]',"
    '\'["screenshot","workspace_deployment"]\')'
)
_NEW = (
    _OLD[:-1]
    + ","
    + ",".join(
        "'" + value + "'"
        for value in (
            '["ui_observation"]',
            '["screenshot","ui_observation"]',
            '["ui_observation","workspace_deployment"]',
            '["screenshot","ui_observation","workspace_deployment"]',
        )
    )
    + ")"
)
_OLD_REVIEW = '(\'["screenshot"]\',\'["screenshot","workspace_deployment"]\')'
_NEW_REVIEW = (
    _OLD_REVIEW[:-1] + ',\'["screenshot","ui_observation"]\','
    '\'["screenshot","ui_observation","workspace_deployment"]\')'
)


def _replace(sets: str, reviewed_sets: str) -> None:
    bind = op.get_bind()
    # SQLite batch recreation loses attached triggers. Retain every existing
    # trigger byte-for-byte, including cross-table identity/authority checks.
    triggers = list(
        bind.execute(
            sa.text(
                "SELECT name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name"
            )
        )
    )
    bind.exec_driver_sql("SAVEPOINT ui_observation_capabilities")
    try:
        for name, _sql in triggers:
            bind.exec_driver_sql('DROP TRIGGER "' + name.replace('"', '""') + '"')
        with op.batch_alter_table(
            "external_bridge_pairings", recreate="always"
        ) as batch:
            batch.drop_constraint("external_bridge_capabilities_closed", type_="check")
            batch.create_check_constraint(
                "external_bridge_capabilities_closed",
                f"observed_capabilities_json IN {sets} AND "
                f"verified_capabilities_json IN {sets}",
            )
        with op.batch_alter_table("reviewed_bridge_builds", recreate="always") as batch:
            batch.drop_constraint("reviewed_bridge_build_sets_bounded", type_="check")
            batch.create_check_constraint(
                "reviewed_bridge_build_sets_bounded",
                f"capabilities_json IN {reviewed_sets} AND "
                "length(supported_kodi_versions_json) BETWEEN 5 AND 256",
            )
        for _name, sql in triggers:
            bind.exec_driver_sql(sql)
    except Exception:
        bind.exec_driver_sql("ROLLBACK TO SAVEPOINT ui_observation_capabilities")
        bind.exec_driver_sql("RELEASE SAVEPOINT ui_observation_capabilities")
        raise
    bind.exec_driver_sql("RELEASE SAVEPOINT ui_observation_capabilities")


def upgrade() -> None:
    _replace(_NEW, _NEW_REVIEW)


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS(SELECT 1 FROM external_bridge_pairings WHERE "
            "observed_capabilities_json LIKE '%ui_observation%' OR "
            "verified_capabilities_json LIKE '%ui_observation%') OR "
            "EXISTS(SELECT 1 FROM reviewed_bridge_builds WHERE "
            "capabilities_json LIKE '%ui_observation%')"
        )
    ):
        raise RuntimeError("persisted UI observation capability cannot be downgraded")
    _replace(_OLD, _OLD_REVIEW)
