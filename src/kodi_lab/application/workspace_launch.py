"""Exact-generation managed GUI dispatch, separate from bounded smoke execution."""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Literal, Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.mcp_workspace_development import (
    McpWorkspaceDevelopmentError,
    McpWorkspaceDevelopmentService,
)
from kodi_lab.application.reconciliation import ManagedInstanceReconciliationService
from kodi_lab.broker.docker_inventory import ContainerRuntimeState
from kodi_lab.broker.operations import ObserveManagedKodiRuntimeResult
from kodi_lab.development.deployment import expected_destination_digest
from kodi_lab.development.launch import manifest_launch_entries
from kodi_lab.domain import (
    AgentWorkspaceBytesState,
    AgentWorkspaceState,
    InstanceType,
    ManagedInstanceMutationKind,
    RuntimeExecutionProfile,
    TargetType,
)
from kodi_lab.kodi.bridge import ExternalBridgeError
from kodi_lab.kodi.launch import (
    BridgeLaunchIntent,
    BridgeLaunchReceipt,
    ManagedLaunchRuntimeBinding,
)
from kodi_lab.storage.managed_activity import record_activity, timestamp
from kodi_lab.storage.models import (
    AgentWorkspaceRow,
    ManagedInstanceDevelopmentSourceRow,
    ManagedInstanceRow,
    McpWorkspaceDeploymentOperationRow,
    McpWorkspaceLaunchRow,
    TargetRow,
)
from kodi_lab.storage.repositories import (
    ManagedInstanceMutationLeaseRepository,
    ManagedRuntimeProjectionRepository,
    RuntimeExecutionProfileRepository,
)
from kodi_lab.storage.workspace_deployments import selected_profile_lineage_sql
from kodi_lab.workspace.storage import (
    AgentWorkspaceStorageError,
    capture_workspace_deployment_tree,
    workspace_coordination_lock,
)


class WorkspaceLaunchError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class WorkspaceLaunchState(StrEnum):
    PENDING = "pending"
    DISPATCHING = "dispatching"
    ACCEPTED = "accepted"
    FAILED = "failed"
    AMBIGUOUS = "ambiguous"
    CONTAINED = "contained"


class WorkspaceLaunchResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: int = 1
    launch_id: UUID
    request_id: UUID
    workspace_id: UUID
    target_id: UUID
    generation: int = Field(ge=0)
    extension_ordinal: int = Field(ge=0, le=63)
    state: WorkspaceLaunchState
    dispatch_accepted: bool
    script_completion: Literal["not_observed"] = "not_observed"
    ui_acceptance: Literal["not_observed"] = "not_observed"
    replayed: bool
    code: str | None = Field(max_length=64)
    next_action: Literal[
        "inspect_ui", "review_and_stop_original", "correct_request", "none"
    ]


class _LaunchBridge(Protocol):
    def binding(
        self, observed: ObserveManagedKodiRuntimeResult, kodi_version: str
    ) -> ManagedLaunchRuntimeBinding: ...
    def call(
        self,
        observed: ObserveManagedKodiRuntimeResult,
        kodi_version: str,
        intent: BridgeLaunchIntent,
        *,
        status_only: bool = False,
    ) -> BridgeLaunchReceipt | None: ...


class WorkspaceLaunchService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        development: McpWorkspaceDevelopmentService,
        bridge: _LaunchBridge,
        *,
        data_root: Path,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        failure_injector: Callable[[str], None] = lambda _phase: None,
    ) -> None:
        self._sessions, self._development, self._bridge = sessions, development, bridge
        self._data_root, self._clock, self._failure = data_root, clock, failure_injector
        self._reconciliation = ManagedInstanceReconciliationService(sessions)

    @staticmethod
    def _result(row: McpWorkspaceLaunchRow, replayed: bool) -> WorkspaceLaunchResult:
        return WorkspaceLaunchResult(
            launch_id=UUID(row.id),
            request_id=UUID(row.request_id),
            workspace_id=UUID(row.workspace_id),
            target_id=UUID(row.target_id),
            generation=row.generation,
            extension_ordinal=row.ordinal,
            state=WorkspaceLaunchState(row.state),
            dispatch_accepted=row.state == "accepted",
            replayed=replayed,
            code=row.failure_code,
            next_action="inspect_ui"
            if row.state == "accepted"
            else "review_and_stop_original"
            if row.state in {"ambiguous", "dispatching"}
            else "correct_request"
            if row.state == "failed"
            else "none",
        )

    def _row(self, operation_id: str) -> McpWorkspaceLaunchRow:
        with self._sessions() as session:
            row = session.get(McpWorkspaceLaunchRow, operation_id)
            if row is None:
                raise WorkspaceLaunchError("launch_receipt_unavailable")
            session.expunge(row)
            return row

    def _session_admission(
        self,
        session: Session,
        *,
        target_id: str,
        revision_id: str,
        workspace_id: str,
        source_id: str,
        container_id: str | None = None,
    ) -> None:
        try:
            self._development._session_admission(
                session,
                target_id=target_id,
                revision_id=revision_id,
                workspace_id=workspace_id,
                source_id=source_id,
                container_id=container_id,
            )
        except McpWorkspaceDevelopmentError as error:
            raise WorkspaceLaunchError(error.code) from error

    def _context(
        self, workspace_id: UUID, generation: int, target_id: UUID
    ) -> McpWorkspaceDeploymentOperationRow:
        with self._sessions() as session:
            workspace = session.get(AgentWorkspaceRow, str(workspace_id))
            target = session.get(TargetRow, str(target_id))
            instance = session.get(ManagedInstanceRow, str(target_id))
            if (
                target is None
                or target.target_type is not TargetType.MANAGED
                or instance is None
                or instance.instance_type is not InstanceType.DISPOSABLE
            ):
                raise WorkspaceLaunchError("launch_requires_managed_disposable")
            if (
                workspace is None
                or workspace.state is not AgentWorkspaceState.READY
                or workspace.bytes_state is not AgentWorkspaceBytesState.PRESENT
                or workspace.generation != generation
            ):
                raise WorkspaceLaunchError("workspace_generation_conflict")
            self._session_admission(
                session,
                target_id=str(target_id),
                revision_id=instance.runtime_revision_id,
                workspace_id=str(workspace_id),
                source_id=workspace.development_source_id,
            )
            if (
                session.get(
                    ManagedInstanceDevelopmentSourceRow,
                    (str(target_id), workspace.development_source_id),
                )
                is None
            ):
                raise WorkspaceLaunchError("launch_source_not_attached")
            latest = session.scalar(
                select(McpWorkspaceDeploymentOperationRow)
                .where(
                    McpWorkspaceDeploymentOperationRow.target_id == str(target_id),
                    McpWorkspaceDeploymentOperationRow.workspace_id
                    == str(workspace_id),
                    selected_profile_lineage_sql(),
                )
                .order_by(
                    McpWorkspaceDeploymentOperationRow.requested_at.desc(),
                    McpWorkspaceDeploymentOperationRow.id.desc(),
                )
                .limit(1)
            )
            if (
                latest is None
                or latest.state != "succeeded"
                or latest.target_class != "internal_lab"
                or latest.generation != generation
                or not latest.addon_enabled
                or latest.source_manifest_digest is None
                or latest.verified_destination_digest is None
                or latest.source_manifest_digest.removeprefix("sha256:")
                != (workspace.current_tree_digest or "").removeprefix("sha256:")
            ):
                raise WorkspaceLaunchError("launch_not_deployed")
            profile = RuntimeExecutionProfileRepository(session).get(
                UUID(instance.runtime_revision_id)
            )
            if (
                profile is None
                or latest.runtime_revision_id != instance.runtime_revision_id
            ):
                raise WorkspaceLaunchError("launch_not_deployed")
            try:
                # The source digest excludes ownership; Docker readback includes
                # the canonical runtime UID/GID. Prove their exact relationship
                # from fresh private bytes under the caller's workspace lock.
                manifest = capture_workspace_deployment_tree(
                    self._data_root, workspace_id
                )
            except (AgentWorkspaceStorageError, OSError):
                raise WorkspaceLaunchError("launch_not_deployed") from None
            expected_owned_digest = expected_destination_digest(
                manifest,
                runtime_uid=profile.runtime_uid,
                runtime_gid=profile.runtime_gid,
            )
            if manifest.digest.removeprefix(
                "sha256:"
            ) != latest.source_manifest_digest.removeprefix(
                "sha256:"
            ) or latest.verified_destination_digest.removeprefix(
                "sha256:"
            ) != expected_owned_digest.removeprefix("sha256:"):
                raise WorkspaceLaunchError("launch_not_deployed")
            session.expunge(latest)
            return latest

    def _verify(
        self, row: McpWorkspaceLaunchRow
    ) -> tuple[ObserveManagedKodiRuntimeResult, RuntimeExecutionProfile]:
        deployment = self._context(
            UUID(row.workspace_id), row.generation, UUID(row.target_id)
        )
        if deployment.id != row.deployment_id:
            raise WorkspaceLaunchError("launch_deployment_changed")
        observed, profile, container = self._development._runtime_context(
            UUID(row.target_id)
        )
        with self._sessions() as session:
            token = ManagedRuntimeProjectionRepository(session).observation_token(
                UUID(row.target_id)
            )
        if (
            token is None
            or str(token.runtime_revision_id) != row.runtime_revision_id
            or token.runtime_state_revision != row.runtime_state_revision
            or container != row.container_id
        ):
            raise WorkspaceLaunchError("launch_runtime_changed")
        descriptor = self._development._workspace_descriptor(UUID(row.workspace_id))
        entries = manifest_launch_entries(descriptor)
        if row.ordinal >= len(entries):
            raise WorkspaceLaunchError("launch_entry_not_declared")
        selected = entries[row.ordinal]
        if not selected.eligible:
            raise WorkspaceLaunchError("launch_" + (selected.reason or "unsupported"))
        inventory = self._development._inventory_factory(
            observed, profile
        ).get_inventory()
        recognized = next(
            (item for item in inventory.items if item.addon_id == row.addon_id), None
        )
        if (
            recognized is None
            or recognized.version != row.addon_version
            or not recognized.enabled
        ):
            raise WorkspaceLaunchError("launch_not_deployed")
        verified = self._development._broker.verify_mcp_workspace_destination(
            deployment_operation_id=row.deployment_id,
            managed_instance_id=row.target_id,
            expected_container_id=row.container_id,
        )
        if (
            not verified.matched
            or verified.addon_id != row.addon_id
            or verified.addon_version != row.addon_version
            or verified.container_id != row.container_id
            or verified.runtime_revision_id != row.runtime_revision_id
        ):
            raise WorkspaceLaunchError("launch_deployment_changed")
        self._intent(
            row
        )  # Validate the closed derived request before its dispatch marker.
        return observed, profile

    def _intent(self, row: McpWorkspaceLaunchRow) -> BridgeLaunchIntent:
        return BridgeLaunchIntent(
            launch_id=UUID(row.id),
            addon_id=row.addon_id,
            addon_version=row.addon_version,
            manifest_digest="sha256:" + row.tree_digest.removeprefix("sha256:"),
            ordinal=row.ordinal,
            runtime_binding=ManagedLaunchRuntimeBinding(
                target_id=row.target_id,
                runtime_revision_id=row.runtime_revision_id,
                container_id=row.container_id,
                generation=row.bridge_generation,
            ),
        )

    def _finish(
        self, operation_id: str, state: str, code: str | None = None
    ) -> McpWorkspaceLaunchRow:
        with self._sessions.begin() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            row = session.get(McpWorkspaceLaunchRow, operation_id)
            assert row is not None
            if row.state in {"accepted", "failed", "contained"}:
                session.expunge(row)
                return row
            row.state, row.phase, row.failure_code = state, "finalize", code
            if state == "ambiguous":
                from kodi_lab.application.live_display import cancel_active_live_display

                cancel_active_live_display(UUID(row.target_id))
            row.completed_at = timestamp(self._clock())
            ManagedInstanceMutationLeaseRepository(session).release_for_operation(
                UUID(row.target_id), UUID(row.id)
            )
        return self._row(operation_id)

    def _replay(self, row: McpWorkspaceLaunchRow) -> McpWorkspaceLaunchRow:
        if row.state not in {"pending", "dispatching", "ambiguous"}:
            return row
        if row.state == "pending":
            return self._finish(row.id, "failed", "launch_interrupted_before_dispatch")
        inventory = self._development._broker.managed_container_inventory()
        records = [
            item
            for item in inventory.containers
            if item.managed_instance_id == row.target_id
        ]
        if (
            len(records) == 1
            and records[0].container_id == row.container_id
            and records[0].runtime_state is ContainerRuntimeState.STOPPED
        ):
            return self._finish(row.id, "contained", "original_container_stopped")
        try:
            observed, profile, container = self._development._runtime_context(
                UUID(row.target_id)
            )
            if (
                container != row.container_id
                or str(profile.runtime_revision_id) != row.runtime_revision_id
            ):
                raise WorkspaceLaunchError("launch_runtime_changed")
            receipt = self._bridge.call(
                observed,
                profile.kodi_release.canonical,
                self._intent(row),
                status_only=True,
            )
            if receipt is not None and receipt.state == "accepted":
                return self._finish(row.id, "accepted")
        except Exception:
            pass
        return self._finish(row.id, "ambiguous", "launch_dispatch_unconfirmed")

    def reconcile(self, target_id: UUID, launch_id: UUID) -> WorkspaceLaunchResult:
        """Explicit Web administrator receipt recovery; never dispatch or start."""
        row = self._row(str(launch_id))
        with self._sessions() as session:
            target = session.get(TargetRow, str(target_id))
            instance = session.get(ManagedInstanceRow, str(target_id))
            if (
                row.target_id != str(target_id)
                or target is None
                or target.target_type is not TargetType.MANAGED
                or instance is None
                or instance.instance_type is not InstanceType.DISPOSABLE
            ):
                raise WorkspaceLaunchError("launch_receipt_unavailable")
        with workspace_coordination_lock(
            self._data_root, UUID(row.workspace_id), exclusive=True, blocking=False
        ):
            return self._result(self._replay(self._row(row.id)), True)

    def launch(
        self,
        *,
        client_id: UUID,
        request_id: UUID,
        workspace_id: UUID,
        expected_generation: int,
        target_id: UUID,
        extension_ordinal: int,
        authorize: Callable[[Session], None],
    ) -> WorkspaceLaunchResult:
        with self._sessions() as session:
            authorize(session)
        # The workspace lock spans claim and dispatch. Other workspaces are also
        # excluded from this target by the normal durable typed mutation lease.
        with workspace_coordination_lock(
            self._data_root, workspace_id, exclusive=True, blocking=False
        ):
            with self._sessions() as session:
                row = session.scalar(
                    select(McpWorkspaceLaunchRow).where(
                        McpWorkspaceLaunchRow.client_id == str(client_id),
                        McpWorkspaceLaunchRow.request_id == str(request_id),
                    )
                )
                if row is not None:
                    if (
                        row.workspace_id,
                        row.target_id,
                        row.generation,
                        row.ordinal,
                    ) != (
                        str(workspace_id),
                        str(target_id),
                        expected_generation,
                        extension_ordinal,
                    ):
                        raise WorkspaceLaunchError("request_id_conflict")
                    session.expunge(row)
            if row is not None:
                result = self._replay(row)
                with self._sessions() as session:
                    authorize(session)
                return self._result(result, True)
            if self._reconciliation.status(target_id).required:
                raise WorkspaceLaunchError("target_reconciliation_required")
            deployment = self._context(workspace_id, expected_generation, target_id)
            observed, profile, container = self._development._runtime_context(target_id)
            binding = self._bridge.binding(observed, profile.kodi_release.canonical)
            with self._sessions.begin() as session:
                session.execute(text("BEGIN IMMEDIATE"))
                authorize(session)
                self._session_admission(
                    session,
                    target_id=str(target_id),
                    revision_id=observed.runtime_revision_id,
                    workspace_id=str(workspace_id),
                    source_id=deployment.development_source_id,
                    container_id=container,
                )
                total = (
                    session.scalar(
                        select(func.count()).select_from(McpWorkspaceLaunchRow)
                    )
                    or 0
                )
                owned = (
                    session.scalar(
                        select(func.count())
                        .select_from(McpWorkspaceLaunchRow)
                        .where(McpWorkspaceLaunchRow.client_id == str(client_id))
                    )
                    or 0
                )
                if total >= 32768 or owned >= 1024:
                    raise WorkspaceLaunchError("launch_receipt_capacity")
                recent = session.scalar(
                    select(McpWorkspaceLaunchRow.id)
                    .where(
                        McpWorkspaceLaunchRow.client_id == str(client_id),
                        McpWorkspaceLaunchRow.target_id == str(target_id),
                        McpWorkspaceLaunchRow.requested_at
                        > timestamp(self._clock() - timedelta(seconds=2)),
                    )
                    .limit(1)
                )
                if recent is not None:
                    raise WorkspaceLaunchError("rate_limited")
                token = ManagedRuntimeProjectionRepository(session).observation_token(
                    target_id
                )
                if (
                    token is None
                    or str(token.runtime_revision_id) != observed.runtime_revision_id
                ):
                    raise WorkspaceLaunchError("launch_runtime_changed")
                row = McpWorkspaceLaunchRow(
                    id=str(uuid4()),
                    client_id=str(client_id),
                    request_id=str(request_id),
                    workspace_id=str(workspace_id),
                    development_source_id=deployment.development_source_id,
                    target_id=str(target_id),
                    deployment_id=deployment.id,
                    generation=expected_generation,
                    ordinal=extension_ordinal,
                    addon_id=deployment.addon_id,
                    addon_version=deployment.addon_version,
                    tree_digest=deployment.source_manifest_digest or "",
                    runtime_revision_id=observed.runtime_revision_id,
                    runtime_state_revision=token.runtime_state_revision,
                    container_id=container,
                    bridge_generation=binding.generation,
                    state="pending",
                    phase="claimed",
                    requested_at=timestamp(self._clock()),
                )
                session.add(row)
                leases = ManagedInstanceMutationLeaseRepository(session)
                leases.reserve(
                    target_id,
                    kind=ManagedInstanceMutationKind.MCP_WORKSPACE_LAUNCH,
                    operation_id=UUID(row.id),
                )
                digest = hashlib.sha256(secrets.token_bytes(32)).hexdigest()
                if not leases.claim(
                    target_id,
                    operation_id=UUID(row.id),
                    token_digest=digest,
                    now=self._clock(),
                    expires_at=self._clock() + timedelta(minutes=15),
                ):
                    raise WorkspaceLaunchError("launch_conflict")
                operation_id = row.id
            self._failure("claimed")
            try:
                observed, profile = self._verify(self._row(operation_id))
                with self._sessions.begin() as session:
                    session.execute(text("BEGIN IMMEDIATE"))
                    authorize(session)
                    current = session.get(McpWorkspaceLaunchRow, operation_id)
                    assert current is not None
                    token = ManagedRuntimeProjectionRepository(
                        session
                    ).observation_token(target_id)
                    lease = ManagedInstanceMutationLeaseRepository(session).current(
                        target_id
                    )
                    if (
                        token is None
                        or token.runtime_state_revision
                        != current.runtime_state_revision
                        or lease is None
                        or lease.operation_id != operation_id
                        or lease.lease_token_digest != digest
                        or lease.lease_expires_at is None
                        or lease.lease_expires_at <= timestamp(self._clock())
                    ):
                        raise WorkspaceLaunchError("launch_runtime_changed")
                    self._session_admission(
                        session,
                        target_id=current.target_id,
                        revision_id=current.runtime_revision_id,
                        workspace_id=current.workspace_id,
                        source_id=current.development_source_id,
                        container_id=current.container_id,
                    )
                    current.state, current.phase = "dispatching", "dispatch"
                    current.started_at = timestamp(self._clock())
                    record_activity(
                        session, target_id, actor=f"mcp:{client_id}", now=self._clock()
                    )
                self._failure("dispatch_marker")
                receipt = self._bridge.call(
                    observed,
                    profile.kodi_release.canonical,
                    self._intent(self._row(operation_id)),
                )
                if receipt is None or receipt.state != "accepted":
                    raise WorkspaceLaunchError("launch_dispatch_unconfirmed")
                self._failure("accepted_response")
                result = self._finish(operation_id, "accepted")
            except Exception as error:
                current = self._row(operation_id)
                dispatched = current.state == "dispatching" and not (
                    isinstance(error, ExternalBridgeError)
                    and error.code == "launch_request_rejected"
                )
                result = self._finish(
                    operation_id,
                    "ambiguous" if dispatched else "failed",
                    "launch_dispatch_unconfirmed"
                    if dispatched
                    else error.code
                    if isinstance(
                        error,
                        (
                            WorkspaceLaunchError,
                            McpWorkspaceDevelopmentError,
                            ExternalBridgeError,
                        ),
                    )
                    else "launch_preflight_failed",
                )
            with self._sessions() as session:
                authorize(session)
            return self._result(result, False)
