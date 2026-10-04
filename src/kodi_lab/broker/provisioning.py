"""Trusted read-only resolution of canonical container provisioning state."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.broker.errors import (
    PersistentProvisioningUnsupportedError,
    ProvisioningStateError,
    RuntimeContainerArtifactUnavailableError,
    RuntimeExecutionProfileUnavailableError,
)
from kodi_lab.config import Settings
from kodi_lab.domain import (
    InstanceType,
    ManagedInstanceMutationKind,
    PythonBytecodePolicy,
    RevisionApprovalState,
    RuntimeExecutionProfile,
)
from kodi_lab.storage.engine import create_database_engine
from kodi_lab.storage.models import (
    ManagedInstanceMutationLeaseRow,
    ManagedInstanceRow,
    ManagedRuntimeReconstructionRow,
)
from kodi_lab.storage.repositories import (
    ManagedInstanceRepository,
    RuntimeContainerArtifactRepository,
    RuntimeDefinitionRepository,
    RuntimeExecutionProfileRepository,
    RuntimeRevisionRepository,
    TargetRepository,
)

MANAGED_NETWORK_NAME = "kodi-lab-managed-v1"
MANAGED_NETWORK_SUBNET = "10.250.0.0/24"
MANAGED_NETWORK_DRIVER = "bridge"
RUNTIME_REVISION_LABEL_KEY = "io.kodi-lab.runtime-revision-id"


@dataclass(frozen=True, slots=True)
class ManagedContainerProvisioningSpec:
    """All immutable broker-trusted values needed for Docker provisioning."""

    managed_instance_id: str
    runtime_revision_id: str
    image_id: str
    container_name: str
    runtime_uid: int
    runtime_gid: int
    python_bytecode_policy: PythonBytecodePolicy
    network_name: str = MANAGED_NETWORK_NAME

    def __post_init__(self) -> None:
        try:
            instance_id = UUID(self.managed_instance_id)
            revision_id = UUID(self.runtime_revision_id)
        except ValueError as error:
            raise ValueError("provisioning identities must be UUIDs") from error
        if str(instance_id) != self.managed_instance_id:
            raise ValueError("managed instance identity must be canonical")
        if str(revision_id) != self.runtime_revision_id:
            raise ValueError("runtime revision identity must be canonical")
        if self.image_id != "sha256:" + self.image_id.removeprefix("sha256:"):
            raise ValueError("image identity must be canonical")
        digest = self.image_id.removeprefix("sha256:")
        if len(digest) != 64 or any(
            character not in "0123456789abcdef" for character in digest
        ):
            raise ValueError("image identity must be immutable SHA-256")
        if self.container_name != f"kodi-lab-managed-{instance_id.hex}":
            raise ValueError("container name must be derived from instance identity")
        for field_name in ("runtime_uid", "runtime_gid"):
            value = getattr(self, field_name)
            if type(value) is not int or not 1 <= value <= 2**31 - 1:
                raise ValueError(f"{field_name} must be a non-root POSIX ID")
        if not isinstance(self.python_bytecode_policy, PythonBytecodePolicy):
            raise ValueError("Python bytecode policy must be trusted closed semantics")
        if self.network_name != MANAGED_NETWORK_NAME:
            raise ValueError("network name must use fixed managed policy")


class ProvisioningResolver(Protocol):
    def resolve(self, managed_instance_id: str) -> ManagedContainerProvisioningSpec:
        """Resolve one canonical managed instance without accepting substitutions."""


class ManagedInstanceRevisionResolver(Protocol):
    def resolve_runtime_revision_id(self, managed_instance_id: str) -> str:
        """Return the independently persisted runtime-revision pin."""


@dataclass(frozen=True, slots=True)
class ManagedRuntimeObservationSpec:
    """Trusted persisted identity and profile for narrow runtime observation."""

    managed_instance_id: str
    runtime_revision_id: str
    profile: RuntimeExecutionProfile


class RuntimeObservationResolver(Protocol):
    def resolve(self, managed_instance_id: str) -> ManagedRuntimeObservationSpec:
        """Resolve exact trusted runtime observation semantics."""


@dataclass(frozen=True, slots=True)
class ManagedRuntimeOwnershipSpec:
    """Broker-trusted runtime filesystem ownership for one pinned instance."""

    managed_instance_id: str
    runtime_revision_id: str
    runtime_uid: int
    runtime_gid: int


class RuntimeOwnershipResolver(Protocol):
    def resolve(self, managed_instance_id: str) -> ManagedRuntimeOwnershipSpec:
        """Resolve ownership without accepting caller-provided UID or GID."""


class SqliteProvisioningResolver:
    """Read-only resolver over existing Phase 1 repositories."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    @classmethod
    def production(cls) -> SqliteProvisioningResolver:
        engine = create_database_engine(Settings().database_url)
        return cls(sessionmaker(engine, expire_on_commit=False))

    def resolve(self, managed_instance_id: str) -> ManagedContainerProvisioningSpec:
        try:
            identity = UUID(managed_instance_id)
            if str(identity) != managed_instance_id:
                raise ValueError
        except ValueError as error:
            raise ProvisioningStateError(
                "Managed instance identity is not canonical"
            ) from error

        try:
            with self._session_factory() as session:
                target = TargetRepository(session).get(identity)
                instance = ManagedInstanceRepository(session).get(identity)
                if target is None or instance is None:
                    raise ProvisioningStateError("Managed instance does not exist")
                if instance.instance_type is not InstanceType.DISPOSABLE:
                    raise PersistentProvisioningUnsupportedError(
                        "Persistent provisioning is not supported"
                    )
                revision = RuntimeRevisionRepository(session).get(
                    instance.runtime_revision_id
                )
                if revision is None:
                    raise ProvisioningStateError(
                        "Pinned runtime revision does not exist"
                    )
                definition = RuntimeDefinitionRepository(session).get(
                    revision.runtime_definition_id
                )
                if definition is None:
                    raise ProvisioningStateError(
                        "Pinned runtime definition does not exist"
                    )
                if target.kodi_release != definition.kodi_release:
                    raise ProvisioningStateError(
                        "Pinned runtime definition is incompatible with target"
                    )
                artifact = RuntimeContainerArtifactRepository(session).get(revision.id)
                if artifact is None:
                    raise RuntimeContainerArtifactUnavailableError(
                        "Pinned runtime container artifact does not exist"
                    )
                profile = RuntimeExecutionProfileRepository(session).get(revision.id)
                if profile is None:
                    raise RuntimeExecutionProfileUnavailableError(
                        "Pinned runtime execution profile does not exist"
                    )
                if profile.runtime_uid == 0 or profile.runtime_gid == 0:
                    raise RuntimeExecutionProfileUnavailableError(
                        "Pinned runtime execution profile is not non-root"
                    )
                return ManagedContainerProvisioningSpec(
                    managed_instance_id=managed_instance_id,
                    runtime_revision_id=str(revision.id),
                    image_id=artifact.image_id,
                    container_name=f"kodi-lab-managed-{identity.hex}",
                    runtime_uid=profile.runtime_uid,
                    runtime_gid=profile.runtime_gid,
                    python_bytecode_policy=profile.python_bytecode_policy,
                )
        except ProvisioningStateError:
            raise
        except SQLAlchemyError as error:
            raise ProvisioningStateError(
                "Canonical provisioning state could not be read"
            ) from error


class SqliteManagedInstanceRevisionResolver:
    """Read-only resolver for destructive-operation revision preconditions."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    @classmethod
    def production(cls) -> SqliteManagedInstanceRevisionResolver:
        engine = create_database_engine(Settings().database_url)
        return cls(sessionmaker(engine, expire_on_commit=False))

    def resolve_runtime_revision_id(self, managed_instance_id: str) -> str:
        try:
            identity = UUID(managed_instance_id)
            if str(identity) != managed_instance_id:
                raise ValueError
        except ValueError as error:
            raise ProvisioningStateError(
                "Managed instance identity is not canonical"
            ) from error

        try:
            with self._session_factory() as session:
                target = TargetRepository(session).get(identity)
                instance = ManagedInstanceRepository(session).get(identity)
                if target is None or instance is None:
                    raise ProvisioningStateError("Managed instance does not exist")
                revision = RuntimeRevisionRepository(session).get(
                    instance.runtime_revision_id
                )
                if revision is None:
                    raise ProvisioningStateError(
                        "Pinned runtime revision does not exist"
                    )
                return str(revision.id)
        except ProvisioningStateError:
            raise
        except SQLAlchemyError as error:
            raise ProvisioningStateError(
                "Canonical managed-instance state could not be read"
            ) from error


class SqliteRuntimeObservationResolver:
    """Read-only resolver for trusted managed-runtime observation semantics."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    @classmethod
    def production(cls) -> SqliteRuntimeObservationResolver:
        engine = create_database_engine(Settings().database_url)
        return cls(sessionmaker(engine, expire_on_commit=False))

    def resolve(self, managed_instance_id: str) -> ManagedRuntimeObservationSpec:
        try:
            identity = UUID(managed_instance_id)
            if str(identity) != managed_instance_id:
                raise ValueError
        except ValueError as error:
            raise ProvisioningStateError(
                "Managed instance identity is not canonical"
            ) from error
        try:
            with self._session_factory() as session:
                instance = ManagedInstanceRepository(session).get(identity)
                if instance is None:
                    raise ProvisioningStateError("Managed instance does not exist")
                profile = RuntimeExecutionProfileRepository(session).get(
                    instance.runtime_revision_id
                )
                if profile is None:
                    raise RuntimeExecutionProfileUnavailableError(
                        "Pinned runtime execution profile does not exist"
                    )
                return ManagedRuntimeObservationSpec(
                    managed_instance_id=managed_instance_id,
                    runtime_revision_id=str(instance.runtime_revision_id),
                    profile=profile,
                )
        except ProvisioningStateError:
            raise
        except SQLAlchemyError as error:
            raise ProvisioningStateError(
                "Canonical runtime observation state could not be read"
            ) from error


class SqliteRuntimeOwnershipResolver:
    """Read-only ManagedInstance-to-execution-profile ownership resolver."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    @classmethod
    def production(cls) -> SqliteRuntimeOwnershipResolver:
        engine = create_database_engine(Settings().database_url)
        return cls(sessionmaker(engine, expire_on_commit=False))

    def resolve(self, managed_instance_id: str) -> ManagedRuntimeOwnershipSpec:
        try:
            identity = UUID(managed_instance_id)
            if str(identity) != managed_instance_id:
                raise ValueError
        except ValueError as error:
            raise ProvisioningStateError(
                "Managed instance identity is not canonical"
            ) from error
        try:
            with self._session_factory() as session:
                instance = ManagedInstanceRepository(session).get(identity)
                if instance is None:
                    raise ProvisioningStateError("Managed instance does not exist")
                revision = RuntimeRevisionRepository(session).get(
                    instance.runtime_revision_id
                )
                if revision is None:
                    raise ProvisioningStateError(
                        "Pinned runtime revision does not exist"
                    )
                profile = RuntimeExecutionProfileRepository(session).get(revision.id)
                if profile is None:
                    raise RuntimeExecutionProfileUnavailableError(
                        "Pinned runtime execution profile does not exist"
                    )
                return ManagedRuntimeOwnershipSpec(
                    managed_instance_id=managed_instance_id,
                    runtime_revision_id=str(revision.id),
                    runtime_uid=profile.runtime_uid,
                    runtime_gid=profile.runtime_gid,
                )
        except ProvisioningStateError:
            raise
        except SQLAlchemyError as error:
            raise ProvisioningStateError(
                "Canonical runtime ownership state could not be read"
            ) from error


class SqliteReconstructionAuthorizationResolver:
    """Independently bind exceptional removal to a live exact operation lease."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory
        self._provisioning = SqliteProvisioningResolver(session_factory)

    @classmethod
    def production(cls) -> SqliteReconstructionAuthorizationResolver:
        engine = create_database_engine(Settings().database_url)
        return cls(sessionmaker(engine, expire_on_commit=False))

    def desired_spec(
        self, managed_instance_id: str
    ) -> ManagedContainerProvisioningSpec:
        spec = self._provisioning.resolve(managed_instance_id)
        with self._session_factory() as session:
            revision = RuntimeRevisionRepository(session).get(
                UUID(spec.runtime_revision_id)
            )
            if (
                revision is None
                or revision.approval_state is not RevisionApprovalState.APPROVED
                or (
                    revision.requires_trusted_build_confirmation
                    and revision.trusted_build_confirmed_at is None
                )
            ):
                raise ProvisioningStateError("Pinned runtime revision is not approved")
        return spec

    def authorize_removal(
        self,
        *,
        managed_instance_id: str,
        operation_id: str,
        lease_token_digest: str,
        owned_runtime_state_revision: int,
        old_container_id: str,
        old_image_id: str,
        old_runtime_revision_id: str,
        desired_runtime_revision_id: str,
    ) -> ManagedContainerProvisioningSpec:
        spec = self.desired_spec(managed_instance_id)
        with self._session_factory() as session:
            instance = session.get(ManagedInstanceRow, managed_instance_id)
            operation = session.get(ManagedRuntimeReconstructionRow, operation_id)
            lease = session.get(ManagedInstanceMutationLeaseRow, managed_instance_id)
            if (
                instance is None
                or instance.instance_type is not InstanceType.DISPOSABLE
                or instance.runtime_revision_id != desired_runtime_revision_id
                or instance.runtime_state_revision != owned_runtime_state_revision
                or spec.runtime_revision_id != desired_runtime_revision_id
                or old_runtime_revision_id == desired_runtime_revision_id
                or operation is None
                or operation.managed_instance_id != managed_instance_id
                or operation.state != "running"
                or operation.phase != "removing_old"
                or operation.old_container_id != old_container_id
                or operation.old_image_id != old_image_id
                or operation.old_runtime_revision_id != old_runtime_revision_id
                or operation.desired_runtime_revision_id != desired_runtime_revision_id
                or operation.desired_image_id != spec.image_id
                or operation.owned_runtime_state_revision
                != owned_runtime_state_revision
                or operation.lease_token_digest != lease_token_digest
                or lease is None
                or lease.operation_kind
                is not ManagedInstanceMutationKind.RECONSTRUCTION
                or lease.operation_id != operation_id
                or lease.lease_token_digest != lease_token_digest
                or lease.lease_expires_at is None
                or operation.lease_expires_at != lease.lease_expires_at
                or datetime.fromisoformat(lease.lease_expires_at.replace("Z", "+00:00"))
                <= datetime.now(UTC)
            ):
                raise ProvisioningStateError(
                    "Reconstruction ownership or identity changed"
                )
        return spec
