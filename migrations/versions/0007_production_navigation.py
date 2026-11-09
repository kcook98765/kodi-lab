"""One OFF-by-default production navigation opt-in; preserve legacy history."""

import sqlalchemy as sa
from alembic import op

revision = "0007_production_navigation"
down_revision = "0006_external_screenshots"
branch_labels = None
depends_on = None

_EXTRA_POLICY = (
    "NEW.remote_navigation_enabled<>OLD.remote_navigation_enabled OR "
    "NEW.navigation_binding_digest IS NOT OLD.navigation_binding_digest OR "
)
_OLD_SCOPE = "length(navigate_scope_id)=36"
_NEW_SCOPE = "(navigate_scope_id IS NULL OR length(navigate_scope_id)=36)"


def _receipt_shapes(*, upgrading: bool) -> None:
    connection = op.get_bind()
    tables = (
        (
            "mcp_production_remote_receipt_bindings",
            "mcp_production_remote_binding_authority_shapes",
        ),
        (
            "mcp_production_navigation_audits",
            "mcp_production_navigation_audit_authority_valid",
        ),
    )
    # SQLite batch replacement must temporarily remove all cross-table triggers
    # referencing these tables, then restore their exact SQL in the transaction.
    triggers = [
        (name, sql)
        for name, sql in connection.execute(
            sa.text(
                "SELECT name,sql FROM sqlite_master "
                "WHERE type='trigger' AND sql IS NOT NULL"
            )
        )
        if any(table in sql for table, _constraint in tables)
    ]
    for name, _sql in triggers:
        op.execute('DROP TRIGGER "' + name.replace('"', '""') + '"')
    for table, constraint in tables:
        checks = sa.inspect(connection).get_check_constraints(table)
        old = next(check["sqltext"] for check in checks if check["name"] == constraint)
        new = (
            old.replace(_OLD_SCOPE, _NEW_SCOPE)
            if upgrading
            else old.replace(_NEW_SCOPE, _OLD_SCOPE)
        )
        with op.batch_alter_table(table, recreate="always") as batch:
            batch.drop_constraint(constraint, type_="check")
            batch.create_check_constraint(constraint, new)
            if table == "mcp_production_navigation_audits":
                batch.alter_column(
                    "navigate_scope_id", existing_type=sa.String(36), nullable=upgrading
                )
    for _name, sql in triggers:
        op.execute(sql)


def _policy_guard(*, upgrading: bool) -> None:
    sql = op.get_bind().scalar(
        sa.text(
            "SELECT sql FROM sqlite_master "
            "WHERE name='trg_external_production_policy_material_revision'"
        )
    )
    assert isinstance(sql, str)
    if upgrading:
        sql = sql.replace(
            "NEW.updated_at<>OLD.updated_at",
            _EXTRA_POLICY + "NEW.updated_at<>OLD.updated_at",
        )
    else:
        sql = sql.replace(_EXTRA_POLICY, "")
    op.execute("DROP TRIGGER trg_external_production_policy_material_revision")
    op.execute(sql)


def upgrade() -> None:
    op.add_column(
        "external_production_policies",
        sa.Column(
            "remote_navigation_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "external_production_policies",
        sa.Column("navigation_binding_digest", sa.String(64)),
    )
    _policy_guard(upgrading=True)
    _receipt_shapes(upgrading=True)


def downgrade() -> None:
    connection = op.get_bind()
    if connection.scalar(
        sa.text(
            "SELECT EXISTS(SELECT 1 FROM external_production_policies "
            "WHERE remote_navigation_enabled=1) "
            "OR EXISTS(SELECT 1 FROM mcp_production_navigation_audits "
            "WHERE navigate_scope_id IS NULL)"
        )
    ):
        raise RuntimeError(
            "production navigation opt-in or receipt history prevents downgrade"
        )
    _receipt_shapes(upgrading=False)
    _policy_guard(upgrading=False)
    op.drop_column("external_production_policies", "navigation_binding_digest")
    op.drop_column("external_production_policies", "remote_navigation_enabled")
