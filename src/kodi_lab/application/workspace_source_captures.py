"""Internal transactional foundation for explicit source-to-workspace capture.

No Web or MCP authority surface is registered in this gate.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.development.deployment import (
    DevelopmentTreeError,
    capture_development_tree,
    compare_captures,
)
from kodi_lab.development.sources import DevelopmentSourceValidator
from kodi_lab.domain import (
    AgentWorkspace,
    AgentWorkspaceBytesState,
    AgentWorkspaceState,
)
from kodi_lab.domain.development_sources import DevelopmentSource, DevelopmentSourceKind
from kodi_lab.storage.models import (
    AgentWorkspaceCaptureOperationRow,
    AgentWorkspaceRow,
    McpWorkspaceDeploymentOperationRow,
    McpWorkspaceExecutionOperationRow,
    RepositoryWorkspaceDeploymentRow,
)
from kodi_lab.storage.repositories import (
    AgentWorkspacePromotionOperationRepository,
    AgentWorkspacePromotionPlanRepository,
    AgentWorkspaceRepository,
    DevelopmentSourceRepository,
    PromotionVerificationOperationRepository,
    _timestamp_to_text,
)
from kodi_lab.workspace.source_capture import (
    _capture_private,
    complete_source_capture_switch,
    stage_source_capture,
)
from kodi_lab.workspace.storage import (
    AgentWorkspaceStorageError,
    workspace_coordination_lock,
)


class WorkspaceSourceCaptureError(RuntimeError):
    def __init__(
        self, code: str, *, observed_current_generation: int | None = None
    ) -> None:
        self.code = code
        self.observed_current_generation = observed_current_generation
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class WorkspaceSourceCaptureResult:
    workspace_id: UUID
    operation_id: UUID | None
    generation: int
    tree_digest: str
    changed: bool


@dataclass(frozen=True, slots=True)
class WorkspaceSourceScanResult:
    workspace_id: UUID
    source_id: UUID
    addon_id: str
    addon_version: str
    tree_digest: str
    scanned_at: datetime


class WorkspaceSourceCaptureService:
    """Capture current registered source bytes into an existing clean workspace."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        data_root: Path,
        development_root: Path,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        uuid_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        self._sessions = session_factory
        self._data_root = data_root
        self._development_root = development_root
        self._clock = clock
        self._uuid_factory = uuid_factory

    def _preflight(
        self,
        session: Session,
        workspace_id: UUID,
        generation: int,
        *,
        allow_private_edits: bool = False,
    ) -> tuple[AgentWorkspace, DevelopmentSource]:
        workspace = AgentWorkspaceRepository(session).get(workspace_id)
        if (
            workspace is not None
            and workspace.state is AgentWorkspaceState.RECONCILIATION_REQUIRED
        ):
            raise WorkspaceSourceCaptureError("capture_reconciliation_required")
        if (
            workspace is not None
            and workspace.state is AgentWorkspaceState.READY
            and workspace.generation != generation
        ):
            raise WorkspaceSourceCaptureError(
                "workspace_generation_conflict",
                observed_current_generation=workspace.generation,
            )
        if (
            workspace is None
            or workspace.state is not AgentWorkspaceState.READY
            or workspace.bytes_state is not AgentWorkspaceBytesState.PRESENT
            or workspace.generation != generation
            or workspace.current_tree_digest is None
            or workspace.baseline_manifest_digest is None
        ):
            raise WorkspaceSourceCaptureError("workspace_not_clean_and_ready")
        source = DevelopmentSourceRepository(session).get(
            workspace.development_source_id
        )
        if (
            source is None
            or source.source_kind is not DevelopmentSourceKind.LOCAL_DIRECTORY
        ):
            raise WorkspaceSourceCaptureError("registered_source_unavailable")
        if (
            not allow_private_edits
            and workspace.current_tree_digest != workspace.baseline_manifest_digest
        ):
            raise WorkspaceSourceCaptureError("workspace_private_edits_present")
        if workspace.active_capture_id is not None:
            prior_capture = session.get(
                AgentWorkspaceCaptureOperationRow, str(workspace.active_capture_id)
            )
            if (
                prior_capture is None
                or prior_capture.state != "succeeded"
                or prior_capture.workspace_id != str(workspace_id)
                or prior_capture.captured_digest != workspace.baseline_manifest_digest
                or workspace.active_baseline_artifact_id != workspace.active_capture_id
            ):
                raise WorkspaceSourceCaptureError("workspace_capture_evidence_changed")
        if (
            AgentWorkspacePromotionPlanRepository(session).active_for_workspace(
                workspace_id, now=self._clock()
            )
            is not None
            or AgentWorkspacePromotionOperationRepository(session).active_for_workspace(
                workspace_id
            )
            is not None
            or PromotionVerificationOperationRepository(session).blocking_for_workspace(
                workspace_id
            )
        ):
            raise WorkspaceSourceCaptureError("workspace_promotion_active")
        pending_repository = session.scalar(
            select(RepositoryWorkspaceDeploymentRow.operation_id)
            .where(
                RepositoryWorkspaceDeploymentRow.workspace_id == str(workspace_id),
                RepositoryWorkspaceDeploymentRow.state.not_in(("succeeded", "failed")),
            )
            .limit(1)
        )
        if pending_repository is not None:
            raise WorkspaceSourceCaptureError("workspace_repository_deployment_active")
        unresolved_deployment = session.scalar(
            select(McpWorkspaceDeploymentOperationRow.id)
            .where(
                McpWorkspaceDeploymentOperationRow.workspace_id == str(workspace_id),
                McpWorkspaceDeploymentOperationRow.state.in_(
                    ("pending", "running", "ambiguous", "reconciliation_required")
                ),
            )
            .limit(1)
        )
        unresolved_execution = session.scalar(
            select(McpWorkspaceExecutionOperationRow.id)
            .where(
                McpWorkspaceExecutionOperationRow.workspace_id == str(workspace_id),
                McpWorkspaceExecutionOperationRow.state.in_(
                    ("pending", "running", "ambiguous", "reconciliation_required")
                ),
            )
            .limit(1)
        )
        if unresolved_deployment is not None or unresolved_execution is not None:
            raise WorkspaceSourceCaptureError("workspace_development_operation_active")
        if (
            _capture_private(self._data_root, workspace_id, "tree")
            != workspace.current_tree_digest
            or _capture_private(self._data_root, workspace_id, "baseline/tree")
            != workspace.baseline_manifest_digest
        ):
            raise WorkspaceSourceCaptureError("workspace_bytes_changed")
        return workspace, source

    def scan_source(self, workspace_id: UUID) -> WorkspaceSourceScanResult:
        """Read-only, fresh, two-pass source digest for later status surfaces."""

        with self._sessions() as session:
            workspace = AgentWorkspaceRepository(session).get(workspace_id)
            source = (
                None
                if workspace is None
                else DevelopmentSourceRepository(session).get(
                    workspace.development_source_id
                )
            )
        if (
            workspace is None
            or source is None
            or source.source_kind is not DevelopmentSourceKind.LOCAL_DIRECTORY
        ):
            raise WorkspaceSourceCaptureError("registered_source_unavailable")
        first = capture_development_tree(
            self._development_root, source.relative_locator
        )
        observation = DevelopmentSourceValidator(self._development_root).validate(
            source.relative_locator
        )
        second = capture_development_tree(
            self._development_root, source.relative_locator
        )
        compare_captures(first, second)
        if observation.addon_id != workspace.source_addon_id:
            raise WorkspaceSourceCaptureError("source_addon_identity_changed")
        return WorkspaceSourceScanResult(
            workspace_id=workspace_id,
            source_id=source.id,
            addon_id=observation.addon_id,
            addon_version=observation.observed_version,
            tree_digest=first.digest,
            scanned_at=self._clock(),
        )

    def capture_eligibility(self, workspace_id: UUID, generation: int) -> str | None:
        """Return a bounded reason, or None when ordinary capture may begin."""

        try:
            with self._sessions() as session:
                self._preflight(session, workspace_id, generation)
        except WorkspaceSourceCaptureError as error:
            return error.code
        except AgentWorkspaceStorageError as error:
            return error.code
        return None

    def capture(
        self,
        workspace_id: UUID,
        *,
        expected_generation: int,
        checkpoint: Callable[[str], None] = lambda _phase: None,
    ) -> WorkspaceSourceCaptureResult:
        """One changed whole-source capture advances exactly one generation."""

        with workspace_coordination_lock(
            self._data_root, workspace_id, exclusive=True, blocking=False
        ):
            with self._sessions() as session:
                workspace, source = self._preflight(
                    session, workspace_id, expected_generation
                )
            if (
                workspace.current_tree_digest is None
                or workspace.baseline_manifest_digest is None
            ):
                raise WorkspaceSourceCaptureError("workspace_not_clean_and_ready")
            first = capture_development_tree(
                self._development_root, source.relative_locator
            )
            second = capture_development_tree(
                self._development_root, source.relative_locator
            )
            compare_captures(first, second)
            if first.digest == workspace.current_tree_digest:
                return WorkspaceSourceCaptureResult(
                    workspace_id, None, workspace.generation, first.digest, False
                )
            operation_id = self._uuid_factory()
            now = self._clock()
            with self._sessions.begin() as session:
                changed = session.execute(
                    update(AgentWorkspaceRow)
                    .where(
                        AgentWorkspaceRow.id == str(workspace_id),
                        AgentWorkspaceRow.state == AgentWorkspaceState.READY,
                        AgentWorkspaceRow.generation == expected_generation,
                        AgentWorkspaceRow.current_tree_digest
                        == workspace.current_tree_digest,
                    )
                    .values(state=AgentWorkspaceState.CAPTURING)
                )
                if getattr(changed, "rowcount", 0) != 1:
                    current = session.get(AgentWorkspaceRow, str(workspace_id))
                    observed_generation = (
                        current.generation
                        if current is not None
                        and current.state is AgentWorkspaceState.READY
                        and current.generation != expected_generation
                        else None
                    )
                    raise WorkspaceSourceCaptureError(
                        "workspace_generation_conflict",
                        observed_current_generation=observed_generation,
                    )
                session.add(
                    AgentWorkspaceCaptureOperationRow(
                        id=str(operation_id),
                        workspace_id=str(workspace_id),
                        development_source_id=str(source.id),
                        state="staging",
                        prior_generation=workspace.generation,
                        resulting_generation=workspace.generation + 1,
                        prior_baseline_id=str(
                            workspace.active_baseline_artifact_id
                            or workspace.baseline_artifact_id
                        ),
                        prior_baseline_digest=workspace.baseline_manifest_digest,
                        prior_tree_digest=workspace.current_tree_digest,
                        captured_digest=None,
                        captured_version=None,
                        requested_at=_timestamp_to_text(now),
                        completed_at=None,
                        failure_code=None,
                    )
                )
            checkpoint("pending")
            try:
                staged = stage_source_capture(
                    self._data_root,
                    workspace_id,
                    operation_id,
                    self._development_root,
                    source.relative_locator,
                    workspace.source_addon_id,
                    prior_digest=workspace.current_tree_digest,
                    checkpoint=checkpoint,
                )
                with self._sessions.begin() as session:
                    row = session.get(
                        AgentWorkspaceCaptureOperationRow, str(operation_id)
                    )
                    if row is None or row.state != "staging":
                        raise WorkspaceSourceCaptureError("capture_operation_changed")
                    row.state = "switching"
                    row.captured_digest = staged.digest
                    row.captured_version = staged.addon_version
                checkpoint("switching")
                complete_source_capture_switch(
                    self._data_root,
                    workspace_id,
                    operation_id,
                    workspace.active_baseline_artifact_id
                    or workspace.baseline_artifact_id,
                    prior_digest=workspace.baseline_manifest_digest,
                    captured_digest=staged.digest,
                    checkpoint=checkpoint,
                )
                self._finalize(operation_id)
                return WorkspaceSourceCaptureResult(
                    workspace_id,
                    operation_id,
                    workspace.generation + 1,
                    staged.digest,
                    True,
                )
            except (DevelopmentTreeError, AgentWorkspaceStorageError):
                with self._sessions.begin() as session:
                    row = session.get(
                        AgentWorkspaceCaptureOperationRow, str(operation_id)
                    )
                    if row is not None and row.state == "staging":
                        row.state = "failed"
                        row.completed_at = _timestamp_to_text(self._clock())
                        row.failure_code = "source_capture_failed"
                        workspace_row = session.get(
                            AgentWorkspaceRow, str(workspace_id)
                        )
                        if workspace_row is not None:
                            workspace_row.state = AgentWorkspaceState.READY
                raise

    def _finalize(self, operation_id: UUID) -> WorkspaceSourceCaptureResult:
        with self._sessions.begin() as session:
            row = session.get(AgentWorkspaceCaptureOperationRow, str(operation_id))
            if row is None:
                raise WorkspaceSourceCaptureError("capture_operation_missing")
            if row.state == "succeeded":
                return WorkspaceSourceCaptureResult(
                    UUID(row.workspace_id),
                    operation_id,
                    row.resulting_generation,
                    row.captured_digest or "",
                    True,
                )
            workspace = session.get(AgentWorkspaceRow, row.workspace_id)
            if (
                row.state != "switching"
                or row.captured_digest is None
                or row.captured_version is None
                or workspace is None
                or workspace.state is not AgentWorkspaceState.CAPTURING
                or workspace.generation != row.prior_generation
                or workspace.current_tree_digest != row.prior_tree_digest
                or workspace.baseline_manifest_digest != row.prior_baseline_digest
            ):
                raise WorkspaceSourceCaptureError("capture_reconciliation_required")
            workspace.state = AgentWorkspaceState.READY
            workspace.generation = row.resulting_generation
            workspace.current_tree_digest = row.captured_digest
            workspace.baseline_manifest_digest = row.captured_digest
            workspace.active_baseline_artifact_id = row.id
            workspace.active_capture_id = row.id
            workspace.active_observed_version = row.captured_version
            workspace.captured_at = _timestamp_to_text(self._clock())
            workspace.updated_at = workspace.captured_at
            row.state = "succeeded"
            row.completed_at = workspace.captured_at
            return WorkspaceSourceCaptureResult(
                UUID(row.workspace_id),
                operation_id,
                row.resulting_generation,
                row.captured_digest,
                True,
            )

    def recover(self, operation_id: UUID) -> WorkspaceSourceCaptureResult | None:
        """Deterministically settle staged or switched evidence after interruption."""

        with self._sessions() as session:
            row = session.get(AgentWorkspaceCaptureOperationRow, str(operation_id))
            if row is None:
                raise WorkspaceSourceCaptureError("capture_operation_missing")
            workspace_id = UUID(row.workspace_id)
        with workspace_coordination_lock(
            self._data_root, workspace_id, exclusive=True, blocking=False
        ):
            with self._sessions() as session:
                row = session.get(AgentWorkspaceCaptureOperationRow, str(operation_id))
                if row is None:
                    raise WorkspaceSourceCaptureError("capture_operation_missing")
                if row.state == "succeeded":
                    return WorkspaceSourceCaptureResult(
                        workspace_id,
                        operation_id,
                        row.resulting_generation,
                        row.captured_digest or "",
                        True,
                    )
                if row.state == "failed":
                    return None
                state = row.state
                prior_digest = row.prior_baseline_digest
                prior_id = UUID(row.prior_baseline_id)
                captured_digest = row.captured_digest
            if state == "staging":
                if (
                    _capture_private(self._data_root, workspace_id, "tree")
                    != prior_digest
                    or _capture_private(self._data_root, workspace_id, "baseline/tree")
                    != prior_digest
                ):
                    self._mark_reconciliation(operation_id)
                    raise WorkspaceSourceCaptureError("capture_reconciliation_required")
                with self._sessions.begin() as session:
                    row = session.get(
                        AgentWorkspaceCaptureOperationRow, str(operation_id)
                    )
                    workspace = session.get(AgentWorkspaceRow, str(workspace_id))
                    if row is None or workspace is None or row.state != "staging":
                        raise WorkspaceSourceCaptureError("capture_operation_changed")
                    row.state = "failed"
                    row.failure_code = "interrupted_before_switch"
                    row.completed_at = _timestamp_to_text(self._clock())
                    workspace.state = AgentWorkspaceState.READY
                return None
            if state != "switching" or captured_digest is None:
                raise WorkspaceSourceCaptureError("capture_reconciliation_required")
            try:
                complete_source_capture_switch(
                    self._data_root,
                    workspace_id,
                    operation_id,
                    prior_id,
                    prior_digest=prior_digest,
                    captured_digest=captured_digest,
                )
            except AgentWorkspaceStorageError:
                self._mark_reconciliation(operation_id)
                raise
            return self._finalize(operation_id)

    def _mark_reconciliation(self, operation_id: UUID) -> None:
        with self._sessions.begin() as session:
            row = session.get(AgentWorkspaceCaptureOperationRow, str(operation_id))
            if row is None:
                return
            workspace = session.get(AgentWorkspaceRow, row.workspace_id)
            row.state = "reconciliation_required"
            row.failure_code = "capture_evidence_inconsistent"
            row.completed_at = _timestamp_to_text(self._clock())
            if workspace is not None:
                workspace.state = AgentWorkspaceState.RECONCILIATION_REQUIRED
