"""Single-operation OTP onboarding for an external development Kodi target."""

from __future__ import annotations

import threading
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.external_bridges import (
    ExternalBridgeService,
    ExternalBridgeServiceError,
    ExternalBridgeStatus,
)
from kodi_lab.application.external_targets import (
    ExternalTargetAdministrationService,
    ExternalTargetError,
    ExternalTargetRead,
)
from kodi_lab.domain import (
    ExternalAdministrativeState,
    ExternalAuthenticationMode,
    ExternalBridgePairingState,
    ExternalProtocolKind,
    ExternalVerificationState,
)
from kodi_lab.kodi.external import ExternalEndpointError, ExternalEndpointPolicy
from kodi_lab.storage.models import ExternalKodiTargetRow, TargetRow

_ALPHABET = frozenset("23456789ABCDEFGHJKLMNPQRSTUVWXYZ")
_CAPABILITIES = {"target_inspect", "screenshot"}


class ExternalKodiOnboardingError(RuntimeError):
    """Bounded onboarding failure; target_id identifies a resumable transaction."""

    def __init__(self, code: str, *, target_id: UUID | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.target_id = target_id


class ExternalKodiOnboardingResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    target_id: UUID
    target_name: str = Field(min_length=1, max_length=120)
    kodi_version: str = Field(min_length=1, max_length=64)
    bridge_id: UUID
    bridge_version: str = Field(min_length=1, max_length=64)
    bridge_fingerprint: str = Field(min_length=64, max_length=64)
    target_class: str = "external_development"
    enabled: bool = True
    verified: bool = True
    paired: bool = True


class ExternalKodiOnboardingService:
    """Stages a disabled target, pairs once, then publishes it when verified."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        targets: ExternalTargetAdministrationService,
        bridges: ExternalBridgeService,
        kodi_endpoint_policy: ExternalEndpointPolicy,
        bridge_endpoint_policy: ExternalEndpointPolicy,
    ) -> None:
        self._sessions = session_factory
        self._targets = targets
        self._bridges = bridges
        self._kodi_endpoint_policy = kodi_endpoint_policy
        self._bridge_endpoint_policy = bridge_endpoint_policy
        self._gate = threading.Lock()

    def onboard(
        self,
        *,
        name: str,
        address: str,
        code: str,
        kodi_port: int = 8080,
        bridge_port: int = 8765,
        username: str | None = None,
        password: str | None = None,
    ) -> ExternalKodiOnboardingResult:
        """Submit the OTP once; never dispatch a second bootstrap on failure."""
        name, address, mode = self._validate(
            name, address, code, kodi_port, bridge_port, username, password
        )
        if not self._gate.acquire(blocking=False):
            raise ExternalKodiOnboardingError("pairing_in_progress")
        try:
            self._require_unique_name(name)
            try:
                target = self._targets.register(
                    logical_name=name,
                    protocol_kind=ExternalProtocolKind.JSONRPC_HTTP,
                    host=address,
                    port=kodi_port,
                    authentication_mode=mode,
                    configured_capabilities=set(_CAPABILITIES),
                    username=username,
                    password=password,
                )
            except ExternalTargetError as error:
                raise self._target_error(error) from error
            except SQLAlchemyError as error:
                raise ExternalKodiOnboardingError(
                    "target_persistence_failed"
                ) from error
            target_id = target.target_id
            pending = False
            try:
                observed = self._targets.probe(target_id)
                if observed.pending_observation_id is None:
                    raise ExternalKodiOnboardingError("kodi_identity_mismatch")
                verified = self._targets.confirm(
                    target_id, observed.pending_observation_id
                )
                if (
                    verified.verification_state
                    is not ExternalVerificationState.VERIFIED
                    or verified.administrative_state
                    is not ExternalAdministrativeState.DISABLED
                ):
                    raise ExternalKodiOnboardingError("kodi_identity_mismatch")
                staged = self._bridges.begin_otp_pairing(
                    target_id, bridge_port=bridge_port, code=code
                )
                if (
                    staged.pairing_state
                    is not ExternalBridgePairingState.PAIRING_PENDING
                ):
                    raise ExternalKodiOnboardingError("pairing_uncertain")
                pending = True
                return self._finish(target_id)
            except ExternalTargetError as error:
                failure = self._target_error(error, target_id=target_id)
            except ExternalBridgeServiceError as error:
                failure = self._bridge_error(error, target_id=target_id)
            except ExternalKodiOnboardingError as error:
                failure = error
            except SQLAlchemyError:
                failure = ExternalKodiOnboardingError(
                    "target_persistence_failed", target_id=target_id
                )
            if not pending:
                # Existing administration policy retains a disabled audit record.
                # No bridge credential was persisted in this stage.
                try:
                    self._targets.deactivate(target_id)
                except (ExternalTargetError, SQLAlchemyError) as error:
                    raise ExternalKodiOnboardingError(
                        "target_persistence_failed", target_id=target_id
                    ) from error
                raise ExternalKodiOnboardingError(failure.code) from failure
            raise failure
        finally:
            self._gate.release()

    def resume(self, target_id: UUID) -> ExternalKodiOnboardingResult:
        """Finish one known pending or committed disabled target without the OTP."""
        if not self._gate.acquire(blocking=False):
            raise ExternalKodiOnboardingError("pairing_in_progress")
        try:
            try:
                target = self._targets.get(target_id)
                bridge = self._bridges.status(target_id)
            except ExternalTargetError as error:
                raise self._target_error(error) from error
            except ExternalBridgeServiceError as error:
                raise self._bridge_error(error) from error
            except SQLAlchemyError as error:
                raise ExternalKodiOnboardingError(
                    "target_persistence_failed", target_id=target_id
                ) from error
            if (
                target.target_class != "external_development"
                or target.verification_state is not ExternalVerificationState.VERIFIED
                or bridge.pairing_state
                not in {
                    ExternalBridgePairingState.PAIRING_PENDING,
                    ExternalBridgePairingState.PAIRED,
                }
            ):
                raise ExternalKodiOnboardingError("pairing_uncertain")
            if target.administrative_state is ExternalAdministrativeState.ENABLED:
                if bridge.pairing_state is not ExternalBridgePairingState.PAIRED:
                    raise ExternalKodiOnboardingError("pairing_uncertain")
                return self._result(target, bridge)
            if target.administrative_state is not ExternalAdministrativeState.DISABLED:
                raise ExternalKodiOnboardingError("pairing_uncertain")
            return self._finish(target_id)
        finally:
            self._gate.release()

    def _finish(self, target_id: UUID) -> ExternalKodiOnboardingResult:
        try:
            bridge = self._bridges.commit_otp_pairing(target_id)
            if bridge.pairing_state is not ExternalBridgePairingState.PAIRED:
                raise ExternalKodiOnboardingError(
                    "pairing_commit_failed", target_id=target_id
                )
            target = self._targets.set_enabled(target_id, enabled=True)
            return self._result(target, bridge)
        except ExternalBridgeServiceError as error:
            raise self._bridge_error(error, target_id=target_id) from error
        except ExternalTargetError as error:
            raise self._target_error(error, target_id=target_id) from error
        except SQLAlchemyError as error:
            raise ExternalKodiOnboardingError(
                "target_persistence_failed", target_id=target_id
            ) from error

    @staticmethod
    def _result(
        target: ExternalTargetRead, bridge: ExternalBridgeStatus
    ) -> ExternalKodiOnboardingResult:
        if (
            target.administrative_state is not ExternalAdministrativeState.ENABLED
            or target.verification_state is not ExternalVerificationState.VERIFIED
            or bridge.pairing_state is not ExternalBridgePairingState.PAIRED
            or bridge.pinned_bridge_id is None
            or bridge.addon_version is None
            or bridge.build_fingerprint is None
            or target.kodi_version is None
        ):
            raise ExternalKodiOnboardingError(
                "pairing_uncertain", target_id=target.target_id
            )
        return ExternalKodiOnboardingResult(
            target_id=target.target_id,
            target_name=target.logical_name,
            kodi_version=target.kodi_version,
            bridge_id=bridge.pinned_bridge_id,
            bridge_version=bridge.addon_version,
            bridge_fingerprint=bridge.build_fingerprint,
        )

    def _validate(
        self,
        name: str,
        address: str,
        code: str,
        kodi_port: int,
        bridge_port: int,
        username: str | None,
        password: str | None,
    ) -> tuple[str, str, ExternalAuthenticationMode]:
        normalized = name.strip()
        if not normalized or len(normalized) > 120:
            raise ExternalKodiOnboardingError("invalid_name")
        if (
            type(code) is not str
            or len(code) != 4
            or any(c not in _ALPHABET for c in code)
        ):
            raise ExternalKodiOnboardingError("otp_invalid_format")
        if type(kodi_port) is not int or type(bridge_port) is not int:
            raise ExternalKodiOnboardingError("invalid_port")
        if not self._kodi_endpoint_policy.allows_port(
            kodi_port
        ) or not self._bridge_endpoint_policy.allows_port(bridge_port):
            raise ExternalKodiOnboardingError("invalid_port")
        try:
            endpoint = self._kodi_endpoint_policy.validate(
                ExternalProtocolKind.JSONRPC_HTTP, address, kodi_port
            )
        except ExternalEndpointError as error:
            raise ExternalKodiOnboardingError("invalid_address") from error
        try:
            self._bridge_endpoint_policy.validate(
                ExternalProtocolKind.JSONRPC_HTTP, endpoint.address, bridge_port
            )
        except ExternalEndpointError as error:
            raise ExternalKodiOnboardingError("invalid_port") from error
        if (username is None) != (password is None):
            raise ExternalKodiOnboardingError("invalid_kodi_credentials")
        if username is not None and (
            not username or len(username) > 256 or not password or len(password) > 1024
        ):
            raise ExternalKodiOnboardingError("invalid_kodi_credentials")
        mode = (
            ExternalAuthenticationMode.NONE
            if username is None
            else ExternalAuthenticationMode.USERNAME_PASSWORD
        )
        return normalized, endpoint.address, mode

    def _require_unique_name(self, name: str) -> None:
        try:
            with self._sessions() as session:
                rows = session.scalars(
                    select(TargetRow.name)
                    .join(
                        ExternalKodiTargetRow,
                        ExternalKodiTargetRow.target_id == TargetRow.id,
                    )
                    .where(
                        ExternalKodiTargetRow.administrative_state
                        != ExternalAdministrativeState.DEACTIVATED
                    )
                )
                if any(existing_name == name for existing_name in rows):
                    raise ExternalKodiOnboardingError("target_already_exists")
        except SQLAlchemyError as error:
            raise ExternalKodiOnboardingError("target_persistence_failed") from error

    @staticmethod
    def _target_error(
        error: ExternalTargetError, *, target_id: UUID | None = None
    ) -> ExternalKodiOnboardingError:
        code = {
            "endpoint_not_allowed": "invalid_address",
            "authentication_failed": "invalid_kodi_credentials",
            "target_unreachable": "kodi_unreachable",
            "identity_mismatch": "kodi_identity_mismatch",
            "invalid_kodi_response": "kodi_identity_mismatch",
            "unsupported_kodi_version": "kodi_identity_mismatch",
            "secret_unavailable": "target_persistence_failed",
            "verification_required": "kodi_identity_mismatch",
        }.get(error.code, "target_persistence_failed")
        return ExternalKodiOnboardingError(code, target_id=target_id)

    @staticmethod
    def _bridge_error(
        error: ExternalBridgeServiceError, *, target_id: UUID | None = None
    ) -> ExternalKodiOnboardingError:
        code = {
            "wrong_code": "otp_invalid",
            "pairing_code_expired": "otp_expired",
            "pairing_temporarily_locked": "otp_locked",
            "already_paired": "bridge_already_paired",
            "bridge_protocol_unsupported": "bootstrap_version_unsupported",
            "bootstrap_version_mismatch": "bootstrap_version_unsupported",
            "bridge_identity_mismatch": "bridge_identity_mismatch",
            "bridge_unreachable": "bridge_unreachable",
            "bootstrap_outcome_uncertain": "pairing_uncertain",
            "bridge_authentication_failed": "pairing_uncertain",
            "pairing_in_progress": "pairing_uncertain",
            "provisional_expired": "pairing_commit_failed",
        }.get(error.code, "pairing_commit_failed")
        return ExternalKodiOnboardingError(code, target_id=target_id)
