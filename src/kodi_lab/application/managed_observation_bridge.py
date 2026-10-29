"""Read-only consumer of managed lifecycle-owned resident Bridge trust."""

import json
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.managed_bridge_trust import (
    identity_json,
    require_approved_identity,
)
from kodi_lab.broker.operations import ObserveManagedKodiRuntimeResult
from kodi_lab.domain import ExternalProtocolKind
from kodi_lab.kodi.bridge import ExternalBridgeClient, ExternalBridgeError
from kodi_lab.kodi.external import ValidatedExternalEndpoint
from kodi_lab.kodi.logs import KodiLogRequest, KodiLogResult
from kodi_lab.kodi.observation_bridge import ObservationBridgeClient
from kodi_lab.kodi.ui_state import UiCollection
from kodi_lab.kodi.ui_state_native import UiObservationError
from kodi_lab.storage.models import ManagedBridgeTrustRow
from kodi_lab.storage.secrets import ExternalBridgeSecretStore


class ManagedObservationBridgeService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        secrets: ExternalBridgeSecretStore,
        *,
        client: ExternalBridgeClient | None = None,
    ) -> None:
        self._sessions, self._secrets = sessions, secrets
        self._client = client or ObservationBridgeClient()

    def close(self) -> None:
        if isinstance(self._client, ObservationBridgeClient):
            self._client.close()

    def collect(
        self,
        target_id: UUID,
        observed: ObserveManagedKodiRuntimeResult,
        kodi_version: str,
    ) -> UiCollection | None:
        with self._sessions() as session:
            row = session.get(ManagedBridgeTrustRow, str(target_id))
            if row is None:
                return None  # native fallback has no resident-Bridge authority
            session.expunge(row)
        try:
            self._require_binding(row, target_id, observed)
            assert row.secret_reference and row.bridge_id and row.identity_json
            endpoint = ValidatedExternalEndpoint(
                ExternalProtocolKind.JSONRPC_HTTP,
                observed.ipv4_address,
                observed.jsonrpc_port,
            )
            secret = self._secrets.read(
                target_id, UUID(row.secret_reference), "request_authentication"
            )
            identity, raw = self._client.inspect_ui_state(
                endpoint,
                bridge_port=row.bridge_port,
                target_id=target_id,
                bridge_id=UUID(row.bridge_id),
                secret=secret,
            )
            require_approved_identity(identity, kodi_version, established=True)
            if identity_json(identity) != row.identity_json:
                raise UiObservationError(
                    "identity_mismatch", reason="managed_bridge_identity_unconfirmed"
                )
            self._require_current(row)
            return raw
        except ExternalBridgeError as error:
            if error.code in {"bridge_unreachable", "bridge_operation_failed"}:
                self._require_current(row)
                return None
            raise UiObservationError(
                "identity_mismatch", reason="managed_bridge_identity_unconfirmed"
            ) from error
        except UiObservationError:
            raise
        except Exception as error:
            raise UiObservationError(
                "identity_mismatch", reason="managed_bridge_identity_unconfirmed"
            ) from error

    def read_log(
        self,
        target_id: UUID,
        observed: ObserveManagedKodiRuntimeResult,
        kodi_version: str,
        request: KodiLogRequest,
    ) -> KodiLogResult:
        with self._sessions() as session:
            row = session.get(ManagedBridgeTrustRow, str(target_id))
            if row is None:
                raise UiObservationError("bridge_not_paired")
            session.expunge(row)
        try:
            self._require_binding(row, target_id, observed)
            assert row.secret_reference and row.bridge_id and row.identity_json
            pinned = ExternalBridgeClient._identity(json.loads(row.identity_json))
            require_approved_identity(pinned, kodi_version, established=True)
            if "log_read" not in pinned.capabilities:
                raise UiObservationError("capability_unavailable")
            endpoint = ValidatedExternalEndpoint(
                ExternalProtocolKind.JSONRPC_HTTP,
                observed.ipv4_address,
                observed.jsonrpc_port,
            )
            secret = self._secrets.read(
                target_id, UUID(row.secret_reference), "request_authentication"
            )
            identity, result = self._client.read_log(
                endpoint,
                bridge_port=row.bridge_port,
                target_id=target_id,
                bridge_id=UUID(row.bridge_id),
                secret=secret,
                request=request,
            )
            require_approved_identity(identity, kodi_version, established=True)
            if identity_json(identity) != row.identity_json:
                raise UiObservationError(
                    "identity_mismatch", reason="managed_bridge_identity_unconfirmed"
                )
            if "log_read" not in identity.capabilities:
                raise UiObservationError("capability_unavailable")
            self._require_current(row)
            return result
        except ExternalBridgeError as error:
            raise UiObservationError(error.code) from error
        except UiObservationError:
            raise
        except Exception as error:
            raise UiObservationError(
                "identity_mismatch", reason="managed_bridge_identity_unconfirmed"
            ) from error

    @staticmethod
    def _require_binding(
        row: ManagedBridgeTrustRow,
        target_id: UUID,
        observed: ObserveManagedKodiRuntimeResult,
    ) -> None:
        if row.state != "active":
            raise UiObservationError(
                "identity_mismatch", reason="managed_bridge_recovery_required"
            )
        if (
            row.container_id != observed.container_id
            or row.runtime_revision_id != observed.runtime_revision_id
            or observed.managed_instance_id != str(target_id)
        ):
            raise UiObservationError(
                "identity_mismatch", reason="managed_bridge_runtime_mismatch"
            )
        if not row.secret_reference or not row.bridge_id or not row.identity_json:
            raise UiObservationError(
                "identity_mismatch", reason="managed_bridge_identity_unconfirmed"
            )

    def _require_current(self, row: ManagedBridgeTrustRow) -> None:
        with self._sessions() as session:
            current = session.get(ManagedBridgeTrustRow, row.target_id)
            if (
                current is None
                or current.state != "active"
                or current.generation != row.generation
                or current.secret_reference != row.secret_reference
                or current.identity_json != row.identity_json
            ):
                raise UiObservationError(
                    "identity_mismatch", reason="managed_bridge_trust_changed"
                )
