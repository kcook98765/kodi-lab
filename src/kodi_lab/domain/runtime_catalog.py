"""Runtime catalog domain models."""

import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Self
from uuid import UUID

_SLUG_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?$")
_SHA256_PATTERN = re.compile(r"^(?:sha256:)?([0-9a-fA-F]{64})$")


class RevisionApprovalState(StrEnum):
    """Selection approval for a finalized runtime revision."""

    NOT_APPROVED = "not_approved"
    APPROVED = "approved"


def _require_nonblank(value: str, field_name: str) -> None:
    if not value.strip():
        raise ValueError(f"{field_name} must not be blank")


def _normalize_optional_nonblank(value: str | None, field_name: str) -> str | None:
    if value is not None:
        _require_nonblank(value, field_name)
    return value


def _normalize_timestamp(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must include a timezone")
    return value.astimezone(UTC)


def normalize_sha256_digest(value: str, field_name: str = "digest") -> str:
    """Validate and normalize a SHA-256 digest to its canonical text form."""

    match = _SHA256_PATTERN.fullmatch(value)
    if match is None:
        raise ValueError(f"{field_name} must be a SHA-256 digest")
    return f"sha256:{match.group(1).lower()}"


@dataclass(frozen=True, slots=True)
class RuntimeDefinition:
    """Logical runtime catalog identity."""

    id: UUID
    slug: str
    kodi_version: str

    def __post_init__(self) -> None:
        if _SLUG_PATTERN.fullmatch(self.slug) is None:
            raise ValueError(
                "slug must use lowercase letters, numbers, '.', '_' or '-', "
                "and start and end with a letter or number"
            )
        _require_nonblank(self.kodi_version, "kodi_version")


@dataclass(frozen=True, slots=True)
class RuntimeRevision:
    """A finalized immutable runtime revision plus mutable lifecycle metadata."""

    id: UUID
    runtime_definition_id: UUID
    revision_number: int
    source_kind: str
    source_reference: str
    content_digest: str
    manifest_reference: str
    manifest_digest: str
    built_at: datetime
    build_log_reference: str | None = None
    approval_state: RevisionApprovalState = RevisionApprovalState.NOT_APPROVED
    approved_at: datetime | None = None
    approval_note: str | None = None
    requires_trusted_build_confirmation: bool = False
    trusted_build_confirmed_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.revision_number <= 0:
            raise ValueError("revision_number must be positive")
        for field_name in ("source_kind", "source_reference", "manifest_reference"):
            _require_nonblank(getattr(self, field_name), field_name)
        _normalize_optional_nonblank(self.build_log_reference, "build_log_reference")
        object.__setattr__(
            self,
            "content_digest",
            normalize_sha256_digest(self.content_digest, "content_digest"),
        )
        object.__setattr__(
            self,
            "manifest_digest",
            normalize_sha256_digest(self.manifest_digest, "manifest_digest"),
        )
        object.__setattr__(
            self, "built_at", _normalize_timestamp(self.built_at, "built_at")
        )

        if self.approval_state is RevisionApprovalState.APPROVED:
            if self.approved_at is None:
                raise ValueError("approved revision requires approved_at")
            object.__setattr__(
                self,
                "approved_at",
                _normalize_timestamp(self.approved_at, "approved_at"),
            )
            _normalize_optional_nonblank(self.approval_note, "approval_note")
        elif self.approved_at is not None or self.approval_note is not None:
            raise ValueError("not-approved revision cannot have approval metadata")

        if self.trusted_build_confirmed_at is not None:
            if not self.requires_trusted_build_confirmation:
                raise ValueError(
                    "trust confirmation requires requires_trusted_build_confirmation"
                )
            object.__setattr__(
                self,
                "trusted_build_confirmed_at",
                _normalize_timestamp(
                    self.trusted_build_confirmed_at,
                    "trusted_build_confirmed_at",
                ),
            )

    def approve(self, *, approved_at: datetime, note: str | None = None) -> Self:
        """Return this revision with approved lifecycle metadata."""

        return replace(
            self,
            approval_state=RevisionApprovalState.APPROVED,
            approved_at=approved_at,
            approval_note=note,
        )

    def revoke_approval(self) -> Self:
        """Return this revision with approval lifecycle metadata cleared."""

        return replace(
            self,
            approval_state=RevisionApprovalState.NOT_APPROVED,
            approved_at=None,
            approval_note=None,
        )

    def confirm_trusted_build(self, *, confirmed_at: datetime) -> Self:
        """Return this revision with independent trust confirmation."""

        return replace(self, trusted_build_confirmed_at=confirmed_at)

    def revoke_trusted_build_confirmation(self) -> Self:
        """Return this revision with trust confirmation cleared."""

        return replace(self, trusted_build_confirmed_at=None)
