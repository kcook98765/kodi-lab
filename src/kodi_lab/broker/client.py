"""Typed unprivileged client for the restricted local broker."""

from __future__ import annotations

import socket
from pathlib import Path
from typing import Literal, TypeVar
from uuid import uuid4

from pydantic import BaseModel, ValidationError

from kodi_lab.broker.docker_inventory import (
    DOCKER_API_TIMEOUT_SECONDS,
    DOCKER_STOP_TIMEOUT_SECONDS,
    ContainerRuntimeState,
)
from kodi_lab.broker.errors import (
    AmbiguousManagedContainerClientError,
    BrokerAuthorizationError,
    BrokerUnavailableError,
    ClientProtocolError,
    ClientTransportError,
    ContainerIdentityPreconditionClientError,
    ContainerMustBeStoppedClientError,
    ContainerStateNotReconcilableClientError,
    ContainerStateNotStoppableClientError,
    DevelopmentSourceDeploymentClientError,
    DockerCreateClientError,
    DockerDaemonUnavailableClientError,
    DockerInventoryClientError,
    DockerRemoveClientError,
    DockerStartClientError,
    DockerStopClientError,
    FixtureApplicationClientError,
    FixturePrerequisiteClientError,
    FrameTooLargeError,
    InventoryLimitClientError,
    ManagedContainerMetadataClientError,
    ManagedContainerNotFoundClientError,
    ManagedRuntimeObservationClientError,
    PartialProvisioningClientError,
    PostCreateVerificationClientError,
    PostDeprovisionVerificationClientError,
    PostStartVerificationClientError,
    PostStopVerificationClientError,
    ProvisioningConflictClientError,
    ProvisioningPrerequisiteClientError,
    RemoteBrokerError,
    RequestError,
    RuntimeRevisionPreconditionClientError,
    RuntimeStatePreconditionClientError,
    SnapshotCaptureClientError,
    SnapshotDeleteClientError,
    SnapshotPrerequisiteClientError,
    SnapshotRestoreClientError,
    TrailingFrameDataError,
    TruncatedFrameError,
)
from kodi_lab.broker.operations import (
    AddonZipDestinationResult,
    AddonZipInstallationParams,
    ApplyAddonZipInstallationResult,
    ApplyAssignedFixtureParams,
    ApplyAssignedFixtureResult,
    ApplyManagedInstanceSnapshotParams,
    ApplyManagedInstanceSnapshotResult,
    ApplyRepositoryReplacementParams,
    ApplyRepositoryReplacementResult,
    BrokerInfoResult,
    CapabilitiesResult,
    CaptureManagedInstanceSnapshotParams,
    CaptureManagedInstanceSnapshotResult,
    CopySyncAttachedDevelopmentSourceParams,
    CopySyncAttachedDevelopmentSourceResult,
    DeleteManagedSnapshotArtifactParams,
    DeleteManagedSnapshotArtifactResult,
    DeprovisionManagedContainerParams,
    DeprovisionManagedContainerResult,
    EmptyParams,
    InspectRepositoryReplacementParams,
    InspectRepositoryReplacementResult,
    ManagedContainerInventoryResult,
    ObserveManagedKodiRuntimeParams,
    ObserveManagedKodiRuntimeResult,
    PingResult,
    ProvisionManagedContainerParams,
    ProvisionManagedContainerResult,
    StartManagedContainerParams,
    StartManagedContainerResult,
    StopManagedContainerParams,
    StopManagedContainerResult,
    VerifyAddonZipEnablementDestinationParams,
    VerifyAddonZipEnablementDestinationResult,
    VerifyAddonZipSmokeDestinationParams,
    VerifyAddonZipSmokeDestinationResult,
    VerifyAttachedDevelopmentSourceDestinationParams,
    VerifyAttachedDevelopmentSourceDestinationResult,
)
from kodi_lab.broker.protocol import (
    PROTOCOL_VERSION,
    ErrorResponse,
    Request,
    SuccessResponse,
    decode_json_object,
    encode_model,
)
from kodi_lab.broker.socket_path import PRODUCTION_SOCKET_PATH
from kodi_lab.broker.transport import (
    receive_frame,
    reject_buffered_trailing_data,
    send_frame,
)

CONNECT_TIMEOUT_SECONDS = 5.0
READ_TIMEOUT_SECONDS = 5.0
WRITE_TIMEOUT_SECONDS = 5.0
STOP_RESPONSE_TIMEOUT_SECONDS = float(
    (4 * DOCKER_API_TIMEOUT_SECONDS) + DOCKER_STOP_TIMEOUT_SECONDS + 5
)
START_RESPONSE_TIMEOUT_SECONDS = float((4 * DOCKER_API_TIMEOUT_SECONDS) + 5)
PROVISION_RESPONSE_TIMEOUT_SECONDS = float((8 * DOCKER_API_TIMEOUT_SECONDS) + 5)
DEPROVISION_RESPONSE_TIMEOUT_SECONDS = float((6 * DOCKER_API_TIMEOUT_SECONDS) + 5)

ResultT = TypeVar("ResultT", bound=BaseModel)


class BrokerClient:
    """Explicit safe-operation client; arbitrary invocation remains private."""

    def __init__(
        self,
        socket_path: Path = PRODUCTION_SOCKET_PATH,
        *,
        connect_timeout: float = CONNECT_TIMEOUT_SECONDS,
        read_timeout: float = READ_TIMEOUT_SECONDS,
        write_timeout: float = WRITE_TIMEOUT_SECONDS,
    ) -> None:
        self._socket_path = socket_path
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout
        self._write_timeout = write_timeout

    def ping(self) -> PingResult:
        """Verify that the authorized broker boundary is healthy."""

        return self._call("ping", EmptyParams(), PingResult)

    def broker_info(self) -> BrokerInfoResult:
        """Return safe broker package/protocol identity."""

        return self._call("broker_info", EmptyParams(), BrokerInfoResult)

    def capabilities(self) -> CapabilitiesResult:
        """Return the exact allowlisted operation names."""

        return self._call("capabilities", EmptyParams(), CapabilitiesResult)

    def managed_container_inventory(self) -> ManagedContainerInventoryResult:
        """Return only bounded, exactly labelled Kodi Lab container records."""

        return self._call(
            "managed_container_inventory",
            EmptyParams(),
            ManagedContainerInventoryResult,
        )

    def observe_managed_kodi_runtime(
        self, *, managed_instance_id: str, expected_container_id: str
    ) -> ObserveManagedKodiRuntimeResult:
        """Observe only the trusted Kodi process signal and managed endpoint."""

        try:
            params = ObserveManagedKodiRuntimeParams(
                managed_instance_id=managed_instance_id,
                expected_container_id=expected_container_id,
            )
        except ValidationError as error:
            raise ClientProtocolError(
                "managed Kodi observation failed local validation"
            ) from error
        result = self._call(
            "observe_managed_kodi_runtime", params, ObserveManagedKodiRuntimeResult
        )
        if (
            result.managed_instance_id != managed_instance_id
            or result.container_id != expected_container_id
        ):
            raise ClientProtocolError(
                "managed Kodi observation identity does not match the request"
            )
        return result

    def provision_managed_container(
        self, *, managed_instance_id: str
    ) -> ProvisionManagedContainerResult:
        """Provision one container entirely from broker-trusted canonical state."""

        try:
            params = ProvisionManagedContainerParams(
                managed_instance_id=managed_instance_id
            )
        except ValidationError as error:
            raise ClientProtocolError(
                "provision request failed local validation"
            ) from error
        result = self._call(
            "provision_managed_container",
            params,
            ProvisionManagedContainerResult,
            response_timeout=PROVISION_RESPONSE_TIMEOUT_SECONDS,
        )
        if result.managed_instance_id != managed_instance_id:
            raise ClientProtocolError(
                "broker provisioning result identity does not match the request"
            )
        return result

    def stop_managed_container(
        self,
        *,
        managed_instance_id: str,
        expected_container_id: str,
        expected_runtime_state: ContainerRuntimeState,
    ) -> StopManagedContainerResult:
        """Stop the exact owned container previously observed by the caller."""

        try:
            params = StopManagedContainerParams(
                managed_instance_id=managed_instance_id,
                expected_container_id=expected_container_id,
                expected_runtime_state=expected_runtime_state,
            )
        except ValidationError as error:
            raise ClientProtocolError("stop request failed local validation") from error
        result = self._call(
            "stop_managed_container",
            params,
            StopManagedContainerResult,
            response_timeout=STOP_RESPONSE_TIMEOUT_SECONDS,
        )
        if (
            result.managed_instance_id != managed_instance_id
            or result.container_id != expected_container_id
        ):
            raise ClientProtocolError(
                "broker stop result identity does not match the request"
            )
        return result

    def start_managed_container(
        self,
        *,
        managed_instance_id: str,
        expected_container_id: str,
        expected_runtime_state: ContainerRuntimeState,
    ) -> StartManagedContainerResult:
        """Start the exact owned container previously observed by the caller."""

        try:
            params = StartManagedContainerParams(
                managed_instance_id=managed_instance_id,
                expected_container_id=expected_container_id,
                expected_runtime_state=expected_runtime_state,
            )
        except ValidationError as error:
            raise ClientProtocolError(
                "start request failed local validation"
            ) from error
        result = self._call(
            "start_managed_container",
            params,
            StartManagedContainerResult,
            response_timeout=START_RESPONSE_TIMEOUT_SECONDS,
        )
        if (
            result.managed_instance_id != managed_instance_id
            or result.container_id != expected_container_id
        ):
            raise ClientProtocolError(
                "broker start result identity does not match the request"
            )
        return result

    def deprovision_managed_container(
        self,
        *,
        managed_instance_id: str,
        expected_container_id: str,
        expected_runtime_revision_id: str,
        expected_runtime_state: Literal[ContainerRuntimeState.STOPPED],
    ) -> DeprovisionManagedContainerResult:
        """Remove the exact owned stopped container under strict preconditions."""

        try:
            params = DeprovisionManagedContainerParams(
                managed_instance_id=managed_instance_id,
                expected_container_id=expected_container_id,
                expected_runtime_revision_id=expected_runtime_revision_id,
                expected_runtime_state=expected_runtime_state,
            )
        except ValidationError as error:
            raise ClientProtocolError(
                "deprovision request failed local validation"
            ) from error
        result = self._call(
            "deprovision_managed_container",
            params,
            DeprovisionManagedContainerResult,
            response_timeout=DEPROVISION_RESPONSE_TIMEOUT_SECONDS,
        )
        if (
            result.managed_instance_id != managed_instance_id
            or result.removed_container_id != expected_container_id
            or result.runtime_revision_id != expected_runtime_revision_id
        ):
            raise ClientProtocolError(
                "broker deprovision result identity does not match the request"
            )
        return result

    def apply_assigned_fixture(
        self, *, managed_instance_id: str, expected_container_id: str
    ) -> ApplyAssignedFixtureResult:
        """Apply only the broker-resolved predefined fixture to a stopped container."""

        try:
            params = ApplyAssignedFixtureParams(
                managed_instance_id=managed_instance_id,
                expected_container_id=expected_container_id,
            )
        except ValidationError as error:
            raise ClientProtocolError(
                "assigned fixture request failed local validation"
            ) from error
        result = self._call(
            "apply_assigned_fixture",
            params,
            ApplyAssignedFixtureResult,
            response_timeout=15.0,
        )
        if (
            result.managed_instance_id != managed_instance_id
            or result.container_id != expected_container_id
        ):
            raise ClientProtocolError(
                "assigned fixture result identity does not match the request"
            )
        return result

    def capture_managed_instance_snapshot(
        self,
        *,
        snapshot_id: str,
        managed_instance_id: str,
        expected_container_id: str,
    ) -> CaptureManagedInstanceSnapshotResult:
        """Capture only broker-resolved state into fixed central storage."""

        try:
            params = CaptureManagedInstanceSnapshotParams(
                snapshot_id=snapshot_id,
                managed_instance_id=managed_instance_id,
                expected_container_id=expected_container_id,
            )
        except ValidationError as error:
            raise ClientProtocolError(
                "snapshot capture request failed local validation"
            ) from error
        result = self._call(
            "capture_managed_instance_snapshot",
            params,
            CaptureManagedInstanceSnapshotResult,
            response_timeout=600.0,
        )
        if (
            result.snapshot_id != snapshot_id
            or result.managed_instance_id != managed_instance_id
            or result.container_id != expected_container_id
        ):
            raise ClientProtocolError(
                "snapshot capture result identity does not match the request"
            )
        return result

    def apply_managed_instance_snapshot(
        self,
        *,
        snapshot_id: str,
        managed_instance_id: str,
        expected_container_id: str,
    ) -> ApplyManagedInstanceSnapshotResult:
        """Apply one broker-resolved snapshot to the fixed Kodi profile root."""

        try:
            params = ApplyManagedInstanceSnapshotParams(
                snapshot_id=snapshot_id,
                managed_instance_id=managed_instance_id,
                expected_container_id=expected_container_id,
            )
        except ValidationError as error:
            raise ClientProtocolError(
                "snapshot restore request failed local validation"
            ) from error
        result = self._call(
            "apply_managed_instance_snapshot",
            params,
            ApplyManagedInstanceSnapshotResult,
            response_timeout=600.0,
        )
        if (
            result.snapshot_id != snapshot_id
            or result.managed_instance_id != managed_instance_id
            or result.container_id != expected_container_id
        ):
            raise ClientProtocolError(
                "snapshot restore result identity does not match the request"
            )
        return result

    def delete_managed_snapshot_artifact(
        self, *, snapshot_id: str
    ) -> DeleteManagedSnapshotArtifactResult:
        """Delete only the broker-resolved UUID artifact subtree."""

        try:
            params = DeleteManagedSnapshotArtifactParams(snapshot_id=snapshot_id)
        except ValidationError as error:
            raise ClientProtocolError(
                "snapshot deletion request failed local validation"
            ) from error
        result = self._call(
            "delete_managed_snapshot_artifact",
            params,
            DeleteManagedSnapshotArtifactResult,
            response_timeout=600.0,
        )
        if result.snapshot_id != snapshot_id or not result.artifact_absent:
            raise ClientProtocolError(
                "snapshot deletion result identity does not match the request"
            )
        return result

    def copy_sync_attached_development_source(
        self,
        *,
        managed_instance_id: str,
        development_source_id: str,
        expected_container_id: str,
        deployment_operation_id: str,
    ) -> CopySyncAttachedDevelopmentSourceResult:
        """Copy/sync only one broker-resolved attached local source."""

        try:
            params = CopySyncAttachedDevelopmentSourceParams(
                managed_instance_id=managed_instance_id,
                development_source_id=development_source_id,
                expected_container_id=expected_container_id,
                deployment_operation_id=deployment_operation_id,
            )
        except ValidationError as error:
            raise ClientProtocolError(
                "development-source deployment failed local validation"
            ) from error
        result = self._call(
            "copy_sync_attached_development_source",
            params,
            CopySyncAttachedDevelopmentSourceResult,
            response_timeout=900.0,
        )
        if (
            result.managed_instance_id != managed_instance_id
            or result.development_source_id != development_source_id
            or result.deployment_operation_id != deployment_operation_id
            or result.container_id != expected_container_id
        ):
            raise ClientProtocolError(
                "development-source deployment result identity differs"
            )
        return result

    def verify_attached_development_source_destination(
        self,
        *,
        managed_instance_id: str,
        development_source_id: str,
        expected_container_id: str,
    ) -> VerifyAttachedDevelopmentSourceDestinationResult:
        """Verify only one broker-derived copied source destination."""

        try:
            params = VerifyAttachedDevelopmentSourceDestinationParams(
                managed_instance_id=managed_instance_id,
                development_source_id=development_source_id,
                expected_container_id=expected_container_id,
            )
        except ValidationError as error:
            raise ClientProtocolError(
                "development-source destination verification failed local validation"
            ) from error
        result = self._call(
            "verify_attached_development_source_destination",
            params,
            VerifyAttachedDevelopmentSourceDestinationResult,
            response_timeout=30.0,
        )
        if (
            result.managed_instance_id != managed_instance_id
            or result.development_source_id != development_source_id
            or result.container_id != expected_container_id
            or not result.matched
        ):
            raise ClientProtocolError(
                "development-source destination verification identity differs"
            )
        return result

    def observe_addon_zip_destination(
        self,
        *,
        managed_instance_id: str,
        artifact_id: str,
        installation_operation_id: str,
        expected_container_id: str,
    ) -> AddonZipDestinationResult:
        """Observe only one broker-resolved ZIP installation destination."""

        try:
            params = AddonZipInstallationParams(
                managed_instance_id=managed_instance_id,
                artifact_id=artifact_id,
                installation_operation_id=installation_operation_id,
                expected_container_id=expected_container_id,
            )
        except ValidationError as error:
            raise ClientProtocolError(
                "uploaded-ZIP destination request failed local validation"
            ) from error
        result = self._call(
            "observe_addon_zip_destination",
            params,
            AddonZipDestinationResult,
            response_timeout=60.0,
        )
        if (
            result.managed_instance_id != managed_instance_id
            or result.artifact_id != artifact_id
            or result.installation_operation_id != installation_operation_id
            or result.container_id != expected_container_id
        ):
            raise ClientProtocolError(
                "uploaded-ZIP destination result identity differs"
            )
        return result

    def apply_addon_zip_installation(
        self,
        *,
        managed_instance_id: str,
        artifact_id: str,
        installation_operation_id: str,
        expected_container_id: str,
    ) -> ApplyAddonZipInstallationResult:
        """Apply only one broker-resolved immutable normalized ZIP payload."""

        try:
            params = AddonZipInstallationParams(
                managed_instance_id=managed_instance_id,
                artifact_id=artifact_id,
                installation_operation_id=installation_operation_id,
                expected_container_id=expected_container_id,
            )
        except ValidationError as error:
            raise ClientProtocolError(
                "uploaded-ZIP installation request failed local validation"
            ) from error
        result = self._call(
            "apply_addon_zip_installation",
            params,
            ApplyAddonZipInstallationResult,
            response_timeout=900.0,
        )
        if (
            result.managed_instance_id != managed_instance_id
            or result.artifact_id != artifact_id
            or result.installation_operation_id != installation_operation_id
            or result.container_id != expected_container_id
            or not result.matched
        ):
            raise ClientProtocolError(
                "uploaded-ZIP installation result identity differs"
            )
        return result

    def verify_addon_zip_enablement_destination(
        self,
        *,
        managed_instance_id: str,
        artifact_id: str,
        enablement_operation_id: str,
        expected_container_id: str,
    ) -> VerifyAddonZipEnablementDestinationResult:
        """Freshly verify only the server-resolved immutable ZIP destination."""

        try:
            params = VerifyAddonZipEnablementDestinationParams(
                managed_instance_id=managed_instance_id,
                artifact_id=artifact_id,
                enablement_operation_id=enablement_operation_id,
                expected_container_id=expected_container_id,
            )
        except ValidationError as error:
            raise ClientProtocolError(
                "ZIP enablement destination request failed local validation"
            ) from error
        result = self._call(
            "verify_addon_zip_enablement_destination",
            params,
            VerifyAddonZipEnablementDestinationResult,
            response_timeout=60.0,
        )
        if (
            result.managed_instance_id != managed_instance_id
            or result.artifact_id != artifact_id
            or result.enablement_operation_id != enablement_operation_id
            or result.container_id != expected_container_id
        ):
            raise ClientProtocolError(
                "ZIP enablement destination result identity differs"
            )
        return result

    def inspect_repository_replacement(
        self,
        *,
        managed_instance_id: str,
        approval_id: str,
        expected_container_id: str,
    ) -> InspectRepositoryReplacementResult:
        """Inspect only one persisted exact replacement plan."""

        try:
            params = InspectRepositoryReplacementParams(
                managed_instance_id=managed_instance_id,
                approval_id=approval_id,
                expected_container_id=expected_container_id,
            )
        except ValidationError as error:
            raise ClientProtocolError(
                "repository replacement inspection failed local validation"
            ) from error
        result = self._call(
            "inspect_repository_replacement",
            params,
            InspectRepositoryReplacementResult,
            response_timeout=60.0,
        )
        if (
            result.managed_instance_id != managed_instance_id
            or result.approval_id != approval_id
            or result.container_id != expected_container_id
        ):
            raise ClientProtocolError(
                "repository replacement inspection identity differs"
            )
        return result

    def apply_repository_replacement(
        self,
        *,
        managed_instance_id: str,
        approval_id: str,
        replacement_operation_id: str,
        expected_container_id: str,
    ) -> ApplyRepositoryReplacementResult:
        """Apply one consumed exact repository replacement approval."""

        try:
            params = ApplyRepositoryReplacementParams(
                managed_instance_id=managed_instance_id,
                approval_id=approval_id,
                replacement_operation_id=replacement_operation_id,
                expected_container_id=expected_container_id,
            )
        except ValidationError as error:
            raise ClientProtocolError(
                "repository replacement application failed local validation"
            ) from error
        result = self._call(
            "apply_repository_replacement",
            params,
            ApplyRepositoryReplacementResult,
            response_timeout=900.0,
        )
        if (
            result.managed_instance_id != managed_instance_id
            or result.approval_id != approval_id
            or result.replacement_operation_id != replacement_operation_id
            or result.container_id != expected_container_id
            or not result.matched
        ):
            raise ClientProtocolError(
                "repository replacement application identity differs"
            )
        return result

    def verify_addon_zip_smoke_destination(
        self,
        *,
        managed_instance_id: str,
        artifact_id: str,
        smoke_operation_id: str,
        expected_container_id: str,
    ) -> VerifyAddonZipSmokeDestinationResult:
        """Verify only the server-resolved ZIP smoke destination."""

        try:
            params = VerifyAddonZipSmokeDestinationParams(
                managed_instance_id=managed_instance_id,
                artifact_id=artifact_id,
                smoke_operation_id=smoke_operation_id,
                expected_container_id=expected_container_id,
            )
        except ValidationError as error:
            raise ClientProtocolError(
                "ZIP smoke destination request failed local validation"
            ) from error
        result = self._call(
            "verify_addon_zip_smoke_destination",
            params,
            VerifyAddonZipSmokeDestinationResult,
            response_timeout=60.0,
        )
        if (
            result.managed_instance_id != managed_instance_id
            or result.artifact_id != artifact_id
            or result.smoke_operation_id != smoke_operation_id
            or result.container_id != expected_container_id
        ):
            raise ClientProtocolError("ZIP smoke destination result identity differs")
        return result

    def _call(
        self,
        operation: str,
        params: BaseModel,
        result_model: type[ResultT],
        *,
        response_timeout: float | None = None,
    ) -> ResultT:
        request_id = str(uuid4())
        try:
            request = Request(
                version=PROTOCOL_VERSION,
                request_id=request_id,
                operation=operation,
                params=params.model_dump(mode="json"),
            )
        except ValidationError as error:
            raise ClientProtocolError(
                "client request failed local validation"
            ) from error

        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            connection.settimeout(self._connect_timeout)
            try:
                connection.connect(str(self._socket_path))
            except (FileNotFoundError, ConnectionRefusedError, TimeoutError) as error:
                raise BrokerUnavailableError("broker is unavailable") from error
            except OSError as error:
                raise BrokerUnavailableError("broker is unavailable") from error

            connection.settimeout(self._write_timeout)
            try:
                send_frame(connection, encode_model(request))
            except (BrokenPipeError, ConnectionResetError):
                # Authorization happens before broker request reads. A denied
                # peer can therefore have a structured response waiting even
                # though the broker has already closed its read side.
                pass
            else:
                connection.shutdown(socket.SHUT_WR)
            connection.settimeout(
                self._read_timeout
                if response_timeout is None
                else max(self._read_timeout, response_timeout)
            )
            try:
                payload = receive_frame(connection)
                reject_buffered_trailing_data(connection)
            except FrameTooLargeError as error:
                raise ClientProtocolError(
                    "broker response exceeds frame limit"
                ) from error
            except TruncatedFrameError as error:
                raise ClientProtocolError(
                    "broker response frame is incomplete"
                ) from error
            except TrailingFrameDataError as error:
                raise ClientProtocolError(
                    "broker response contains unexpected trailing data"
                ) from error
            except TimeoutError as error:
                raise ClientTransportError("broker response timed out") from error
        except (BrokerUnavailableError, ClientProtocolError, ClientTransportError):
            raise
        except (OSError, TimeoutError) as error:
            raise ClientTransportError("broker transport failed") from error
        finally:
            connection.close()

        return self._validate_response(payload, request_id, result_model)

    @staticmethod
    def _validate_response(
        payload: bytes, request_id: str, result_model: type[ResultT]
    ) -> ResultT:
        try:
            document = decode_json_object(payload)
        except RequestError as error:
            raise ClientProtocolError("broker response is not valid JSON") from error

        version = document.get("version")
        if (
            not isinstance(version, int)
            or isinstance(version, bool)
            or version != PROTOCOL_VERSION
        ):
            raise ClientProtocolError("broker response uses an unsupported version")

        ok = document.get("ok")
        if ok is True:
            try:
                response = SuccessResponse.model_validate(document)
            except ValidationError as error:
                raise ClientProtocolError(
                    "broker success response does not match schema"
                ) from error
            if response.request_id != request_id:
                raise ClientProtocolError("broker response request ID does not match")
            try:
                return result_model.model_validate(response.result)
            except ValidationError as error:
                raise ClientProtocolError(
                    "broker operation result does not match schema"
                ) from error

        if ok is False:
            try:
                error_response = ErrorResponse.model_validate(document)
            except ValidationError as error:
                raise ClientProtocolError(
                    "broker error response does not match schema"
                ) from error
            if (
                error_response.request_id is not None
                and error_response.request_id != request_id
            ):
                raise ClientProtocolError("broker response request ID does not match")
            if (
                error_response.request_id is None
                and error_response.error.code != "unauthorized_peer"
            ):
                raise ClientProtocolError(
                    "broker error response omitted a recoverable request ID"
                )
            error_types: dict[str, type[RemoteBrokerError]] = {
                "unauthorized_peer": BrokerAuthorizationError,
                "docker_daemon_unavailable": DockerDaemonUnavailableClientError,
                "provisioning_state_unavailable": ProvisioningPrerequisiteClientError,
                "persistent_provisioning_unsupported": (
                    ProvisioningPrerequisiteClientError
                ),
                "runtime_container_artifact_unavailable": (
                    ProvisioningPrerequisiteClientError
                ),
                "runtime_execution_profile_unavailable": (
                    ProvisioningPrerequisiteClientError
                ),
                "runtime_image_unavailable": ProvisioningPrerequisiteClientError,
                "managed_network_unavailable": ProvisioningPrerequisiteClientError,
                "managed_network_conflict": ProvisioningConflictClientError,
                "managed_container_provisioning_conflict": (
                    ProvisioningConflictClientError
                ),
                "docker_create_failed": DockerCreateClientError,
                "post_create_verification_failed": (PostCreateVerificationClientError),
                "partial_provisioning_requires_reconciliation": (
                    PartialProvisioningClientError
                ),
                "docker_inventory_failed": DockerInventoryClientError,
                "invalid_managed_container_metadata": (
                    ManagedContainerMetadataClientError
                ),
                "inventory_limit_exceeded": InventoryLimitClientError,
                "managed_container_not_found": ManagedContainerNotFoundClientError,
                "ambiguous_managed_container": AmbiguousManagedContainerClientError,
                "container_identity_precondition_failed": (
                    ContainerIdentityPreconditionClientError
                ),
                "runtime_state_precondition_failed": (
                    RuntimeStatePreconditionClientError
                ),
                "container_state_not_stoppable": (
                    ContainerStateNotStoppableClientError
                ),
                "container_state_not_reconcilable": (
                    ContainerStateNotReconcilableClientError
                ),
                "docker_start_failed": DockerStartClientError,
                "docker_stop_failed": DockerStopClientError,
                "post_start_verification_failed": (PostStartVerificationClientError),
                "post_stop_verification_failed": PostStopVerificationClientError,
                "managed_instance_state_unavailable": (
                    ProvisioningPrerequisiteClientError
                ),
                "runtime_revision_precondition_failed": (
                    RuntimeRevisionPreconditionClientError
                ),
                "container_must_be_stopped": ContainerMustBeStoppedClientError,
                "docker_remove_failed": DockerRemoveClientError,
                "post_deprovision_verification_failed": (
                    PostDeprovisionVerificationClientError
                ),
                "managed_runtime_not_running": ManagedRuntimeObservationClientError,
                "managed_runtime_endpoint_invalid": (
                    ManagedRuntimeObservationClientError
                ),
                "persistent_fixture_unsupported": FixturePrerequisiteClientError,
                "assigned_fixture_missing": FixturePrerequisiteClientError,
                "assigned_fixture_incompatible": FixturePrerequisiteClientError,
                "packaged_fixture_unavailable": FixturePrerequisiteClientError,
                "assigned_fixture_state_unavailable": FixturePrerequisiteClientError,
                "fixture_baseline_conflict": FixtureApplicationClientError,
                "fixture_archive_failed": FixtureApplicationClientError,
                "persistent_snapshot_unsupported": SnapshotPrerequisiteClientError,
                "snapshot_state_unavailable": SnapshotPrerequisiteClientError,
                "snapshot_capture_failed": SnapshotCaptureClientError,
                "insufficient_snapshot_storage": SnapshotCaptureClientError,
                "snapshot_restore_incompatible": SnapshotPrerequisiteClientError,
                "snapshot_restore_failed": SnapshotRestoreClientError,
                "snapshot_delete_incompatible": SnapshotPrerequisiteClientError,
                "snapshot_delete_storage_unsafe": SnapshotDeleteClientError,
                "snapshot_delete_target_unsafe": SnapshotDeleteClientError,
                "snapshot_delete_unsafe": SnapshotDeleteClientError,
                "snapshot_delete_race": SnapshotDeleteClientError,
                "snapshot_delete_failed": SnapshotDeleteClientError,
                "snapshot_delete_absence_unproven": SnapshotDeleteClientError,
                "development_source_deployment_state_invalid": (
                    DevelopmentSourceDeploymentClientError
                ),
                "development_source_identity_invalid": (
                    DevelopmentSourceDeploymentClientError
                ),
                "development_source_identity_changed": (
                    DevelopmentSourceDeploymentClientError
                ),
                "development_source_changed_during_capture": (
                    DevelopmentSourceDeploymentClientError
                ),
                "development_source_tree_bounds": (
                    DevelopmentSourceDeploymentClientError
                ),
                "development_source_unsafe_name": (
                    DevelopmentSourceDeploymentClientError
                ),
                "development_source_unsafe_entry": (
                    DevelopmentSourceDeploymentClientError
                ),
                "development_source_unreadable": (
                    DevelopmentSourceDeploymentClientError
                ),
                "development_source_unavailable": (
                    DevelopmentSourceDeploymentClientError
                ),
                "development_source_replacement_conflict": (
                    DevelopmentSourceDeploymentClientError
                ),
                "development_source_type_conflict": (
                    DevelopmentSourceDeploymentClientError
                ),
                "development_destination_unsafe": (
                    DevelopmentSourceDeploymentClientError
                ),
                "development_destination_bounds": (
                    DevelopmentSourceDeploymentClientError
                ),
                "development_destination_verification_failed": (
                    DevelopmentSourceDeploymentClientError
                ),
                "development_source_apply_unverified": (
                    DevelopmentSourceDeploymentClientError
                ),
                "development_source_reconciliation_required": (
                    DevelopmentSourceDeploymentClientError
                ),
                "development_source_deployment_failed": (
                    DevelopmentSourceDeploymentClientError
                ),
                "development_source_staging_unavailable": (
                    DevelopmentSourceDeploymentClientError
                ),
            }
            error_type = error_types.get(error_response.error.code, RemoteBrokerError)
            raise error_type(
                error_response.error.code,
                error_response.error.message,
                request_id=error_response.request_id,
            )

        raise ClientProtocolError("broker response does not identify success or error")
