"""Administrator-only household coordination over existing production trust."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, SecretStr, StrictBool
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.external_bridges import (
    ExternalBridgeService,
    ExternalBridgeServiceError,
    ExternalBridgeStatus,
    ProductionHttpAcknowledgementBinding,
)
from kodi_lab.application.external_onboarding import (
    ExternalKodiOnboardingError,
    ExternalKodiOnboardingService,
)
from kodi_lab.application.external_targets import (
    ExternalTargetAdministrationService,
    ExternalTargetError,
    ExternalTargetRead,
)
from kodi_lab.application.mcp_administration import McpAdministrationService
from kodi_lab.application.production_inspection import (
    PRODUCTION_TRANSPORT_POLICY_REVISION,
    PRODUCTION_VISUAL_PRESET_ID,
    ProductionInspectionError,
    evaluate_production_inspection_state,
)
from kodi_lab.application.screenshot_preferences import (
    ScreenshotPreferenceError,
    read_screenshot_preference,
)
from kodi_lab.domain import (
    ExternalAdministrativeState,
    ExternalAuthenticationMode,
    ExternalBridgePairingState,
    ExternalProtocolKind,
    ExternalVerificationState,
    ProductionDesignationState,
    TargetType,
)
from kodi_lab.storage.models import (
    ExternalKodiTargetRow,
    HouseholdOnboardingRow,
    McpTargetPresetAssignmentRow,
    TargetRow,
)
from kodi_lab.storage.secrets import SecretStoreError


class HouseholdStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation_id: UUID
    name: str = Field(min_length=1, max_length=120)
    address: str = Field(min_length=2, max_length=64)
    code: SecretStr = Field(repr=False)
    kodi_port: int = Field(default=8080, ge=1, le=65535, strict=True)
    bridge_port: int = Field(default=8765, ge=1, le=65535, strict=True)
    username: str | None = Field(default=None, min_length=1, max_length=256)
    password: SecretStr | None = Field(default=None, repr=False)


class HouseholdTransportDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    disclosure_token: str = Field(pattern="^[0-9a-f]{64}$")
    accept_http_risk: StrictBool


class HouseholdResumeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: SecretStr | None = Field(default=None, repr=False)
    transport_decision: HouseholdTransportDecision | None = None


class HouseholdTransportDisclosure(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    disclosure_token: str
    kodi_endpoint: str
    bridge_endpoint: str
    message: str = (
        "These endpoints use unencrypted HTTP on your private network. Kodi login "
        "credentials, library and UI information, and screenshot pixels can be "
        "observed in transit. Bridge requests authenticate with a persistent "
        "credential. Pairing codes and Bridge credentials also travel without "
        "encryption. HTTP does not protect confidentiality or prevent all "
        "active network attacks. Continue only on a network you trust. This "
        "decision authorizes read-only onboarding and does not enable control."
    )


class HouseholdOnboardingResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    operation_id: UUID
    state: Literal["connected", "incomplete", "requires_action", "failed"]
    target_id: UUID | None = None
    name: str | None = None
    code: str
    message: str
    transport: HouseholdTransportDisclosure | None = None
    existing_target_id: UUID | None = None
    existing_target_class: (
        Literal["external_development", "external_production"] | None
    ) = None
    existing_operation_id: UUID | None = None


_MESSAGES = {
    "invalid_name": "Enter a friendly name of at most 120 characters.",
    "invalid_address": "Enter a private Kodi IP address allowed by Kodi Lab.",
    "invalid_port": "Select Kodi and Bridge ports allowed by Kodi Lab.",
    "otp_invalid_format": "Enter the four-character pairing code shown in Bridge.",
    "invalid_kodi_credentials": "Enter both the Kodi username and password.",
    "connected": "Connected.",
    "operation_in_progress": "Connect is still running. Resume this operation shortly.",
    "operation_not_found": "This Connect operation was not found.",
    "operation_conflict": "This operation ID belongs to a different Connect request.",
    "endpoint_conflict": (
        "This endpoint already has a target or Connect operation. Review "
        "it in administration."
    ),
    "name_conflict": "Choose a different friendly name.",
    "transport_consent_required": (
        "Review the HTTP disclosure and explicitly accept it to continue."
    ),
    "transport_disclosure_changed": (
        "The verified target changed. Review the current HTTP disclosure."
    ),
    "otp_required": "Enter the current four-character code shown in Bridge.",
    "wrong_code": "Code not accepted. Enter the current code shown in Bridge.",
    "pairing_code_expired": "Code expired. Generate a new code in Kodi and resume.",
    "pairing_code_unavailable": "Generate a new pairing code in Kodi and resume.",
    "already_paired": (
        "Bridge is paired to another transaction or Kodi Lab. Abandon this "
        "operation and explicitly reset Bridge pairing in Kodi before retrying."
    ),
    "pairing_temporarily_locked": "Wait for Bridge's cooldown, then generate a code.",
    "bridge_protocol_unsupported": (
        "Install a compatible Kodi Lab Bridge from the repository."
    ),
    "bridge_credential_store_unavailable": (
        "Configure Kodi Lab's credential encryption key before resuming Connect."
    ),
    "secret_unavailable": (
        "Kodi credentials could not be stored. Check Kodi Lab's credential "
        "encryption key and protected storage, then retry Connect with the "
        "same operation ID and Kodi credentials."
    ),
    "registration_retry_required": (
        "Retry Connect with the same operation ID and Kodi credentials."
    ),
    "bridge_build_not_reviewed": (
        "This Bridge build is not recognized. Install a compatible Kodi Lab Bridge "
        "from the Kodi Lab repository; see Advanced details for older or custom builds."
    ),
    "pairing_recovery_required": (
        "The bootstrap outcome is uncertain and no recoverable credential "
        "is available. Abandon this operation, reset Bridge pairing in "
        "Kodi, then start a new Connect operation."
    ),
    "identity_mismatch": (
        "Kodi or Bridge identity changed. Review the target; this "
        "operation cannot adopt the replacement."
    ),
    "administration_changed": (
        "Target administration changed. Review the target before starting again."
    ),
    "onboarding_binding_changed": (
        "Kodi's endpoint, verified identity or policy changed. Abandon this "
        "operation and review the target before starting again."
    ),
    "abandoned": (
        "Connect was abandoned. Reset Bridge pairing in Kodi before starting again."
    ),
    "persistence_failed": (
        "Connect could not save its progress. Resume the same operation ID."
    ),
}
_RETRYABLE = {
    "bridge_unreachable",
    "target_unreachable",
    "authentication_failed",
    "wrong_code",
    "pairing_code_expired",
    "pairing_code_unavailable",
    "pairing_temporarily_locked",
    "otp_invalid_format",
    "already_paired",
    "bridge_build_not_reviewed",
    "bridge_protocol_unsupported",
    "bridge_credential_store_unavailable",
    "bootstrap_version_mismatch",
}


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


class HouseholdKodiOnboardingService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        targets: ExternalTargetAdministrationService,
        bridges: ExternalBridgeService,
        validation: ExternalKodiOnboardingService,
        administration: McpAdministrationService,
        lock_root: Path,
    ) -> None:
        self._sessions = sessions
        self._targets = targets
        self._bridges = bridges
        self._validation = validation
        self._administration = administration
        self._lock_root = lock_root

    @contextmanager
    def _lock(self, operation_id: UUID) -> Iterator[bool]:
        # Same data-root is required for all Web workers using this database.
        # Never unlink lock files: unlink/recreate could admit overlapping workers.
        self._lock_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(
            self._lock_root / f"{operation_id}.lock",
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
            else:
                yield True
        finally:
            os.close(descriptor)

    def start(self, request: HouseholdStartRequest) -> HouseholdOnboardingResult:
        operation_id = request.operation_id
        try:
            name, host, mode = self._validation._validate(
                request.name,
                request.address,
                request.code.get_secret_value(),
                request.kodi_port,
                request.bridge_port,
                request.username,
                None
                if request.password is None
                else request.password.get_secret_value(),
            )
            request_digest = _digest(
                [
                    name,
                    host,
                    request.kodi_port,
                    request.bridge_port,
                    mode.value,
                    request.username,
                ]
            )
            with self._lock(operation_id) as acquired:
                if not acquired:
                    return self._result(
                        operation_id, "incomplete", "operation_in_progress"
                    )
                with self._sessions.begin() as session:
                    session.connection().exec_driver_sql("BEGIN IMMEDIATE")
                    row = session.get(HouseholdOnboardingRow, str(operation_id))
                    if row is not None:
                        if row.request_digest != request_digest:
                            return self._result(
                                operation_id, "failed", "operation_conflict"
                            )
                    else:
                        conflict = self._existing_connection(
                            session, operation_id, host, request.kodi_port
                        )
                        if conflict is not None:
                            return conflict
                        if (
                            session.scalar(
                                select(TargetRow.id)
                                .join(
                                    ExternalKodiTargetRow,
                                    ExternalKodiTargetRow.target_id == TargetRow.id,
                                )
                                .where(
                                    TargetRow.name == name,
                                    ExternalKodiTargetRow.administrative_state
                                    != ExternalAdministrativeState.DEACTIVATED,
                                )
                            )
                            is not None
                        ):
                            return self._result(operation_id, "failed", "name_conflict")
                        row = HouseholdOnboardingRow(
                            id=str(operation_id),
                            target_id=str(uuid4()),
                            name=name,
                            host=host,
                            kodi_port=request.kodi_port,
                            bridge_port=request.bridge_port,
                            request_digest=request_digest,
                            stage="reserved",
                            error_code=None,
                            disclosure_token=None,
                            created_by="web_admin",
                            created_at=_now(),
                            updated_at=_now(),
                        )
                        session.add(row)
                return self._advance(
                    operation_id,
                    HouseholdResumeRequest(code=request.code),
                    start=request,
                )
        except ExternalKodiOnboardingError as error:
            return self._result(operation_id, "failed", error.code)
        except (SQLAlchemyError, OSError):
            return self._result(operation_id, "incomplete", "persistence_failed")

    def resume(
        self, operation_id: UUID, request: HouseholdResumeRequest
    ) -> HouseholdOnboardingResult:
        try:
            with self._lock(operation_id) as acquired:
                if not acquired:
                    return self._result(
                        operation_id, "incomplete", "operation_in_progress"
                    )
                return self._advance(operation_id, request)
        except (SQLAlchemyError, OSError):
            return self._result(operation_id, "incomplete", "persistence_failed")

    def _row(self, operation_id: UUID) -> HouseholdOnboardingRow | None:
        with self._sessions() as session:
            return session.get(HouseholdOnboardingRow, str(operation_id))

    def _existing_connection(
        self, session: Session, operation_id: UUID, host: str, port: int
    ) -> HouseholdOnboardingResult | None:
        reservation = session.scalar(
            select(HouseholdOnboardingRow).where(
                HouseholdOnboardingRow.host == host,
                HouseholdOnboardingRow.kodi_port == port,
                HouseholdOnboardingRow.stage != "abandoned",
            )
        )
        targets = session.execute(
            select(TargetRow.id, TargetRow.target_type)
            .join(
                ExternalKodiTargetRow, ExternalKodiTargetRow.target_id == TargetRow.id
            )
            .where(
                ExternalKodiTargetRow.host == host,
                ExternalKodiTargetRow.port == port,
                ExternalKodiTargetRow.administrative_state
                != ExternalAdministrativeState.DEACTIVATED,
                TargetRow.target_type.in_(
                    (TargetType.EXTERNAL_DEVELOPMENT, TargetType.EXTERNAL_PRODUCTION)
                ),
            )
            .limit(2)
        ).all()
        if reservation is None and not targets:
            return None
        # A shared endpoint or conflicting reservation cannot select a device by
        # name or guess which registration the administrator intended.
        unambiguous = len(targets) <= 1 and (
            reservation is None or not targets or targets[0].id == reservation.target_id
        )
        existing = targets[0] if unambiguous and len(targets) == 1 else None
        resumable = (
            reservation is not None
            and unambiguous
            and reservation.stage
            in {"reserved", "native_verified", "bootstrap_started", "paired"}
        )
        if resumable and reservation is not None:
            native = session.get(ExternalKodiTargetRow, reservation.target_id)
            resumable = (
                native is None
                or native.administrative_state
                is not ExternalAdministrativeState.DEACTIVATED
            )
        return HouseholdOnboardingResult(
            operation_id=operation_id,
            state="failed",
            code="endpoint_conflict",
            message=_MESSAGES["endpoint_conflict"],
            existing_target_id=None if existing is None else UUID(existing.id),
            existing_target_class=(
                None
                if existing is None
                else "external_development"
                if existing.target_type is TargetType.EXTERNAL_DEVELOPMENT
                else "external_production"
            ),
            existing_operation_id=(
                UUID(reservation.id) if resumable and reservation is not None else None
            ),
        )

    def _save(self, operation_id: UUID, stage: str, code: str | None = None) -> None:
        with self._sessions.begin() as session:
            row = session.get(HouseholdOnboardingRow, str(operation_id))
            assert row is not None
            row.stage, row.error_code, row.updated_at = stage, code, _now()

    def _advance(
        self,
        operation_id: UUID,
        request: HouseholdResumeRequest,
        *,
        start: HouseholdStartRequest | None = None,
    ) -> HouseholdOnboardingResult:
        row = self._row(operation_id)
        if row is None:
            return self._result(operation_id, "failed", "operation_not_found")
        if row.stage in {"blocked", "abandoned"}:
            return self._result(
                operation_id, "requires_action", row.error_code or "abandoned", row
            )
        target_id = UUID(row.target_id)
        bootstrap_attempt = False
        try:
            if row.stage == "reserved":
                with self._sessions() as session:
                    exists = session.get(TargetRow, row.target_id) is not None
                if not exists:
                    if start is None:
                        return self._result(
                            operation_id,
                            "requires_action",
                            "registration_retry_required",
                        )
                    try:
                        self._targets.register(
                            logical_name=row.name,
                            protocol_kind=ExternalProtocolKind.JSONRPC_HTTP,
                            host=row.host,
                            port=row.kodi_port,
                            authentication_mode=(
                                ExternalAuthenticationMode.NONE
                                if start.username is None
                                else ExternalAuthenticationMode.USERNAME_PASSWORD
                            ),
                            configured_capabilities={
                                "target_inspect",
                                "addon_inventory",
                                "screenshot",
                            },
                            username=start.username,
                            password=None
                            if start.password is None
                            else start.password.get_secret_value(),
                            _household_operation_id=operation_id,
                        )
                    except ExternalTargetError as error:
                        if error.code != "secret_unavailable":
                            raise
                        # Registration did not publish a target. Keep its durable
                        # request/reservation recoverable, without claiming identity
                        # drift or exposing the internally reserved target UUID.
                        return self._result(operation_id, "incomplete", error.code)
                target = self._targets.get(target_id)
                if target.verification_state is ExternalVerificationState.UNVERIFIED:
                    target = self._targets.probe(target_id)
                    if target.pending_observation_id is None:
                        raise ExternalTargetError("identity_mismatch")
                    target = self._targets.confirm(
                        target_id, target.pending_observation_id
                    )
                policy = target.production_policy
                if (
                    policy is not None
                    and policy.designation_state
                    is ProductionDesignationState.PENDING_CONFIRMATION
                ):
                    assert target.pending_observation_id is not None
                    self._targets.confirm_production_designation(
                        target_id,
                        target.pending_observation_id,
                        administrator_reference="web_admin",
                    )
                if target.administrative_state is ExternalAdministrativeState.DISABLED:
                    self._targets.set_enabled(target_id, enabled=True)
                # Assigning the maximum advances native policy revision. Complete
                # this coordinator-owned change before binding and disclosing it.
                with self._sessions() as session:
                    assignment = session.get(
                        McpTargetPresetAssignmentRow, row.target_id
                    )
                    if assignment is not None and assignment.preset_id != str(
                        PRODUCTION_VISUAL_PRESET_ID
                    ):
                        raise ExternalTargetError("administration_changed")
                if assignment is None:
                    self._administration.assign_target_preset(
                        target_id, PRODUCTION_VISUAL_PRESET_ID
                    )
                self._bind_native(operation_id, self._targets.get(target_id))
                refreshed = self._row(operation_id)
                assert refreshed is not None
                row = refreshed
            target = self._targets.get(target_id)
            self._require_binding(row, target)
            self._targets.verify_current_identity(target_id)
            if row.stage == "native_verified":
                disclosure, expected = self._disclosure(row)
                if (
                    request.transport_decision is None
                    or not request.transport_decision.accept_http_risk
                ):
                    return self._result(
                        operation_id,
                        "requires_action",
                        "transport_consent_required",
                        row,
                        disclosure,
                    )
                if (
                    request.transport_decision.disclosure_token
                    != disclosure.disclosure_token
                    or row.disclosure_token != disclosure.disclosure_token
                ):
                    return self._result(
                        operation_id,
                        "requires_action",
                        "transport_disclosure_changed",
                        row,
                        disclosure,
                    )
                self._bridges.acknowledge_production_http_risk(
                    target_id,
                    bridge_port=row.bridge_port,
                    administrator_reference="web_admin",
                    expected=expected,
                )
                self._require_binding(row, self._targets.get(target_id))
                if request.code is None:
                    return self._result(
                        operation_id, "requires_action", "otp_required", row, disclosure
                    )
                # Durable before dispatch. Recovery never sends another bootstrap
                # merely because no credential/pairing row appeared afterward.
                self._save(operation_id, "bootstrap_started")
                row.stage = "bootstrap_started"
                bootstrap_attempt = True
                self._bridges.begin_otp_pairing(
                    target_id,
                    bridge_port=row.bridge_port,
                    code=request.code.get_secret_value(),
                )
                bootstrap_attempt = False
            bridge = self._bridges.status(target_id)
            if bridge.pairing_state not in {
                ExternalBridgePairingState.PAIRING_PENDING,
                ExternalBridgePairingState.PAIRED,
            }:
                return self._result(
                    operation_id, "requires_action", "pairing_recovery_required", row
                )
            self._bridges.commit_otp_pairing(target_id)
            if row.stage != "connected":
                self._save(operation_id, "paired")
            with self._sessions() as session:
                assignment = session.get(McpTargetPresetAssignmentRow, row.target_id)
                if assignment is None or assignment.preset_id != str(
                    PRODUCTION_VISUAL_PRESET_ID
                ):
                    raise ExternalTargetError("administration_changed")
            # New external registration initialized the independent preference.
            # Never reset a subsequent administrator decision on a retry.
            with self._sessions() as session:
                read_screenshot_preference(session, target_id)
                evaluate_production_inspection_state(
                    session,
                    target_id,
                    required_capabilities=frozenset({"target_inspect"}),
                    _allow_household_onboarding=True,
                )
            final_native = self._targets.verify_current_identity(target_id)
            final_bridge = self._bridges.commit_otp_pairing(target_id)
            self._complete(operation_id, final_native, final_bridge)
            return self._result(operation_id, "connected", "connected", row)
        except ExternalBridgeServiceError as error:
            # Lifecycle checks can reject an edit before the acknowledgement's
            # expected-state comparison. Report binding drift, not OTP uncertainty.
            try:
                self._require_binding(row, self._targets.get(target_id))
            except ExternalTargetError as changed:
                return self._block(operation_id, row, changed.code)
            if error.code == "transport_binding_changed":
                return self._block(operation_id, row, "onboarding_binding_changed")
            if error.code in _RETRYABLE and bootstrap_attempt and error.otp_retry_safe:
                self._save(operation_id, "native_verified", error.code)
                return self._result(operation_id, "requires_action", error.code, row)
            if error.code in {
                "bridge_build_not_reviewed",
                "transport_acknowledgement_required",
            }:
                return self._result(operation_id, "requires_action", error.code, row)
            if error.code in {
                "bridge_identity_mismatch",
                "needs_reverification",
                "bridge_authentication_failed",
            }:
                if (
                    error.code != "bridge_identity_mismatch"
                    and self._bridges.status(target_id).pairing_state
                    is ExternalBridgePairingState.UNPAIRED
                ):
                    return self._result(
                        operation_id,
                        "requires_action",
                        "pairing_recovery_required",
                        row,
                    )
                return self._block(operation_id, row, "identity_mismatch")
            if (
                bootstrap_attempt
                and not error.otp_retry_safe
                and self._bridges.status(target_id).pairing_state
                is ExternalBridgePairingState.UNPAIRED
            ):
                return self._result(
                    operation_id, "requires_action", "pairing_recovery_required", row
                )
            return self._result(
                operation_id, "incomplete", "pairing_commit_failed", row
            )
        except (
            ExternalTargetError,
            ProductionInspectionError,
            ScreenshotPreferenceError,
        ) as error:
            if error.code in {"target_unreachable", "authentication_failed"}:
                return self._result(operation_id, "incomplete", error.code, row)
            return self._block(
                operation_id,
                row,
                error.code
                if error.code
                in {"administration_changed", "onboarding_binding_changed"}
                else "identity_mismatch",
            )

    @staticmethod
    def _native_digest(row: HouseholdOnboardingRow, target: ExternalTargetRead) -> str:
        if (
            str(target.target_id) != row.target_id
            or target.host != row.host
            or target.port != row.kodi_port
            or target.logical_name != row.name
            or target.target_class != "external_production"
            or target.protocol_kind is not ExternalProtocolKind.JSONRPC_HTTP
            or target.production_policy is None
            or not target.production_policy.mutations_suspended
            or target.production_policy.designation_state
            is not ProductionDesignationState.CONFIRMED
            or set(target.configured_capabilities)
            != {"target_inspect", "addon_inventory", "screenshot"}
            or target.verification_state is not ExternalVerificationState.VERIFIED
        ):
            raise ExternalTargetError("onboarding_binding_changed")
        if target.administrative_state is not ExternalAdministrativeState.ENABLED:
            raise ExternalTargetError("administration_changed")
        return _digest(
            [
                row.target_id,
                row.host,
                row.kodi_port,
                row.bridge_port,
                target.production_policy.identity_generation,
                target.production_policy.policy_revision,
                target.production_policy.confirmed_observation_digest,
            ]
        )

    def _require_binding(
        self, row: HouseholdOnboardingRow, target: ExternalTargetRead
    ) -> None:
        if row.native_binding_digest != self._native_digest(row, target):
            raise ExternalTargetError("onboarding_binding_changed")

    def _bind_native(self, operation_id: UUID, target: ExternalTargetRead) -> None:
        with self._sessions.begin() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            row = session.get(HouseholdOnboardingRow, str(operation_id))
            assert row is not None
            if row.stage != "reserved" or self._targets.get(target.target_id) != target:
                raise ExternalTargetError("onboarding_binding_changed")
            digest = self._native_digest(row, target)
            if row.native_binding_digest not in {None, digest}:
                raise ExternalTargetError("onboarding_binding_changed")
            row.native_binding_digest = digest
            row.stage, row.error_code, row.updated_at = "native_verified", None, _now()

    @staticmethod
    def _transport_token(
        row: HouseholdOnboardingRow,
        target: ExternalTargetRead,
        bridge: ExternalBridgeStatus,
    ) -> str:
        assert target.production_policy is not None
        return _digest(
            [
                row.id,
                row.target_id,
                row.host,
                row.kodi_port,
                row.bridge_port,
                target.production_policy.identity_generation,
                target.production_policy.policy_revision,
                target.production_policy.confirmed_observation_digest,
                bridge.production_bridge_trust_revision or 1,
                PRODUCTION_TRANSPORT_POLICY_REVISION,
            ]
        )

    def _disclosure(
        self, row: HouseholdOnboardingRow
    ) -> tuple[HouseholdTransportDisclosure, ProductionHttpAcknowledgementBinding]:
        with self._sessions.begin() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            persisted = session.get(HouseholdOnboardingRow, row.id)
            assert persisted is not None
            target = self._targets.get(UUID(row.target_id))
            bridge = self._bridges.status(UUID(row.target_id))
            self._require_binding(persisted, target)
            assert target.production_policy is not None
            expected = ProductionHttpAcknowledgementBinding(
                host=target.host,
                native_port=target.port,
                bridge_port=row.bridge_port,
                identity_generation=target.production_policy.identity_generation,
                policy_revision=target.production_policy.policy_revision,
                confirmed_observation_digest=(
                    target.production_policy.confirmed_observation_digest
                ),
                bridge_trust_revision=bridge.production_bridge_trust_revision or 1,
            )
            token = self._transport_token(persisted, target, bridge)
            persisted.disclosure_token = token
        host = f"[{row.host}]" if ":" in row.host else row.host
        return (
            HouseholdTransportDisclosure(
                disclosure_token=token,
                kodi_endpoint=f"http://{host}:{row.kodi_port}",
                bridge_endpoint=f"http://{host}:{row.bridge_port}",
            ),
            expected,
        )

    def _complete(
        self,
        operation_id: UUID,
        native: ExternalTargetRead,
        bridge: ExternalBridgeStatus,
    ) -> None:
        # Final admission opens atomically only for the freshly verified bindings.
        with self._sessions.begin() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            row = session.get(HouseholdOnboardingRow, str(operation_id))
            assert row is not None
            self._require_binding(row, native)
            if row.disclosure_token != self._transport_token(row, native, bridge):
                raise ExternalTargetError("onboarding_binding_changed")
            assignment = session.get(McpTargetPresetAssignmentRow, row.target_id)
            if (
                self._targets.get(native.target_id) != native
                or self._bridges.status(native.target_id) != bridge
                or assignment is None
                or assignment.preset_id != str(PRODUCTION_VISUAL_PRESET_ID)
            ):
                raise ExternalTargetError("administration_changed")
            evaluate_production_inspection_state(
                session,
                native.target_id,
                required_capabilities=frozenset({"target_inspect"}),
                _allow_household_onboarding=True,
            )
            row.stage, row.error_code, row.updated_at = "connected", None, _now()

    def _block(
        self, operation_id: UUID, row: HouseholdOnboardingRow, code: str
    ) -> HouseholdOnboardingResult:
        self._save(operation_id, "blocked", code)
        target_id = UUID(row.target_id)
        with self._sessions() as session:
            exists = session.get(TargetRow, row.target_id) is not None
        if exists and (
            self._targets.get(target_id).administrative_state
            is not ExternalAdministrativeState.DEACTIVATED
        ):
            self._targets.set_enabled(target_id, enabled=False)
        return self._result(
            operation_id, "requires_action", code, row if exists else None
        )

    def abandon(self, operation_id: UUID) -> HouseholdOnboardingResult:
        """Explicit local retirement; no remote reset or device mutation."""
        try:
            with self._lock(operation_id) as acquired:
                if not acquired:
                    return self._result(
                        operation_id, "incomplete", "operation_in_progress"
                    )
                row = self._row(operation_id)
                if row is None:
                    return self._result(operation_id, "failed", "operation_not_found")
                with self._sessions() as session:
                    exists = session.get(TargetRow, row.target_id) is not None
                if row.stage == "connected" and (
                    not exists
                    or self._targets.get(UUID(row.target_id)).administrative_state
                    is not ExternalAdministrativeState.DEACTIVATED
                ):
                    return self._result(
                        operation_id, "requires_action", "administration_changed", row
                    )
                if row.stage != "abandoned":
                    if exists and (
                        self._targets.get(UUID(row.target_id)).administrative_state
                        is not ExternalAdministrativeState.DEACTIVATED
                    ):
                        try:
                            self._bridges.revoke(UUID(row.target_id))
                        except ExternalBridgeServiceError as error:
                            if error.code != "bridge_not_paired":
                                raise
                        self._targets.deactivate(UUID(row.target_id))
                    # Deactivation closes new pairing publication. Retire any
                    # residual authority admitted between revoke and deactivate,
                    # and retry both stores' previously interrupted deletions.
                    self._bridges.invalidate_production_native_identity(
                        UUID(row.target_id)
                    )
                    self._targets.retire_deactivated_credentials(UUID(row.target_id))
                self._save(operation_id, "abandoned", "abandoned")
                return self._result(operation_id, "requires_action", "abandoned", row)
        except (
            SQLAlchemyError,
            OSError,
            ExternalTargetError,
            ExternalBridgeServiceError,
            SecretStoreError,
        ):
            return self._result(operation_id, "incomplete", "persistence_failed")

    def finish_retired_connection(self, target_id: UUID) -> None:
        """Release a retired registration's reservation after protected cleanup.

        The durable operation and its target identity remain available for audit.
        An in-flight Connect keeps its reservation until explicit cleanup retries.
        """
        with self._sessions() as session:
            operation_id = session.scalar(
                select(HouseholdOnboardingRow.id).where(
                    HouseholdOnboardingRow.target_id == str(target_id)
                )
            )
        if operation_id is None:
            return
        target = self._targets.get(target_id)
        if target.administrative_state is not ExternalAdministrativeState.DEACTIVATED:
            raise ExternalTargetError("verification_required")
        result = self.abandon(UUID(operation_id))
        if result.code != "abandoned":
            raise ExternalTargetError("connection_retirement_incomplete")

    @staticmethod
    def _result(
        operation_id: UUID,
        state: Literal["connected", "incomplete", "requires_action", "failed"],
        code: str,
        row: HouseholdOnboardingRow | None = None,
        transport: HouseholdTransportDisclosure | None = None,
    ) -> HouseholdOnboardingResult:
        return HouseholdOnboardingResult(
            operation_id=operation_id,
            state=state,
            code=code,
            message=_MESSAGES.get(
                code,
                "Connect could not finish. Check Kodi and Bridge, "
                "then resume this operation.",
            ),
            target_id=None if row is None else UUID(row.target_id),
            name=None if row is None else row.name,
            transport=transport,
        )
