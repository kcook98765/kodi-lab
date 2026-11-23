"""Canonical identity binding shared by preview, approval and broker dispatch."""

from dataclasses import asdict
from uuid import UUID

from sqlalchemy.orm import Session

from kodi_lab.development.addition_reviews import identity_digest
from kodi_lab.storage.development_sessions import require_source_session_admission
from kodi_lab.storage.models import AgentWorkspaceRow
from kodi_lab.storage.repositories import (
    DevelopmentSourceDeploymentRepository,
    DevelopmentSourceRepository,
    ManagedInstanceDevelopmentSourceRepository,
    ManagedInstanceRepository,
    RuntimeExecutionProfileRepository,
    RuntimeRevisionRepository,
)


def addition_review_scope(
    session: Session, target_id: str, source_id: str, container_id: str
) -> str:
    instance = ManagedInstanceRepository(session).get(UUID(target_id))
    source = DevelopmentSourceRepository(session).get(UUID(source_id))
    attachment = ManagedInstanceDevelopmentSourceRepository(session).get(
        UUID(target_id), UUID(source_id)
    )
    baseline = DevelopmentSourceDeploymentRepository(session).get(
        UUID(target_id), UUID(source_id)
    )
    if instance is None or source is None or attachment is None or baseline is None:
        raise ValueError("Addition review scope is unavailable")
    selected = require_source_session_admission(
        session,
        target_id=target_id,
        revision_id=str(instance.runtime_revision_id),
        source_id=source_id,
        container_id=container_id,
    )
    workspace = (
        None
        if selected is None
        else session.get(AgentWorkspaceRow, selected.workspace_id)
    )
    if selected is not None and workspace is None:
        raise ValueError("Addition review workspace is unavailable")
    revision = RuntimeRevisionRepository(session).get(instance.runtime_revision_id)
    profile = RuntimeExecutionProfileRepository(session).get(
        instance.runtime_revision_id
    )
    if revision is None or profile is None:
        raise ValueError("Addition review runtime is unavailable")
    return identity_digest(
        {
            "target": str(instance.target_id),
            "type": instance.instance_type,
            "container": container_id,
            "runtime": asdict(revision),
            "profile": asdict(profile),
            "source": asdict(source),
            "attachment": asdict(attachment),
            "baseline": asdict(baseline),
            "selection": None
            if selected is None
            else {
                name: getattr(selected, name)
                for name in (
                    "generation",
                    "target_id",
                    "runtime_revision_id",
                    "source_id",
                    "workspace_id",
                    "container_id",
                    "phase",
                )
            },
            "workspace": None
            if workspace is None
            else {
                column.name: getattr(workspace, column.name)
                for column in workspace.__table__.columns
            },
        }
    )
