import json
from collections.abc import Callable
from dataclasses import replace
from uuid import UUID

import pytest

from kodi_lab.broker.docker_inventory import (
    ContainerHealthState,
    ContainerRuntimeState,
    KodiProcessState,
)
from kodi_lab.broker.errors import ManagedRuntimeObservationClientError
from kodi_lab.broker.operations import (
    ManagedContainerInventoryEntry,
    ManagedContainerInventoryResult,
    ObserveManagedKodiRuntimeResult,
)
from kodi_lab.domain import (
    ProcessObservationKind,
    PythonBytecodePolicy,
    ReadinessProbeKind,
    RuntimeExecutionProfile,
    RuntimeKind,
)
from kodi_lab.kodi.readiness import (
    JsonRpcInvalidResponseError,
    JsonRpcState,
    JsonRpcUnavailableError,
    KodiApplicationIdentity,
    KodiJsonRpcReadinessAdapter,
    KodiReadinessCoordinator,
    ReadinessCancelledError,
    ReadinessFailureReason,
    UsabilityState,
)

INSTANCE_ID = str(UUID(int=1))
REVISION_ID = UUID(int=2)
CONTAINER_ID = "a" * 64


def profile() -> RuntimeExecutionProfile:
    return RuntimeExecutionProfile(
        runtime_revision_id=REVISION_ID,
        runtime_kind=RuntimeKind.KODI,
        runtime_uid=1000,
        runtime_gid=1000,
        python_bytecode_policy=PythonBytecodePolicy.INTERPRETER_DEFAULT,
        expected_application_name="Kodi",
        expected_kodi_version="21.3",
        expected_version_tag="stable",
        jsonrpc_port=8080,
        jsonrpc_path="/jsonrpc",
        process_observation=ProcessObservationKind.DOCKER_HEALTHCHECK,
        readiness_probe=ReadinessProbeKind.KODI_JSONRPC,
        readiness_timeout_seconds=90,
        poll_interval_seconds=1.0,
        http_timeout_seconds=3.0,
    )


def preview_profile() -> RuntimeExecutionProfile:
    values = profile()
    return RuntimeExecutionProfile(
        runtime_revision_id=values.runtime_revision_id,
        runtime_kind=values.runtime_kind,
        runtime_uid=values.runtime_uid,
        runtime_gid=values.runtime_gid,
        python_bytecode_policy=values.python_bytecode_policy,
        expected_application_name=values.expected_application_name,
        expected_kodi_version="22.0",
        expected_version_tag="beta",
        expected_prerelease_ordinal=2,
        jsonrpc_port=values.jsonrpc_port,
        jsonrpc_path=values.jsonrpc_path,
        process_observation=values.process_observation,
        readiness_probe=values.readiness_probe,
        readiness_timeout_seconds=values.readiness_timeout_seconds,
        poll_interval_seconds=values.poll_interval_seconds,
        http_timeout_seconds=values.http_timeout_seconds,
    )


def observation(
    process_state: KodiProcessState = KodiProcessState.RUNNING,
) -> ObserveManagedKodiRuntimeResult:
    return ObserveManagedKodiRuntimeResult(
        managed_instance_id=INSTANCE_ID,
        runtime_revision_id=str(REVISION_ID),
        container_id=CONTAINER_ID,
        runtime_state=ContainerRuntimeState.RUNNING,
        process_state=process_state,
        ipv4_address="10.250.0.9",
        jsonrpc_port=8080,
    )


class FakeTransport:
    def __init__(self, responses: list[bytes | BaseException]) -> None:
        self.responses = responses
        self.requests: list[dict[str, object]] = []

    def post(
        self, *, host: str, port: int, path: str, body: bytes, timeout: float
    ) -> bytes:
        assert host == "10.250.0.9"
        assert port == 8080
        assert path == "/jsonrpc"
        assert 0 < timeout <= 3
        self.requests.append(json.loads(body))
        result = self.responses.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def response(request_id: str, result: object) -> bytes:
    return json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}).encode()


def test_fixed_jsonrpc_ping_and_identity_methods() -> None:
    transport = FakeTransport(
        [
            response("kodi-lab-readiness-ping", "pong"),
            response(
                "kodi-lab-readiness-identity",
                {
                    "name": "Kodi",
                    "version": {"major": 21, "minor": 3, "tag": "stable"},
                },
            ),
        ]
    )
    adapter = KodiJsonRpcReadinessAdapter(observation(), profile(), transport=transport)
    adapter.ping()
    assert adapter.get_application_identity() == KodiApplicationIdentity(
        name="Kodi", major=21, minor=3, tag="stable"
    )
    assert transport.requests == [
        {
            "jsonrpc": "2.0",
            "id": "kodi-lab-readiness-ping",
            "method": "JSONRPC.Ping",
        },
        {
            "jsonrpc": "2.0",
            "id": "kodi-lab-readiness-identity",
            "method": "Application.GetProperties",
            "params": {"properties": ["name", "version"]},
        },
    ]
    assert not hasattr(adapter, "call")


def test_identity_preserves_observable_prerelease_ordinal() -> None:
    transport = FakeTransport(
        [
            response(
                "kodi-lab-readiness-identity",
                {
                    "name": "Kodi",
                    "version": {
                        "major": 22,
                        "minor": 0,
                        "tag": "beta",
                        "tagversion": "2",
                    },
                },
            )
        ]
    )
    identity = KodiJsonRpcReadinessAdapter(
        observation(), preview_profile(), transport=transport
    ).get_application_identity()
    assert identity == KodiApplicationIdentity("Kodi", 22, 0, "beta", 2)


@pytest.mark.parametrize(
    ("major", "minor", "tag", "ordinal", "canonical"),
    [
        (19, 4, "stable", None, "19.4"),
        (20, 5, "stable", "", "20.5"),
        (21, 3, "stable", None, "21.3"),
        (22, 0, "alpha", "1", "22.0-alpha.1"),
        (22, 0, "beta", "2", "22.0-beta.2"),
        (22, 0, "rc", "1", "22.0-rc.1"),
        (22, 0, "releasecandidate", "1", "22.0-rc.1"),
        (22, 0, "releasecandidate", 1, "22.0-rc.1"),
        (22, 0, "releasecandidate", "2", "22.0-rc.2"),
        (22, 1, "releasecandidate", "1", "22.1-rc.1"),
        (23, 0, "releasecandidate", "1", "23.0-rc.1"),
    ],
)
def test_native_release_identity_normalizes_only_the_channel(
    major: int, minor: int, tag: str, ordinal: int | str | None, canonical: str
) -> None:
    transport = FakeTransport(
        [
            response(
                "kodi-lab-readiness-identity",
                {
                    "name": "Kodi",
                    "version": {
                        "major": major,
                        "minor": minor,
                        "revision": "20260927-28ea2eac-dirty",
                        "tag": tag,
                        "tagversion": ordinal,
                    },
                },
            )
        ]
    )
    identity = KodiJsonRpcReadinessAdapter(
        observation(), profile(), transport=transport
    ).get_application_identity()
    assert identity.tag == tag
    assert identity.kodi_release.canonical == canonical


@pytest.mark.parametrize(
    "version",
    [
        {"major": 22, "minor": 0, "tag": "releasecandidate"},
        *[
            {"major": 22, "minor": 0, "tag": "releasecandidate", "tagversion": value}
            for value in (
                None,
                "",
                0,
                "0",
                -1,
                "-1",
                True,
                1.0,
                "1.0",
                "rc1",
                "+1",
                " 1",
                "²",
                "\u0661",
            )
        ],
        {"major": 22, "minor": 0, "tag": "stable", "tagversion": "1"},
        {"major": 22, "minor": 0, "tag": "ReleaseCandidate", "tagversion": "1"},
        {"major": 22, "minor": 0, "tagversion": "1"},
        {"minor": 0, "tag": "releasecandidate", "tagversion": "1"},
        {"major": 22, "tag": "releasecandidate", "tagversion": "1"},
        {"major": "22", "minor": 0, "tag": "releasecandidate", "tagversion": "1"},
        {"major": True, "minor": 0, "tag": "releasecandidate", "tagversion": "1"},
        {"major": 22, "minor": -1, "tag": "releasecandidate", "tagversion": "1"},
    ],
)
def test_native_identity_rejects_invalid_or_conflicting_release_fields(
    version: dict[str, object],
) -> None:
    adapter = KodiJsonRpcReadinessAdapter(
        observation(),
        profile(),
        transport=FakeTransport(
            [
                response(
                    "kodi-lab-readiness-identity", {"name": "Kodi", "version": version}
                )
            ]
        ),
    )
    with pytest.raises(JsonRpcInvalidResponseError):
        adapter.get_application_identity()


def test_kodi_19_identity_accepts_revision_without_broadening_release() -> None:
    transport = FakeTransport(
        [
            response(
                "kodi-lab-readiness-identity",
                {
                    "name": "Kodi",
                    "version": {
                        "major": 19,
                        "minor": 5,
                        "revision": "20221224-f8fdeb6b1b",
                        "tag": "stable",
                    },
                },
            )
        ]
    )
    identity = KodiJsonRpcReadinessAdapter(
        observation(), profile(), transport=transport
    ).get_application_identity()
    assert identity == KodiApplicationIdentity("Kodi", 19, 5, "stable")


@pytest.mark.parametrize(
    "payload",
    [
        b"not-json",
        b"{}",
        response("wrong-id", "pong"),
        response("kodi-lab-readiness-ping", "not-pong"),
    ],
)
def test_ping_rejects_malformed_jsonrpc(payload: bytes) -> None:
    adapter = KodiJsonRpcReadinessAdapter(
        observation(), profile(), transport=FakeTransport([payload])
    )
    with pytest.raises(JsonRpcInvalidResponseError):
        adapter.ping()


def test_transport_timeout_remains_unavailable() -> None:
    adapter = KodiJsonRpcReadinessAdapter(
        observation(),
        profile(),
        transport=FakeTransport([JsonRpcUnavailableError("timeout")]),
    )
    with pytest.raises(JsonRpcUnavailableError):
        adapter.ping()


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += seconds


class FakeBroker:
    def __init__(
        self,
        runtime_state: ContainerRuntimeState = ContainerRuntimeState.RUNNING,
        process_state: KodiProcessState = KodiProcessState.RUNNING,
    ) -> None:
        self.runtime_state = runtime_state
        self.process_state = process_state

    def managed_container_inventory(self) -> ManagedContainerInventoryResult:
        return ManagedContainerInventoryResult(
            containers=[
                ManagedContainerInventoryEntry(
                    managed_instance_id=INSTANCE_ID,
                    container_id=CONTAINER_ID,
                    image_id="sha256:" + ("b" * 64),
                    runtime_state=self.runtime_state,
                    health_state=ContainerHealthState.NOT_CONFIGURED,
                )
            ]
        )

    def observe_managed_kodi_runtime(
        self, *, managed_instance_id: str, expected_container_id: str
    ) -> ObserveManagedKodiRuntimeResult:
        assert managed_instance_id == INSTANCE_ID
        assert expected_container_id == CONTAINER_ID
        return observation(self.process_state)


class FailingObservationBroker(FakeBroker):
    def observe_managed_kodi_runtime(
        self, *, managed_instance_id: str, expected_container_id: str
    ) -> ObserveManagedKodiRuntimeResult:
        raise ManagedRuntimeObservationClientError(
            "managed_runtime_observation_failed",
            "process health unavailable",
            request_id=None,
        )


class BrokenObservationBroker(FakeBroker):
    def observe_managed_kodi_runtime(
        self, *, managed_instance_id: str, expected_container_id: str
    ) -> ObserveManagedKodiRuntimeResult:
        raise AssertionError("programming defect")


class FakeAdapter:
    def __init__(
        self,
        *,
        identity: KodiApplicationIdentity | None = None,
        ping_error: BaseException | None = None,
    ) -> None:
        self.identity = identity or KodiApplicationIdentity("Kodi", 21, 3, "stable")
        self.ping_error = ping_error

    def ping(self) -> None:
        if self.ping_error is not None:
            raise self.ping_error

    def get_application_identity(self) -> KodiApplicationIdentity:
        return self.identity


def factory(adapter: FakeAdapter) -> Callable[..., FakeAdapter]:
    return lambda *_args: adapter


def test_readiness_success_requires_all_four_layers() -> None:
    clock = FakeClock()
    result = KodiReadinessCoordinator(
        FakeBroker(),
        profile(),
        clock=clock,
        sleep=clock.sleep,
        adapter_factory=factory(FakeAdapter()),  # type: ignore[arg-type]
    ).wait_until_usable(INSTANCE_ID)
    assert result.usability_state is UsabilityState.USABLE
    assert result.container_state is ContainerRuntimeState.RUNNING
    assert result.process_state is KodiProcessState.RUNNING
    assert result.jsonrpc_state is JsonRpcState.RESPONSIVE
    assert result.failure_reason is None


@pytest.mark.parametrize(
    ("runtime_state", "process_state", "last_failure"),
    [
        (
            ContainerRuntimeState.STOPPED,
            KodiProcessState.RUNNING,
            ReadinessFailureReason.CONTAINER_NOT_RUNNING,
        ),
        (
            ContainerRuntimeState.RUNNING,
            KodiProcessState.NOT_RUNNING,
            ReadinessFailureReason.KODI_PROCESS_NOT_RUNNING,
        ),
        (
            ContainerRuntimeState.RUNNING,
            KodiProcessState.STARTING,
            ReadinessFailureReason.PROCESS_HEALTH_FAILED,
        ),
    ],
)
def test_readiness_total_deadline_preserves_specific_last_failure(
    runtime_state: ContainerRuntimeState,
    process_state: KodiProcessState,
    last_failure: ReadinessFailureReason,
) -> None:
    clock = FakeClock()
    result = KodiReadinessCoordinator(
        FakeBroker(runtime_state, process_state),
        profile(),
        clock=clock,
        sleep=clock.sleep,
        adapter_factory=factory(FakeAdapter()),  # type: ignore[arg-type]
    ).wait_until_usable(INSTANCE_ID)
    assert result.failure_reason is ReadinessFailureReason.READINESS_TIMEOUT
    assert result.last_observed_failure is last_failure
    assert result.elapsed_seconds == 90
    assert result.attempts == 90


def test_unreachable_jsonrpc_times_out_with_specific_last_failure() -> None:
    clock = FakeClock()
    result = KodiReadinessCoordinator(
        FakeBroker(),
        profile(),
        clock=clock,
        sleep=clock.sleep,
        adapter_factory=factory(FakeAdapter(ping_error=JsonRpcUnavailableError())),  # type: ignore[arg-type]
    ).wait_until_usable(INSTANCE_ID)
    assert result.failure_reason is ReadinessFailureReason.READINESS_TIMEOUT
    assert result.last_observed_failure is ReadinessFailureReason.JSONRPC_UNREACHABLE


def test_typed_broker_observation_failure_remains_process_health_failure() -> None:
    clock = FakeClock()
    result = KodiReadinessCoordinator(
        FailingObservationBroker(),
        profile(),
        clock=clock,
        sleep=clock.sleep,
        adapter_factory=factory(FakeAdapter()),  # type: ignore[arg-type]
    ).wait_until_usable(INSTANCE_ID)
    assert result.failure_reason is ReadinessFailureReason.READINESS_TIMEOUT
    assert result.last_observed_failure is ReadinessFailureReason.PROCESS_HEALTH_FAILED


def test_programming_errors_are_not_hidden_as_readiness_timeouts() -> None:
    with pytest.raises(AssertionError, match="programming defect"):
        KodiReadinessCoordinator(
            BrokenObservationBroker(),
            profile(),
            adapter_factory=factory(FakeAdapter()),  # type: ignore[arg-type]
        ).wait_until_usable(INSTANCE_ID)


def test_readiness_can_be_cancelled_by_ephemeral_web_registry() -> None:
    with pytest.raises(ReadinessCancelledError):
        KodiReadinessCoordinator(
            FakeBroker(),
            profile(),
            adapter_factory=factory(FakeAdapter()),  # type: ignore[arg-type]
        ).wait_until_usable(INSTANCE_ID, cancelled=lambda: True)


def test_identity_probe_does_not_start_after_total_deadline() -> None:
    clock = FakeClock()

    class DeadlineBroker(FakeBroker):
        def managed_container_inventory(self) -> ManagedContainerInventoryResult:
            clock.value += 87.0
            return super().managed_container_inventory()

    class DeadlineAdapter(FakeAdapter):
        def ping(self) -> None:
            clock.value += 3.0

        def get_application_identity(self) -> KodiApplicationIdentity:
            raise AssertionError("identity probe started after deadline")

    result = KodiReadinessCoordinator(
        DeadlineBroker(),
        profile(),
        clock=clock,
        sleep=clock.sleep,
        adapter_factory=factory(DeadlineAdapter()),  # type: ignore[arg-type]
    ).wait_until_usable(INSTANCE_ID)
    assert result.failure_reason is ReadinessFailureReason.READINESS_TIMEOUT
    assert result.elapsed_seconds == 90.0


@pytest.mark.parametrize(
    ("identity", "reason"),
    [
        (
            KodiApplicationIdentity("Other", 21, 3, "stable"),
            ReadinessFailureReason.UNEXPECTED_APPLICATION_IDENTITY,
        ),
        (
            KodiApplicationIdentity("Kodi", 21, 2, "stable"),
            ReadinessFailureReason.UNEXPECTED_KODI_VERSION,
        ),
        (
            KodiApplicationIdentity("Kodi", 21, 3, "beta", 1),
            ReadinessFailureReason.UNEXPECTED_KODI_VERSION,
        ),
    ],
)
def test_identity_mismatch_fails_without_waiting(
    identity: KodiApplicationIdentity, reason: ReadinessFailureReason
) -> None:
    clock = FakeClock()
    result = KodiReadinessCoordinator(
        FakeBroker(),
        profile(),
        clock=clock,
        sleep=clock.sleep,
        adapter_factory=factory(FakeAdapter(identity=identity)),  # type: ignore[arg-type]
    ).wait_until_usable(INSTANCE_ID)
    assert result.failure_reason is reason
    assert result.elapsed_seconds == 0


def test_prerelease_ordinal_mismatch_is_not_compatible() -> None:
    clock = FakeClock()
    result = KodiReadinessCoordinator(
        FakeBroker(),
        preview_profile(),
        clock=clock,
        sleep=clock.sleep,
        adapter_factory=factory(
            FakeAdapter(identity=KodiApplicationIdentity("Kodi", 22, 0, "beta", 1))
        ),  # type: ignore[arg-type]
    ).wait_until_usable(INSTANCE_ID)
    assert result.failure_reason is ReadinessFailureReason.UNEXPECTED_KODI_VERSION
    assert result.elapsed_seconds == 0


@pytest.mark.parametrize(
    ("major", "minor", "tag", "ordinal", "usable"),
    [
        (22, 0, "releasecandidate", "1", True),
        (22, 0, "releasecandidate", "2", False),
        (22, 1, "releasecandidate", "1", False),
        (23, 0, "releasecandidate", "1", False),
        (22, 0, "beta", "1", False),
        (22, 0, "stable", "", False),
    ],
)
def test_rc_readiness_requires_the_exact_profile_release(
    major: int, minor: int, tag: str, ordinal: str, usable: bool
) -> None:
    rc_profile = replace(
        preview_profile(), expected_version_tag="rc", expected_prerelease_ordinal=1
    )
    adapter = KodiJsonRpcReadinessAdapter(
        observation(),
        rc_profile,
        transport=FakeTransport(
            [
                response("kodi-lab-readiness-ping", "pong"),
                response(
                    "kodi-lab-readiness-identity",
                    {
                        "name": "Kodi",
                        "version": {
                            "major": major,
                            "minor": minor,
                            "tag": tag,
                            "tagversion": ordinal,
                        },
                    },
                ),
            ]
        ),
    )
    clock = FakeClock()
    result = KodiReadinessCoordinator(
        FakeBroker(),
        rc_profile,
        clock=clock,
        sleep=clock.sleep,
        adapter_factory=lambda *_args: adapter,
    ).wait_until_usable(INSTANCE_ID)
    assert result.usability_state is (
        UsabilityState.USABLE if usable else UsabilityState.NOT_USABLE
    )
    assert result.failure_reason is (
        None if usable else ReadinessFailureReason.UNEXPECTED_KODI_VERSION
    )
    assert result.elapsed_seconds == 0


def test_invalid_jsonrpc_is_protocol_error_not_generic_timeout() -> None:
    clock = FakeClock()
    result = KodiReadinessCoordinator(
        FakeBroker(),
        profile(),
        clock=clock,
        sleep=clock.sleep,
        adapter_factory=factory(FakeAdapter(ping_error=JsonRpcInvalidResponseError())),  # type: ignore[arg-type]
    ).wait_until_usable(INSTANCE_ID)
    assert result.failure_reason is ReadinessFailureReason.JSONRPC_INVALID_RESPONSE
    assert result.jsonrpc_state is JsonRpcState.PROTOCOL_ERROR
