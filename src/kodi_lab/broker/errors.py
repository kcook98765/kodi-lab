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
        audit_fields: dict[str, object] | None = None,
    ) -> None:
        self.code = code
        self.safe_message = message
        self.request_id = request_id
        self.operation = operation
        self.protocol_version = protocol_version
        self.audit_fields = audit_fields or {}
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

    def __init__(
        self,
        code: str,
        message: str,
        *,
        audit_fields: dict[str, object] | None = None,
    ) -> None:
        self.code = code
        self.safe_message = message
        self.audit_fields = audit_fields or {}
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


class ManagedContainerNotFoundError(DockerInventoryError):
    """No container has the requested exact managed-instance ownership labels."""


class AmbiguousManagedContainerError(DockerInventoryError):
    """More than one container has the requested managed-instance identity."""


class ContainerIdentityPreconditionError(DockerInventoryError):
    """The owned container is not the immutable container the caller observed."""


class RuntimeStatePreconditionError(DockerInventoryError):
    """The owned container runtime state changed after caller observation."""


class ContainerStateNotReconcilableError(DockerInventoryError):
    """The owned container is outside the safe running/stopped state pair."""


class ContainerStateNotStoppableError(ContainerStateNotReconcilableError):
    """The owned container is in a state this operation will not mutate."""


class DockerStartError(DockerInventoryError):
    """Docker did not safely complete the fixed semantic start request."""


class DockerStopError(DockerInventoryError):
    """Docker did not safely complete the fixed semantic stop request."""


class PostStopVerificationError(DockerInventoryError):
    """The broker could not verify the required stopped postcondition."""


class PostStartVerificationError(DockerInventoryError):
    """The broker could not verify the required running postcondition."""


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


class ManagedContainerNotFoundClientError(RemoteBrokerError):
    """The requested managed instance has no owned Docker container."""


class AmbiguousManagedContainerClientError(RemoteBrokerError):
    """The requested managed instance has ambiguous Docker ownership."""


class ContainerIdentityPreconditionClientError(RemoteBrokerError):
    """The owned Docker container identity differs from caller observation."""


class RuntimeStatePreconditionClientError(RemoteBrokerError):
    """The owned Docker container state differs from caller observation."""


class ContainerStateNotStoppableClientError(RemoteBrokerError):
    """The owned Docker container is not safely stoppable."""


class ContainerStateNotReconcilableClientError(RemoteBrokerError):
    """The owned Docker container is outside safe two-state reconciliation."""


class DockerStartClientError(RemoteBrokerError):
    """The broker could not safely complete Docker start."""


class DockerStopClientError(RemoteBrokerError):
    """The broker could not safely complete Docker stop."""


class PostStopVerificationClientError(RemoteBrokerError):
    """The broker could not verify the stopped postcondition."""


class PostStartVerificationClientError(RemoteBrokerError):
    """The broker could not verify the running postcondition."""
