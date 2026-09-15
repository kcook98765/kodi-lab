from uuid import uuid4

import pytest

from kodi_lab.application.reset_activity import (
    ResetActivityCapacityError,
    ResetActivityConflictError,
    ResetActivityRegistry,
    ResetActivityState,
    ResetOutcome,
)


def test_registry_allows_one_ordered_reset_per_instance() -> None:
    instance_id = uuid4()
    registry = ResetActivityRegistry()
    active = registry.begin(instance_id)
    with pytest.raises(ResetActivityConflictError):
        registry.begin(instance_id)
    registry.transition(instance_id, active.generation, ResetActivityState.PROVISIONING)
    with pytest.raises(ResetActivityConflictError):
        registry.transition(instance_id, active.generation, ResetActivityState.STOPPING)
    completed = registry.succeed(instance_id, active.generation)
    assert completed.last_outcome is ResetOutcome.SUCCEEDED


def test_subsequent_reset_replaces_result_and_instances_are_independent() -> None:
    first_id, second_id = uuid4(), uuid4()
    registry = ResetActivityRegistry()
    first = registry.begin(first_id)
    registry.begin(second_id)
    registry.fail(
        first_id,
        first.generation,
        failure_phase="provision",
        failure_reason="bounded reason",
        reconciliation_required=False,
    )
    assert registry.snapshot(second_id).active
    replacement = registry.begin(first_id)
    registry.succeed(first_id, replacement.generation)
    assert replacement.generation == 2
    assert registry.snapshot(first_id).failure_reason is None
    assert registry.snapshot(first_id).last_outcome is ResetOutcome.SUCCEEDED


def test_failure_is_bounded_and_reconciliation_is_semantic() -> None:
    instance_id = uuid4()
    registry = ResetActivityRegistry()
    activity = registry.begin(instance_id)
    result = registry.fail(
        instance_id,
        activity.generation,
        failure_phase="x" * 100,
        failure_reason=" secret-safe " + ("x" * 300),
        reconciliation_required=True,
    )
    assert result.state is ResetActivityState.RECONCILIATION_REQUIRED
    assert result.last_outcome is ResetOutcome.RECONCILIATION_REQUIRED
    assert len(result.failure_phase or "") == 64
    assert len(result.failure_reason or "") == 256


def test_registry_cleanup_is_bounded_and_does_not_evict_active_work() -> None:
    registry = ResetActivityRegistry(max_entries=2)
    first_id, second_id, third_id = uuid4(), uuid4(), uuid4()
    first = registry.begin(first_id)
    registry.succeed(first_id, first.generation)
    registry.begin(second_id)
    registry.begin(third_id)
    assert registry.snapshot(first_id).state is ResetActivityState.IDLE
    assert registry.snapshot(second_id).active
    assert registry.snapshot(third_id).active

    full = ResetActivityRegistry(max_entries=1)
    full.begin(first_id)
    with pytest.raises(ResetActivityCapacityError):
        full.begin(second_id)


def test_new_process_registry_reconstructs_no_stale_activity() -> None:
    instance_id = uuid4()
    previous = ResetActivityRegistry()
    activity = previous.begin(instance_id)
    previous.succeed(instance_id, activity.generation)
    restarted = ResetActivityRegistry()
    assert restarted.snapshot(instance_id).state is ResetActivityState.IDLE
    assert restarted.snapshot(instance_id).last_outcome is ResetOutcome.NONE
