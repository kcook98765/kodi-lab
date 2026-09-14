"""Safe broker boundary exceptions."""

from __future__ import annotations


class BrokerError(Exception):
    """Base class for broker implementation failures."""


class BrokerStartupError(BrokerError):
    """The broker could not establish its security boundary."""


class FrameError(BrokerError):
    """A length-prefixed protocol frame was invalid."""


class FrameTooLargeError(FrameError):
    """A frame announced more bytes than the protocol permits."""


class TruncatedFrameError(FrameError):
    """A peer closed the connection before a complete frame arrived."""


class TrailingFrameDataError(FrameError):
    """A peer sent bytes after the one permitted frame."""


class RequestError(BrokerError):
    """A request failed safely before or during dispatch."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        request_id: str | None = None,
        operation: str | None = None,
        protocol_version: int | None = None,
    ) -> None:
        self.code = code
        self.safe_message = message
        self.request_id = request_id
        self.operation = operation
        self.protocol_version = protocol_version
        super().__init__(message)


class HandlerExecutionError(RequestError):
    """An allowlisted handler failed without exposing its exception."""

    def __init__(
        self,
        *,
        request_id: str,
        operation: str,
        protocol_version: int,
    ) -> None:
        super().__init__(
            "internal_error",
            "The broker could not complete the request.",
            request_id=request_id,
            operation=operation,
            protocol_version=protocol_version,
        )


class OperationError(BrokerError):
    """A handler's expected failure with an approved safe protocol mapping."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.safe_message = message
        super().__init__(message)


class DockerInventoryError(BrokerError):
    """Base class for trusted Docker inventory adapter failures."""


class DockerDaemonUnavailableError(DockerInventoryError):
    """The fixed local Docker daemon endpoint could not be reached."""


class DockerInventoryReadError(DockerInventoryError):
    """Docker inventory data could not be read or validated."""


class InvalidManagedContainerMetadataError(DockerInventoryError):
    """A managed container violated the ownership identity contract."""


class InventoryLimitExceededError(DockerInventoryError):
    """More managed containers exist than the bounded result permits."""


class ClientError(BrokerError):
    """Base class for typed unprivileged-client failures."""


class BrokerUnavailableError(ClientError):
    """The broker Unix socket could not be reached."""


class ClientTransportError(ClientError):
    """A connected broker transport failed."""


class ClientProtocolError(ClientError):
    """The broker returned an invalid or inconsistent response."""


class RemoteBrokerError(ClientError):
    """The broker returned a structured protocol error."""

    def __init__(self, code: str, message: str, *, request_id: str | None) -> None:
        self.code = code
        self.safe_message = message
        self.request_id = request_id
        super().__init__(f"{code}: {message}")


class BrokerAuthorizationError(RemoteBrokerError):
    """The kernel-authenticated peer was not authorized."""


class DockerDaemonUnavailableClientError(RemoteBrokerError):
    """The broker could not reach its fixed Docker daemon endpoint."""


class DockerInventoryClientError(RemoteBrokerError):
    """The broker could not safely read Docker inventory."""


class ManagedContainerMetadataClientError(RemoteBrokerError):
    """Kodi Lab ownership metadata is invalid or ambiguous."""


class InventoryLimitClientError(RemoteBrokerError):
    """The managed inventory exceeds the protocol's fixed count limit."""
