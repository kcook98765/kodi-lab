"""Runtime catalog domain models."""

import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Self
from uuid import UUID

from kodi_lab.domain.kodi_release import (
    KodiReleaseChannel,
    KodiReleaseIdentity,
    RuntimeSupportTier,
    validate_support_tier_for_release,
)

_SLUG_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?$")
_SHA256_PATTERN = re.compile(r"^(?:sha256:)?([0-9a-fA-F]{64})$")
_VALIDATION_CONTRACT_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?$")

KODI_RUNTIME_VALIDATION_CONTRACT = "kodi-runtime-v1"
RECOGNIZED_VALIDATION_CONTRACTS = frozenset({KODI_RUNTIME_VALIDATION_CONTRACT})


class RevisionApprovalState(StrEnum):
    """Selection approval for a finalized runtime revision."""

    NOT_APPROVED = "not_approved"
    APPROVED = "approved"


class RuntimeKind(StrEnum):
    """Execution kinds understood by trusted runtime adapters."""

    KODI = "kodi"


class PythonBytecodePolicy(StrEnum):
    """Closed managed-runtime policy for embedded Python bytecode writes."""

    INTERPRETER_DEFAULT = "interpreter_default"
    SUPPRESS = "suppress"


class ProcessObservationKind(StrEnum):
    """Bounded process observation mechanisms."""

    DOCKER_HEALTHCHECK = "docker_healthcheck"


class ReadinessProbeKind(StrEnum):
    """Bounded application readiness probe families."""

    KODI_JSONRPC = "kodi_jsonrpc"


class RuntimeValidationResult(StrEnum):
    """Immutable outcome recorded by a trusted runtime validation workflow."""

    PASSED = "passed"
    FAILED = "failed"


class RuntimeRecipeInstallationState(StrEnum):
    """Durable, bounded phases for one reviewed local image installation."""

    PENDING = "pending"
    ACQUIRING = "acquiring"
    ASSEMBLING = "assembling"
    VALIDATING = "validating"
    READY = "ready"
    FAILED = "failed"


class EffectiveValidationState(StrEnum):
    """Derived state for one revision under a required contract version."""

    UNVALIDATED = "unvalidated"
    VALIDATED = "validated"
    FAILED = "failed"


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
    support_tier: RuntimeSupportTier = RuntimeSupportTier.CURRENT

    def __post_init__(self) -> None:
        if _SLUG_PATTERN.fullmatch(self.slug) is None:
            raise ValueError(
                "slug must use lowercase letters, numbers, '.', '_' or '-', "
                "and start and end with a letter or number"
            )
        _require_nonblank(self.kodi_version, "kodi_version")
        release = KodiReleaseIdentity.parse(self.kodi_version)
        validate_support_tier_for_release(release, self.support_tier)
        object.__setattr__(self, "kodi_version", release.canonical)

    @property
    def kodi_release(self) -> KodiReleaseIdentity:
        return KodiReleaseIdentity.parse(self.kodi_version)


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


@dataclass(frozen=True, slots=True)
class RuntimeContainerArtifact:
    """Immutable materialized Docker image identity for one runtime revision."""

    runtime_revision_id: UUID
    image_id: str

    def __post_init__(self) -> None:
        normalized = normalize_sha256_digest(self.image_id, "image_id")
        if normalized != self.image_id:
            raise ValueError("image_id must use canonical sha256 lowercase form")


@dataclass(frozen=True, slots=True)
class RuntimeExecutionProfile:
    """Immutable trusted execution semantics for one finalized revision."""

    runtime_revision_id: UUID
    runtime_kind: RuntimeKind
    runtime_uid: int
    runtime_gid: int
    python_bytecode_policy: PythonBytecodePolicy
    expected_application_name: str
    expected_kodi_version: str
    expected_version_tag: str
    jsonrpc_port: int
    jsonrpc_path: str
    process_observation: ProcessObservationKind
    readiness_probe: ReadinessProbeKind
    readiness_timeout_seconds: int
    poll_interval_seconds: float
    http_timeout_seconds: float
    expected_prerelease_ordinal: int | None = None

    def __post_init__(self) -> None:
        if self.runtime_kind is not RuntimeKind.KODI:
            raise ValueError("runtime_kind is not supported")
        for field_name in ("runtime_uid", "runtime_gid"):
            value = getattr(self, field_name)
            if type(value) is not int or not 0 <= value <= 2**31 - 1:
                raise ValueError(f"{field_name} must be a POSIX ID from 0 to 2^31 - 1")
        if not isinstance(self.python_bytecode_policy, PythonBytecodePolicy):
            raise ValueError("python_bytecode_policy is not supported")
        if self.expected_application_name != "Kodi":
            raise ValueError("expected_application_name must be Kodi")
        if (
            re.fullmatch(r"[1-9][0-9]*\.(?:0|[1-9][0-9]*)", self.expected_kodi_version)
            is None
        ):
            raise ValueError("expected_kodi_version must be canonical major.minor")
        try:
            channel = KodiReleaseChannel(self.expected_version_tag)
        except ValueError as error:
            raise ValueError(
                "expected_version_tag is not a recognized channel"
            ) from error
        major, minor = (int(part) for part in self.expected_kodi_version.split("."))
        KodiReleaseIdentity(
            major,
            minor,
            channel=channel,
            prerelease_ordinal=self.expected_prerelease_ordinal,
        )
        if not 1 <= self.jsonrpc_port <= 65535:
            raise ValueError("jsonrpc_port must be between 1 and 65535")
        if (
            not self.jsonrpc_path.startswith("/")
            or len(self.jsonrpc_path) > 64
            or any(character.isspace() for character in self.jsonrpc_path)
        ):
            raise ValueError("jsonrpc_path must be a bounded absolute path")
        if self.process_observation is not ProcessObservationKind.DOCKER_HEALTHCHECK:
            raise ValueError("process_observation is not supported")
        if self.readiness_probe is not ReadinessProbeKind.KODI_JSONRPC:
            raise ValueError("readiness_probe is not supported")
        if not 1 <= self.readiness_timeout_seconds <= 600:
            raise ValueError("readiness_timeout_seconds must be between 1 and 600")
        if not 0.1 <= self.poll_interval_seconds <= 10.0:
            raise ValueError("poll_interval_seconds must be between 0.1 and 10")
        if self.poll_interval_seconds >= self.readiness_timeout_seconds:
            raise ValueError("poll_interval_seconds must be below readiness timeout")
        if not 0.1 <= self.http_timeout_seconds <= 5.0:
            raise ValueError("http_timeout_seconds must be between 0.1 and 5")

    @property
    def kodi_release(self) -> KodiReleaseIdentity:
        major, minor = (int(part) for part in self.expected_kodi_version.split("."))
        return KodiReleaseIdentity(
            major,
            minor,
            KodiReleaseChannel(self.expected_version_tag),
            self.expected_prerelease_ordinal,
        )


@dataclass(frozen=True, slots=True)
class RuntimeValidationEvidence:
    """Append-only evidence for one immutable runtime revision."""

    id: UUID
    runtime_revision_id: UUID
    contract_version: str
    result: RuntimeValidationResult
    started_at: datetime
    completed_at: datetime
    evidence_reference: str
    recorded_at: datetime
    summary: str | None = None

    def __post_init__(self) -> None:
        if _VALIDATION_CONTRACT_PATTERN.fullmatch(self.contract_version) is None:
            raise ValueError("contract_version must be a bounded canonical identifier")
        if not 1 <= len(self.evidence_reference) <= 512:
            raise ValueError("evidence_reference must contain 1 to 512 characters")
        _require_nonblank(self.evidence_reference, "evidence_reference")
        if self.summary is not None:
            if len(self.summary) > 512:
                raise ValueError("summary must not exceed 512 characters")
            _require_nonblank(self.summary, "summary")
        started = _normalize_timestamp(self.started_at, "started_at")
        completed = _normalize_timestamp(self.completed_at, "completed_at")
        recorded = _normalize_timestamp(self.recorded_at, "recorded_at")
        if completed < started:
            raise ValueError("completed_at must not precede started_at")
        if recorded < completed:
            raise ValueError("recorded_at must not precede completed_at")
        object.__setattr__(self, "started_at", started)
        object.__setattr__(self, "completed_at", completed)
        object.__setattr__(self, "recorded_at", recorded)


def effective_validation_state(
    evidence: RuntimeValidationEvidence | None,
) -> EffectiveValidationState:
    if evidence is None:
        return EffectiveValidationState.UNVALIDATED
    if evidence.result is RuntimeValidationResult.PASSED:
        return EffectiveValidationState.VALIDATED
    return EffectiveValidationState.FAILED
