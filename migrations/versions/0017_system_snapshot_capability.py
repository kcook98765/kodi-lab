"""Admit system_snapshot in closed Bridge sets; retain target authority."""

import itertools
import json

import sqlalchemy as sa
from alembic import op

revision = "0017_system_snapshot_capability"
down_revision = "0016_reviewed_bridge_updates"
branch_labels = None
depends_on = None


def _sets(*, reviewed: bool, snapshot: bool) -> str:
    # Migration-local vocabulary: future application changes cannot rewrite history.
    values = [
        "log_read",
        "log_scan",
        "screenshot",
        "ui_observation",
        "workspace_deployment",
    ]
    if snapshot:
        values.insert(3, "system_snapshot")
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


def _replace(*, snapshot: bool) -> None:
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
    caps = _sets(reviewed=False, snapshot=snapshot)
    reviewed = _sets(reviewed=True, snapshot=snapshot)
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
    _replace(snapshot=True)


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS(SELECT 1 FROM external_bridge_pairings "
            "WHERE observed_capabilities_json LIKE '%\"system_snapshot\"%' "
            "OR verified_capabilities_json LIKE '%\"system_snapshot\"%') "
            "OR EXISTS(SELECT 1 FROM reviewed_bridge_builds "
            "WHERE capabilities_json LIKE '%\"system_snapshot\"%')"
        )
    ):
        raise RuntimeError(
            "system_snapshot pairing or reviewed build exists; refusing lossy downgrade"
        )
    _replace(snapshot=False)
