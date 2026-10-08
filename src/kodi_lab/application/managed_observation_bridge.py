"""Read-only consumer of managed lifecycle-owned resident Bridge trust."""

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
            if (
                row.state != "active"
                or row.container_id != observed.container_id
                or row.runtime_revision_id != observed.runtime_revision_id
                or observed.managed_instance_id != str(target_id)
                or not row.secret_reference
                or not row.bridge_id
                or not row.identity_json
            ):
                raise UiObservationError("identity_mismatch")
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
            require_approved_identity(identity, kodi_version)
            if identity_json(identity) != row.identity_json:
                raise UiObservationError("identity_mismatch")
            self._require_current(row)
            return raw
        except ExternalBridgeError as error:
            if error.code in {"bridge_unreachable", "bridge_operation_failed"}:
                self._require_current(row)
                return None
            raise UiObservationError("identity_mismatch") from error
        except Exception as error:
            raise UiObservationError("identity_mismatch") from error

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
                raise UiObservationError("identity_mismatch")
