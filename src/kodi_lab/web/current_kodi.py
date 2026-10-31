"""Presentation of the selected session using existing runtime observations."""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.development_session_workflow import (
    CurrentKodiSummary,
    DevelopmentSessionWorkflow,
    SessionView,
)
from kodi_lab.application.development_sessions import DevelopmentSessionService
from kodi_lab.application.reconciliation import ManagedInstanceReconciliationService
from kodi_lab.application.runtime_status import (
    ManagedRuntimeStatusService,
    ManagedStatusObservation,
)
from kodi_lab.broker.errors import ClientError
from kodi_lab.storage.development_sessions import DevelopmentSessionConflict
from kodi_lab.storage.models import (
    GlobalHeavyOperationLeaseRow,
    ManagedInstanceMutationLeaseRow,
)
from kodi_lab.storage.repositories import AgentWorkspaceRepository, TargetRepository
from kodi_lab.storage.retained_development import retained_targets


def friendly_kodi_name(version: str | None) -> str:
    generation = {"19": "Matrix", "20": "Nexus", "21": "Omega", "22": "Piers"}
    if not version:
        return "Selected Kodi"
    return f"{generation.get(version.split('.')[0], 'Kodi')} {version}"


class CurrentKodiPresentation:
    def __init__(
        self,
        factory: sessionmaker[Session],
        workflow: DevelopmentSessionWorkflow,
        sessions: DevelopmentSessionService,
        status: ManagedRuntimeStatusService,
        reconciliation: ManagedInstanceReconciliationService,
    ) -> None:
        self.factory, self.workflow, self.sessions = factory, workflow, sessions
        self.status, self.reconciliation = status, reconciliation

    def identity(self) -> SessionView:
        """Stored identity only, suitable for navigation and initial rendering."""
        view = self.workflow.view()
        current = view.current
        if current is None:
            return view
        with self.factory() as db:
            target = TargetRepository(db).get(current.target_id)
            workspace = AgentWorkspaceRepository(db).get(current.workspace_id)
            retained = str(current.target_id) in retained_targets(db)
        view.summary = CurrentKodiSummary(
            name=friendly_kodi_name(None if target is None else target.kodi_version),
            workspace_name="Workspace needs review"
            if workspace is None
            else workspace.source_logical_name,
            addon_id="" if workspace is None else workspace.source_addon_id,
            status="Checking status",
            detail="Checking the selected environment…",
            actions=[],
            checked_at=datetime.now(UTC),
            recovery_url=f"/instances/{current.target_id}",
            retained=retained,
        )
        return view

    def observe(self) -> SessionView:
        view = self.identity()
        current, summary = view.current, view.summary
        if current is None or summary is None:
            return view
        summary.status = "Needs Attention"
        summary.detail = (
            "Could not confirm this environment. Review its exact status "
            "and history before acting."
        )
        try:
            observation = self.status.get_observation(current.target_id)
            runtime = observation.status
            view.runtime = runtime
            reconciliation = self.reconciliation.status(current.target_id)
            with self.factory() as db:
                busy = (
                    db.scalar(select(GlobalHeavyOperationLeaseRow).limit(1)) is not None
                )
                busy = (
                    busy
                    or db.get(ManagedInstanceMutationLeaseRow, str(current.target_id))
                    is not None
                )
                eligible = True
                try:
                    self.sessions._runtime(
                        db, current.target_id, current.runtime_revision_id
                    )
                except (DevelopmentSessionConflict, ValueError, OSError):
                    eligible = False
            if runtime.runtime_revision_id != current.runtime_revision_id:
                raise DevelopmentSessionConflict("development_session_identity_changed")
            if reconciliation.operation_uuid is not None:
                summary.recovery_url = (
                    f"/instances/{current.target_id}/history"
                    f"#activity-{reconciliation.operation_uuid}"
                )
            if current.phase == "uncertain" or reconciliation.blocking:
                summary.detail = (
                    "An earlier action needs review. Do not repeat it; "
                    "inspect its saved operation."
                )
            elif busy or current.phase == "provisioning":
                summary.status = (
                    "Preparing" if current.phase == "provisioning" else "Working"
                )
                summary.detail = (
                    "An operation is in progress. Wait for its confirmed outcome."
                )
            elif current.phase == "reserved":
                if (
                    runtime.provision_state == "unprovisioned"
                    and "provision" in runtime.allowed_actions
                ):
                    summary.status = "Preparation required"
                    summary.detail = (
                        "Prepare the selected clean, stopped Kodi profile. "
                        "Your workspace is retained."
                    )
                    if eligible:
                        summary.actions = ["prepare"]
            elif current.phase == "bound":
                inventory = observation.inventory
                from kodi_lab.storage.retained_development import (
                    session_inventory_matches,
                )

                with self.factory() as db:
                    confirmed = inventory is not None and session_inventory_matches(
                        db,
                        inventory.containers,
                        str(current.target_id),
                        current.container_id,
                    )
                if not confirmed:
                    summary.detail = (
                        "The selected Kodi profile is absent or unconfirmed. "
                        "Review its exact history."
                    )
                else:
                    self._bound_summary(view, eligible, observation)
            if (
                not busy
                and not reconciliation.blocking
                and current.phase in {"reserved", "bound"}
                and observation.inventory is not None
                and not observation.inventory.containers
                and not self.sessions._inventory().containers
            ):
                summary.actions.append("close-absent")
            if not eligible:
                summary.status = "Needs Attention"
                summary.detail = (
                    "Runtime approval or provenance needs review. "
                    "Preparation and Start are blocked; "
                    "exact recovery actions remain available."
                )
            summary.checked_at = datetime.now(UTC)
        except (ClientError, DevelopmentSessionConflict, ValueError):
            summary.actions = []
        # Observations can cross a switch, profile replacement or workspace rebind.
        # Never merge facts from different selections or grant actions from them.
        if self.workflow.view().current != current:
            view = self.identity()
            if view.summary is not None:
                view.summary.status = "Needs Attention"
                view.summary.detail = (
                    "The selected environment changed. "
                    "Refresh Current Kodi before acting."
                )
        return view

    def _bound_summary(
        self, view: SessionView, eligible: bool, observation: ManagedStatusObservation
    ) -> None:
        current, summary, runtime = view.current, view.summary, view.runtime
        assert current is not None and summary is not None and runtime is not None
        # Reuse the same observation. The identity was fenced in observe().
        inventory = observation.inventory
        assert inventory is not None
        container = next(
            (
                item
                for item in inventory.containers
                if item.managed_instance_id == str(current.target_id)
            ),
            None,
        )
        if (
            container is None
            or container.container_id != current.container_id
            or container.runtime_revision_id != str(current.runtime_revision_id)
        ):
            raise DevelopmentSessionConflict("development_session_identity_changed")
        if eligible and "start" in runtime.allowed_actions:
            summary.actions.append("resume")
        if "stop" in runtime.allowed_actions:
            summary.actions.append("stop")
        if runtime.failure_code or runtime.usability_state == "failed":
            summary.detail = (
                "Kodi needs attention. Review its exact status and retained operation."
            )
        elif runtime.container_state == "stopped":
            summary.status = "Stopped"
            summary.detail = "Start Kodi to continue with the same profile."
        elif runtime.container_state == "running":
            summary.status = (
                "Ready"
                if (
                    runtime.usability_state == "usable"
                    and runtime.process_state == "running"
                    and runtime.jsonrpc_state == "responsive"
                )
                else "Starting"
                if runtime.readiness_state == "checking"
                else "Running"
            )
            summary.native_status = (
                "Responding"
                if runtime.jsonrpc_state == "responsive"
                else "Not yet confirmed"
            )
            summary.detail = (
                "This is the Kodi used by agents assigned to your workspace."
            )
