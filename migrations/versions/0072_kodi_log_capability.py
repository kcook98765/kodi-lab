"""Add bounded log inspection to the closed Bridge capability vocabulary."""

import itertools
import json

import sqlalchemy as sa
from alembic import op

revision = "0072_kodi_log_capability"
down_revision = "0071_household_onboarding"
branch_labels = None
depends_on = None


def _sets(logs: bool, reviewed: bool) -> str:
    values = (("log_read",) if logs else ()) + (
        "screenshot",
        "ui_observation",
        "workspace_deployment",
    )
    return (
        "("
        + ",".join(
            "'" + json.dumps(subset, separators=(",", ":")) + "'"
            for count in range(len(values) + 1)
            for subset in itertools.combinations(values, count)
            if not reviewed or "screenshot" in subset
        )
        + ")"
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
    bind.exec_driver_sql("SAVEPOINT kodi_log_capabilities")
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
        bind.exec_driver_sql("ROLLBACK TO SAVEPOINT kodi_log_capabilities")
        bind.exec_driver_sql("RELEASE SAVEPOINT kodi_log_capabilities")
        raise
    bind.exec_driver_sql("RELEASE SAVEPOINT kodi_log_capabilities")


def upgrade() -> None:
    _replace(_sets(True, False), _sets(True, True))


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS(SELECT 1 FROM external_bridge_pairings WHERE "
            "observed_capabilities_json LIKE '%log_read%' OR "
            "verified_capabilities_json LIKE '%log_read%') OR "
            "EXISTS(SELECT 1 FROM reviewed_bridge_builds WHERE "
            "capabilities_json LIKE '%log_read%')"
        )
    ):
        raise RuntimeError("persisted Kodi log capability cannot be downgraded")
    _replace(_sets(False, False), _sets(False, True))
