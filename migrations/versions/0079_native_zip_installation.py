"""Admit passive repository ZIPs and retain expiring native delivery evidence."""

import sqlalchemy as sa
from alembic import op

revision = "0079_native_zip_installation"
down_revision = "0078_workspace_deploy_recovery"
branch_labels = None
depends_on = None


def _classification(values: str) -> None:
    with op.batch_alter_table("addon_zip_artifacts", recreate="always") as batch:
        batch.drop_constraint("addon_zip_extension_classification", type_="check")
        batch.create_check_constraint(
            "addon_zip_extension_classification",
            f"extension_classification IN ({values})",
        )


def upgrade() -> None:
    _classification("'python_video_plugin','kodi_repository'")
    op.create_table(
        "native_zip_installations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("request_id", sa.String(36), nullable=False),
        sa.Column(
            "client_id",
            sa.String(36),
            sa.ForeignKey("mcp_clients.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "credential_id",
            sa.String(36),
            sa.ForeignKey("mcp_credentials.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "target_id",
            sa.String(36),
            sa.ForeignKey("managed_instances.target_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "artifact_id",
            sa.String(36),
            sa.ForeignKey("addon_zip_artifacts.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "runtime_revision_id",
            sa.String(36),
            sa.ForeignKey("runtime_revisions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("expected_container_id", sa.String(64), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("created_at", sa.String(27), nullable=False),
        sa.Column("expires_at", sa.String(27), nullable=False),
        sa.Column("stage_ready", sa.Boolean(), nullable=False),
        sa.Column("zip_get_requested_at", sa.String(27)),
        sa.Column("completed_at", sa.String(27)),
        sa.Column("failure_reason", sa.String(128)),
        sa.UniqueConstraint(
            "client_id", "request_id", name="native_zip_request_identity"
        ),
        sa.CheckConstraint(
            "state IN ('requested','completed','failed','unverified')",
            name="native_zip_state",
        ),
        sa.CheckConstraint(
            "length(expected_container_id)=64", name="native_zip_container"
        ),
    )
    op.create_index("ix_native_zip_expiry", "native_zip_installations", ["expires_at"])


def downgrade() -> None:
    connection = op.get_bind()
    if (
        connection.scalar(sa.text("SELECT count(*) FROM native_zip_installations"))
        or connection.scalar(
            sa.text(
                "SELECT count(*) FROM addon_zip_artifacts "
                "WHERE extension_classification='kodi_repository'"
            )
        )
        or connection.scalar(
            sa.text(
                "SELECT count(*) FROM mcp_preset_permissions "
                "WHERE permission='addon.install'"
            )
        )
    ):
        raise RuntimeError("Native ZIP evidence and authority must be preserved")
    op.drop_index("ix_native_zip_expiry", table_name="native_zip_installations")
    op.drop_table("native_zip_installations")
    _classification("'python_video_plugin'")
