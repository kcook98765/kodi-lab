"""Typed unprivileged client for the restricted local broker."""

from __future__ import annotations

import socket
from pathlib import Path
from typing import TypeVar
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
    ContainerStateNotReconcilableClientError,
    ContainerStateNotStoppableClientError,
    DockerDaemonUnavailableClientError,
    DockerInventoryClientError,
    DockerStartClientError,
    DockerStopClientError,
    FrameTooLargeError,
    InventoryLimitClientError,
    ManagedContainerMetadataClientError,
    ManagedContainerNotFoundClientError,
    PostStartVerificationClientError,
    PostStopVerificationClientError,
    RemoteBrokerError,
    RequestError,
    RuntimeStatePreconditionClientError,
    TrailingFrameDataError,
    TruncatedFrameError,
)
from kodi_lab.broker.operations import (
    BrokerInfoResult,
    CapabilitiesResult,
    EmptyParams,
    ManagedContainerInventoryResult,
    PingResult,
    StartManagedContainerParams,
    StartManagedContainerResult,
    StopManagedContainerParams,
    StopManagedContainerResult,
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
            }
            error_type = error_types.get(error_response.error.code, RemoteBrokerError)
            raise error_type(
                error_response.error.code,
                error_response.error.message,
                request_id=error_response.request_id,
            )

        raise ClientProtocolError("broker response does not identify success or error")
