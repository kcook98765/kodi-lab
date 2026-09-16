"""Broker-trusted resolution for one immutable snapshot capture."""

from __future__ import annotations

import grp
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.broker.errors import (
    PersistentSnapshotUnsupportedError,
    SnapshotStateError,
)
from kodi_lab.config import Settings
from kodi_lab.domain import InstanceType, SnapshotState
from kodi_lab.snapshot.storage import SnapshotCaptureContext
from kodi_lab.storage.engine import create_database_engine
from kodi_lab.storage.repositories import (
    FixtureDefinitionRepository,
    ManagedInstanceRepository,
    RuntimeDefinitionRepository,
    RuntimeRevisionRepository,
    SnapshotRepository,
)


@dataclass(frozen=True, slots=True)
class SnapshotCaptureSpec:
    context: SnapshotCaptureContext
    data_root: Path
    owner_uid: int
    owner_gid: int


class SnapshotCaptureResolver(Protocol):
    def resolve(
        self, snapshot_id: str, managed_instance_id: str
    ) -> SnapshotCaptureSpec: ...


class SqliteSnapshotCaptureResolver:
    """Resolve all capture and storage authority from trusted local state."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        data_root: Path,
        owner_uid: int = 0,
        owner_gid: int | None,
    ) -> None:
        self._session_factory = session_factory
        self._data_root = data_root
        self._owner_uid = owner_uid
        self._owner_gid = owner_gid

    @classmethod
    def production(cls) -> SqliteSnapshotCaptureResolver:
        settings = Settings()
        engine = create_database_engine(settings.database_url)
        return cls(
            sessionmaker(engine, expire_on_commit=False),
            data_root=settings.data_root,
            owner_gid=None,
        )

    def resolve(
        self, snapshot_id: str, managed_instance_id: str
    ) -> SnapshotCaptureSpec:
        try:
            snapshot_identity = UUID(snapshot_id)
            instance_identity = UUID(managed_instance_id)
            if str(snapshot_identity) != snapshot_id:
                raise ValueError
            if str(instance_identity) != managed_instance_id:
                raise ValueError
        except ValueError as error:
            raise SnapshotStateError("Snapshot identities are not canonical") from error
        try:
            with self._session_factory() as session:
                snapshot = SnapshotRepository(session).get(snapshot_identity)
                instance = ManagedInstanceRepository(session).get(instance_identity)
                if snapshot is None or instance is None:
                    raise SnapshotStateError("Snapshot source state does not exist")
                if snapshot.source_managed_instance_id != instance_identity:
                    raise SnapshotStateError("Snapshot source identity differs")
                if snapshot.state is not SnapshotState.CREATING:
                    raise SnapshotStateError("Snapshot is not awaiting capture")
                if snapshot.prior_runtime_state is None:
                    raise SnapshotStateError(
                        "Snapshot prior runtime state is unavailable"
                    )
                if instance.instance_type is not InstanceType.DISPOSABLE:
                    raise PersistentSnapshotUnsupportedError(
                        "Persistent snapshot capture is not supported"
                    )
                if snapshot.runtime_revision_id != instance.runtime_revision_id:
                    raise SnapshotStateError("Snapshot runtime pin differs")
                if (
                    snapshot.captured_fixture_definition_id
                    != instance.fixture_definition_id
                ):
                    raise SnapshotStateError("Snapshot fixture assignment differs")
                revision = RuntimeRevisionRepository(session).get(
                    instance.runtime_revision_id
                )
                if revision is None:
                    raise SnapshotStateError("Pinned runtime revision does not exist")
                definition = RuntimeDefinitionRepository(session).get(
                    revision.runtime_definition_id
                )
                if definition is None:
                    raise SnapshotStateError("Pinned runtime definition does not exist")
                if instance.fixture_definition_id is not None:
                    fixture = FixtureDefinitionRepository(session).get(
                        instance.fixture_definition_id
                    )
                    if fixture is None:
                        raise SnapshotStateError(
                            "Captured fixture definition does not exist"
                        )
                owner_gid = self._owner_gid
                if owner_gid is None:
                    try:
                        owner_gid = grp.getgrnam("kodi-lab").gr_gid
                    except KeyError as error:
                        raise SnapshotStateError(
                            "Dedicated snapshot storage group is unavailable"
                        ) from error
                return SnapshotCaptureSpec(
                    context=SnapshotCaptureContext(
                        snapshot_id=snapshot.id,
                        source_managed_instance_id=instance.target_id,
                        runtime_revision_id=revision.id,
                        kodi_release=definition.kodi_version,
                        captured_fixture_definition_id=instance.fixture_definition_id,
                        captured_at=snapshot.captured_at,
                        prior_runtime_state=snapshot.prior_runtime_state.value,
                    ),
                    data_root=self._data_root,
                    owner_uid=self._owner_uid,
                    owner_gid=owner_gid,
                )
        except SnapshotStateError:
            raise
        except SQLAlchemyError as error:
            raise SnapshotStateError(
                "Canonical snapshot state could not be read"
            ) from error
