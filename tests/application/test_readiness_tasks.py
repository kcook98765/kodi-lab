import threading
import time
from uuid import UUID, uuid4

from kodi_lab.application.readiness_tasks import (
    ReadinessTaskRegistry,
    ReadinessTaskState,
)
from kodi_lab.broker.docker_inventory import ContainerRuntimeState, KodiProcessState
from kodi_lab.kodi.readiness import (
    JsonRpcState,
    ReadinessCancelledError,
    ReadinessFailureReason,
    ReadinessResult,
    UsabilityState,
)


def usable_result() -> ReadinessResult:
    return ReadinessResult(
        container_state=ContainerRuntimeState.RUNNING,
        process_state=KodiProcessState.RUNNING,
        jsonrpc_state=JsonRpcState.RESPONSIVE,
        usability_state=UsabilityState.USABLE,
        failure_reason=None,
        last_observed_failure=None,
        identity=None,
        elapsed_seconds=1.0,
        attempts=1,
    )


def wait_for_state(
    registry: ReadinessTaskRegistry,
    instance_id: UUID,
    expected: ReadinessTaskState,
) -> None:
    for _ in range(100):
        if registry.snapshot(instance_id).state is expected:
            return
        time.sleep(0.005)
    raise AssertionError(f"readiness task did not reach {expected}")


def test_registry_deduplicates_active_task_and_cancels_it() -> None:
    registry = ReadinessTaskRegistry(max_workers=1)
    instance_id = uuid4()
    entered = threading.Event()
    runs = 0

    def run(cancelled: threading.Event) -> ReadinessResult:
        nonlocal runs
        runs += 1
        entered.set()
        cancelled.wait(timeout=2)
        raise ReadinessCancelledError()

    first = registry.start(instance_id, run)
    assert entered.wait(timeout=1)
    second = registry.start(instance_id, run)
    assert second.generation == first.generation
    assert runs == 1
    registry.cancel(instance_id)
    wait_for_state(registry, instance_id, ReadinessTaskState.CANCELLED)
    registry.close()


def test_registry_records_success_timeout_and_internal_failure() -> None:
    registry = ReadinessTaskRegistry(max_workers=1)
    successful_id = uuid4()
    registry.start(successful_id, lambda _cancelled: usable_result())
    wait_for_state(registry, successful_id, ReadinessTaskState.USABLE)

    timeout_id = uuid4()

    def timeout(_cancelled: threading.Event) -> ReadinessResult:
        return ReadinessResult(
            container_state=ContainerRuntimeState.RUNNING,
            process_state=KodiProcessState.STARTING,
            jsonrpc_state=JsonRpcState.UNAVAILABLE,
            usability_state=UsabilityState.NOT_USABLE,
            failure_reason=ReadinessFailureReason.READINESS_TIMEOUT,
            last_observed_failure=ReadinessFailureReason.PROCESS_HEALTH_FAILED,
            identity=None,
            elapsed_seconds=90,
            attempts=90,
        )

    registry.start(timeout_id, timeout)
    wait_for_state(registry, timeout_id, ReadinessTaskState.TIMED_OUT)

    failed_id = uuid4()

    def fail(_cancelled: threading.Event) -> ReadinessResult:
        raise RuntimeError("internal secret")

    registry.start(failed_id, fail)
    wait_for_state(registry, failed_id, ReadinessTaskState.FAILED)
    assert registry.snapshot(failed_id).result is None
    registry.close()


def test_replacement_waits_for_cancelled_predecessor() -> None:
    registry = ReadinessTaskRegistry(max_workers=2)
    instance_id = uuid4()
    first_entered = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()

    def first(cancelled: threading.Event) -> ReadinessResult:
        first_entered.set()
        assert cancelled.wait(timeout=1)
        release_first.wait(timeout=1)
        raise ReadinessCancelledError()

    def second(_cancelled: threading.Event) -> ReadinessResult:
        second_entered.set()
        return usable_result()

    registry.start(instance_id, first)
    assert first_entered.wait(timeout=1)
    registry.cancel(instance_id)
    registry.start(instance_id, second)
    assert not second_entered.wait(timeout=0.05)
    release_first.set()
    assert second_entered.wait(timeout=1)
    wait_for_state(registry, instance_id, ReadinessTaskState.USABLE)
    registry.close()
