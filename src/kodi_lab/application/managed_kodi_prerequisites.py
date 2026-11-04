"""Informational Create Kodi observations. Never reserve, reconcile or install."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from kodi_lab.application.managed_resources import ManagedResourceService
from kodi_lab.application.runtime_installations import (
    RuntimeInstallationInspection,
    RuntimeInstallationService,
)
from kodi_lab.runtime.recipes import load_installation_recipe, recipe_target_id

StableGeneration = Literal["matrix", "nexus", "omega"]
CheckStatus = Literal["passed", "attention", "existing", "unknown", "info"]
LABELS: dict[CheckStatus, str] = {
    "passed": "Verified",
    "attention": "Needs attention",
    "existing": "Existing setup",
    "unknown": "Not automatically verified",
    "info": "Information",
}


class PrerequisiteCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    key: str
    title: str
    status: CheckStatus
    label: str
    message: str
    detail_url: str | None = None
    available_bytes: int | None = Field(default=None, ge=0)


class PrerequisiteSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    generation: StableGeneration
    version: str
    recipe_id: str
    recipe_revision: int
    recipe_digest: str
    target_id: UUID
    checked_at: datetime
    summary: str
    checks: list[PrerequisiteCheck]


@dataclass(frozen=True)
class DataStorageObservation:
    available_bytes: int
    read_only: bool


def read_data_storage(root: Path) -> DataStorageObservation:
    """Read only the configured Lab data filesystem; never probe by writing."""
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        value = os.fstatvfs(fd)
        return DataStorageObservation(
            value.f_bavail * (value.f_frsize or value.f_bsize),
            bool(value.f_flag & os.ST_RDONLY),
        )
    finally:
        os.close(fd)


def check(
    key: str,
    title: str,
    status: CheckStatus,
    message: str,
    detail_url: str | None = None,
    *,
    available_bytes: int | None = None,
) -> PrerequisiteCheck:
    return PrerequisiteCheck(
        key=key,
        title=title,
        status=status,
        label=LABELS[status],
        message=message,
        detail_url=detail_url,
        available_bytes=available_bytes,
    )


class ManagedKodiPrerequisiteService:
    def __init__(
        self,
        installations: RuntimeInstallationService,
        resources: ManagedResourceService,
        *,
        data_root: Path,
        storage: Callable[[], DataStorageObservation] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._installations, self._resources = installations, resources
        self._storage = storage or (lambda: read_data_storage(data_root))
        self._clock = clock

    def snapshot(self, generation: StableGeneration = "omega") -> PrerequisiteSnapshot:
        reviewed = load_installation_recipe(generation)
        checks = [
            check(
                "recipe",
                "Kodi version",
                "passed",
                f"{reviewed.recipe.display_generation} "
                f"({reviewed.recipe.kodi_version}) "
                f"has a reviewed {reviewed.recipe.platform} recipe.",
            )
        ]
        try:
            inspection = self._installations.inspect(generation)
        except Exception:
            inspection = None
        checks.extend(self._installation_checks(inspection))
        try:
            capacity = self._resources.capacity()
            checks.append(
                check(
                    "running_policy",
                    "One Kodi at a time",
                    "passed" if capacity.limit == 1 else "attention",
                    "The effective running limit is one Kodi."
                    if capacity.limit == 1
                    else "Create Kodi requires a running limit of 1. "
                    f"Your current limit is {capacity.limit}; it has not been changed. "
                    "Open Managed Kodi resource policy, set Maximum running "
                    "managed Kodi to 1, save, then Check again. "
                    "This does not stop an existing Kodi.",
                    "/managed-resources" if capacity.limit != 1 else None,
                )
            )
            checks.append(
                check(
                    "capacity",
                    "Running Kodi capacity",
                    "passed" if capacity.available else "attention",
                    f"{capacity.used} of {capacity.limit} running slots are in use. "
                    + (
                        "This check does not reserve a slot."
                        if capacity.available
                        else "Starting an additional Kodi needs a free slot. "
                        "Review your installations."
                    ),
                    "/managed-resources" if not capacity.available else None,
                )
            )
        except Exception:
            checks.append(
                check(
                    "running_policy",
                    "One Kodi at a time",
                    "unknown",
                    "The running limit could not be verified. Check again.",
                )
            )
            checks.append(
                check(
                    "capacity",
                    "Running Kodi capacity",
                    "unknown",
                    "Running slots could not be checked. "
                    "Check the installation service and try again.",
                )
            )
        try:
            storage = self._storage()
            if storage.available_bytes < 0:
                raise ValueError("invalid filesystem observation")
            checks.append(
                check(
                    "data_storage",
                    "Kodi Lab data storage",
                    "attention"
                    if storage.read_only or storage.available_bytes == 0
                    else "info",
                    (
                        "The Lab data filesystem is read-only. "
                        "Review its storage settings."
                        if storage.read_only
                        else "The Lab data filesystem has no free space. "
                        "Review its storage settings."
                        if storage.available_bytes == 0
                        else f"{storage.available_bytes / 1024**3:.2f} GiB available "
                        "on the Lab data filesystem. This does not measure Docker "
                        "image/build storage or guarantee enough space for setup."
                    ),
                    available_bytes=storage.available_bytes,
                )
            )
        except (OSError, ValueError, AttributeError, NotImplementedError):
            checks.append(
                check(
                    "data_storage",
                    "Kodi Lab data storage",
                    "unknown",
                    "Free space on the Lab data filesystem could not be read. "
                    "Review storage access.",
                )
            )
        checks.extend(
            [
                check(
                    "docker_storage",
                    "Docker image and build storage",
                    "unknown",
                    "Space for Docker images and builds cannot be measured here. "
                    "Review Docker storage before a new download.",
                ),
                check(
                    "host_storage",
                    "Physical host disk space",
                    "unknown",
                    "The Lab filesystem reading does not verify physical host space. "
                    "On Windows/WSL2, also check the Windows drive holding "
                    "Docker and WSL data.",
                ),
                check(
                    "memory",
                    "Memory available for Kodi",
                    "unknown",
                    "Memory headroom for a new Kodi cannot be verified here. "
                    "Start with one Kodi; on Docker Desktop/WSL2, "
                    "review VM resource settings.",
                ),
            ]
        )
        return PrerequisiteSnapshot(
            generation=generation,
            version=reviewed.recipe.kodi_version,
            recipe_id=reviewed.recipe.recipe_id,
            recipe_revision=reviewed.recipe.recipe_revision,
            recipe_digest=reviewed.digest,
            target_id=recipe_target_id(reviewed),
            checked_at=self._clock(),
            checks=checks,
            summary="Some setup checks need attention."
            if any(item.status == "attention" for item in checks)
            else "Automatic checks complete; "
            "some resources cannot be verified automatically.",
        )

    @staticmethod
    def _installation_checks(
        inspection: RuntimeInstallationInspection | None,
    ) -> list[PrerequisiteCheck]:
        if inspection is None:
            return [
                check(
                    key,
                    title,
                    "unknown",
                    "Setup information could not be read. Check again.",
                )
                for key, title in [
                    ("installer", "Kodi installation service"),
                    ("runtime", "Runtime files"),
                    ("activity", "Installation activity"),
                    ("instance", "Existing Kodi"),
                ]
            ]
        view = inspection.installation
        issue = inspection.backend_issue
        service_check = check(
            "installer",
            "Kodi installation service",
            "attention"
            if issue in {"backend_capability_missing", "broker_authorization_denied"}
            else "unknown"
            if issue
            else "passed",
            (view.failure_message or "The installation service could not be verified.")
            if issue
            else "The installation service supports reviewed runtime setup.",
        )
        if issue:
            activity = check(
                "activity",
                "Installation activity",
                "unknown",
                "Active installation work could not be verified. Check again.",
            )
        elif inspection.active_operation_id:
            activity = check(
                "activity",
                "Installation activity",
                "existing",
                "A Kodi installation is already running. "
                "Review its saved setup before another request.",
                "/runtimes#installation-records",
            )
        elif inspection.lock_held:
            activity = check(
                "activity",
                "Installation activity",
                "attention",
                "Installation resources are busy. Wait and check again.",
            )
        else:
            activity = check(
                "activity",
                "Installation activity",
                "passed",
                "No active runtime installation was observed.",
            )
        status: CheckStatus = "info"
        message = (
            "Runtime download and validation will be needed after you approve setup. "
            "This check downloads nothing."
        )
        if issue and inspection.has_installation_record:
            status, message = (
                "existing",
                "A saved installation record exists for this version. "
                "Runtime files could not be verified. Review its saved setup.",
            )
        elif view.status == "ready":
            status, message = (
                "passed",
                "Approved runtime files are installed and verified. "
                "This does not mean Kodi is running or viewable.",
            )
        elif view.status in {"pending", "acquiring", "assembling", "validating"}:
            if inspection.active_operation_id == view.operation_id:
                status, message = (
                    "existing",
                    f"An existing installation is in progress: {view.phase}.",
                )
            else:
                status, message = (
                    "attention",
                    "Saved setup is incomplete; its worker was not confirmed active. "
                    "Review the saved setup; this check does not resume it.",
                )
        elif view.status != "not_installed":
            status = (
                "unknown"
                if issue or view.unavailable_code == "image_check_unavailable"
                else "attention"
            )
            message = (
                view.failure_message
                or "Runtime files or saved setup need review before a new request."
            )
        instance = inspection.existing_instance_id
        return [
            service_check,
            check(
                "runtime",
                "Runtime files",
                status,
                message,
                "/runtimes#installation-records"
                if status in {"attention", "existing"}
                else None,
            ),
            activity,
            check(
                "instance",
                "Existing Kodi",
                "existing" if instance else "info",
                "This reviewed version already has a managed Kodi. "
                "Open its details to review it."
                if instance
                else "No managed Kodi was found for this reviewed version.",
                f"/instances/{instance}" if instance else None,
            ),
        ]
