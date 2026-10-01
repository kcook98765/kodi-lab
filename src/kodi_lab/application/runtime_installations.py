"""Safe Web admission and presentation for reviewed runtime acquisitions."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.runtime_eligibility import (
    RuntimeRevisionEligibilityPolicy,
    RuntimeSelectionPolicy,
)
from kodi_lab.config import Settings
from kodi_lab.runtime.recipes import (
    load_recorded_reviewed_recipe,
    load_reviewed_recipe,
    recipe_target_id,
)
from kodi_lab.storage.models import RuntimeRecipeInstallationRow
from kodi_lab.storage.repositories import (
    ManagedInstanceRepository,
    RuntimeDefinitionRepository,
    RuntimeRecipeInstallationRepository,
    RuntimeRevisionRepository,
)

Generation = Literal["matrix", "nexus", "omega"]
GENERATIONS: tuple[Generation, ...] = ("matrix", "nexus", "omega")
STABLE_RELEASES: dict[Generation, tuple[str, str]] = {
    "matrix": ("19", "19.4"),
    "nexus": ("20", "20.5"),
    "omega": ("21", "21.3"),
}
PHASES = {
    "pending": "Preparing",
    "acquiring": "Acquiring runtime",
    "assembling": "Downloading packages and building runtime",
    "validating": "Validating Kodi",
    "ready": "Ready",
    "failed": "Installation failed",
}
FAILURE_MESSAGE = (
    "Could not acquire or build this reviewed runtime. "
    "Check network access and available disk space, then retry."
)
NETWORK_FAILURE_MESSAGES = {
    "acquisition_network_creation_failed": (
        "Could not create the temporary acquisition network. "
        "Check Docker network capacity."
    ),
    "validation_network_creation_failed": (
        "Could not create the isolated validation network. "
        "Check Docker network capacity."
    ),
    "temporary_network_capacity_unavailable": (
        "Docker has no available automatic subnet for temporary runtime networks. "
        "Free an owned unused network or configure a Kodi Lab temporary pool."
    ),
    "temporary_network_pool_conflict": (
        "The configured Kodi Lab temporary network pool conflicts with "
        "a current route or Docker network."
    ),
    "temporary_network_pool_exhausted": (
        "The configured Kodi Lab temporary network pool has no free child subnet."
    ),
    "temporary_network_route_inventory_unavailable": (
        "Could not check host routes for temporary runtime networks."
    ),
    "temporary_network_inventory_unavailable": (
        "Could not check Docker networks for temporary runtime networks."
    ),
    "temporary_network_identity_mismatch": (
        "A temporary runtime network did not match its reviewed identity."
    ),
    "temporary_network_cleanup_failed": (
        "A temporary runtime network could not be safely removed."
    ),
}
REQUIRED_OPERATIONS = {
    "active_reviewed_runtime_installation",
    "launch_reviewed_runtime_installation",
    "observe_reviewed_runtime_installation",
    "reviewed_runtime_image_present",
}


class RuntimeInstallationBusy(RuntimeError):
    pass


class RuntimeInstallationUnavailable(RuntimeError):
    pass


class RuntimeInstallationActionConflict(RuntimeError):
    pass


@dataclass(frozen=True)
class RuntimeInstallationView:
    generation: Generation
    display_name: str
    version: str
    status: str
    phase: str
    operation_id: str | None = None
    status_url: str | None = None
    action: Literal["install", "retry", "reacquire", "open", "none"] = "none"
    managed_instance_url: str | None = None
    failure_message: str | None = None
    note: str | None = None
    newly_accepted: bool = False
    technical_details: dict[str, str] | None = None


class RuntimeInstallationService:
    """Use durable rows and broker facts without exposing Docker details."""

    def __init__(
        self,
        factory: sessionmaker[Session],
        broker: Any,
        *,
        data_root: Path | None = None,
    ) -> None:
        self._factory = factory
        self._broker = broker
        self._data_root = data_root or Settings().data_root

    def _eligible_ready(self, row: RuntimeRecipeInstallationRow) -> bool:
        if row.runtime_revision_id is None:
            return False
        try:
            reviewed = load_recorded_reviewed_recipe(
                row.generation, row.recipe_id, row.recipe_revision, row.recipe_digest
            )
            with self._factory() as session:
                revision = RuntimeRevisionRepository(session).get(
                    UUID(row.runtime_revision_id)
                )
                definition = RuntimeDefinitionRepository(session).get(
                    reviewed.recipe.definition_id
                )
                return (
                    revision is not None
                    and definition is not None
                    and RuntimeRevisionEligibilityPolicy(data_root=self._data_root)
                    .evaluate_for_release(
                        session,
                        required_release=definition.kodi_release,
                        definition=definition,
                        revision=revision,
                        policy=RuntimeSelectionPolicy(
                            include_legacy=True,
                            validation_contract=reviewed.recipe.validation_contract,
                        ),
                    )
                    .eligible
                )
        except (ValueError, OSError):
            return False

    def _capable(self) -> bool:
        try:
            return REQUIRED_OPERATIONS.issubset(self._broker.capabilities().operations)
        except Exception:
            return False

    def _active_worker(self) -> Any:
        try:
            return self._broker.active_reviewed_runtime_installation()
        except Exception as error:
            raise RuntimeInstallationUnavailable from error

    def _latest(self, generation: Generation) -> RuntimeRecipeInstallationRow | None:
        with self._factory() as session:
            return session.scalar(
                select(RuntimeRecipeInstallationRow)
                .where(RuntimeRecipeInstallationRow.generation == generation)
                .order_by(
                    RuntimeRecipeInstallationRow.created_at.desc(),
                    RuntimeRecipeInstallationRow.id.desc(),
                )
                .limit(1)
            )

    def _reconcile(
        self, row: RuntimeRecipeInstallationRow
    ) -> RuntimeRecipeInstallationRow:
        if row.state.value not in ("pending", "acquiring", "assembling", "validating"):
            return row
        try:
            observed = self._broker.observe_reviewed_runtime_installation(row.id)
        except Exception as error:
            raise RuntimeInstallationUnavailable from error
        if observed.state == "mismatch":
            raise RuntimeInstallationUnavailable
        if observed.state == "running" or observed.lock_held:
            return row
        # A stopped/gone exact worker and a free authoritative lock prove
        # interruption. No elapsed-time criterion is involved.
        with self._factory.begin() as session:
            current = RuntimeRecipeInstallationRepository(session).get(UUID(row.id))
            if current is not None and current.state.value in (
                "pending",
                "acquiring",
                "assembling",
                "validating",
            ):
                RuntimeRecipeInstallationRepository(session).fail(
                    UUID(row.id), code="interrupted", now=datetime.now(UTC)
                )
        return self._latest(row.generation) or row  # type: ignore[arg-type]

    def _view(
        self,
        generation: Generation,
        row: RuntimeRecipeInstallationRow | None,
        *,
        capable: bool,
        worker_operation_id: str | None = None,
    ) -> RuntimeInstallationView:
        recipe = load_reviewed_recipe(generation).recipe
        major, stable_version = STABLE_RELEASES[generation]
        base = RuntimeInstallationView(
            generation=generation,
            display_name=f"Kodi {major} — {generation.capitalize()}",
            version=stable_version,
            status="not_installed",
            phase="Not installed",
            action="install",
        )
        if not capable:
            return replace(
                base,
                status="unavailable",
                phase="Installation unavailable",
                action="none",
            )
        if worker_operation_id is not None and (
            row is None or row.id != worker_operation_id
        ):
            return replace(
                base,
                status="pending",
                phase=PHASES["pending"],
                operation_id=worker_operation_id,
                status_url=f"/api/runtime-installations/{worker_operation_id}",
                action="none",
            )
        if row is None:
            return base
        row = self._reconcile(row)
        state = row.state.value
        operation_id = row.id
        details = {
            "provider": recipe.provider.type,
            "recipe": recipe.recipe_id,
            "package_version": recipe.critical_packages[0].version,
            "acquired_at": row.completed_at or "",
            "revision_id": row.runtime_revision_id or "",
            "image_id": row.image_id or "",
            "validation": "Passed" if state == "ready" else "",
        }
        current = replace(
            base,
            status=state,
            phase=PHASES[state],
            operation_id=operation_id,
            status_url=f"/api/runtime-installations/{operation_id}",
            action="none",
            technical_details=details,
        )
        if state == "failed":
            code = row.failure_code or "installation_failed"
            message = NETWORK_FAILURE_MESSAGES.get(code, FAILURE_MESSAGE)
            if code == "docker_address_pool_exhausted":
                phase = "validation" if row.image_id else "acquisition"
                message = (
                    "Docker address pools were exhausted while creating "
                    f"the {phase} network."
                )
            elif code == "validation_failed":
                message = "The assembled Kodi runtime did not pass validation."
            return replace(
                current,
                action="retry",
                failure_message=message,
                technical_details={**details, "failure_code": code},
            )
        if state != "ready":
            return current
        if not self._eligible_ready(row):
            return replace(
                current,
                status="unavailable",
                phase="Installation unavailable",
                action="none",
            )
        try:
            present = self._broker.reviewed_runtime_image_present(operation_id).present
        except Exception as error:
            raise RuntimeInstallationUnavailable from error
        if not present:
            return replace(
                current,
                status="missing",
                phase="Runtime files missing",
                action="reacquire",
            )
        target_id = recipe_target_id(load_reviewed_recipe(generation))
        with self._factory() as session:
            instance = ManagedInstanceRepository(session).get(target_id)
        if instance is None:
            return replace(
                current,
                status="setup_incomplete",
                phase="Installed; managed instance setup incomplete",
                note=(
                    "An administrator can rerun the reviewed runtime installer "
                    "to complete managed instance setup."
                ),
            )
        note = (
            "This managed instance keeps its previous exact runtime revision; "
            "reacquisition does not restore or repin it."
            if str(instance.runtime_revision_id) != row.runtime_revision_id
            else None
        )
        return replace(
            current,
            action="open",
            managed_instance_url=f"/instances/{target_id}",
            note=note,
        )

    def list(self) -> list[RuntimeInstallationView]:
        capable = self._capable()
        if not capable:
            return [self._view(gen, None, capable=False) for gen in GENERATIONS]
        try:
            active = self._active_worker()
        except RuntimeInstallationUnavailable:
            return [self._view(gen, None, capable=False) for gen in GENERATIONS]
        views = []
        for generation in GENERATIONS:
            row = self._latest(generation)
            worker_id = active.operation_id if active.generation == generation else None
            try:
                views.append(
                    self._view(
                        generation, row, capable=True, worker_operation_id=worker_id
                    )
                )
            except RuntimeInstallationUnavailable:
                views.append(self._view(generation, None, capable=False))
        return views

    def get(self, operation_id: UUID) -> RuntimeInstallationView | None:
        if not self._capable():
            raise RuntimeInstallationUnavailable
        with self._factory() as session:
            row = RuntimeRecipeInstallationRepository(session).get(operation_id)
        if row is not None:
            if row.generation not in GENERATIONS:
                return None
            return self._view(row.generation, row, capable=True)
        active = self._active_worker()
        if (
            active.operation_id == str(operation_id)
            and active.generation in GENERATIONS
        ):
            return self._view(
                active.generation,
                None,
                capable=True,
                worker_operation_id=str(operation_id),
            )
        return None

    def start(
        self,
        generation: Generation,
        *,
        action: Literal["install", "retry", "reacquire"] = "install",
    ) -> RuntimeInstallationView:
        if not self._capable():
            raise RuntimeInstallationUnavailable
        latest = self._latest(generation)
        current = self._view(generation, latest, capable=True)
        if current.status == "ready":
            return current
        active = self._active_worker()
        if active.operation_id is not None:
            if active.generation == generation:
                existing = self.get(UUID(active.operation_id))
                assert existing is not None
                return existing
            raise RuntimeInstallationBusy
        for other in GENERATIONS:
            row = self._latest(other)
            if row is None or row.state.value not in (
                "pending",
                "acquiring",
                "assembling",
                "validating",
            ):
                continue
            row = self._reconcile(row)
            if row.state.value in ("pending", "acquiring", "assembling", "validating"):
                if other == generation:
                    return self._view(generation, row, capable=True)
                raise RuntimeInstallationBusy
        if active.lock_held:
            raise RuntimeInstallationBusy
        allowed = {
            "not_installed": "install",
            "failed": "retry",
            "missing": "reacquire",
        }
        if allowed.get(current.status) != action:
            raise RuntimeInstallationActionConflict
        operation_id = uuid4()
        try:
            result = self._broker.launch_reviewed_runtime_installation(
                generation=generation, operation_id=str(operation_id)
            )
        except Exception as error:
            # A second Web request may have won broker admission after our read.
            active = self._active_worker()
            if active.operation_id is not None and active.generation == generation:
                existing = self.get(UUID(active.operation_id))
                assert existing is not None
                return existing
            if active.operation_id is not None or active.lock_held:
                raise RuntimeInstallationBusy from error
            raise RuntimeInstallationUnavailable from error
        observed = self.get(UUID(result.operation_id))
        if observed is not None:
            return replace(observed, newly_accepted=result.newly_accepted)
        return replace(
            self._view(
                generation,
                None,
                capable=True,
                worker_operation_id=result.operation_id,
            ),
            newly_accepted=result.newly_accepted,
        )
