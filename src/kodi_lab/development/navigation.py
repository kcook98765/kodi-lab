"""Content-free immutable context for managed development input receipts."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DevelopmentNavigationBinding:
    session_generation: int
    workspace_id: str
    source_id: str
    workspace_generation: int
    deployment_id: str
    addon_id: str
    addon_version: str
    tree_digest: str
    destination_digest: str
    runtime_revision_id: str
    runtime_state_revision: int
    container_id: str
    bridge_generation: int
    window_id: int
    control_id: int
