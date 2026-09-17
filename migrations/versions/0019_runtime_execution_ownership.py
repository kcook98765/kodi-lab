"""Add trusted runtime execution ownership.

Revision ID: 0019_runtime_execution_ownership
Revises: 0018_development_source_attach
Create Date: 2026-09-16
"""

from collections.abc import Sequence
from typing import Final

import sqlalchemy as sa
from alembic import op

revision: str = "0019_runtime_execution_ownership"
down_revision: str | Sequence[str] | None = "0018_development_source_attach"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_CURATED_OWNERSHIP: Final[dict[str, tuple[object, ...]]] = {
    # revision: (number, definition release, kind, app, profile release,
    #            channel, prerelease, image, uid, gid)
    "51950000-0000-4000-8000-000000000002": (
        1,
        "19.5",
        "kodi",
        "Kodi",
        "19.5",
        "stable",
        None,
        "sha256:73c221f1fc1fc14cbfdfd82e62fd4b87752ef71e0521227cb6790451fa51d2bd",
        1000,
        1000,
    ),
    "52000000-0000-4000-8000-000000000002": (
        1,
        "20.5",
        "kodi",
        "Kodi",
        "20.5",
        "stable",
        None,
        "sha256:be35d7cb8899dfe474c6a1eca62da99c4ada1280f4325933d7b6193e83537638",
        1000,
        1000,
    ),
    "3a000000-0000-4000-8000-000000000002": (
        1,
        "21.3",
        "kodi",
        "Kodi",
        "21.3",
        "stable",
        None,
        "sha256:1ac4521ef5f0f86eb497106c7fd3bdaff11ac989b34e803c3d105e5a24ff6247",
        1000,
        1000,
    ),
    "3a000000-0000-4000-8000-000000000004": (
        2,
        "21.3",
        "kodi",
        "Kodi",
        "21.3",
        "stable",
        None,
        "sha256:70e0166fd79ea731c3f81056c2f0516f8fe5c9c8ec729b0c0f1c3e6bac66f254",
        1000,
        1000,
    ),
    "52200000-0000-4000-8000-000000000002": (
        1,
        "22.0-beta.2",
        "kodi",
        "Kodi",
        "22.0",
        "beta",
        2,
        "sha256:4fb611666a2637904d6edd0cf90aa8cfc12c8d89edaa7a7fa60c61ef26655918",
        1000,
        1000,
    ),
}


def upgrade() -> None:
    connection = op.get_bind()
    rows = list(
        connection.execute(
            sa.text(
                "SELECT p.runtime_revision_id, r.revision_number, d.kodi_version, "
                "p.runtime_kind, p.expected_application_name, "
                "p.expected_kodi_version, p.expected_version_tag, "
                "p.expected_prerelease_ordinal, a.image_id "
                "FROM runtime_execution_profiles AS p "
                "JOIN runtime_revisions AS r ON r.id = p.runtime_revision_id "
                "JOIN runtime_definitions AS d ON d.id = r.runtime_definition_id "
                "LEFT JOIN runtime_container_artifacts AS a "
                "ON a.runtime_revision_id = p.runtime_revision_id"
            )
        ).mappings()
    )
    backfill: list[tuple[str, object, object]] = []
    for row in rows:
        revision_id = row["runtime_revision_id"]
        expected = _CURATED_OWNERSHIP.get(revision_id)
        observed = (
            row["revision_number"],
            row["kodi_version"],
            row["runtime_kind"],
            row["expected_application_name"],
            row["expected_kodi_version"],
            row["expected_version_tag"],
            row["expected_prerelease_ordinal"],
            row["image_id"],
        )
        if expected is None or observed != expected[:8]:
            raise RuntimeError(
                "Cannot backfill runtime ownership for an unknown or mismatched "
                f"execution profile: {revision_id}"
            )
        backfill.append((revision_id, expected[8], expected[9]))

    op.add_column(
        "runtime_execution_profiles",
        sa.Column("runtime_uid", sa.Integer(), nullable=True),
    )
    op.add_column(
        "runtime_execution_profiles",
        sa.Column("runtime_gid", sa.Integer(), nullable=True),
    )

    for revision_id, runtime_uid, runtime_gid in backfill:
        connection.execute(
            sa.text(
                "UPDATE runtime_execution_profiles "
                "SET runtime_uid = :runtime_uid, runtime_gid = :runtime_gid "
                "WHERE runtime_revision_id = :runtime_revision_id"
            ),
            {
                "runtime_revision_id": revision_id,
                "runtime_uid": runtime_uid,
                "runtime_gid": runtime_gid,
            },
        )

    unresolved = connection.scalar(
        sa.text(
            "SELECT COUNT(*) FROM runtime_execution_profiles "
            "WHERE runtime_uid IS NULL OR runtime_gid IS NULL"
        )
    )
    if unresolved:
        raise RuntimeError("Cannot leave runtime ownership unresolved")

    with op.batch_alter_table(
        "runtime_execution_profiles", recreate="always"
    ) as batch_op:
        batch_op.alter_column("runtime_uid", existing_type=sa.Integer(), nullable=False)
        batch_op.alter_column("runtime_gid", existing_type=sa.Integer(), nullable=False)
        batch_op.create_check_constraint(
            "runtime_profile_uid_range", "runtime_uid BETWEEN 0 AND 2147483647"
        )
        batch_op.create_check_constraint(
            "runtime_profile_gid_range", "runtime_gid BETWEEN 0 AND 2147483647"
        )


def downgrade() -> None:
    count = op.get_bind().scalar(
        sa.text("SELECT COUNT(*) FROM runtime_execution_profiles")
    )
    if count:
        raise RuntimeError(
            "Cannot downgrade populated runtime execution ownership metadata."
        )
    with op.batch_alter_table(
        "runtime_execution_profiles", recreate="always"
    ) as batch_op:
        batch_op.drop_constraint("runtime_profile_uid_range", type_="check")
        batch_op.drop_constraint("runtime_profile_gid_range", type_="check")
        batch_op.drop_column("runtime_uid")
        batch_op.drop_column("runtime_gid")
