"""Closed resident Bridge GUI launch documents."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ManagedLaunchRuntimeBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    target_id: str
    runtime_revision_id: str
    container_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    generation: int = Field(ge=1)


class BridgeLaunchIntent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    launch_id: UUID
    addon_id: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,128}$")
    addon_version: str = Field(min_length=1, max_length=128)
    manifest_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    ordinal: int = Field(ge=0, le=63)
    runtime_binding: ManagedLaunchRuntimeBinding


class BridgeLaunchReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    launch_id: UUID
    intent: BridgeLaunchIntent
    state: Literal["pending", "accepted"]
    accepted_at: str | None = Field(max_length=32)
