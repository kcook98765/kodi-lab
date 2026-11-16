"""Admit the closed read-only log_scan capability without changing target grants."""

import itertools
import json

import sqlalchemy as sa
from alembic import op

revision = "0015_log_scan_capability"
down_revision = "0014_human_reset_bridge"
branch_labels = None
depends_on = None


def _sets(*, reviewed: bool, scanning: bool) -> str:
    # Migration-local vocabulary: future application changes cannot rewrite history.
    values = ["log_read", "screenshot", "ui_observation", "workspace_deployment"]
    if scanning:
        values.insert(1, "log_scan")
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


def _replace(*, scanning: bool) -> None:
    connection = op.get_bind()
    tables = ("external_bridge_pairings", "reviewed_bridge_builds")
    triggers = [
        (name, sql)
        for name, sql in connection.execute(
            sa.text(
                "SELECT name,sql FROM sqlite_master "
                "WHERE type='trigger' AND sql IS NOT NULL"
            )
        )
        if any(table in sql for table in tables)
    ]
    for name, _ in triggers:
        op.execute('DROP TRIGGER "' + name.replace('"', '""') + '"')
    caps = _sets(reviewed=False, scanning=scanning)
    reviewed = _sets(reviewed=True, scanning=scanning)
    for table, constraint, expression in (
        (
            "external_bridge_pairings",
            "external_bridge_capabilities_closed",
            f"observed_capabilities_json IN {caps} "
            f"AND verified_capabilities_json IN {caps}",
        ),
        (
            "reviewed_bridge_builds",
            "reviewed_bridge_build_sets_bounded",
            f"capabilities_json IN {reviewed} "
            "AND length(supported_kodi_versions_json) BETWEEN 5 AND 256",
        ),
    ):
        with op.batch_alter_table(table, recreate="always") as batch:
            batch.drop_constraint(constraint, type_="check")
            batch.create_check_constraint(constraint, expression)
    for _, sql in triggers:
        op.execute(sql)


def upgrade() -> None:
    _replace(scanning=True)


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS(SELECT 1 FROM external_bridge_pairings "
            "WHERE observed_capabilities_json LIKE '%\"log_scan\"%' "
            "OR verified_capabilities_json LIKE '%\"log_scan\"%') "
            "OR EXISTS(SELECT 1 FROM reviewed_bridge_builds "
            "WHERE capabilities_json LIKE '%\"log_scan\"%')"
        )
    ):
        raise RuntimeError(
            "log_scan pairing or reviewed build exists; refusing lossy downgrade"
        )
    _replace(scanning=False)
