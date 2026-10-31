"""Safe Web admission and presentation for reviewed runtime acquisitions."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast
from uuid import UUID, uuid4

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.runtime_eligibility import (
    RuntimeEligibility,
    RuntimeIneligibilityReason,
    RuntimeRevisionEligibilityPolicy,
    RuntimeSelectionPolicy,
)
from kodi_lab.broker.errors import BrokerAuthorizationError, RemoteBrokerError
from kodi_lab.broker.operations import ReviewedInstallationLaunchResult
from kodi_lab.config import Settings
from kodi_lab.runtime.preview import PreviewAcknowledgement
from kodi_lab.runtime.recipes import (
    NOVNC_PHASE1_DIGEST,
    load_display_candidate_recipe,
    load_installation_recipe,
    load_recorded_reviewed_recipe,
    recipe_target_id,
)
from kodi_lab.storage.models import RuntimeRecipeInstallationRow
from kodi_lab.storage.repositories import (
    ManagedInstanceRepository,
    RuntimeDefinitionRepository,
    RuntimeRecipeInstallationRepository,
    RuntimeRevisionRepository,
)

Generation = Literal["matrix", "nexus", "omega", "piers"]
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


def preview_acknowledgement(*, display_candidate: bool = False) -> dict[str, object]:
    reviewed = (
        load_display_candidate_recipe("piers")
        if display_candidate
        else load_installation_recipe("piers")
    )
    return PreviewAcknowledgement.model_validate(
        {
            "recipe_id": reviewed.recipe.recipe_id,
            "recipe_revision": reviewed.recipe.recipe_revision,
            "recipe_digest": reviewed.digest,
            "release": reviewed.recipe.kodi_version,
            "support_tier": "preview",
            "acknowledgement": "development-only-prerelease",
        }
    ).model_dump(mode="json")


@dataclass(frozen=True)
class RuntimeInstallationView:
    generation: Generation
    display_name: str
    version: str
    status: str
    phase: str
    operation_id: str | None = None
    status_url: str | None = None
    action: Literal["install", "retry", "reacquire", "create", "open", "none"] = "none"
    managed_instance_url: str | None = None
    failure_message: str | None = None
    note: str | None = None
    newly_accepted: bool = False
    technical_details: dict[str, str] | None = None
    unavailable_code: str | None = None
    preview_acknowledgement: dict[str, object] | None = None
    created_at: str | None = None
    phase_started_at: str | None = None
    completed_at: str | None = None
    managed_instance_name: str | None = None
    eligibility_reason: str | None = None


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
        return self._runtime_eligibility(row).eligible

    def _runtime_eligibility(
        self, row: RuntimeRecipeInstallationRow
    ) -> RuntimeEligibility:
        missing = RuntimeEligibility(
            False, RuntimeIneligibilityReason.RECIPE_PROVENANCE_UNAVAILABLE
        )
        if row.runtime_revision_id is None:
            return missing
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
                if revision is None or definition is None:
                    return missing
                return RuntimeRevisionEligibilityPolicy(
                    data_root=self._data_root
                ).evaluate_for_release(
                    session,
                    required_release=definition.kodi_release,
                    definition=definition,
                    revision=revision,
                    policy=RuntimeSelectionPolicy(
                        include_legacy=True,
                        include_prerelease=row.generation == "piers",
                        preview_recipe_digest=reviewed.digest
                        if row.generation == "piers"
                        else None,
                        validation_contract=reviewed.recipe.validation_contract,
                    ),
                )
        except (ValueError, OSError):
            return missing

    def _eligible_development_record(self, row: RuntimeRecipeInstallationRow) -> bool:
        """Describe an existing sealed pin; never admit a new installation."""
        from kodi_lab.application.development_sessions import DevelopmentSessionService
        from kodi_lab.storage.development_sessions import DevelopmentSessionConflict

        try:
            reviewed = load_recorded_reviewed_recipe(
                row.generation, row.recipe_id, row.recipe_revision, row.recipe_digest
            )
            if (
                reviewed.digest != NOVNC_PHASE1_DIGEST
                or row.runtime_revision_id is None
            ):
                return False
            service = DevelopmentSessionService(
                self._factory,
                lambda: self._broker.managed_container_inventory(),
                data_root=self._data_root,
            )
            with self._factory() as session:
                service._runtime(
                    session, recipe_target_id(reviewed), UUID(row.runtime_revision_id)
                )
            return True
        except (ValueError, OSError, DevelopmentSessionConflict):
            return False

    def start_preview(
        self,
        *,
        development_mode: bool,
        acknowledgement: PreviewAcknowledgement,
        display_candidate: bool = False,
        action: Literal["install", "retry", "reacquire"] | None = None,
    ) -> ReviewedInstallationLaunchResult:
        """Admission retains exact consent and broker-owned operation semantics."""
        if development_mode is not True:
            raise RuntimeInstallationUnavailable("Development mode required")
        ack = PreviewAcknowledgement.model_validate(acknowledgement)
        if ack.model_dump(mode="json") != preview_acknowledgement(
            display_candidate=display_candidate
        ):
            raise RuntimeInstallationActionConflict("Preview acknowledgement differs")
        if action is not None:
            current = self._view(
                "piers",
                self._latest("piers", display_candidate=display_candidate),
                capable=True,
                display_candidate=display_candidate,
            )
            if current.action != action:
                raise RuntimeInstallationActionConflict
        if not self._capable():
            raise RuntimeInstallationUnavailable
        active = self._active_worker()
        if active.operation_id is not None or active.lock_held:
            raise RuntimeInstallationBusy
        from kodi_lab.runtime.preview import admission_evidence, reviewed_preview_recipe
        from kodi_lab.runtime.recipes import installation_generations

        reviewed = reviewed_preview_recipe(ack)
        operation = uuid4()
        with self._factory() as session:
            previous = RuntimeRecipeInstallationRepository(session).active("piers")
        if previous is not None and previous.state.value != "pending":
            reconciled = self._reconcile(previous)
            if reconciled.state.value not in ("failed", "ready"):
                raise RuntimeInstallationBusy
        with self._factory.begin() as session:
            repo = RuntimeRecipeInstallationRepository(session)
            if any(
                repo.active(generation) is not None
                for generation in installation_generations()
                if generation != "piers"
            ):
                raise RuntimeInstallationBusy
            row = repo.active("piers")
            if row is None:
                row = repo.create(
                    operation_id=operation,
                    recipe_id=reviewed.recipe.recipe_id,
                    recipe_revision=reviewed.recipe.recipe_revision,
                    recipe_digest=reviewed.digest,
                    generation="piers",
                    now=datetime.now(UTC),
                )
                row.attestation_json = admission_evidence(ack, operation)
            else:
                operation = UUID(row.id)
                if (
                    row.state.value != "pending"
                    or row.recipe_digest != reviewed.digest
                    or row.attestation_json != admission_evidence(ack, operation)
                ):
                    raise RuntimeInstallationBusy
        try:
            result = self._broker.launch_reviewed_runtime_installation(
                generation="piers",
                operation_id=str(operation),
                preview_acknowledgement=ack,
            )
        except Exception as error:
            # A running exact worker owns an ambiguous response; do not mark its
            # installation failed merely because the client lost the response.
            observed = self._broker.observe_reviewed_runtime_installation(
                str(operation)
            )
            if observed.state == "running":
                return ReviewedInstallationLaunchResult(
                    operation_id=str(operation),
                    generation="piers",
                    newly_accepted=False,
                )
            with self._factory.begin() as session:
                RuntimeRecipeInstallationRepository(session).fail(
                    operation, code="worker_launch_failed", now=datetime.now(UTC)
                )
            raise RuntimeInstallationUnavailable from error
        return ReviewedInstallationLaunchResult.model_validate(result)

    def _capable(self) -> bool:
        return self._capability_failure() is None

    def create_preview_instance(
        self,
        *,
        development_mode: bool,
        acknowledgement: PreviewAcknowledgement,
        display_candidate: bool = False,
    ) -> RuntimeInstallationView:
        """Create only the exact sealed Preview revision through normal selection."""
        if development_mode is not True:
            raise RuntimeInstallationUnavailable("Development mode required")
        ack = PreviewAcknowledgement.model_validate(acknowledgement)
        if ack.model_dump(mode="json") != preview_acknowledgement(
            display_candidate=display_candidate
        ):
            raise RuntimeInstallationActionConflict("Preview acknowledgement differs")
        current = self._view(
            "piers",
            self._latest("piers", display_candidate=display_candidate),
            capable=self._capable(),
            display_candidate=display_candidate,
        )
        if current.action == "open":
            return current
        if current.action != "create":
            raise RuntimeInstallationActionConflict
        from kodi_lab.application.managed_instances import (
            ExactRevisionSelection,
            ManagedInstanceService,
        )
        from kodi_lab.domain import (
            ConnectionState,
            HealthStatus,
            InstanceType,
            Target,
            TargetAvailability,
            TargetType,
        )
        from kodi_lab.storage.repositories import TargetRepository

        reviewed = (
            load_display_candidate_recipe("piers")
            if display_candidate
            else load_installation_recipe("piers")
        )
        row = self._latest("piers", display_candidate=display_candidate)
        if (
            row is None
            or row.runtime_revision_id is None
            or not self._eligible_ready(row)
        ):
            raise RuntimeInstallationUnavailable
        target_id = recipe_target_id(reviewed)
        with self._factory.begin() as session:
            targets = TargetRepository(session)
            existing = targets.get(target_id)
            if existing is None:
                targets.create(
                    Target(
                        id=target_id,
                        name="Kodi 22 Piers RC1",
                        target_type=TargetType.MANAGED,
                        kodi_version=reviewed.recipe.kodi_version,
                        availability=TargetAvailability.UNAVAILABLE,
                        health=HealthStatus.WARNING,
                        connection_state=ConnectionState.DISCONNECTED,
                    )
                )
            elif (
                existing.target_type is not TargetType.MANAGED
                or existing.kodi_release.canonical != reviewed.recipe.kodi_version
            ):
                raise RuntimeInstallationActionConflict
        ManagedInstanceService(
            self._factory,
            include_prerelease=True,
            preview_recipe_digest=reviewed.digest,
        ).create(
            target_id=target_id,
            selection=ExactRevisionSelection(
                reviewed.recipe.definition_id, UUID(row.runtime_revision_id)
            ),
            instance_type=InstanceType.DISPOSABLE,
        )
        return self._view(
            "piers", row, capable=True, display_candidate=display_candidate
        )

    def _capability_failure(self) -> str | None:
        try:
            if REQUIRED_OPERATIONS.issubset(self._broker.capabilities().operations):
                return None
            return "backend_capability_missing"
        except AttributeError:
            return "backend_capability_missing"
        except BrokerAuthorizationError:
            return "broker_authorization_denied"
        except RemoteBrokerError as error:
            return (
                "backend_capability_missing"
                if error.code == "unknown_operation"
                else "broker_unreachable"
            )
        except Exception:
            return "broker_unreachable"

    def _unavailable(
        self, view: RuntimeInstallationView, code: str
    ) -> RuntimeInstallationView:
        messages = {
            "broker_authorization_denied": (
                "The broker denied this Web service's installation access. "
                "An administrator must check the configured service identity."
            ),
            "backend_capability_missing": (
                "This broker does not provide the reviewed runtime installation API. "
                "An administrator must use a compatible installed broker."
            ),
            "broker_unreachable": (
                "Could not contact the installation broker. Check its service and "
                "connection before retrying."
            ),
            "worker_unavailable": (
                "Could not safely verify the installation worker or acquisition lock. "
                "Check broker health and installation diagnostics before retrying."
            ),
            "image_check_unavailable": (
                "Could not verify the exact local runtime image. Check the broker "
                "and image storage before retrying."
            ),
            "runtime_ineligible": (
                "The recorded runtime does not meet current approval, provenance or "
                "validation requirements. An administrator must review the runtime "
                "record before installation can continue."
            ),
            "recorded_recipe_unavailable": (
                "This installation record no longer matches a reviewed recipe. "
                "Review this exact record before preparing Kodi."
            ),
        }
        return replace(
            view,
            status="unavailable",
            phase="Installation unavailable",
            action="none",
            unavailable_code=code,
            failure_message=messages[code],
        )

    def _active_worker(self) -> Any:
        try:
            return self._broker.active_reviewed_runtime_installation()
        except Exception as error:
            raise RuntimeInstallationUnavailable from error

    def _latest(
        self, generation: Generation, *, display_candidate: bool = False
    ) -> RuntimeRecipeInstallationRow | None:
        reviewed = (
            load_display_candidate_recipe(generation)
            if display_candidate
            else load_installation_recipe(generation)
        )
        with self._factory() as session:
            return session.scalar(
                select(RuntimeRecipeInstallationRow)
                .where(
                    RuntimeRecipeInstallationRow.generation == generation,
                    RuntimeRecipeInstallationRow.recipe_id == reviewed.recipe.recipe_id,
                    RuntimeRecipeInstallationRow.recipe_revision
                    == reviewed.recipe.recipe_revision,
                )
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
        with self._factory() as session:
            return RuntimeRecipeInstallationRepository(session).get(UUID(row.id)) or row

    def _view(
        self,
        generation: Generation,
        row: RuntimeRecipeInstallationRow | None,
        *,
        capable: bool,
        worker_operation_id: str | None = None,
        unavailable_code: str = "broker_unreachable",
        recorded: bool = False,
        reconcile: bool = True,
        display_candidate: bool = False,
    ) -> RuntimeInstallationView:
        reviewed = (
            load_display_candidate_recipe(generation)
            if display_candidate
            else load_installation_recipe(generation)
        )
        recorded_recipe_unavailable = False
        if recorded and row is not None:
            try:
                reviewed = load_recorded_reviewed_recipe(
                    row.generation,
                    row.recipe_id,
                    row.recipe_revision,
                    row.recipe_digest,
                )
            except (ValueError, OSError):
                recorded_recipe_unavailable = True
        recipe = reviewed.recipe
        development_variant = reviewed.digest == NOVNC_PHASE1_DIGEST
        major, stable_version = (
            ("22", "22.0 RC1") if generation == "piers" else STABLE_RELEASES[generation]
        )
        base = RuntimeInstallationView(
            generation=generation,
            display_name="Kodi 22 Piers RC1"
            if generation == "piers"
            else f"Kodi {major} — {generation.capitalize()}",
            version=stable_version,
            managed_instance_name=(
                "Kodi 22 Piers RC1"
                if generation == "piers"
                else f"{recipe.display_generation} Lab"
                + (" · experimental noVNC" if development_variant else "")
            ),
            status="not_installed",
            phase="Not installed",
            action="install",
            preview_acknowledgement=preview_acknowledgement()
            if generation == "piers"
            else None,
        )
        if recorded_recipe_unavailable and row is not None:
            return self._unavailable(
                replace(
                    base,
                    managed_instance_name=(
                        f"{generation.capitalize()} installation record"
                    ),
                    operation_id=row.id,
                    technical_details={
                        "recipe": row.recipe_id,
                        "recipe_revision": str(row.recipe_revision),
                        "recipe_digest": row.recipe_digest,
                        "recorded_state": row.state.value,
                        "revision_id": row.runtime_revision_id or "",
                        "image_id": row.image_id or "",
                        "failure_code": row.failure_code or "",
                    },
                ),
                "recorded_recipe_unavailable",
            )
        if not capable:
            if row is not None:
                target_id = recipe_target_id(reviewed)
                with self._factory() as session:
                    instance = ManagedInstanceRepository(session).get(target_id)
                base = replace(
                    base,
                    operation_id=row.id,
                    technical_details={
                        "recorded_state": row.state.value,
                        "revision_id": row.runtime_revision_id or "",
                        "image_id": row.image_id or "",
                        "failure_code": row.failure_code or "",
                    },
                    note="Stored records do not prove runtime files are available.",
                    managed_instance_url=f"/instances/{target_id}"
                    if instance
                    else None,
                )
            return self._unavailable(base, unavailable_code)
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
        if reconcile:
            row = self._reconcile(row)
        state = row.state.value
        operation_id = row.id
        details = {
            "provider": recipe.provider.type,
            "recipe": recipe.recipe_id,
            "recipe_revision": str(recipe.recipe_revision),
            "package_version": recipe.critical_packages[0].version
            if recipe.critical_packages
            else recipe.kodi_version,
            "acquired_at": row.completed_at or "",
            "revision_id": row.runtime_revision_id or "",
            "image_id": row.image_id or "",
            "validation": "Recorded as passed" if state == "ready" else "",
        }
        current = replace(
            base,
            status=state,
            phase=PHASES[state],
            operation_id=operation_id,
            status_url=f"/api/runtime-installations/{operation_id}",
            action="none",
            technical_details=details,
            created_at=row.created_at,
            phase_started_at=row.updated_at,
            completed_at=row.completed_at,
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
            elif code == "pinned_support_unavailable":
                message = (
                    "An exact reviewed Ubuntu support package could not be acquired. "
                    "Check its pinned availability before retrying; "
                    "no replacement package was used."
                )
            elif code in {"support_artifact_mismatch", "support_download_bound"}:
                message = (
                    "Reviewed Ubuntu support acquisition failed "
                    "its exact verification or bounds."
                )
            return replace(
                current,
                action="none" if development_variant else "retry",
                failure_message=message,
                technical_details={**details, "failure_code": code},
            )
        if state != "ready":
            return current
        eligible = (
            self._eligible_development_record(row)
            if development_variant
            else self._eligible_ready(row)
        )
        if not eligible:
            reason = (
                "development_runtime_not_eligible"
                if development_variant
                else self._runtime_eligibility(row).reason
            )
            return replace(
                self._unavailable(current, "runtime_ineligible"),
                eligibility_reason=None if reason is None else str(reason),
            )
        try:
            present = self._broker.reviewed_runtime_image_present(operation_id).present
        except Exception:
            return self._unavailable(current, "image_check_unavailable")
        if not present:
            return replace(
                current,
                status="missing",
                phase="Runtime files missing",
                action="none" if development_variant else "reacquire",
            )
        target_id = recipe_target_id(reviewed)
        with self._factory() as session:
            instance = ManagedInstanceRepository(session).get(target_id)
        if instance is None:
            if generation == "piers":
                return replace(
                    current,
                    action="create",
                    note=(
                        "Preview / Prerelease · Development only. "
                        "Create a managed instance to provision and start Kodi."
                    ),
                )
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
            note=(
                "Reviewed live-screen development runtime. Use Current Kodi for the "
                "selected environment; standard installation uses a separate recipe."
                if development_variant
                else note
            ),
        )

    def records(
        self,
        *,
        include_preview: bool = False,
        before: UUID | None = None,
        limit: int = 256,
    ) -> list[RuntimeInstallationView]:
        """One stable page of recorded history, without reconciling operations."""
        generations = (*GENERATIONS, "piers") if include_preview else GENERATIONS
        with self._factory() as session:
            query = select(RuntimeRecipeInstallationRow).where(
                RuntimeRecipeInstallationRow.generation.in_(generations)
            )
            if before is not None:
                cursor = session.get(RuntimeRecipeInstallationRow, str(before))
                if cursor is None or cursor.generation not in generations:
                    return []
                query = query.where(
                    or_(
                        RuntimeRecipeInstallationRow.created_at < cursor.created_at,
                        and_(
                            RuntimeRecipeInstallationRow.created_at
                            == cursor.created_at,
                            RuntimeRecipeInstallationRow.id < cursor.id,
                        ),
                    )
                )
            rows = session.scalars(
                query.order_by(
                    RuntimeRecipeInstallationRow.created_at.desc(),
                    RuntimeRecipeInstallationRow.id.desc(),
                ).limit(limit)
            ).all()
        capable = self._capable()
        return [
            replace(
                self._view(
                    cast(Generation, row.generation),
                    row,
                    capable=capable,
                    recorded=True,
                    reconcile=False,
                ),
                action="none",
            )
            for row in rows
        ]

    def list(self, *, include_preview: bool = False) -> list[RuntimeInstallationView]:
        generations: tuple[Generation, ...] = (
            (*GENERATIONS, "piers") if include_preview else GENERATIONS
        )
        failure = self._capability_failure()
        if failure is not None:
            return [
                self._view(
                    gen, self._latest(gen), capable=False, unavailable_code=failure
                )
                for gen in generations
            ]
        try:
            active = self._active_worker()
        except RuntimeInstallationUnavailable:
            return [
                self._view(
                    gen,
                    self._latest(gen),
                    capable=False,
                    unavailable_code="worker_unavailable",
                )
                for gen in generations
            ]
        views = []
        for generation in generations:
            row = self._latest(generation)
            worker_id = active.operation_id if active.generation == generation else None
            try:
                views.append(
                    self._view(
                        generation, row, capable=True, worker_operation_id=worker_id
                    )
                )
            except RuntimeInstallationUnavailable:
                views.append(
                    self._view(
                        generation,
                        row,
                        capable=False,
                        unavailable_code="worker_unavailable",
                    )
                )
        return views

    def get(self, operation_id: UUID) -> RuntimeInstallationView | None:
        if not self._capable():
            raise RuntimeInstallationUnavailable
        with self._factory() as session:
            row = RuntimeRecipeInstallationRepository(session).get(operation_id)
        if row is not None:
            if row.generation not in (*GENERATIONS, "piers"):
                return None
            return self._view(row.generation, row, capable=True, recorded=True)
        active = self._active_worker()
        if active.operation_id == str(operation_id) and active.generation in (
            *GENERATIONS,
            "piers",
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
