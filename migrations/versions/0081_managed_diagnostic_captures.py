"""Retain bounded Web-selected diagnostics independently of disposable profiles."""

import sqlalchemy as sa
from alembic import op

revision = "0081_managed_diagnostic_captures"
down_revision = "0080_development_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "managed_diagnostic_captures",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("request_id", sa.String(36), nullable=False),
        sa.Column("target_id", sa.String(36), nullable=False),
        sa.Column("runtime_revision_id", sa.String(36), nullable=False),
        sa.Column("container_id", sa.String(64), nullable=False),
        sa.Column("image_id", sa.String(71), nullable=False),
        sa.Column("context_json", sa.String(2048), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("created_at", sa.String(27), nullable=False),
        sa.Column("review_after", sa.String(27), nullable=False),
        sa.Column("completed_at", sa.String(27)),
        sa.Column("discarded_at", sa.String(27)),
        sa.Column("payload_json", sa.Text()),
        sa.Column("payload_sha256", sa.String(64)),
        sa.Column("failure_code", sa.String(64)),
        sa.UniqueConstraint("request_id", name="diagnostic_capture_request"),
        sa.CheckConstraint(
            "length(id)=36 AND length(request_id)=36 AND length(target_id)=36 "
            "AND length(runtime_revision_id)=36",
            name="diagnostic_capture_ids",
        ),
        sa.CheckConstraint(
            "length(container_id)=64 AND container_id NOT GLOB '*[^0-9a-f]*' AND "
            "length(image_id)=71 AND substr(image_id,1,7)='sha256:'",
            name="diagnostic_capture_binding",
        ),
        sa.CheckConstraint(
            "state IN ('capturing','retained','failed','discarded')",
            name="diagnostic_capture_state",
        ),
        sa.CheckConstraint(
            "length(context_json) BETWEEN 2 AND 2048", name="diagnostic_capture_context"
        ),
        sa.CheckConstraint(
            "payload_json IS NULL OR length(CAST(payload_json AS BLOB)) BETWEEN "
            "2 AND 49152",
            name="diagnostic_capture_payload_bound",
        ),
        sa.CheckConstraint(
            "payload_sha256 IS NULL OR (length(payload_sha256)=64 AND "
            "payload_sha256 NOT GLOB '*[^0-9a-f]*')",
            name="diagnostic_capture_hash",
        ),
        sa.CheckConstraint(
            "(state='retained' AND payload_json IS NOT NULL AND payload_sha256 "
            "IS NOT NULL) OR (state<>'retained' AND payload_json IS NULL)",
            name="diagnostic_capture_body_state",
        ),
        sa.CheckConstraint(
            "(state='capturing' AND completed_at IS NULL) OR (state<>'capturing' "
            "AND completed_at IS NOT NULL)",
            name="diagnostic_capture_completion",
        ),
        sa.CheckConstraint(
            "(state='discarded' AND discarded_at IS NOT NULL) OR "
            "(state<>'discarded' AND discarded_at IS NULL)",
            name="diagnostic_capture_discard",
        ),
        sa.CheckConstraint(
            "(state='failed' AND failure_code IS NOT NULL) OR (state<>'failed' "
            "AND failure_code IS NULL)",
            name="diagnostic_capture_failure",
        ),
        sa.CheckConstraint(
            "failure_code IS NULL OR length(failure_code) BETWEEN 1 AND 64",
            name="diagnostic_capture_failure_bound",
        ),
    )
    op.create_index(
        "ix_diagnostic_capture_target_created",
        "managed_diagnostic_captures",
        ["target_id", "created_at"],
    )
    op.create_index(
        "uq_diagnostic_capture_active_target",
        "managed_diagnostic_captures",
        ["target_id"],
        unique=True,
        sqlite_where=sa.text("state='capturing'"),
    )


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.text("SELECT count(*) FROM managed_diagnostic_captures")
    ):
        raise RuntimeError(
            "Retained diagnostic evidence/history cannot be safely downgraded"
        )
    op.drop_index(
        "uq_diagnostic_capture_active_target", table_name="managed_diagnostic_captures"
    )
    op.drop_index(
        "ix_diagnostic_capture_target_created", table_name="managed_diagnostic_captures"
    )
    op.drop_table("managed_diagnostic_captures")
