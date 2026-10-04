"""The exceptional broker operation remains exact and fail-closed."""

from uuid import UUID

import pytest

from kodi_lab.broker.docker_inventory import (
    ContainerRuntimeState,
    DeprovisionedManagedContainerRecord,
)
from kodi_lab.broker.errors import (
    OperationError,
    ProvisioningStateError,
    RuntimeImageUnavailableError,
)
from kodi_lab.broker.operations import (
    RemoveMismatchedManagedContainerParams,
    RemoveMismatchedManagedContainerResult,
    build_foundation_registry,
)
from kodi_lab.broker.provisioning import ManagedContainerProvisioningSpec
from kodi_lab.domain import PythonBytecodePolicy

INSTANCE = str(UUID(int=101))
OPERATION = str(UUID(int=102))
OLD = str(UUID(int=103))
DESIRED = str(UUID(int=104))
OLD_ID = "a" * 64
OLD_IMAGE = "sha256:" + "b" * 64
DESIRED_IMAGE = "sha256:" + "c" * 64


class Resolver:
    def __init__(self, *, deny: bool = False) -> None:
        self.deny = deny
        self.authorized = 0

    def desired_spec(self, _instance_id: str) -> ManagedContainerProvisioningSpec:
        return self._spec()

    def authorize_removal(self, **kwargs: object) -> ManagedContainerProvisioningSpec:
        self.authorized += 1
        if self.deny:
            raise ProvisioningStateError("stale lease")
        assert kwargs["old_container_id"] == OLD_ID
        assert kwargs["old_image_id"] == OLD_IMAGE
        assert kwargs["old_runtime_revision_id"] == OLD
        assert kwargs["desired_runtime_revision_id"] == DESIRED
        return self._spec()

    @staticmethod
    def _spec() -> ManagedContainerProvisioningSpec:
        return ManagedContainerProvisioningSpec(
            managed_instance_id=INSTANCE,
            runtime_revision_id=DESIRED,
            image_id=DESIRED_IMAGE,
            container_name=f"kodi-lab-managed-{UUID(INSTANCE).hex}",
            runtime_uid=1000,
            runtime_gid=1000,
            python_bytecode_policy=PythonBytecodePolicy.INTERPRETER_DEFAULT,
        )


class Provider:
    def __init__(self, *, image_missing: bool = False) -> None:
        self.image_missing = image_missing
        self.preflights = 0
        self.removals = 0

    def validate_reconstruction_destination(
        self, spec: ManagedContainerProvisioningSpec
    ) -> None:
        self.preflights += 1
        assert spec.image_id == DESIRED_IMAGE
        if self.image_missing:
            raise RuntimeImageUnavailableError("image gone")

    def deprovision_managed_container(
        self, **kwargs: object
    ) -> DeprovisionedManagedContainerRecord:
        self.removals += 1
        assert kwargs == {
            "managed_instance_id": INSTANCE,
            "expected_container_id": OLD_ID,
            "expected_runtime_revision_id": OLD,
            "expected_runtime_state": ContainerRuntimeState.STOPPED,
            "expected_image_id": OLD_IMAGE,
        }
        return DeprovisionedManagedContainerRecord(
            managed_instance_id=INSTANCE,
            removed_container_id=OLD_ID,
            runtime_revision_id=OLD,
            removed=True,
        )


def invoke(
    resolver: Resolver, provider: Provider
) -> RemoveMismatchedManagedContainerResult:
    registry = build_foundation_registry(
        reconstruction_resolver=resolver,  # type: ignore[arg-type]
        reconstruction_provider=provider,  # type: ignore[arg-type]
    )
    operation = registry.definition("remove_mismatched_managed_container")
    assert operation is not None
    params = RemoveMismatchedManagedContainerParams(
        managed_instance_id=INSTANCE,
        operation_id=OPERATION,
        lease_token_digest="d" * 64,
        owned_runtime_state_revision=2,
        old_container_id=OLD_ID,
        old_image_id=OLD_IMAGE,
        old_runtime_revision_id=OLD,
        desired_runtime_revision_id=DESIRED,
        expected_runtime_state=ContainerRuntimeState.STOPPED,
    )
    result = operation.handler(params)
    assert isinstance(result, RemoveMismatchedManagedContainerResult)
    return result


def test_broker_removal_uses_only_exact_confirmed_container() -> None:
    resolver = Resolver()
    provider = Provider()
    result = invoke(resolver, provider)
    assert result.removed and result.removed_container_id == OLD_ID
    assert resolver.authorized == provider.preflights == provider.removals == 1


@pytest.mark.parametrize("deny,image_missing", [(True, False), (False, True)])
def test_preflight_failure_skips_docker_removal(
    deny: bool, image_missing: bool
) -> None:
    resolver = Resolver(deny=deny)
    provider = Provider(image_missing=image_missing)
    with pytest.raises(OperationError):
        invoke(resolver, provider)
    assert provider.removals == 0
