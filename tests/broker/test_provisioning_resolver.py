"""Trusted SQLite provisioning resolution tests."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from kodi_lab.broker.errors import (
    PersistentProvisioningUnsupportedError,
    ProvisioningStateError,
    RuntimeContainerArtifactUnavailableError,
    RuntimeExecutionProfileUnavailableError,
)
from kodi_lab.broker.provisioning import (
    MANAGED_NETWORK_NAME,
    MANAGED_NETWORK_SUBNET,
    ManagedContainerProvisioningSpec,
    SqliteManagedInstanceRevisionResolver,
    SqliteProvisioningResolver,
    SqliteRuntimeOwnershipResolver,
)
from kodi_lab.domain import (
    ConnectionState,
    HealthStatus,
    InstanceRuntimeState,
    InstanceType,
    ManagedInstance,
    ProcessObservationKind,
    PythonBytecodePolicy,
    ReadinessProbeKind,
    RuntimeContainerArtifact,
    RuntimeDefinition,
    RuntimeExecutionProfile,
    RuntimeKind,
    RuntimeRevision,
    Target,
    TargetAvailability,
    TargetType,
)
from kodi_lab.storage import metadata
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.storage.repositories import (
    ManagedInstanceRepository,
    RuntimeContainerArtifactRepository,
    RuntimeDefinitionRepository,
    RuntimeExecutionProfileRepository,
    RuntimeRevisionRepository,
    TargetRepository,
)

INSTANCE_ID = UUID(int=1)
REVISION_ID = UUID(int=2)
DEFINITION_ID = UUID(int=3)
IMAGE_ID = "sha256:" + ("a" * 64)


def resolver_with_state(
    tmp_path: Path,
    *,
    instance_type: InstanceType = InstanceType.DISPOSABLE,
    include_artifact: bool = True,
    remove_pinned_revision: bool = False,
    include_profile: bool = True,
) -> SqliteProvisioningResolver:
    tmp_path.mkdir(parents=True, exist_ok=True)
    engine = create_database_engine(f"sqlite:///{tmp_path / 'resolver.db'}")
    metadata.create_all(engine)
    factory = create_session_factory(engine)
    with factory.begin() as session:
        TargetRepository(session).create(
            Target(
                id=INSTANCE_ID,
                name="Caller cannot turn this into a Docker name",
                target_type=TargetType.MANAGED,
                kodi_version="21.3",
                availability=TargetAvailability.UNAVAILABLE,
                health=HealthStatus.WARNING,
                connection_state=ConnectionState.DISCONNECTED,
            )
        )
        RuntimeDefinitionRepository(session).create(
            RuntimeDefinition(
                id=DEFINITION_ID, slug="caller-cannot-use-slug", kodi_version="21.3"
            )
        )
        RuntimeRevisionRepository(session).create(
            RuntimeRevision(
                id=REVISION_ID,
                runtime_definition_id=DEFINITION_ID,
                revision_number=1,
                source_kind="PRIVATE-untrusted-as-docker-image",
                source_reference="PRIVATE:mutable-tag",
                content_digest="b" * 64,
                manifest_reference="catalog/test.yaml",
                manifest_digest="c" * 64,
                built_at=datetime(2026, 9, 14, tzinfo=UTC),
            )
        )
        ManagedInstanceRepository(session).create(
            ManagedInstance(
                target_id=INSTANCE_ID,
                instance_type=instance_type,
                runtime_revision_id=REVISION_ID,
                runtime_state=InstanceRuntimeState.STOPPED,
            )
        )
        if include_artifact:
            RuntimeContainerArtifactRepository(session).create(
                RuntimeContainerArtifact(
                    runtime_revision_id=REVISION_ID, image_id=IMAGE_ID
                )
            )
        if include_profile:
            RuntimeExecutionProfileRepository(session).create(
                RuntimeExecutionProfile(
                    runtime_revision_id=REVISION_ID,
                    runtime_kind=RuntimeKind.KODI,
                    runtime_uid=1234,
                    runtime_gid=2345,
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
            )
    if remove_pinned_revision:
        with engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            connection.exec_driver_sql(
                "DELETE FROM runtime_container_artifacts WHERE runtime_revision_id = ?",
                (str(REVISION_ID),),
            )
            connection.exec_driver_sql(
                "DELETE FROM runtime_revisions WHERE id = ?", (str(REVISION_ID),)
            )
            connection.commit()
    return SqliteProvisioningResolver(factory)


def test_resolver_uses_exact_pinned_artifact_and_fixed_policy(tmp_path: Path) -> None:
    spec = resolver_with_state(tmp_path).resolve(str(INSTANCE_ID))
    assert spec.managed_instance_id == str(INSTANCE_ID)
    assert spec.runtime_revision_id == str(REVISION_ID)
    assert spec.image_id == IMAGE_ID
    assert spec.container_name == f"kodi-lab-managed-{INSTANCE_ID.hex}"
    assert spec.python_bytecode_policy is PythonBytecodePolicy.INTERPRETER_DEFAULT
    assert spec.network_name == MANAGED_NETWORK_NAME
    assert "PRIVATE" not in repr(spec)


def test_missing_artifact_and_persistent_instance_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(RuntimeContainerArtifactUnavailableError):
        resolver_with_state(tmp_path / "missing", include_artifact=False).resolve(
            str(INSTANCE_ID)
        )
    with pytest.raises(PersistentProvisioningUnsupportedError):
        resolver_with_state(
            tmp_path / "persistent", instance_type=InstanceType.PERSISTENT
        ).resolve(str(INSTANCE_ID))


def test_corrupt_missing_pinned_revision_fails_closed(tmp_path: Path) -> None:
    resolver = resolver_with_state(tmp_path, remove_pinned_revision=True)
    with pytest.raises(ProvisioningStateError, match="revision does not exist"):
        resolver.resolve(str(INSTANCE_ID))


def test_missing_or_noncanonical_instance_cannot_resolve(tmp_path: Path) -> None:
    resolver = resolver_with_state(tmp_path)
    with pytest.raises(ProvisioningStateError):
        resolver.resolve(str(UUID(int=99)))
    with pytest.raises(ProvisioningStateError):
        resolver.resolve("AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA")


@pytest.mark.parametrize(
    "overrides",
    [
        {"container_name": "caller-selected"},
        {"network_name": "legacy"},
    ],
)
def test_trusted_spec_rejects_noncanonical_policy_substitutions(
    overrides: dict[str, str],
) -> None:
    values: dict[str, Any] = {
        "managed_instance_id": str(INSTANCE_ID),
        "runtime_revision_id": str(REVISION_ID),
        "image_id": IMAGE_ID,
        "container_name": f"kodi-lab-managed-{INSTANCE_ID.hex}",
        "python_bytecode_policy": PythonBytecodePolicy.INTERPRETER_DEFAULT,
        "network_name": MANAGED_NETWORK_NAME,
    }
    values.update(overrides)
    with pytest.raises(ValueError):
        ManagedContainerProvisioningSpec(**values)


def test_fixed_network_policy_is_not_caller_selectable(tmp_path: Path) -> None:
    resolved = resolver_with_state(tmp_path).resolve(str(INSTANCE_ID))
    assert resolved.network_name == MANAGED_NETWORK_NAME
    assert MANAGED_NETWORK_SUBNET == "10.250.0.0/24"


def revision_resolver_for(path: Path) -> SqliteManagedInstanceRevisionResolver:
    engine = create_database_engine(f"sqlite:///{path / 'resolver.db'}")
    return SqliteManagedInstanceRevisionResolver(create_session_factory(engine))


def test_revision_resolver_returns_exact_persisted_pin(tmp_path: Path) -> None:
    resolver_with_state(tmp_path)
    resolver = revision_resolver_for(tmp_path)
    assert resolver.resolve_runtime_revision_id(str(INSTANCE_ID)) == str(REVISION_ID)


def test_revision_resolver_rejects_missing_instance_and_revision(
    tmp_path: Path,
) -> None:
    resolver_with_state(tmp_path / "valid")
    valid = revision_resolver_for(tmp_path / "valid")
    with pytest.raises(ProvisioningStateError):
        valid.resolve_runtime_revision_id(str(UUID(int=99)))

    resolver_with_state(tmp_path / "corrupt", remove_pinned_revision=True)
    corrupt = revision_resolver_for(tmp_path / "corrupt")
    with pytest.raises(ProvisioningStateError, match="revision does not exist"):
        corrupt.resolve_runtime_revision_id(str(INSTANCE_ID))


def ownership_resolver_for(path: Path) -> SqliteRuntimeOwnershipResolver:
    engine = create_database_engine(f"sqlite:///{path / 'resolver.db'}")
    return SqliteRuntimeOwnershipResolver(create_session_factory(engine))


def test_ownership_resolver_uses_only_pinned_execution_profile(tmp_path: Path) -> None:
    resolver_with_state(tmp_path)
    resolved = ownership_resolver_for(tmp_path).resolve(str(INSTANCE_ID))
    assert resolved.managed_instance_id == str(INSTANCE_ID)
    assert resolved.runtime_revision_id == str(REVISION_ID)
    assert (resolved.runtime_uid, resolved.runtime_gid) == (1234, 2345)


def test_ownership_resolver_has_no_caller_ownership_parameters(tmp_path: Path) -> None:
    resolver_with_state(tmp_path)
    resolver = ownership_resolver_for(tmp_path)
    with pytest.raises(TypeError):
        resolver.resolve(str(INSTANCE_ID), runtime_uid=0)  # type: ignore[call-arg]


def test_ownership_resolver_fails_closed_without_profile(tmp_path: Path) -> None:
    resolver_with_state(tmp_path, include_profile=False)
    with pytest.raises(RuntimeExecutionProfileUnavailableError):
        resolver_with_state(tmp_path / "provisioning", include_profile=False).resolve(
            str(INSTANCE_ID)
        )
    with pytest.raises(RuntimeExecutionProfileUnavailableError):
        ownership_resolver_for(tmp_path).resolve(str(INSTANCE_ID))
