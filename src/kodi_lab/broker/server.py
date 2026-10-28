"""Restricted local Unix-socket broker server."""

from __future__ import annotations

import grp
import logging
import os
import pwd
import socket
import struct
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Event

from kodi_lab.broker.authorization import (
    ExactUidAuthorizationPolicy,
    PeerCredentialExtractor,
    PeerCredentials,
    extract_peer_credentials,
)
from kodi_lab.broker.errors import (
    BrokerStartupError,
    FrameTooLargeError,
    RequestError,
    TrailingFrameDataError,
    TruncatedFrameError,
)
from kodi_lab.broker.novnc import DisplayLane
from kodi_lab.broker.operations import OperationRegistry, build_foundation_registry
from kodi_lab.broker.protocol import (
    Response,
    decode_request,
    encode_model,
    error_response,
    success_response,
)
from kodi_lab.broker.socket_path import (
    PRODUCTION_RUNTIME_DIRECTORY,
    PRODUCTION_SOCKET_PATH,
    PreparedUnixListener,
    SocketPathConfig,
    prepare_unix_listener,
)
from kodi_lab.broker.transport import (
    receive_frame,
    reject_buffered_trailing_data,
    send_frame,
)

AUDIT_LOGGER_NAME = "kodi_lab.broker.audit"
READ_TIMEOUT_SECONDS = 5.0
WRITE_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class ProductionBrokerConfiguration:
    """Resolved installation-time identities and production path policy."""

    socket: SocketPathConfig
    authorization: ExactUidAuthorizationPolicy

    @classmethod
    def resolve(
        cls,
        *,
        client_username: str = "kodi-lab",
        client_group: str = "kodi-lab",
    ) -> ProductionBrokerConfiguration:
        """Fail unless root can resolve the approved dedicated identity."""

        if os.geteuid() != 0:
            raise BrokerStartupError("production broker must run as root")
        try:
            client = pwd.getpwnam(client_username)
            group = grp.getgrnam(client_group)
        except KeyError as error:
            raise BrokerStartupError(
                "configured Kodi Lab client identity could not be resolved"
            ) from error
        if client.pw_gid != group.gr_gid:
            raise BrokerStartupError(
                "configured Kodi Lab user does not use the dedicated primary group"
            )
        return cls(
            socket=SocketPathConfig(
                runtime_directory=PRODUCTION_RUNTIME_DIRECTORY,
                socket_path=PRODUCTION_SOCKET_PATH,
                directory_owner_uid=0,
                directory_group_gid=group.gr_gid,
                socket_owner_uid=0,
                socket_group_gid=group.gr_gid,
            ),
            authorization=ExactUidAuthorizationPolicy(client.pw_uid),
        )


class BrokerServer:
    """One-request-per-connection restricted broker."""

    def __init__(
        self,
        *,
        socket_config: SocketPathConfig,
        authorization: ExactUidAuthorizationPolicy,
        registry: OperationRegistry | None = None,
        credential_extractor: PeerCredentialExtractor = extract_peer_credentials,
        audit_logger: logging.Logger | None = None,
        read_timeout: float = READ_TIMEOUT_SECONDS,
        write_timeout: float = WRITE_TIMEOUT_SECONDS,
        display_lane: DisplayLane | None = None,
    ) -> None:
        self._socket_config = socket_config
        self._authorization = authorization
        self._registry = registry or build_foundation_registry()
        self._credential_extractor = credential_extractor
        self._audit_logger = audit_logger or logging.getLogger(AUDIT_LOGGER_NAME)
        self._read_timeout = read_timeout
        self._write_timeout = write_timeout
        self._prepared: PreparedUnixListener | None = None
        self._display_lane = display_lane

    @classmethod
    def production(cls) -> BrokerServer:
        """Resolve the approved root/kodi-lab production policy."""

        configuration = ProductionBrokerConfiguration.resolve()
        from kodi_lab.config import Settings

        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        lane = None
        if settings.experimental_novnc_instances:
            from kodi_lab.broker.novnc_service import HumanDisplayService
            from kodi_lab.storage.engine import (
                create_database_engine,
                create_session_factory,
            )

            sessions = create_session_factory(
                create_database_engine(
                    "sqlite:///file:"
                    + str(settings.data_root / "database/kodi-lab.db")
                    + "?mode=ro&uri=true"
                )
            )
            lane = HumanDisplayService(settings, sessions).lane(configuration)
        return cls(
            socket_config=configuration.socket,
            authorization=configuration.authorization,
            display_lane=lane,
        )

    @property
    def socket_path(self) -> Path:
        return self._socket_config.socket_path

    @property
    def listener_family(self) -> socket.AddressFamily | None:
        if self._prepared is None:
            return None
        return self._prepared.socket.family

    def open(self) -> None:
        """Securely create the Unix listener without starting the accept loop."""

        if self._prepared is not None:
            raise BrokerStartupError("broker listener is already open")
        self._prepared = prepare_unix_listener(self._socket_config)

    def close(self) -> None:
        """Close the listener and safely remove only its socket inode."""

        if self._display_lane is not None:
            self._display_lane.close()
        prepared = self._prepared
        self._prepared = None
        if prepared is not None:
            prepared.close()

    def serve_forever(self, stop_event: Event | None = None) -> None:
        """Serve sequential bounded requests until explicitly stopped."""

        if self._prepared is None:
            self.open()
        assert self._prepared is not None
        listener = self._prepared.socket
        listener.settimeout(0.2)
        try:
            if self._display_lane is not None:
                self._display_lane.start(stop_event)
            while stop_event is None or not stop_event.is_set():
                try:
                    connection, _address = listener.accept()
                except TimeoutError:
                    continue
                except OSError:
                    if stop_event is not None and stop_event.is_set():
                        break
                    raise
                with connection:
                    self._handle_connection(connection)
        finally:
            self.close()

    def _handle_connection(self, connection: socket.socket) -> None:
        started = time.monotonic()
        peer: PeerCredentials | None = None
        request_id: str | None = None
        operation: str | None = None
        protocol_version: int | None = None
        outcome_code = "internal_error"
        operation_audit_fields: dict[str, object] = {}
        response: Response | None = None

        try:
            try:
                peer = self._credential_extractor(connection)
            except (OSError, ValueError, TypeError, struct.error) as error:
                raise RequestError(
                    "unauthorized_peer", "The local peer is not authorized."
                ) from error
            if not self._authorization.is_authorized(peer):
                raise RequestError(
                    "unauthorized_peer", "The local peer is not authorized."
                )

            connection.settimeout(self._read_timeout)
            try:
                payload = receive_frame(connection)
                reject_buffered_trailing_data(connection)
            except FrameTooLargeError as error:
                raise RequestError(
                    "request_too_large", "Request exceeds the maximum frame size."
                ) from error
            except TruncatedFrameError as error:
                raise RequestError(
                    "malformed_request", "Request frame is incomplete."
                ) from error
            except TrailingFrameDataError as error:
                raise RequestError(
                    "malformed_request", "Request contains unexpected trailing data."
                ) from error
            except TimeoutError as error:
                raise RequestError(
                    "request_timeout", "Request was not received within the timeout."
                ) from error

            request = decode_request(payload)
            request_id = request.request_id
            operation = request.operation
            protocol_version = request.version
            result = self._registry.dispatch(request)
            operation_audit_fields = self._registry.audit_fields_for(
                request.operation, result
            )
            response = success_response(request.request_id, result)
            outcome_code = "success"
        except RequestError as error:
            operation_audit_fields = error.audit_fields
            request_id = error.request_id or request_id
            operation = error.operation or operation
            if error.protocol_version is not None:
                protocol_version = error.protocol_version
            outcome_code = error.code
            response = error_response(error)
            if error.code == "internal_error" and error.__cause__ is not None:
                self._audit_logger.error(
                    "broker handler failed",
                    exc_info=error.__cause__,
                    extra={"broker_internal_error": True},
                )
        except Exception as error:
            self._audit_logger.error(
                "broker connection failed",
                exc_info=error,
                extra={"broker_internal_error": True},
            )
            safe_error = RequestError(
                "internal_error",
                "The broker could not complete the request.",
                request_id=request_id,
                operation=operation,
                protocol_version=protocol_version,
            )
            response = error_response(safe_error)
            outcome_code = safe_error.code

        assert response is not None
        try:
            connection.settimeout(self._write_timeout)
            send_frame(connection, encode_model(response))
        except (OSError, TimeoutError):
            outcome_code = "response_transport_error"
        finally:
            self._audit(
                peer=peer,
                request_id=request_id,
                operation=operation,
                protocol_version=protocol_version,
                outcome_code=outcome_code,
                elapsed_seconds=time.monotonic() - started,
                operation_fields=operation_audit_fields,
            )

    def _audit(
        self,
        *,
        peer: PeerCredentials | None,
        request_id: str | None,
        operation: str | None,
        protocol_version: int | None,
        outcome_code: str,
        elapsed_seconds: float,
        operation_fields: dict[str, object],
    ) -> None:
        self._audit_logger.info(
            "broker request completed",
            extra={
                **operation_fields,
                "broker_request_id": request_id,
                "broker_peer_pid": peer.pid if peer is not None else None,
                "broker_peer_uid": peer.uid if peer is not None else None,
                "broker_peer_gid": peer.gid if peer is not None else None,
                "broker_operation": operation,
                "broker_protocol_version": protocol_version,
                "broker_result_code": outcome_code,
                "broker_elapsed_seconds": elapsed_seconds,
            },
        )
