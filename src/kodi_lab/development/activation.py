"""Conservative descriptor planning and historical deployment proof vocabulary."""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from kodi_lab.domain import normalize_extension_types

Constraint = Literal[
    "service_start_requires_opt_in",
    "dependent_interpreters_require_restart",
    "skin_selection_and_recovery_unqualified",
    "resource_consumer_refresh_unqualified",
    "repository_refresh_unqualified",
    "native_or_unknown_extension_unqualified",
    "mixed_extensions_require_all_lifecycles",
]


class ActivationAssessment(BaseModel):
    """Stored descriptor guidance, never live observation or admission."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    declared_extensions: tuple[str, ...] = Field(max_length=64)
    candidate_method: Literal["stopped_copy_restart"] = "stopped_copy_restart"
    admission: Literal["not_assessed"] = "not_assessed"
    live_qualified: Literal[False] = False
    owned_removal_qualified: Literal[False] = False
    tree_policy: Literal["retain_destination_only"] = "retain_destination_only"
    descriptor_metadata: Literal["not_assessed"] = "not_assessed"
    runtime_qualification: Literal["not_assessed"] = "not_assessed"
    constraints: tuple[Constraint, ...] = Field(max_length=7)


class ManagedIterationEvidence(BaseModel):
    """Derived solely from durable deployment fields, as of that operation."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    scope: Literal["historical_operation"] = "historical_operation"
    file_application: Literal["verified_at_operation", "not_verified"]
    tree_policy: Literal["retain_destination_only"] = "retain_destination_only"
    exact_mirror: Literal["not_established"] = "not_established"
    kodi_restart: Literal["observed_at_operation", "not_observed"]
    addon_launch: Literal["not_dispatched"] = "not_dispatched"
    fresh_code: Literal["not_observed"] = "not_observed"
    functional_smoke: Literal["not_run"] = "not_run"


def assess_declared_extensions(encoded: str) -> ActivationAssessment | None:
    """Use only bounded validated extension identifiers, never file/path inputs."""

    if len(encoded) > 4096:
        return None
    try:
        values = json.loads(encoded)
        if (
            not isinstance(values, list)
            or not 1 <= len(values) <= 64
            or any(not isinstance(value, str) for value in values)
        ):
            return None
        extensions = normalize_extension_types(tuple(values))
    except (ValueError, TypeError, RecursionError):
        return None
    executable = tuple(p for p in extensions if p != "xbmc.addon.metadata")
    constraints: list[Constraint] = []
    if "xbmc.service" in executable:
        constraints.append("service_start_requires_opt_in")
    if "xbmc.python.module" in executable:
        constraints.append("dependent_interpreters_require_restart")
    if "xbmc.gui.skin" in executable:
        constraints.append("skin_selection_and_recovery_unqualified")
    if any(p.startswith("kodi.resource.") for p in executable):
        constraints.append("resource_consumer_refresh_unqualified")
    if "xbmc.addon.repository" in executable:
        constraints.append("repository_refresh_unqualified")
    known = {
        "xbmc.service",
        "xbmc.python.module",
        "xbmc.gui.skin",
        "xbmc.addon.repository",
        "xbmc.python.pluginsource",
        "xbmc.python.script",
        "xbmc.python.weather",
        "xbmc.python.lyrics",
        "xbmc.python.library",
    }
    if not executable or any(
        p not in known and not p.startswith("kodi.resource.") for p in executable
    ):
        constraints.append("native_or_unknown_extension_unqualified")
    if len(executable) > 1:
        constraints.append("mixed_extensions_require_all_lifecycles")
    return ActivationAssessment(
        declared_extensions=extensions, constraints=tuple(constraints)
    )
