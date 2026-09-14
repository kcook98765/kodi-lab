from uuid import uuid4

import pytest

from kodi_lab.domain import (
    ConnectionState,
    HealthStatus,
    InstanceRuntimeState,
    InstanceType,
    ManagedInstance,
    Target,
    TargetAvailability,
    TargetType,
)


def test_valid_target_construction() -> None:
    target_id = uuid4()

    target = Target(
        id=target_id,
        name="Kodi 21 development",
        target_type=TargetType.MANAGED,
        kodi_version="21.3",
        availability=TargetAvailability.AVAILABLE,
        health=HealthStatus.HEALTHY,
        connection_state=ConnectionState.CONNECTED,
    )

    assert target.id == target_id
    assert target.target_type is TargetType.MANAGED


def test_valid_managed_instance_construction() -> None:
    target_id = uuid4()
    revision_id = uuid4()

    instance = ManagedInstance(
        target_id=target_id,
        instance_type=InstanceType.PERSISTENT,
        runtime_revision_id=revision_id,
        runtime_state=InstanceRuntimeState.STOPPED,
    )

    assert instance.target_id == target_id
    assert instance.runtime_revision_id == revision_id
    assert instance.start_on_host_boot is False
    assert instance.notes is None


@pytest.mark.parametrize(
    ("name", "kodi_version", "invalid_field"),
    [("  ", "21.3", "name"), ("target", "  ", "kodi_version")],
)
def test_target_rejects_blank_required_text(
    name: str, kodi_version: str, invalid_field: str
) -> None:
    with pytest.raises(ValueError, match=invalid_field):
        Target(
            id=uuid4(),
            name=name,
            target_type=TargetType.MANAGED,
            kodi_version=kodi_version,
            availability=TargetAvailability.UNAVAILABLE,
            health=HealthStatus.WARNING,
            connection_state=ConnectionState.DISCONNECTED,
        )


def test_canonical_enum_values() -> None:
    assert [item.value for item in HealthStatus] == ["healthy", "warning", "error"]
    assert [item.value for item in InstanceType] == ["disposable", "persistent"]
    assert [item.value for item in InstanceRuntimeState] == ["running", "stopped"]
