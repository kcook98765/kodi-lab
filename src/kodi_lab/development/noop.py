"""Bounded historical proof; never substitutes for fresh no-op verification."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from kodi_lab.development.deployment import MAX_ENTRIES


class ManagedDeploymentVerification(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    container_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    runtime_revision_id: UUID
    runtime_state_revision: int = Field(ge=0)
    observed_runtime_state_revision: int = Field(ge=0)
    bridge_generation: int = Field(ge=1)
    bridge_identity_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    source_manifest_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    destination_manifest_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    destination_only_count: int | None = Field(
        default=None, ge=0, le=MAX_ENTRIES, strict=True
    )
    reused_deployment_id: UUID | None = None
    readiness_checked: Literal[True] = True
    activation: Literal["already_satisfied"] = "already_satisfied"
    execution: Literal["not_performed"] = "not_performed"
    native_restart_count: int = Field(ge=0, le=1)
    bytes_staged: Literal[0] | None = None
    bytes_transferred: Literal[0] | None = None
    source_files_scanned: int | None = Field(default=None, ge=0)
    source_bytes_hashed: int | None = Field(default=None, ge=0)
    docker_readback_bytes: int | None = Field(default=None, ge=0)
