"""Catalog fence for explicitly pinned disposable Phase 1 runtime candidates.

Pins are injected by the qualification launcher, never selected by a browser.
They do not register a default production capability or promote a recipe.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.broker.novnc import ViewBinding
from kodi_lab.domain import InstanceType
from kodi_lab.storage.development_sessions import active_session
from kodi_lab.storage.models import AgentWorkspaceRow, ManagedInstanceRow
from kodi_lab.storage.repositories import (
    ManagedInstanceMutationLeaseRepository,
    ManagedRuntimeProjectionRepository,
    RuntimeContainerArtifactRepository,
    RuntimeExecutionProfileRepository,
)


class CatalogViewAuthority:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        pins: Mapping[UUID, ViewBinding],
    ) -> None:
        self.sessions = sessions
        self.pins = dict(pins)

    def resolve(self, instance_id: UUID) -> ViewBinding:
        pin = self.pins[instance_id]
        with self.sessions() as db:
            instance = db.get(ManagedInstanceRow, str(instance_id))
            projection = ManagedRuntimeProjectionRepository(db)
            if (
                instance is None
                or instance.instance_type is not InstanceType.DISPOSABLE
                or projection.has_blocking_runtime_reconciliation(instance_id)
                or ManagedInstanceMutationLeaseRepository(db).current(instance_id)
                is not None
            ):
                raise ValueError("view runtime unavailable")
            revision = UUID(instance.runtime_revision_id)
            artifact = RuntimeContainerArtifactRepository(db).get(revision)
            profile = RuntimeExecutionProfileRepository(db).get(revision)
            if (
                artifact is None
                or artifact.image_id != pin.image
                or profile is None
                or profile.expected_application_name != "Kodi"
                or profile.expected_kodi_version != "21.3"
                or profile.runtime_uid != 1000
                or profile.runtime_gid != 1000
            ):
                raise ValueError("view runtime not admitted")
            current = active_session(db)
            selected: tuple[object, ...] = (None,)
            if current is not None:
                workspace = db.get(AgentWorkspaceRow, current.workspace_id)
                if (
                    current.phase != "bound"
                    or current.target_id != str(instance_id)
                    or current.runtime_revision_id != str(revision)
                    or current.container_id != pin.container
                    or workspace is None
                    or workspace.development_source_id != current.source_id
                ):
                    raise ValueError("view session identity changed")
                selected = (
                    current.generation,
                    current.workspace_id,
                    current.source_id,
                    workspace.generation,
                )
            identity = (str(revision), instance.runtime_state_revision, *selected)
            value = replace(pin, identity=identity)
            if pin.identity and pin.identity != identity:
                raise ValueError("view catalog identity changed")
            return value
