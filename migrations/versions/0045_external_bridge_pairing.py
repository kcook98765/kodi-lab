"""Add authenticated external bridge pairing and replay receipts.

Revision ID: 0045_external_bridge_pairing
Revises: 0044_external_development_targets
"""

# Closed SQLite constraints are intentionally explicit and auditable.
# ruff: noqa: E501

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0045_external_bridge_pairing"
down_revision: str | Sequence[str] | None = "0044_external_development_targets"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "external_bridge_pairings",
        sa.Column("target_id", sa.String(36), nullable=False),
        sa.Column("bridge_port", sa.Integer()),
        sa.Column(
            "pairing_state",
            sa.Enum(
                "unpaired",
                "pairing_pending",
                "paired",
                "needs_reverification",
                "revoked",
                name="external_bridge_pairing_state",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "reachability_state",
            sa.Enum(
                "unknown",
                "reachable",
                "unreachable",
                "authentication_failed",
                "identity_mismatch",
                "protocol_unsupported",
                name="external_bridge_reachability_state",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("pairing_token_reference", sa.String(36)),
        sa.Column("bridge_secret_reference", sa.String(36)),
        sa.Column("pairing_token_expires_at", sa.String(27)),
        sa.Column("observed_bridge_id", sa.String(36)),
        sa.Column("observed_protocol_version", sa.String(16)),
        sa.Column("observed_addon_id", sa.String(128)),
        sa.Column("observed_addon_version", sa.String(64)),
        sa.Column("observed_kodi_version", sa.String(64)),
        sa.Column("observed_build_fingerprint", sa.String(64)),
        sa.Column("observed_capabilities_json", sa.String(256), nullable=False),
        sa.Column("pinned_bridge_id", sa.String(36)),
        sa.Column("pinned_protocol_version", sa.String(16)),
        sa.Column("pinned_addon_id", sa.String(128)),
        sa.Column("pinned_addon_version", sa.String(64)),
        sa.Column("pinned_kodi_version", sa.String(64)),
        sa.Column("pinned_build_fingerprint", sa.String(64)),
        sa.Column("verified_capabilities_json", sa.String(256), nullable=False),
        sa.Column("endpoint_revision_digest", sa.String(64)),
        sa.Column("token_generated_at", sa.String(27)),
        sa.Column("pairing_attempted_at", sa.String(27)),
        sa.Column("paired_at", sa.String(27)),
        sa.Column("verified_at", sa.String(27)),
        sa.Column("revoked_at", sa.String(27)),
        sa.CheckConstraint(
            "pairing_state IN ('unpaired','pairing_pending','paired','needs_reverification','revoked')",
            name="external_bridge_pairing_state_closed",
        ),
        sa.CheckConstraint(
            "reachability_state IN ('unknown','reachable','unreachable','authentication_failed','identity_mismatch','protocol_unsupported')",
            name="external_bridge_reachability_closed",
        ),
        sa.CheckConstraint(
            "bridge_port IS NULL OR bridge_port BETWEEN 1 AND 65535",
            name="external_bridge_port_valid",
        ),
        sa.CheckConstraint(
            "pairing_token_reference IS NULL OR length(pairing_token_reference)=36",
            name="external_bridge_pairing_token_reference_shape",
        ),
        sa.CheckConstraint(
            "bridge_secret_reference IS NULL OR length(bridge_secret_reference)=36",
            name="external_bridge_secret_reference_shape",
        ),
        sa.CheckConstraint(
            "pairing_token_expires_at IS NULL OR length(pairing_token_expires_at)=27",
            name="external_bridge_token_expiry_shape",
        ),
        sa.CheckConstraint(
            "length(observed_capabilities_json) BETWEEN 2 AND 256 AND length(verified_capabilities_json) BETWEEN 2 AND 256",
            name="external_bridge_capabilities_bounded",
        ),
        sa.CheckConstraint(
            "observed_capabilities_json IN ('[]','[\"screenshot\"]') AND verified_capabilities_json IN ('[]','[\"screenshot\"]')",
            name="external_bridge_capabilities_closed",
        ),
        sa.CheckConstraint(
            "(pairing_token_reference IS NULL) = (pairing_token_expires_at IS NULL)",
            name="external_bridge_pairing_token_complete",
        ),
        sa.CheckConstraint(
            "pairing_token_reference IS NULL OR pairing_state='unpaired'",
            name="external_bridge_token_only_unpaired",
        ),
        sa.CheckConstraint(
            "pairing_state<>'revoked' OR (pairing_token_reference IS NULL AND bridge_secret_reference IS NULL AND verified_capabilities_json='[]')",
            name="external_bridge_revocation_retires_authority",
        ),
        sa.CheckConstraint(
            "observed_build_fingerprint IS NULL OR (length(observed_build_fingerprint)=64 AND observed_build_fingerprint NOT GLOB '*[^0-9a-f]*')",
            name="external_bridge_observed_fingerprint_sha256",
        ),
        sa.CheckConstraint(
            "pinned_build_fingerprint IS NULL OR (length(pinned_build_fingerprint)=64 AND pinned_build_fingerprint NOT GLOB '*[^0-9a-f]*')",
            name="external_bridge_pinned_fingerprint_sha256",
        ),
        sa.CheckConstraint(
            "endpoint_revision_digest IS NULL OR (length(endpoint_revision_digest)=64 AND endpoint_revision_digest NOT GLOB '*[^0-9a-f]*')",
            name="external_bridge_endpoint_revision_sha256",
        ),
        sa.CheckConstraint(
            "(pairing_state='pairing_pending' AND bridge_secret_reference IS NOT NULL AND observed_bridge_id IS NOT NULL) OR pairing_state<>'pairing_pending'",
            name="external_bridge_pending_complete",
        ),
        sa.CheckConstraint(
            "(pairing_state IN ('paired','needs_reverification') AND bridge_secret_reference IS NOT NULL AND pinned_bridge_id IS NOT NULL) OR pairing_state NOT IN ('paired','needs_reverification')",
            name="external_bridge_active_identity_complete",
        ),
        sa.ForeignKeyConstraint(
            ["target_id"], ["external_kodi_targets.target_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("target_id"),
    )
    op.create_index(
        "ix_external_bridge_pairing_state",
        "external_bridge_pairings",
        ["pairing_state", "reachability_state"],
    )

    op.create_table(
        "external_bridge_request_receipts",
        sa.Column("target_id", sa.String(36), nullable=False),
        sa.Column("request_id", sa.String(36), nullable=False),
        sa.Column("nonce", sa.String(43), nullable=False),
        sa.Column("operation", sa.String(32), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("response_digest", sa.String(64)),
        sa.Column("byte_size", sa.Integer()),
        sa.Column("width", sa.Integer()),
        sa.Column("height", sa.Integer()),
        sa.Column("created_at", sa.String(27), nullable=False),
        sa.Column("completed_at", sa.String(27)),
        sa.Column("expires_at", sa.String(27), nullable=False),
        sa.CheckConstraint(
            "operation IN ('bridge_identity','bridge_capabilities','capture_screenshot')",
            name="external_bridge_receipt_operation_closed",
        ),
        sa.CheckConstraint(
            "state IN ('pending','completed','ambiguous','rejected')",
            name="external_bridge_receipt_state_closed",
        ),
        sa.CheckConstraint("length(nonce)=43", name="external_bridge_nonce_shape"),
        sa.CheckConstraint(
            "response_digest IS NULL OR (length(response_digest)=64 AND response_digest NOT GLOB '*[^0-9a-f]*')",
            name="external_bridge_response_digest_sha256",
        ),
        sa.CheckConstraint(
            "byte_size IS NULL OR byte_size BETWEEN 1 AND 8388608",
            name="external_bridge_receipt_size_bounded",
        ),
        sa.CheckConstraint(
            "(width IS NULL AND height IS NULL) OR (width BETWEEN 1 AND 4096 AND height BETWEEN 1 AND 2160)",
            name="external_bridge_receipt_dimensions_bounded",
        ),
        sa.CheckConstraint(
            "length(created_at)=27 AND length(expires_at)=27 AND (completed_at IS NULL OR length(completed_at)=27)",
            name="external_bridge_receipt_timestamps_format",
        ),
        sa.CheckConstraint(
            "(state='pending' AND completed_at IS NULL AND response_digest IS NULL AND byte_size IS NULL AND width IS NULL AND height IS NULL) OR (state='completed' AND completed_at IS NOT NULL AND response_digest IS NOT NULL AND byte_size IS NOT NULL AND width IS NOT NULL AND height IS NOT NULL) OR (state IN ('ambiguous','rejected') AND completed_at IS NOT NULL)",
            name="external_bridge_receipt_terminal_coherent",
        ),
        sa.ForeignKeyConstraint(
            ["target_id"], ["external_bridge_pairings.target_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("target_id", "request_id"),
        sa.UniqueConstraint("target_id", "nonce", name="uq_external_bridge_nonce"),
    )
    op.create_index(
        "ix_external_bridge_receipt_expiry",
        "external_bridge_request_receipts",
        ["expires_at", "target_id", "request_id"],
    )

    op.create_table(
        "external_bridge_events",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("target_id", sa.String(36), nullable=False),
        sa.Column("bridge_id", sa.String(36)),
        sa.Column("request_id", sa.String(36)),
        sa.Column("event_kind", sa.String(64), nullable=False),
        sa.Column("classification", sa.String(64)),
        sa.Column("byte_size", sa.Integer()),
        sa.Column("width", sa.Integer()),
        sa.Column("height", sa.Integer()),
        sa.Column("content_digest", sa.String(64)),
        sa.Column("occurred_at", sa.String(27), nullable=False),
        sa.CheckConstraint(
            "event_kind IN ('pairing_token_generated','pairing_attempted','bridge_paired','pairing_failed','bridge_revoked','bridge_reverified','bridge_reverification_required','screenshot_requested','screenshot_completed','screenshot_failed')",
            name="external_bridge_event_kind_closed",
        ),
        sa.CheckConstraint(
            "classification IS NULL OR length(classification) BETWEEN 1 AND 64",
            name="external_bridge_event_classification_bounded",
        ),
        sa.CheckConstraint(
            "content_digest IS NULL OR (length(content_digest)=64 AND content_digest NOT GLOB '*[^0-9a-f]*')",
            name="external_bridge_event_digest_sha256",
        ),
        sa.CheckConstraint(
            "length(occurred_at)=27", name="external_bridge_event_timestamp_format"
        ),
        sa.ForeignKeyConstraint(
            ["target_id"], ["external_bridge_pairings.target_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_external_bridge_event_time",
        "external_bridge_events",
        ["target_id", "occurred_at", "id"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    for table in (
        "external_bridge_request_receipts",
        "external_bridge_events",
        "external_bridge_pairings",
    ):
        if bind.scalar(sa.text(f"SELECT COUNT(*) FROM {table}")):
            raise RuntimeError("Cannot downgrade populated external bridge state.")
    op.drop_index("ix_external_bridge_event_time", table_name="external_bridge_events")
    op.drop_table("external_bridge_events")
    op.drop_index(
        "ix_external_bridge_receipt_expiry",
        table_name="external_bridge_request_receipts",
    )
    op.drop_table("external_bridge_request_receipts")
    op.drop_index(
        "ix_external_bridge_pairing_state", table_name="external_bridge_pairings"
    )
    op.drop_table("external_bridge_pairings")
