"""Consume active managed trust; a launch never establishes or replaces trust."""

import json
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.managed_bridge_trust import (
    ManagedBridgeTrustError,
    identity_json,
    require_approved_identity,
)
from kodi_lab.broker.managed_bridge_trust import approved_bridge_build
from kodi_lab.broker.operations import ObserveManagedKodiRuntimeResult
from kodi_lab.domain import ExternalProtocolKind
from kodi_lab.kodi.bridge import ExternalBridgeClient, ExternalBridgeError
from kodi_lab.kodi.external import ValidatedExternalEndpoint
from kodi_lab.kodi.launch import (
    BridgeLaunchIntent,
    BridgeLaunchReceipt,
    ManagedLaunchRuntimeBinding,
)
from kodi_lab.storage.models import ManagedBridgeTrustRow
from kodi_lab.storage.secrets import ExternalBridgeSecretStore


class ManagedLaunchBridgeService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        secrets: ExternalBridgeSecretStore,
        *,
        client: ExternalBridgeClient | None = None,
    ) -> None:
        self._sessions, self._secrets = sessions, secrets
        self._client = client or ExternalBridgeClient(response_timeout=5)

    def binding(
        self, observed: ObserveManagedKodiRuntimeResult, kodi_version: str
    ) -> ManagedLaunchRuntimeBinding:
        with self._sessions() as session:
            row = session.get(ManagedBridgeTrustRow, observed.managed_instance_id)
            if (
                row is None
                or row.state != "active"
                or row.container_id != observed.container_id
                or row.runtime_revision_id != observed.runtime_revision_id
                or not row.identity_json
                or not row.secret_reference
                or not row.bridge_id
            ):
                raise ExternalBridgeError("managed_bridge_upgrade_required")
            pinned = ExternalBridgeClient._identity(json.loads(row.identity_json))
            try:
                require_approved_identity(pinned, kodi_version)
            except ManagedBridgeTrustError as error:
                raise ExternalBridgeError("managed_bridge_upgrade_required") from error
            if (
                pinned.addon_version
                != approved_bridge_build(kodi_version)["addon_version"]
            ):
                raise ExternalBridgeError("managed_bridge_upgrade_required")
            return ManagedLaunchRuntimeBinding(
                target_id=row.target_id,
                runtime_revision_id=row.runtime_revision_id,
                container_id=row.container_id,
                generation=row.generation,
            )

    def call(
        self,
        observed: ObserveManagedKodiRuntimeResult,
        kodi_version: str,
        intent: BridgeLaunchIntent,
        *,
        status_only: bool = False,
    ) -> BridgeLaunchReceipt | None:
        if self.binding(observed, kodi_version) != intent.runtime_binding:
            raise ExternalBridgeError("managed_bridge_identity_mismatch")
        with self._sessions() as session:
            row = session.get(ManagedBridgeTrustRow, observed.managed_instance_id)
            assert row is not None and row.secret_reference and row.bridge_id
            session.expunge(row)
        secret = self._secrets.read(
            UUID(row.target_id), UUID(row.secret_reference), "request_authentication"
        )
        identity, receipt = self._client.managed_launch(
            ValidatedExternalEndpoint(
                ExternalProtocolKind.JSONRPC_HTTP,
                observed.ipv4_address,
                observed.jsonrpc_port,
            ),
            bridge_port=row.bridge_port,
            target_id=UUID(row.target_id),
            bridge_id=UUID(row.bridge_id),
            secret=secret,
            intent=intent,
            status_only=status_only,
        )
        require_approved_identity(identity, kodi_version)
        if (
            identity_json(identity) != row.identity_json
            or self.binding(observed, kodi_version) != intent.runtime_binding
        ):
            raise ExternalBridgeError("managed_bridge_identity_mismatch")
        return receipt
