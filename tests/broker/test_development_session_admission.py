"""The production registry wraps provisioning before any Docker creation."""

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from kodi_lab.broker.errors import RequestError
from kodi_lab.broker.managed_capacity import ManagedCapacityAdmission
from kodi_lab.broker.operations import build_foundation_registry
from kodi_lab.broker.provisioning import ManagedContainerProvisioningSpec
from kodi_lab.domain import PythonBytecodePolicy
from kodi_lab.storage.models import ManagedProvisionReservationRow
from tests.application import test_mcp_workspace_development as fixture
from tests.application.test_development_sessions import environment
from tests.broker.test_authorization_dispatch import request


def test_production_provision_uses_shared_durable_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = environment(tmp_path, monkeypatch)
    state.service.reserve(state.request)

    class Provider:
        calls = 0

        def list_managed_containers(self) -> Any:
            return tuple(state.records)

        def resolve(self, instance_id: str) -> ManagedContainerProvisioningSpec:
            return ManagedContainerProvisioningSpec(
                instance_id,
                str(fixture.RUNTIME_REVISION_ID),
                fixture.IMAGE_ID,
                "kodi-lab-managed-" + fixture.TARGET_ID.hex,
                1000,
                1000,
                PythonBytecodePolicy.SUPPRESS,
            )

        def provision_managed_container(self, _spec: Any) -> Any:
            with state.env.sessions() as session:
                assert (
                    session.get(ManagedProvisionReservationRow, str(fixture.TARGET_ID))
                    is not None
                )
            assert state.service.status().phase == "provisioning"
            self.calls += 1
            return state.dispatch()

    provider = Provider()
    monkeypatch.setattr(
        "kodi_lab.broker.operations.DockerManagedContainerAdapter", lambda: provider
    )
    monkeypatch.setattr(
        ManagedCapacityAdmission, "production", lambda _inventory: state.admission
    )
    registry = build_foundation_registry(provisioning_resolver=provider)
    outcome = registry.dispatch(
        request(
            "provision_managed_container",
            {"managed_instance_id": str(fixture.TARGET_ID)},
        )
    )
    assert outcome.model_dump()["created"] and provider.calls == 1
    assert state.service.status().phase == "bound"
    with state.env.sessions() as session:
        assert session.scalar(select(ManagedProvisionReservationRow)) is None
    state.records.clear()
    with pytest.raises(RequestError, match="session or reservation review"):
        registry.dispatch(
            request(
                "provision_managed_container",
                {"managed_instance_id": str(fixture.TARGET_ID)},
            )
        )
    assert provider.calls == 1
