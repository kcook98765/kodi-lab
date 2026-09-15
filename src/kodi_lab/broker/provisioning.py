"""Trusted read-only resolution of canonical container provisioning state."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.broker.errors import (
    PersistentProvisioningUnsupportedError,
    ProvisioningStateError,
    RuntimeContainerArtifactUnavailableError,
)
from kodi_lab.config import Settings
from kodi_lab.domain import InstanceType
from kodi_lab.storage.engine import create_database_engine
from kodi_lab.storage.repositories import (
    ManagedInstanceRepository,
    RuntimeContainerArtifactRepository,
    RuntimeDefinitionRepository,
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
        if self.network_name != MANAGED_NETWORK_NAME:
            raise ValueError("network name must use fixed managed policy")


class ProvisioningResolver(Protocol):
    def resolve(self, managed_instance_id: str) -> ManagedContainerProvisioningSpec:
        """Resolve one canonical managed instance without accepting substitutions."""


class ManagedInstanceRevisionResolver(Protocol):
    def resolve_runtime_revision_id(self, managed_instance_id: str) -> str:
        """Return the independently persisted runtime-revision pin."""


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
                if target.kodi_version.strip() != definition.kodi_version.strip():
                    raise ProvisioningStateError(
                        "Pinned runtime definition is incompatible with target"
                    )
                artifact = RuntimeContainerArtifactRepository(session).get(revision.id)
                if artifact is None:
                    raise RuntimeContainerArtifactUnavailableError(
                        "Pinned runtime container artifact does not exist"
                    )
                return ManagedContainerProvisioningSpec(
                    managed_instance_id=managed_instance_id,
                    runtime_revision_id=str(revision.id),
                    image_id=artifact.image_id,
                    container_name=f"kodi-lab-managed-{identity.hex}",
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
