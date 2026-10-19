"""Guided Web reconnect of one retained external registration and policy."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.external_bridges import (
    PRODUCTION_TRANSPORT_POLICY_REVISION,
    ExternalBridgeService,
    ExternalBridgeServiceError,
    ProductionHttpAcknowledgementBinding,
)
from kodi_lab.application.external_targets import (
    ExternalTargetAdministrationService,
    ExternalTargetError,
    ExternalTargetRead,
)
from kodi_lab.application.household_onboarding import (
    HouseholdOnboardingResult,
    HouseholdResumeRequest,
    HouseholdTransportDisclosure,
)
from kodi_lab.domain import (
    ExternalAdministrativeState,
    ExternalBridgePairingState,
    ExternalVerificationState,
    TargetType,
)
from kodi_lab.storage.models import (
    ExternalBridgePairingRow,
    ExternalKodiTargetRow,
    ExternalProductionPolicyRow,
    ExternalReconnectOperationRow,
    HouseholdOnboardingRow,
    McpTargetPresetAssignmentRow,
    TargetRow,
)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class ReconnectStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: UUID
    bridge_port: int | None = Field(default=None, ge=1, le=65535, strict=True)


class ReconnectConfirmation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmed: Literal[True]


class ReconnectResult(HouseholdOnboardingResult):
    target_class: Literal["external_development", "external_production"] | None = None


class ReconnectError(RuntimeError):
    pass


_MESSAGES = {
    "connected": (
        "Connection restored using this Kodi's existing access settings. Agent "
        "grants were not changed."
    ),
    "transport_consent_required": (
        "Review the current connection details and accept the HTTP risks before "
        "entering a current Bridge code."
    ),
    "transport_disclosure_changed": (
        "The connection disclosure changed. Review the current details and accept "
        "them again."
    ),
    "otp_required": ("Enter the current four-character code shown in Kodi Lab Bridge."),
    "wrong_code": (
        "The code was not accepted. Check the current code in Bridge and try "
        "again after reviewing the disclosure."
    ),
    "pairing_code_expired": (
        "Generate a fresh code in Bridge and review the disclosure to continue."
    ),
    "pairing_code_unavailable": (
        "Generate a pairing code in Kodi Lab Bridge to continue."
    ),
    "pairing_temporarily_locked": (
        "Bridge temporarily blocked pairing attempts. Wait, then explicitly resume."
    ),
    "replace_pairing_required": (
        "The saved pairing needs replacement. Explicitly revoke its local Lab "
        "authority below, then review a fresh disclosure. A Kodi-side pairing "
        "reset may also be needed."
    ),
    "pairing_recovery_required": (
        "The code may have been consumed. Resume without a code to recover saved "
        "credentials. If no credential was retained, cancel this operation and "
        "reset pairing locally in Kodi before starting again."
    ),
    "pairing_commit_failed": (
        "The connection outcome is incomplete. Resume this saved operation "
        "without another code."
    ),
    "connection_disabled": (
        "This connection is disabled. Enable it explicitly in its settings before "
        "reconnecting."
    ),
    "connection_retired": (
        "This registration is permanently retired. Add a new connection instead."
    ),
    "verification_required": (
        "Review and verify this Kodi's native identity in its settings before "
        "reconnecting."
    ),
    "saved_connection_incomplete": (
        "This Kodi has an unfinished Add Kodi operation. Resume that saved "
        "connection from Add Kodi before using reconnect."
    ),
    "onboarding_binding_changed": (
        "The connection identity or access settings changed. Cancel this "
        "operation, review the current settings and start a fresh reconnect."
    ),
    "operation_conflict": (
        "This operation identifier belongs to another reconnect request."
    ),
    "operation_not_found": (
        "No saved reconnect operation was found. Start a new reconnect for this "
        "registration."
    ),
    "operation_in_progress": (
        "Reconnect is still running. Wait briefly and resume the same operation."
    ),
    "bridge_unreachable": (
        "Bridge could not be reached. Check Kodi and the saved Bridge port, then "
        "resume."
    ),
    "target_unreachable": (
        "Kodi could not be reached. Check its address and web interface, then resume."
    ),
    "authentication_failed": (
        "Kodi rejected its saved sign-in. Update its native credentials in "
        "connection settings, then start a fresh reconnect."
    ),
    "bridge_build_not_reviewed": (
        "Install a compatible official Kodi Lab Bridge, or review this custom "
        "build in Advanced administration."
    ),
    "cancelled": (
        "Reconnect was cancelled. The registration and access settings were "
        "retained. Reset pairing locally in Kodi if Bridge still reports it "
        "paired."
    ),
    "persistence_failed": (
        "Progress could not be confirmed. Resume this operation without "
        "submitting another code."
    ),
}


class ExternalReconnectService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        development: ExternalTargetAdministrationService,
        production: ExternalTargetAdministrationService,
        bridges: ExternalBridgeService,
        lock_root: Path,
    ) -> None:
        self._sessions, self._development, self._production = (
            sessions,
            development,
            production,
        )
        self._bridges, self._lock_root = bridges, lock_root

    @contextmanager
    def _lock(self, target_id: UUID) -> Iterator[bool]:
        self._lock_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(
            self._lock_root / f"{target_id}.lock",
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
            else:
                yield True
        finally:
            os.close(fd)

    def target(self, target_id: UUID) -> ExternalTargetRead:
        return self._target_service(target_id).get(target_id)

    def _target_service(self, target_id: UUID) -> ExternalTargetAdministrationService:
        with self._sessions() as session:
            target = session.get(TargetRow, str(target_id))
        if target is None:
            raise ExternalTargetError("target_not_found")
        if target.target_type is TargetType.EXTERNAL_DEVELOPMENT:
            return self._development
        if target.target_type is TargetType.EXTERNAL_PRODUCTION:
            return self._production
        raise ExternalTargetError("target_not_found")

    def active_operation(self, target_id: UUID) -> ExternalReconnectOperationRow | None:
        with self._sessions() as session:
            return session.scalar(
                select(ExternalReconnectOperationRow).where(
                    ExternalReconnectOperationRow.target_id == str(target_id),
                    ExternalReconnectOperationRow.stage.not_in(
                        ("connected", "cancelled")
                    ),
                )
            )

    @staticmethod
    def _binding(session: Session, target_id: UUID) -> str:
        target = session.get(TargetRow, str(target_id))
        external = session.get(ExternalKodiTargetRow, str(target_id))
        if (
            target is None
            or external is None
            or target.target_type
            not in {TargetType.EXTERNAL_DEVELOPMENT, TargetType.EXTERNAL_PRODUCTION}
        ):
            raise ReconnectError("operation_conflict")
        if external.administrative_state is ExternalAdministrativeState.DEACTIVATED:
            raise ReconnectError("connection_retired")
        if external.administrative_state is not ExternalAdministrativeState.ENABLED:
            raise ReconnectError("connection_disabled")
        if external.verification_state is not ExternalVerificationState.VERIFIED:
            raise ReconnectError("verification_required")
        unfinished = session.scalar(
            select(HouseholdOnboardingRow.id).where(
                HouseholdOnboardingRow.target_id == str(target_id),
                HouseholdOnboardingRow.stage.not_in(("connected", "abandoned")),
            )
        )
        if unfinished is not None:
            raise ReconnectError("saved_connection_incomplete")
        policy = session.get(ExternalProductionPolicyRow, str(target_id))
        assignment = session.get(McpTargetPresetAssignmentRow, str(target_id))
        return _digest(
            [
                str(target_id),
                target.name,
                target.target_type.value,
                external.protocol_kind.value,
                external.host,
                external.port,
                external.authentication_mode.value,
                external.secret_reference,
                external.configured_capabilities_json,
                external.verified_capabilities_json,
                external.verified_addresses_json,
                external.verified_kodi_name,
                external.verified_kodi_version,
                external.observed_protocol_version,
                None if assignment is None else assignment.preset_id,
                None
                if policy is None
                else [
                    policy.identity_generation,
                    policy.policy_revision,
                    policy.confirmed_observation_digest,
                    policy.designation_state.value,
                    policy.mutations_suspended,
                ],
            ]
        )

    def _require_binding(
        self, row: ExternalReconnectOperationRow, session: Session
    ) -> None:
        if row.native_binding != self._binding(session, UUID(row.target_id)):
            raise ReconnectError("onboarding_binding_changed")

    def _row(
        self, target_id: UUID, operation_id: UUID
    ) -> ExternalReconnectOperationRow:
        with self._sessions() as session:
            row = session.get(ExternalReconnectOperationRow, str(operation_id))
        if row is None or row.target_id != str(target_id):
            raise ReconnectError("operation_not_found")
        return row

    def _result(
        self,
        target_id: UUID,
        operation_id: UUID,
        code: str,
        *,
        transport: HouseholdTransportDisclosure | None = None,
    ) -> ReconnectResult:
        target = self.target(target_id)
        return ReconnectResult(
            operation_id=operation_id,
            target_id=target_id,
            name=target.logical_name,
            target_class=cast(
                Literal["external_development", "external_production"],
                target.target_class,
            ),
            state="connected"
            if code == "connected"
            else "incomplete"
            if code
            in {
                "operation_in_progress",
                "persistence_failed",
                "pairing_commit_failed",
                "bridge_unreachable",
                "target_unreachable",
            }
            else "requires_action",
            code=code,
            message=_MESSAGES.get(
                code,
                "Reconnect needs attention. Review Advanced details, "
                "then explicitly resume this operation.",
            ),
            transport=transport,
        )

    def _save(
        self, row: ExternalReconnectOperationRow, stage: str, error: str | None = None
    ) -> None:
        with self._sessions.begin() as session:
            current = session.get(ExternalReconnectOperationRow, row.id)
            assert current is not None
            current.stage, current.error_code, current.updated_at = stage, error, _now()
        row.stage, row.error_code = stage, error

    def start(self, target_id: UUID, request: ReconnectStartRequest) -> ReconnectResult:
        try:
            with self._lock(target_id) as acquired:
                if not acquired:
                    return self._result(
                        target_id, request.operation_id, "operation_in_progress"
                    )
                with self._sessions.begin() as session:
                    session.connection().exec_driver_sql("BEGIN IMMEDIATE")
                    row = session.get(
                        ExternalReconnectOperationRow, str(request.operation_id)
                    )
                    if row is not None and row.target_id != str(target_id):
                        return self._result(
                            target_id, request.operation_id, "operation_conflict"
                        )
                    if row is None:
                        row = session.scalar(
                            select(ExternalReconnectOperationRow).where(
                                ExternalReconnectOperationRow.target_id
                                == str(target_id),
                                ExternalReconnectOperationRow.stage.not_in(
                                    ("connected", "cancelled")
                                ),
                            )
                        )
                    if row is None:
                        binding = self._binding(session, target_id)
                        external = session.get(ExternalKodiTargetRow, str(target_id))
                        target = session.get(TargetRow, str(target_id))
                        assert external is not None and target is not None
                        pairing = session.get(ExternalBridgePairingRow, str(target_id))
                        port = (
                            request.bridge_port
                            or (None if pairing is None else pairing.bridge_port)
                            or 8765
                        )
                        self._bridges._validate_bridge_endpoint(external, port)
                        row = ExternalReconnectOperationRow(
                            id=str(request.operation_id),
                            target_id=str(target_id),
                            target_class=target.target_type.value,
                            bridge_port=port,
                            stage="native_verified",
                            native_binding=binding,
                            disclosure_token=None,
                            expected_bridge_revision=self._bridges._prospective_production_revision(
                                pairing
                            ),
                            initial_bridge_reference=None
                            if pairing is None
                            else pairing.bridge_secret_reference,
                            owned_bridge_reference=None,
                            error_code=None,
                            created_at=_now(),
                            updated_at=_now(),
                        )
                        session.add(row)
                    elif (
                        request.bridge_port is not None
                        and request.bridge_port != row.bridge_port
                    ):
                        return self._result(
                            target_id, request.operation_id, "operation_conflict"
                        )
                return self._advance(row, HouseholdResumeRequest())
        except ReconnectError as error:
            return self._result(target_id, request.operation_id, str(error))
        except (ExternalTargetError, ExternalBridgeServiceError) as error:
            return self._result(target_id, request.operation_id, error.code)
        except (SQLAlchemyError, OSError):
            return self._result(target_id, request.operation_id, "persistence_failed")

    def resume(
        self, target_id: UUID, operation_id: UUID, request: HouseholdResumeRequest
    ) -> ReconnectResult:
        try:
            with self._lock(target_id) as acquired:
                if not acquired:
                    return self._result(
                        target_id, operation_id, "operation_in_progress"
                    )
                return self._advance(self._row(target_id, operation_id), request)
        except ReconnectError as error:
            return self._result(target_id, operation_id, str(error))
        except (SQLAlchemyError, OSError):
            return self._result(target_id, operation_id, "persistence_failed")

    def _disclosure(
        self, row: ExternalReconnectOperationRow
    ) -> tuple[
        HouseholdTransportDisclosure, ProductionHttpAcknowledgementBinding | None
    ]:
        with self._sessions.begin() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            self._require_binding(row, session)
            current = session.get(ExternalReconnectOperationRow, row.id)
            external = session.get(ExternalKodiTargetRow, row.target_id)
            pairing = session.get(ExternalBridgePairingRow, row.target_id)
            assert current is not None and external is not None
            revision = self._bridges._prospective_production_revision(pairing)
            token = _digest(
                [
                    row.id,
                    row.native_binding,
                    row.bridge_port,
                    revision,
                    PRODUCTION_TRANSPORT_POLICY_REVISION,
                ]
            )
            current.disclosure_token, current.expected_bridge_revision = token, revision
            row.disclosure_token, row.expected_bridge_revision = token, revision
            expected = None
            if row.target_class == "external_production":
                policy = session.get(ExternalProductionPolicyRow, row.target_id)
                assert policy is not None
                expected = ProductionHttpAcknowledgementBinding(
                    host=external.host,
                    native_port=external.port,
                    bridge_port=row.bridge_port,
                    identity_generation=policy.identity_generation,
                    policy_revision=policy.policy_revision,
                    confirmed_observation_digest=policy.confirmed_observation_digest,
                    bridge_trust_revision=revision,
                )
            host = f"[{external.host}]" if ":" in external.host else external.host
            return HouseholdTransportDisclosure(
                disclosure_token=token,
                kodi_endpoint=f"{external.protocol_kind.value.replace('jsonrpc_', '')}://{host}:{external.port}",
                bridge_endpoint=f"http://{host}:{row.bridge_port}",
            ), expected

    def _owned_pairing(
        self, row: ExternalReconnectOperationRow, session: Session
    ) -> ExternalBridgePairingRow:
        self._require_binding(row, session)
        pairing = session.get(ExternalBridgePairingRow, row.target_id)
        if (
            pairing is None
            or row.owned_bridge_reference is None
            or pairing.bridge_secret_reference != row.owned_bridge_reference
            or pairing.bridge_port != row.bridge_port
        ):
            raise ReconnectError("pairing_recovery_required")
        if (
            row.target_class == "external_production"
            and pairing.production_bridge_trust_revision != row.expected_bridge_revision
        ):
            raise ReconnectError("onboarding_binding_changed")
        return pairing

    def _advance(
        self, row: ExternalReconnectOperationRow, request: HouseholdResumeRequest
    ) -> ReconnectResult:
        target_id, operation_id = UUID(row.target_id), UUID(row.id)
        if row.stage in {"blocked", "cancelled"}:
            return self._result(target_id, operation_id, row.error_code or "cancelled")
        try:
            with self._sessions() as session:
                self._require_binding(row, session)
            self._target_service(target_id).verify_current_identity(target_id)
            with self._sessions() as session:
                self._require_binding(row, session)
                pairing = session.get(ExternalBridgePairingRow, row.target_id)
            if row.stage == "connected":
                if (
                    pairing is None
                    or pairing.pairing_state is not ExternalBridgePairingState.PAIRED
                    or pairing.bridge_secret_reference != row.owned_bridge_reference
                ):
                    return self._result(
                        target_id, operation_id, "onboarding_binding_changed"
                    )
                self._bridges.commit_otp_pairing(target_id)
                return self._result(target_id, operation_id, "connected")
            if row.stage == "requires_revoke":
                return self._result(target_id, operation_id, "replace_pairing_required")
            if row.stage == "native_verified":
                if pairing is not None and pairing.pairing_state not in {
                    ExternalBridgePairingState.UNPAIRED,
                    ExternalBridgePairingState.REVOKED,
                }:
                    if pairing.bridge_secret_reference != row.initial_bridge_reference:
                        raise ReconnectError("onboarding_binding_changed")
                    if (
                        pairing.pairing_state is ExternalBridgePairingState.PAIRED
                        and pairing.bridge_port == row.bridge_port
                    ):
                        try:
                            self._bridges.reverify(target_id)
                        except ExternalBridgeServiceError:
                            pass
                        else:
                            with self._sessions.begin() as session:
                                self._require_binding(row, session)
                                current = session.get(
                                    ExternalReconnectOperationRow, row.id
                                )
                                active = session.get(
                                    ExternalBridgePairingRow, row.target_id
                                )
                                if (
                                    current is None
                                    or active is None
                                    or active.bridge_secret_reference
                                    != row.initial_bridge_reference
                                ):
                                    raise ReconnectError("onboarding_binding_changed")
                                if (
                                    active.pairing_state
                                    is not ExternalBridgePairingState.PAIRED
                                ):
                                    current.stage, current.updated_at = (
                                        "requires_revoke",
                                        _now(),
                                    )
                                    return self._result(
                                        target_id,
                                        operation_id,
                                        "replace_pairing_required",
                                    )
                                current.owned_bridge_reference = (
                                    active.bridge_secret_reference
                                )
                                current.stage, current.updated_at = "connected", _now()
                            return self._result(target_id, operation_id, "connected")
                    self._save(row, "requires_revoke")
                    return self._result(
                        target_id, operation_id, "replace_pairing_required"
                    )
                previous_token = row.disclosure_token
                disclosure, expected = self._disclosure(row)
                decision = request.transport_decision
                if decision is None or not decision.accept_http_risk:
                    return self._result(
                        target_id,
                        operation_id,
                        "transport_consent_required",
                        transport=disclosure,
                    )
                if (
                    decision.disclosure_token != disclosure.disclosure_token
                    or previous_token != disclosure.disclosure_token
                ):
                    return self._result(
                        target_id,
                        operation_id,
                        "transport_disclosure_changed",
                        transport=disclosure,
                    )
                if expected is not None:
                    self._bridges.acknowledge_production_http_risk(
                        target_id,
                        bridge_port=row.bridge_port,
                        administrator_reference="web_admin",
                        expected=expected,
                    )
                if request.code is None:
                    return self._result(
                        target_id, operation_id, "otp_required", transport=disclosure
                    )
                self._save(row, "bootstrap_started")

                def before_dispatch() -> None:
                    with self._sessions() as session:
                        self._require_binding(row, session)
                        pairing = session.get(ExternalBridgePairingRow, row.target_id)
                        if (
                            self._bridges._prospective_production_revision(pairing)
                            != row.expected_bridge_revision
                        ):
                            raise ReconnectError("onboarding_binding_changed")

                def staged(session: Session, pairing: ExternalBridgePairingRow) -> None:
                    self._require_binding(row, session)
                    current = session.get(ExternalReconnectOperationRow, row.id)
                    if (
                        current is None
                        or current.stage != "bootstrap_started"
                        or pairing.bridge_port != row.bridge_port
                    ):
                        raise ReconnectError("onboarding_binding_changed")
                    current.owned_bridge_reference = pairing.bridge_secret_reference

                try:
                    self._bridges.begin_otp_pairing(
                        target_id,
                        bridge_port=row.bridge_port,
                        code=request.code.get_secret_value(),
                        before_dispatch=before_dispatch,
                        on_staged=staged,
                    )
                except ExternalBridgeServiceError as error:
                    if error.otp_retry_safe:
                        self._save(row, "native_verified", error.code)
                        return self._result(target_id, operation_id, error.code)
                    raise
                row = self._row(target_id, operation_id)
            with self._sessions() as session:
                self._owned_pairing(row, session)
            self._bridges.commit_otp_pairing(target_id)
            self._save(row, "paired")
            self._target_service(target_id).verify_current_identity(target_id)
            with self._sessions.begin() as session:
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
                pairing = self._owned_pairing(row, session)
                if pairing.pairing_state is not ExternalBridgePairingState.PAIRED:
                    raise ReconnectError("pairing_recovery_required")
                current = session.get(ExternalReconnectOperationRow, row.id)
                assert current is not None
                current.stage, current.error_code, current.updated_at = (
                    "connected",
                    None,
                    _now(),
                )
            return self._result(target_id, operation_id, "connected")
        except ReconnectError as error:
            if row.stage != "connected":
                self._save(row, "blocked", str(error))
            return self._result(target_id, operation_id, str(error))
        except ExternalTargetError as error:
            return self._result(target_id, operation_id, error.code)
        except ExternalBridgeServiceError as error:
            current = self._row(target_id, operation_id)
            code = (
                "pairing_recovery_required"
                if current.stage == "bootstrap_started"
                and current.owned_bridge_reference is None
                else "pairing_commit_failed"
            )
            if error.code in {"bridge_unreachable", "bridge_build_not_reviewed"}:
                code = error.code
            return self._result(target_id, operation_id, code)

    def replace_pairing(self, target_id: UUID, operation_id: UUID) -> ReconnectResult:
        try:
            with self._lock(target_id) as acquired:
                if not acquired:
                    return self._result(
                        target_id, operation_id, "operation_in_progress"
                    )
                row = self._row(target_id, operation_id)
                if row.stage != "requires_revoke":
                    raise ReconnectError("operation_conflict")
                with self._sessions() as session:
                    self._require_binding(row, session)
                    pairing = session.get(ExternalBridgePairingRow, row.target_id)
                    if (
                        pairing is None
                        or pairing.bridge_secret_reference
                        != row.initial_bridge_reference
                    ):
                        raise ReconnectError("onboarding_binding_changed")
                self._bridges.revoke_exact(target_id, row.initial_bridge_reference)
                self._save(row, "native_verified")
                return self._advance(
                    self._row(target_id, operation_id), HouseholdResumeRequest()
                )
        except ReconnectError as error:
            return self._result(target_id, operation_id, str(error))
        except (SQLAlchemyError, OSError, ExternalBridgeServiceError):
            return self._result(target_id, operation_id, "persistence_failed")

    def cancel(self, target_id: UUID, operation_id: UUID) -> ReconnectResult:
        try:
            with self._lock(target_id) as acquired:
                if not acquired:
                    return self._result(
                        target_id, operation_id, "operation_in_progress"
                    )
                row = self._row(target_id, operation_id)
                if row.stage == "connected":
                    raise ReconnectError("operation_conflict")
                with self._sessions() as session:
                    pairing = session.get(ExternalBridgePairingRow, row.target_id)
                    owned = (
                        row.owned_bridge_reference is not None
                        and pairing is not None
                        and pairing.bridge_secret_reference
                        == row.owned_bridge_reference
                    )
                if owned:
                    self._bridges.revoke_exact(target_id, row.owned_bridge_reference)
                self._bridges._retire_pending_bridge_secrets(target_id)
                self._save(row, "cancelled", "cancelled")
                return self._result(target_id, operation_id, "cancelled")
        except ReconnectError as error:
            return self._result(target_id, operation_id, str(error))
        except (SQLAlchemyError, OSError, ExternalBridgeServiceError):
            return self._result(target_id, operation_id, "persistence_failed")
