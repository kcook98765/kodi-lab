"""Pure declaration comparison and live dependency-state evaluation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from kodi_lab.kodi.addons import (
    KodiAddonInventoryRecord,
    KodiReportedAddonDependency,
)

_BUILTIN_DEPENDENCY_IDS = frozenset(
    {"xbmc.gui", "xbmc.json", "xbmc.metadata", "xbmc.python"}
)


class AddonDependencyDeclaration(Protocol):
    @property
    def dependency_id(self) -> str: ...

    @property
    def version(self) -> str | None: ...

    @property
    def minversion(self) -> str | None: ...

    @property
    def optional(self) -> bool: ...


class DependencyStatus(StrEnum):
    PRESENT_ENABLED = "present_enabled"
    MISSING = "missing"
    DISABLED = "disabled"
    OPTIONAL_MISSING = "optional_missing"
    VERSION_UNVERIFIED = "version_unverified"
    UNKNOWN = "unknown"


class AddonDependencyState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dependency_id: str = Field(max_length=128)
    declared_version: str | None = Field(default=None, max_length=128)
    declared_minversion: str | None = Field(default=None, max_length=128)
    optional: bool
    recognized: bool
    enabled: bool | None
    reported_version: str | None = Field(default=None, max_length=128)
    status: DependencyStatus


def reported_constraint(value: str | None) -> str | None:
    """Map only Kodi's exact neutral sentinel to no declaration constraint."""

    return None if value in (None, "0.0.0") else value


def declaration_mismatch(
    declared_items: Sequence[AddonDependencyDeclaration],
    reported_items: Sequence[KodiReportedAddonDependency],
) -> AddonDependencyState | None:
    reported_by_id = {item.dependency_id: item for item in reported_items}
    declared_ids = {item.dependency_id for item in declared_items}
    if declared_ids != set(reported_by_id):
        dependency_id = sorted(declared_ids ^ set(reported_by_id))[0]
        declared = next(
            (item for item in declared_items if item.dependency_id == dependency_id),
            None,
        )
        return AddonDependencyState(
            dependency_id=dependency_id,
            declared_version=None if declared is None else declared.version,
            declared_minversion=None if declared is None else declared.minversion,
            optional=False if declared is None else declared.optional,
            recognized=False,
            enabled=None,
            reported_version=(
                None
                if dependency_id not in reported_by_id
                else reported_by_id[dependency_id].version
                or reported_by_id[dependency_id].minversion
            ),
            status=DependencyStatus.UNKNOWN,
        )
    for declared in declared_items:
        reported = reported_by_id[declared.dependency_id]
        if (
            declared.optional != reported.optional
            or declared.version != reported_constraint(reported.version)
            or declared.minversion != reported_constraint(reported.minversion)
        ):
            return AddonDependencyState(
                dependency_id=declared.dependency_id,
                declared_version=declared.version,
                declared_minversion=declared.minversion,
                optional=declared.optional,
                recognized=True,
                enabled=None,
                reported_version=reported.version or reported.minversion,
                status=DependencyStatus.UNKNOWN,
            )
    return None


def evaluate_dependencies(
    declarations: Sequence[AddonDependencyDeclaration],
    inventory: Mapping[str, KodiAddonInventoryRecord],
) -> list[AddonDependencyState]:
    result: list[AddonDependencyState] = []
    for declared in declarations:
        record = inventory.get(declared.dependency_id)
        constraint = declared.version or declared.minversion
        if record is None and is_builtin_dependency(declared.dependency_id):
            result.append(
                AddonDependencyState(
                    dependency_id=declared.dependency_id,
                    declared_version=declared.version,
                    declared_minversion=declared.minversion,
                    optional=declared.optional,
                    recognized=True,
                    enabled=True,
                    reported_version=None,
                    status=(
                        DependencyStatus.PRESENT_ENABLED
                        if constraint is None
                        else DependencyStatus.VERSION_UNVERIFIED
                    ),
                )
            )
            continue
        if record is None:
            result.append(
                AddonDependencyState(
                    dependency_id=declared.dependency_id,
                    declared_version=declared.version,
                    declared_minversion=declared.minversion,
                    optional=declared.optional,
                    recognized=False,
                    enabled=None,
                    reported_version=None,
                    status=(
                        DependencyStatus.OPTIONAL_MISSING
                        if declared.optional
                        else DependencyStatus.MISSING
                    ),
                )
            )
            continue
        if not record.enabled:
            status = DependencyStatus.DISABLED
        elif constraint is not None:
            status = DependencyStatus.VERSION_UNVERIFIED
        else:
            status = DependencyStatus.PRESENT_ENABLED
        result.append(
            AddonDependencyState(
                dependency_id=declared.dependency_id,
                declared_version=declared.version,
                declared_minversion=declared.minversion,
                optional=declared.optional,
                recognized=True,
                enabled=record.enabled,
                reported_version=record.version,
                status=status,
            )
        )
    return result


def first_blocking_dependency(
    states: Sequence[AddonDependencyState],
) -> AddonDependencyState | None:
    return next(
        (
            item
            for item in states
            if not item.optional
            and item.status
            in {
                DependencyStatus.MISSING,
                DependencyStatus.DISABLED,
                DependencyStatus.VERSION_UNVERIFIED,
                DependencyStatus.UNKNOWN,
            }
        ),
        None,
    )


def dependency_failure_code(status: DependencyStatus) -> str:
    return {
        DependencyStatus.MISSING: "dependency_missing",
        DependencyStatus.DISABLED: "dependency_disabled",
        DependencyStatus.VERSION_UNVERIFIED: "dependency_version_unverified",
        DependencyStatus.UNKNOWN: "dependency_state_unknown",
    }[status]


def is_builtin_dependency(dependency_id: str) -> bool:
    return dependency_id in _BUILTIN_DEPENDENCY_IDS or dependency_id.startswith(
        "kodi.binary.global."
    )
