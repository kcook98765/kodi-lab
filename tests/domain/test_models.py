from dataclasses import FrozenInstanceError
from uuid import uuid4

import pytest

from kodi_lab.domain import (
    ConnectionState,
    HealthStatus,
    Target,
    TargetAvailability,
    TargetType,
)


def test_valid_target_construction() -> None:
    target_id = uuid4()

    target = Target(
        id=target_id,
        name="Kodi 21 development",
        target_type=TargetType.EXTERNAL_DEVELOPMENT,
        kodi_version="21.3",
        availability=TargetAvailability.AVAILABLE,
        health=HealthStatus.HEALTHY,
        connection_state=ConnectionState.CONNECTED,
    )

    assert target.id == target_id
    assert target.target_type is TargetType.EXTERNAL_DEVELOPMENT


def test_target_uses_exact_prerelease_identity() -> None:
    beta_1 = Target(
        id=uuid4(),
        name="Kodi 22 Beta 1",
        target_type=TargetType.EXTERNAL_DEVELOPMENT,
        kodi_version="22.0-beta.1",
        availability=TargetAvailability.AVAILABLE,
        health=HealthStatus.HEALTHY,
        connection_state=ConnectionState.CONNECTED,
    )
    beta_2 = Target(
        id=uuid4(),
        name="Kodi 22 Beta 2",
        target_type=TargetType.EXTERNAL_DEVELOPMENT,
        kodi_version="22.0-beta.2",
        availability=TargetAvailability.AVAILABLE,
        health=HealthStatus.HEALTHY,
        connection_state=ConnectionState.CONNECTED,
    )
    assert beta_1.kodi_release != beta_2.kodi_release


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
            target_type=TargetType.EXTERNAL_DEVELOPMENT,
            kodi_version=kodi_version,
            availability=TargetAvailability.UNAVAILABLE,
            health=HealthStatus.WARNING,
            connection_state=ConnectionState.DISCONNECTED,
        )


def test_canonical_enum_values() -> None:
    assert [item.value for item in HealthStatus] == ["healthy", "warning", "error"]


def test_external_production_target_class_is_domain_immutable() -> None:
    target = Target(
        id=uuid4(),
        name="Production Kodi",
        target_type=TargetType.EXTERNAL_PRODUCTION,
        kodi_version=None,
        availability=TargetAvailability.UNAVAILABLE,
        health=HealthStatus.WARNING,
        connection_state=ConnectionState.DISCONNECTED,
    )
    with pytest.raises(FrozenInstanceError):
        target.target_type = TargetType.EXTERNAL_DEVELOPMENT  # type: ignore[misc]
