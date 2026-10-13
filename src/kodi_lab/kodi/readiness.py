"""Unprivileged, fixed-method Kodi JSON-RPC readiness coordination."""

from __future__ import annotations

import http.client
import json
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, cast

from kodi_lab.broker.docker_inventory import ContainerRuntimeState, KodiProcessState
from kodi_lab.broker.errors import ClientError
from kodi_lab.broker.operations import (
    ManagedContainerInventoryResult,
    ObserveManagedKodiRuntimeResult,
)
from kodi_lab.domain import (
    KodiReleaseChannel,
    KodiReleaseIdentity,
    RuntimeExecutionProfile,
)

MAX_JSONRPC_RESPONSE_BYTES = 65_536


class JsonRpcState(StrEnum):
    UNAVAILABLE = "unavailable"
    RESPONSIVE = "responsive"
    PROTOCOL_ERROR = "protocol_error"


class UsabilityState(StrEnum):
    NOT_USABLE = "not_usable"
    USABLE = "usable"


class ReadinessFailureReason(StrEnum):
    CONTAINER_NOT_RUNNING = "container_not_running"
    KODI_PROCESS_NOT_RUNNING = "kodi_process_not_running"
    PROCESS_HEALTH_FAILED = "process_health_failed"
    JSONRPC_UNREACHABLE = "jsonrpc_unreachable"
    JSONRPC_INVALID_RESPONSE = "jsonrpc_invalid_response"
    UNEXPECTED_APPLICATION_IDENTITY = "unexpected_application_identity"
    UNEXPECTED_KODI_VERSION = "unexpected_kodi_version"
    READINESS_TIMEOUT = "readiness_timeout"


@dataclass(frozen=True, slots=True)
class KodiApplicationIdentity:
    name: str
    major: int
    minor: int
    tag: str
    tag_version: int | None = None

    @property
    def kodi_release(self) -> KodiReleaseIdentity:
        # Keep the observed upstream tag while normalizing its canonical channel.
        channel = (
            KodiReleaseChannel.RC
            if self.tag == "releasecandidate"
            else KodiReleaseChannel(self.tag)
        )
        return KodiReleaseIdentity(
            self.major,
            self.minor,
            channel,
            self.tag_version,
        )


@dataclass(frozen=True, slots=True)
class ReadinessResult:
    container_state: ContainerRuntimeState
    process_state: KodiProcessState
    jsonrpc_state: JsonRpcState
    usability_state: UsabilityState
    failure_reason: ReadinessFailureReason | None
    last_observed_failure: ReadinessFailureReason | None
    identity: KodiApplicationIdentity | None
    elapsed_seconds: float
    attempts: int


class JsonRpcUnavailableError(RuntimeError):
    pass


class JsonRpcInvalidResponseError(RuntimeError):
    pass


class ReadinessCancelledError(RuntimeError):
    """The Web layer invalidated an ephemeral readiness observation."""


class _BrokerObservationClient(Protocol):
    def managed_container_inventory(self) -> ManagedContainerInventoryResult: ...

    def observe_managed_kodi_runtime(
        self, *, managed_instance_id: str, expected_container_id: str
    ) -> ObserveManagedKodiRuntimeResult: ...


class _HttpTransport(Protocol):
    def post(
        self, *, host: str, port: int, path: str, body: bytes, timeout: float
    ) -> bytes: ...


class _BoundedHttpTransport:
    def post(
        self, *, host: str, port: int, path: str, body: bytes, timeout: float
    ) -> bytes:
        deadline = time.monotonic() + timeout
        connection = http.client.HTTPConnection(host, port, timeout=timeout)
        try:
            connection.request(
                "POST",
                path,
                body=body,
                headers={"Content-Type": "application/json"},
            )
            transport_socket = connection.sock
            if transport_socket is None:
                raise JsonRpcUnavailableError("Kodi JSON-RPC connection is unavailable")
            self._set_remaining_socket_timeout(transport_socket, deadline)
            response = connection.getresponse()
            if response.status < 200 or response.status >= 300:
                raise JsonRpcUnavailableError("Kodi JSON-RPC returned non-success HTTP")
            content_length = response.getheader("Content-Length")
            if content_length is not None:
                try:
                    if int(content_length) > MAX_JSONRPC_RESPONSE_BYTES:
                        raise JsonRpcInvalidResponseError(
                            "JSON-RPC response is too large"
                        )
                except ValueError as error:
                    raise JsonRpcInvalidResponseError(
                        "JSON-RPC content length is invalid"
                    ) from error
            self._set_remaining_socket_timeout(transport_socket, deadline)
            payload = response.read(MAX_JSONRPC_RESPONSE_BYTES + 1)
            if len(payload) > MAX_JSONRPC_RESPONSE_BYTES:
                raise JsonRpcInvalidResponseError("JSON-RPC response is too large")
            return payload
        except JsonRpcInvalidResponseError:
            raise
        except (OSError, TimeoutError, http.client.HTTPException) as error:
            raise JsonRpcUnavailableError("Kodi JSON-RPC is unavailable") from error
        finally:
            connection.close()

    @staticmethod
    def _set_remaining_socket_timeout(
        transport_socket: socket.socket, deadline: float
    ) -> None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise JsonRpcUnavailableError("Kodi JSON-RPC request timed out")
        transport_socket.settimeout(remaining)


class KodiJsonRpcReadinessAdapter:
    """Fixed-method Kodi JSON-RPC adapter bound to a broker-trusted endpoint."""

    def __init__(
        self,
        observation: ObserveManagedKodiRuntimeResult,
        profile: RuntimeExecutionProfile,
        *,
        timeout: float | None = None,
        transport: _HttpTransport | None = None,
    ) -> None:
        if observation.runtime_revision_id != str(profile.runtime_revision_id):
            raise ValueError("observation and execution profile revisions differ")
        if observation.jsonrpc_port != profile.jsonrpc_port:
            raise ValueError("observation and execution profile ports differ")
        self._host = observation.ipv4_address
        self._port = profile.jsonrpc_port
        self._path = profile.jsonrpc_path
        self._timeout = profile.http_timeout_seconds if timeout is None else timeout
        if not 0 < self._timeout <= profile.http_timeout_seconds:
            raise ValueError("HTTP timeout exceeds the trusted execution profile")
        self._transport = transport or _BoundedHttpTransport()

    def ping(self) -> None:
        document = self._fixed_request("kodi-lab-readiness-ping", "JSONRPC.Ping", None)
        if document.get("result") != "pong":
            raise JsonRpcInvalidResponseError("JSONRPC.Ping result is invalid")

    def get_application_identity(self) -> KodiApplicationIdentity:
        document = self._fixed_request(
            "kodi-lab-readiness-identity",
            "Application.GetProperties",
            {"properties": ["name", "version"]},
        )
        result = document.get("result")
        if not isinstance(result, dict) or set(result) != {"name", "version"}:
            raise JsonRpcInvalidResponseError("application identity result is invalid")
        version = result.get("version")
        if not isinstance(version, dict):
            raise JsonRpcInvalidResponseError("application version is invalid")
        name = result.get("name")
        major = version.get("major")
        minor = version.get("minor")
        tag = version.get("tag")
        raw_tag_version = version.get("tagversion")
        if (
            not isinstance(name, str)
            or not isinstance(major, int)
            or isinstance(major, bool)
            or not isinstance(minor, int)
            or isinstance(minor, bool)
            or not isinstance(tag, str)
        ):
            raise JsonRpcInvalidResponseError("application identity fields are invalid")
        tag_version: int | None
        if raw_tag_version is None or raw_tag_version == "":
            tag_version = None
        elif isinstance(raw_tag_version, int) and not isinstance(raw_tag_version, bool):
            tag_version = raw_tag_version
        elif (
            isinstance(raw_tag_version, str)
            and raw_tag_version.isascii()
            and raw_tag_version.isdigit()
        ):
            tag_version = int(raw_tag_version)
        else:
            raise JsonRpcInvalidResponseError(
                "application prerelease ordinal is invalid"
            )
        try:
            identity = KodiApplicationIdentity(
                name=name,
                major=major,
                minor=minor,
                tag=tag,
                tag_version=tag_version,
            )
            _ = identity.kodi_release
        except ValueError as error:
            raise JsonRpcInvalidResponseError(
                "application release identity is invalid"
            ) from error
        return identity

    def _fixed_request(
        self, request_id: str, method: str, params: dict[str, object] | None
    ) -> dict[str, object]:
        request: dict[str, object] = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
        }
        if params is not None:
            request["params"] = params
        payload = self._transport.post(
            host=self._host,
            port=self._port,
            path=self._path,
            body=json.dumps(request, separators=(",", ":")).encode(),
            timeout=self._timeout,
        )
        try:
            document = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise JsonRpcInvalidResponseError(
                "JSON-RPC response is not valid JSON"
            ) from error
        if (
            not isinstance(document, dict)
            or document.get("jsonrpc") != "2.0"
            or document.get("id") != request_id
            or "error" in document
            or "result" not in document
        ):
            raise JsonRpcInvalidResponseError("JSON-RPC response envelope is invalid")
        return cast(dict[str, object], document)


type _AdapterFactory = Callable[
    [ObserveManagedKodiRuntimeResult, RuntimeExecutionProfile, float],
    KodiJsonRpcReadinessAdapter,
]


class KodiReadinessCoordinator:
    """Bounded four-layer readiness coordinator for one trusted Kodi runtime."""

    def __init__(
        self,
        broker: _BrokerObservationClient,
        profile: RuntimeExecutionProfile,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        adapter_factory: _AdapterFactory | None = None,
    ) -> None:
        self._broker = broker
        self._profile = profile
        self._clock = clock
        self._sleep = sleep
        self._adapter_factory = adapter_factory or (
            lambda observation, profile, timeout: KodiJsonRpcReadinessAdapter(
                observation, profile, timeout=timeout
            )
        )

    def wait_until_usable(
        self,
        managed_instance_id: str,
        *,
        cancelled: Callable[[], bool] | None = None,
    ) -> ReadinessResult:
        started = self._clock()
        deadline = started + self._profile.readiness_timeout_seconds
        attempts = 0
        container_state = ContainerRuntimeState.UNKNOWN
        process_state = KodiProcessState.UNKNOWN_ERROR
        jsonrpc_state = JsonRpcState.UNAVAILABLE
        last_failure = ReadinessFailureReason.CONTAINER_NOT_RUNNING

        while self._clock() < deadline:
            if cancelled is not None and cancelled():
                raise ReadinessCancelledError("readiness observation was cancelled")
            attempts += 1
            inventory = self._broker.managed_container_inventory()
            match = next(
                (
                    item
                    for item in inventory.containers
                    if item.managed_instance_id == managed_instance_id
                ),
                None,
            )
            if (
                match is None
                or match.runtime_state is not ContainerRuntimeState.RUNNING
            ):
                container_state = (
                    ContainerRuntimeState.UNKNOWN
                    if match is None
                    else match.runtime_state
                )
                last_failure = ReadinessFailureReason.CONTAINER_NOT_RUNNING
                self._bounded_sleep(deadline)
                continue
            container_state = ContainerRuntimeState.RUNNING
            try:
                observation = self._broker.observe_managed_kodi_runtime(
                    managed_instance_id=managed_instance_id,
                    expected_container_id=match.container_id,
                )
            except ClientError:
                process_state = KodiProcessState.UNKNOWN_ERROR
                last_failure = ReadinessFailureReason.PROCESS_HEALTH_FAILED
                self._bounded_sleep(deadline)
                continue
            process_state = observation.process_state
            if process_state is not KodiProcessState.RUNNING:
                last_failure = (
                    ReadinessFailureReason.KODI_PROCESS_NOT_RUNNING
                    if process_state is KodiProcessState.NOT_RUNNING
                    else ReadinessFailureReason.PROCESS_HEALTH_FAILED
                )
                self._bounded_sleep(deadline)
                continue
            remaining = deadline - self._clock()
            if remaining <= 0:
                break
            ping_adapter = self._adapter_factory(
                observation,
                self._profile,
                min(self._profile.http_timeout_seconds, remaining),
            )
            try:
                ping_adapter.ping()
            except JsonRpcUnavailableError:
                jsonrpc_state = JsonRpcState.UNAVAILABLE
                last_failure = ReadinessFailureReason.JSONRPC_UNREACHABLE
                self._bounded_sleep(deadline)
                continue
            except JsonRpcInvalidResponseError:
                return self._failure_result(
                    started,
                    attempts,
                    container_state,
                    process_state,
                    JsonRpcState.PROTOCOL_ERROR,
                    ReadinessFailureReason.JSONRPC_INVALID_RESPONSE,
                )
            jsonrpc_state = JsonRpcState.RESPONSIVE
            remaining = deadline - self._clock()
            if remaining <= 0:
                last_failure = ReadinessFailureReason.READINESS_TIMEOUT
                break
            identity_adapter = self._adapter_factory(
                observation,
                self._profile,
                min(self._profile.http_timeout_seconds, remaining),
            )
            try:
                identity = identity_adapter.get_application_identity()
            except JsonRpcUnavailableError:
                jsonrpc_state = JsonRpcState.UNAVAILABLE
                last_failure = ReadinessFailureReason.JSONRPC_UNREACHABLE
                self._bounded_sleep(deadline)
                continue
            except JsonRpcInvalidResponseError:
                return self._failure_result(
                    started,
                    attempts,
                    container_state,
                    process_state,
                    JsonRpcState.PROTOCOL_ERROR,
                    ReadinessFailureReason.JSONRPC_INVALID_RESPONSE,
                )
            if identity.name != self._profile.expected_application_name:
                return self._failure_result(
                    started,
                    attempts,
                    container_state,
                    process_state,
                    jsonrpc_state,
                    ReadinessFailureReason.UNEXPECTED_APPLICATION_IDENTITY,
                    identity,
                )
            if identity.kodi_release != self._profile.kodi_release:
                return self._failure_result(
                    started,
                    attempts,
                    container_state,
                    process_state,
                    jsonrpc_state,
                    ReadinessFailureReason.UNEXPECTED_KODI_VERSION,
                    identity,
                )
            return ReadinessResult(
                container_state=container_state,
                process_state=process_state,
                jsonrpc_state=jsonrpc_state,
                usability_state=UsabilityState.USABLE,
                failure_reason=None,
                last_observed_failure=None,
                identity=identity,
                elapsed_seconds=self._clock() - started,
                attempts=attempts,
            )

        return ReadinessResult(
            container_state=container_state,
            process_state=process_state,
            jsonrpc_state=jsonrpc_state,
            usability_state=UsabilityState.NOT_USABLE,
            failure_reason=ReadinessFailureReason.READINESS_TIMEOUT,
            last_observed_failure=last_failure,
            identity=None,
            elapsed_seconds=self._clock() - started,
            attempts=attempts,
        )

    def _bounded_sleep(self, deadline: float) -> None:
        remaining = deadline - self._clock()
        if remaining > 0:
            self._sleep(min(self._profile.poll_interval_seconds, remaining))

    def _failure_result(
        self,
        started: float,
        attempts: int,
        container_state: ContainerRuntimeState,
        process_state: KodiProcessState,
        jsonrpc_state: JsonRpcState,
        reason: ReadinessFailureReason,
        identity: KodiApplicationIdentity | None = None,
    ) -> ReadinessResult:
        return ReadinessResult(
            container_state=container_state,
            process_state=process_state,
            jsonrpc_state=jsonrpc_state,
            usability_state=UsabilityState.NOT_USABLE,
            failure_reason=reason,
            last_observed_failure=reason,
            identity=identity,
            elapsed_seconds=self._clock() - started,
            attempts=attempts,
        )
