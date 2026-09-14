"""Add runtime catalog and exact managed-instance revision references.

Revision ID: 0002_runtime_catalog
Revises: 0001_targets_instances
Create Date: 2026-09-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_runtime_catalog"
down_revision: str | Sequence[str] | None = "0001_targets_instances"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LEGACY_DATA_ERROR = (
    "Cannot migrate managed_instances with pre-catalog runtime_revision values. "
    "This development data predates the runtime catalog and cannot be converted "
    "without inventing provenance. Recreate or reset the pre-catalog database, "
    "then run the migration again."
)


def _require_empty_managed_instances(error_message: str = _LEGACY_DATA_ERROR) -> None:
    count = op.get_bind().scalar(sa.text("SELECT COUNT(*) FROM managed_instances"))
    if count:
        raise RuntimeError(error_message)


def _require_empty_runtime_catalog() -> None:
    count = op.get_bind().scalar(sa.text("SELECT COUNT(*) FROM runtime_revisions"))
    if count:
        raise RuntimeError(
            "Cannot downgrade a populated runtime catalog to the pre-catalog "
            "schema. Remove the development database only if its catalog history "
            "is intentionally being discarded."
        )


def _sha256_constraint(column: str) -> str:
    return (
        f"length({column}) = 71 "
        f"AND substr({column}, 1, 7) = 'sha256:' "
        f"AND substr({column}, 8) NOT GLOB '*[^0-9a-f]*'"
    )


def upgrade() -> None:
    _require_empty_managed_instances()

    op.create_table(
        "runtime_definitions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("kodi_version", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "length(trim(kodi_version)) > 0",
            name="runtime_definition_kodi_version_not_blank",
        ),
        sa.CheckConstraint("length(trim(slug)) > 0", name="runtime_slug_not_blank"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug", name="uq_runtime_definitions_slug"),
    )
    op.create_table(
        "runtime_revisions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("runtime_definition_id", sa.String(length=36), nullable=False),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column("source_kind", sa.Text(), nullable=False),
        sa.Column("source_reference", sa.Text(), nullable=False),
        sa.Column("content_digest", sa.String(length=71), nullable=False),
        sa.Column("manifest_reference", sa.Text(), nullable=False),
        sa.Column("manifest_digest", sa.String(length=71), nullable=False),
        sa.Column("built_at", sa.Text(), nullable=False),
        sa.Column("build_log_reference", sa.Text(), nullable=True),
        sa.Column(
            "approval_state",
            sa.Enum(
                "not_approved",
                "approved",
                name="revision_approval_state",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("approved_at", sa.Text(), nullable=True),
        sa.Column("approval_note", sa.Text(), nullable=True),
        sa.Column(
            "requires_trusted_build_confirmation",
            sa.Boolean(
                create_constraint=True,
                name="requires_trusted_build_confirmation_bool",
            ),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("trusted_build_confirmed_at", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "(approval_state = 'approved' AND approved_at IS NOT NULL "
            "AND length(trim(approved_at)) > 0) OR "
            "(approval_state = 'not_approved' AND approved_at IS NULL "
            "AND approval_note IS NULL)",
            name="runtime_revision_approval_consistent",
        ),
        sa.CheckConstraint(
            "approval_note IS NULL OR length(trim(approval_note)) > 0",
            name="runtime_revision_approval_note_not_blank",
        ),
        sa.CheckConstraint(
            "build_log_reference IS NULL OR length(trim(build_log_reference)) > 0",
            name="runtime_revision_build_log_reference_not_blank",
        ),
        sa.CheckConstraint(
            "length(trim(built_at)) > 0",
            name="runtime_revision_built_at_not_blank",
        ),
        sa.CheckConstraint(
            _sha256_constraint("content_digest"),
            name="runtime_revision_content_digest_sha256",
        ),
        sa.CheckConstraint(
            _sha256_constraint("manifest_digest"),
            name="runtime_revision_manifest_digest_sha256",
        ),
        sa.CheckConstraint(
            "length(trim(manifest_reference)) > 0",
            name="runtime_revision_manifest_reference_not_blank",
        ),
        sa.CheckConstraint(
            "revision_number > 0", name="runtime_revision_number_positive"
        ),
        sa.CheckConstraint(
            "length(trim(source_kind)) > 0",
            name="runtime_revision_source_kind_not_blank",
        ),
        sa.CheckConstraint(
            "length(trim(source_reference)) > 0",
            name="runtime_revision_source_reference_not_blank",
        ),
        sa.CheckConstraint(
            "requires_trusted_build_confirmation = 1 "
            "OR trusted_build_confirmed_at IS NULL",
            name="runtime_revision_trust_confirmation_consistent",
        ),
        sa.CheckConstraint(
            "trusted_build_confirmed_at IS NULL "
            "OR length(trim(trusted_build_confirmed_at)) > 0",
            name="runtime_revision_trust_timestamp_not_blank",
        ),
        sa.ForeignKeyConstraint(
            ["runtime_definition_id"],
            ["runtime_definitions.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "runtime_definition_id",
            "revision_number",
            name="uq_runtime_revision_number",
        ),
    )

    with op.batch_alter_table("managed_instances", recreate="always") as batch_op:
        batch_op.drop_constraint(
            "managed_instance_runtime_revision_not_blank", type_="check"
        )
        batch_op.drop_column("runtime_revision")
        batch_op.add_column(
            sa.Column("runtime_revision_id", sa.String(length=36), nullable=False)
        )
        batch_op.create_foreign_key(
            "fk_managed_instances_runtime_revision_id",
            "runtime_revisions",
            ["runtime_revision_id"],
            ["id"],
            ondelete="RESTRICT",
        )


def downgrade() -> None:
    _require_empty_managed_instances(
        "Cannot downgrade managed instances with exact runtime-revision "
        "references to the pre-catalog schema. Remove the development database "
        "only if those instance records are intentionally being discarded."
    )
    _require_empty_runtime_catalog()

    with op.batch_alter_table("managed_instances", recreate="always") as batch_op:
        batch_op.drop_constraint(
            "fk_managed_instances_runtime_revision_id", type_="foreignkey"
        )
        batch_op.drop_column("runtime_revision_id")
        batch_op.add_column(sa.Column("runtime_revision", sa.Text(), nullable=False))
        batch_op.create_check_constraint(
            "managed_instance_runtime_revision_not_blank",
            "length(trim(runtime_revision)) > 0",
        )

    op.drop_table("runtime_revisions")
    op.drop_table("runtime_definitions")
