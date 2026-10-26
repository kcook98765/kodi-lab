"""One selected managed workspace, one durable typed settings-write dispatch."""

from __future__ import annotations

import hashlib
import json
import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol, cast
from uuid import UUID, uuid4

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.mcp_workspace_development import (
    McpWorkspaceDevelopmentService,
)
from kodi_lab.application.reconciliation import ManagedInstanceReconciliationService
from kodi_lab.application.workspace_launch import WorkspaceLaunchService
from kodi_lab.broker.docker_inventory import ContainerRuntimeState
from kodi_lab.broker.operations import ObserveManagedKodiRuntimeResult
from kodi_lab.domain import ManagedInstanceMutationKind, RuntimeExecutionProfile
from kodi_lab.kodi.addon_settings import (
    AddonSettingOutcome,
    AddonSettingsSetInput,
    AddonSettingsSetResult,
)
from kodi_lab.kodi.launch import ManagedLaunchRuntimeBinding
from kodi_lab.storage.development_sessions import (
    active_session,
    require_workspace_session_admission,
)
from kodi_lab.storage.managed_activity import timestamp
from kodi_lab.storage.models import McpAddonSettingsOperationRow
from kodi_lab.storage.repositories import (
    ManagedInstanceMutationLeaseRepository,
    ManagedRuntimeProjectionRepository,
)
from kodi_lab.workspace import workspace_coordination_lock


class AddonSettingsError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class SettingsBridge(Protocol):
    def settings_binding(
        self, observed: ObserveManagedKodiRuntimeResult, kodi_version: str
    ) -> ManagedLaunchRuntimeBinding: ...
    def settings(
        self,
        observed: ObserveManagedKodiRuntimeResult,
        kodi_version: str,
        payload: dict[str, object],
    ) -> tuple[AddonSettingOutcome, ...]: ...


class AddonSettingsService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        development: McpWorkspaceDevelopmentService,
        launch: WorkspaceLaunchService,
        bridge: SettingsBridge,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sessions, self._development, self._launch, self._bridge = (
            sessions,
            development,
            launch,
            bridge,
        )
        self._clock = clock
        self._reconciliation = ManagedInstanceReconciliationService(sessions)

    @staticmethod
    def _result(
        row: McpAddonSettingsOperationRow, *, replayed: bool = False
    ) -> AddonSettingsSetResult:
        return AddonSettingsSetResult(
            operation_id=UUID(row.id),
            request_id=UUID(row.request_id),
            workspace_id=UUID(row.workspace_id),
            target_id=UUID(row.target_id),
            session_generation=row.session_generation,
            generation=row.generation,
            state=cast(
                Literal[
                    "pending",
                    "dispatching",
                    "completed",
                    "failed",
                    "ambiguous",
                    "contained",
                ],
                row.state,
            ),
            replayed=replayed,
            code=row.failure_code,
            results=tuple(
                AddonSettingOutcome.model_validate(v)
                for v in json.loads(row.results_json or "[]")
            ),
        )

    def _row(self, operation_id: str) -> McpAddonSettingsOperationRow:
        with self._sessions() as session:
            row = session.get(McpAddonSettingsOperationRow, operation_id)
            if row is None:
                raise AddonSettingsError("settings_receipt_unavailable")
            session.expunge(row)
            return row

    def _selection(self, session: Session, expected: int) -> tuple[UUID, UUID, int]:
        current = active_session(session)
        if (
            current is None
            or current.phase != "bound"
            or current.generation != expected
            or current.workspace_id is None
        ):
            raise AddonSettingsError("settings_stale_session")
        from kodi_lab.storage.models import AgentWorkspaceRow

        workspace = session.get(AgentWorkspaceRow, current.workspace_id)
        if workspace is None:
            raise AddonSettingsError("settings_workspace_unavailable")
        return UUID(current.workspace_id), UUID(current.target_id), workspace.generation

    def _verify(
        self,
        row: McpAddonSettingsOperationRow,
        authorize: Callable[[Session, UUID, UUID], None],
    ) -> tuple[ObserveManagedKodiRuntimeResult, RuntimeExecutionProfile]:
        workspace, target = UUID(row.workspace_id), UUID(row.target_id)
        with self._sessions() as session:
            authorize(session, workspace, target)
            if self._selection(session, row.session_generation) != (
                workspace,
                target,
                row.generation,
            ):
                raise AddonSettingsError("settings_stale_session")
            require_workspace_session_admission(
                session,
                target_id=row.target_id,
                revision_id=row.runtime_revision_id,
                workspace_id=row.workspace_id,
                source_id=row.development_source_id,
                container_id=row.container_id,
            )
        deployment = self._launch._context(workspace, row.generation, target)
        if deployment.id != row.deployment_id:
            raise AddonSettingsError("settings_deployment_changed")
        observed, profile, container = self._development._runtime_context(target)
        with self._sessions() as session:
            token = ManagedRuntimeProjectionRepository(session).observation_token(
                target
            )
        binding = self._bridge.settings_binding(
            observed, profile.kodi_release.canonical
        )
        if (
            token is None
            or token.runtime_state_revision != row.runtime_state_revision
            or str(token.runtime_revision_id) != row.runtime_revision_id
            or container != row.container_id
            or binding.generation != row.bridge_generation
        ):
            raise AddonSettingsError("settings_stale_target")
        inventory = self._development._inventory_factory(
            observed, profile
        ).get_inventory()
        if not any(
            item.addon_id == row.addon_id
            and item.version == row.addon_version
            and item.enabled
            for item in inventory.items
        ):
            raise AddonSettingsError("settings_addon_unavailable")
        verified = self._development._broker.verify_mcp_workspace_destination(
            deployment_operation_id=row.deployment_id,
            managed_instance_id=row.target_id,
            expected_container_id=row.container_id,
        )
        if not verified.matched or (
            verified.addon_id,
            verified.addon_version,
            verified.container_id,
            verified.runtime_revision_id,
        ) != (
            row.addon_id,
            row.addon_version,
            row.container_id,
            row.runtime_revision_id,
        ):
            raise AddonSettingsError("settings_deployment_changed")
        return observed, profile

    def _owns(
        self,
        session: Session,
        row: McpAddonSettingsOperationRow,
        digest: str,
        state: str,
    ) -> bool:
        lease = ManagedInstanceMutationLeaseRepository(session).current(
            UUID(row.target_id)
        )
        return (
            row.state == state
            and lease is not None
            and lease.operation_kind is ManagedInstanceMutationKind.MCP_ADDON_SETTINGS
            and lease.operation_id == row.id
            and lease.lease_token_digest == digest
            and lease.lease_expires_at is not None
            and lease.lease_expires_at > timestamp(self._clock())
        )

    def set(
        self,
        *,
        client_id: UUID,
        request: AddonSettingsSetInput,
        authorize: Callable[[Session, UUID, UUID], None],
    ) -> AddonSettingsSetResult:
        with self._sessions() as session:
            workspace, target, generation = self._selection(
                session, request.expected_session_generation
            )
            authorize(session, workspace, target)
            if generation != request.expected_workspace_generation:
                raise AddonSettingsError("workspace_generation_conflict")
        with workspace_coordination_lock(
            self._launch._data_root, workspace, exclusive=True, blocking=False
        ):
            with self._sessions() as session:
                prior = session.scalar(
                    select(McpAddonSettingsOperationRow).where(
                        McpAddonSettingsOperationRow.client_id == str(client_id),
                        McpAddonSettingsOperationRow.request_id
                        == str(request.request_id),
                    )
                )
                if prior is not None:
                    if (
                        prior.session_generation,
                        prior.workspace_id,
                        prior.target_id,
                        prior.generation,
                    ) != (
                        request.expected_session_generation,
                        str(workspace),
                        str(target),
                        generation,
                    ):
                        raise AddonSettingsError("settings_stale_session")
                    # Request IDs are single use. Never compare/store raw values,
                    # and never claim a changed payload was applied on replay.
                    raise AddonSettingsError("settings_request_already_used")
            if self._reconciliation.status(target).required:
                raise AddonSettingsError("target_reconciliation_required")
            deployment = self._launch._context(workspace, generation, target)
            observed, profile, container = self._development._runtime_context(target)
            if profile.kodi_release.canonical not in {"20.5", "21.3"}:
                raise AddonSettingsError("settings_kodi_unsupported")
            binding = self._bridge.settings_binding(
                observed, profile.kodi_release.canonical
            )
            with self._sessions.begin() as session:
                session.execute(text("BEGIN IMMEDIATE"))
                authorize(session, workspace, target)
                if self._selection(session, request.expected_session_generation) != (
                    workspace,
                    target,
                    generation,
                ):
                    raise AddonSettingsError("settings_stale_session")
                require_workspace_session_admission(
                    session,
                    target_id=str(target),
                    revision_id=observed.runtime_revision_id,
                    workspace_id=str(workspace),
                    source_id=deployment.development_source_id,
                    container_id=container,
                )
                if (
                    session.scalar(
                        select(func.count()).select_from(McpAddonSettingsOperationRow)
                    )
                    or 0
                ) >= 32768:
                    raise AddonSettingsError("settings_receipt_capacity")
                if (
                    session.scalar(
                        select(func.count())
                        .select_from(McpAddonSettingsOperationRow)
                        .where(McpAddonSettingsOperationRow.client_id == str(client_id))
                    )
                    or 0
                ) >= 1024:
                    raise AddonSettingsError("settings_receipt_capacity")
                if (
                    session.scalar(
                        select(McpAddonSettingsOperationRow.id)
                        .where(
                            McpAddonSettingsOperationRow.client_id == str(client_id),
                            McpAddonSettingsOperationRow.target_id == str(target),
                            McpAddonSettingsOperationRow.requested_at
                            > timestamp(self._clock() - timedelta(seconds=2)),
                        )
                        .limit(1)
                    )
                    is not None
                ):
                    raise AddonSettingsError("rate_limited")
                token = ManagedRuntimeProjectionRepository(session).observation_token(
                    target
                )
                if (
                    token is None
                    or str(token.runtime_revision_id) != observed.runtime_revision_id
                ):
                    raise AddonSettingsError("settings_stale_target")
                row = McpAddonSettingsOperationRow(
                    id=str(uuid4()),
                    client_id=str(client_id),
                    request_id=str(request.request_id),
                    workspace_id=str(workspace),
                    development_source_id=deployment.development_source_id,
                    target_id=str(target),
                    deployment_id=deployment.id,
                    runtime_revision_id=observed.runtime_revision_id,
                    container_id=container,
                    addon_id=deployment.addon_id,
                    addon_version=deployment.addon_version,
                    tree_digest=deployment.source_manifest_digest or "",
                    generation=generation,
                    session_generation=request.expected_session_generation,
                    runtime_state_revision=token.runtime_state_revision,
                    bridge_generation=binding.generation,
                    state="pending",
                    requested_at=timestamp(self._clock()),
                )
                session.add(row)
                leases = ManagedInstanceMutationLeaseRepository(session)
                leases.reserve(
                    target,
                    kind=ManagedInstanceMutationKind.MCP_ADDON_SETTINGS,
                    operation_id=UUID(row.id),
                )
                digest = hashlib.sha256(secrets.token_bytes(32)).hexdigest()
                if not leases.claim(
                    target,
                    operation_id=UUID(row.id),
                    token_digest=digest,
                    now=self._clock(),
                    expires_at=self._clock() + timedelta(minutes=15),
                ):
                    raise AddonSettingsError("settings_conflict")
                operation_id = row.id
            dispatched = False
            try:
                row = self._row(operation_id)
                observed, profile = self._verify(row, authorize)
                with self._sessions.begin() as session:
                    session.execute(text("BEGIN IMMEDIATE"))
                    authorize(session, workspace, target)
                    current = session.get(McpAddonSettingsOperationRow, operation_id)
                    lease = ManagedInstanceMutationLeaseRepository(session).current(
                        target
                    )
                    if (
                        current is None
                        or current.state != "pending"
                        or lease is None
                        or lease.operation_id != operation_id
                        or lease.lease_token_digest != digest
                        or lease.lease_expires_at is None
                        or lease.lease_expires_at <= timestamp(self._clock())
                    ):
                        raise AddonSettingsError("settings_conflict")
                    if self._selection(session, row.session_generation) != (
                        workspace,
                        target,
                        generation,
                    ):
                        raise AddonSettingsError("settings_stale_session")
                    token = ManagedRuntimeProjectionRepository(
                        session
                    ).observation_token(target)
                    if (
                        token is None
                        or token.runtime_state_revision != row.runtime_state_revision
                        or str(token.runtime_revision_id) != row.runtime_revision_id
                    ):
                        raise AddonSettingsError("settings_stale_target")
                    dispatch_deadline = min(
                        lease.lease_expires_at,
                        timestamp(self._clock() + timedelta(seconds=10)),
                    )
                    current.state = "dispatching"
                dispatched = True
                results = self._bridge.settings(
                    observed,
                    profile.kodi_release.canonical,
                    {
                        "operation_id": operation_id,
                        "not_after": dispatch_deadline,
                        "addon_id": row.addon_id,
                        "addon_version": row.addon_version,
                        "manifest_digest": row.tree_digest,
                        "runtime_binding": {
                            "target_id": row.target_id,
                            "runtime_revision_id": row.runtime_revision_id,
                            "container_id": row.container_id,
                            "generation": row.bridge_generation,
                        },
                        "settings": [item.model_dump() for item in request.settings],
                    },
                )
                if tuple(v.id for v in results) != tuple(
                    v.id for v in request.settings
                ):
                    raise AddonSettingsError("settings_response_invalid")
                with self._sessions.begin() as session:
                    current = session.get(McpAddonSettingsOperationRow, operation_id)
                    assert current is not None
                    if not self._owns(session, current, digest, "dispatching"):
                        raise AddonSettingsError("settings_ownership_lost")
                    current.results_json = json.dumps(
                        [v.model_dump() for v in results], separators=(",", ":")
                    )
                self._verify(row, authorize)
                with self._sessions.begin() as session:
                    authorize(session, workspace, target)
                    current = session.get(McpAddonSettingsOperationRow, operation_id)
                    assert current is not None
                    if not self._owns(session, current, digest, "dispatching"):
                        raise AddonSettingsError("settings_ownership_lost")
                    current.state, current.completed_at = (
                        "ambiguous"
                        if any(v.status == "write_unconfirmed" for v in results)
                        else "completed",
                        timestamp(self._clock()),
                    )
                    current.failure_code = (
                        "settings_write_unconfirmed"
                        if current.state == "ambiguous"
                        else None
                    )
                    current.results_json = json.dumps(
                        [v.model_dump() for v in results], separators=(",", ":")
                    )
                    if not ManagedInstanceMutationLeaseRepository(session).release(
                        target, operation_id=UUID(operation_id), token_digest=digest
                    ):
                        raise AddonSettingsError("settings_ownership_lost")
                return self._result(self._row(operation_id))
            except Exception:
                with self._sessions.begin() as session:
                    current = session.get(McpAddonSettingsOperationRow, operation_id)
                    assert current is not None
                    if self._owns(
                        session,
                        current,
                        digest,
                        "dispatching" if dispatched else "pending",
                    ):
                        current.state = "ambiguous" if dispatched else "failed"
                        current.failure_code = (
                            "settings_write_unconfirmed"
                            if dispatched
                            else "settings_prerequisite_changed"
                        )
                        current.completed_at = timestamp(self._clock())
                        if not ManagedInstanceMutationLeaseRepository(session).release(
                            target, operation_id=UUID(operation_id), token_digest=digest
                        ):
                            raise AddonSettingsError(
                                "settings_ownership_lost"
                            ) from None
                # Sweeping is the existing typed crash classifier, never a retry
                # or success inference. Preserve any prior containment decision.
                from kodi_lab.application.reconciliation import (
                    StaleOperationReconciliationDispatcher,
                )

                StaleOperationReconciliationDispatcher(
                    self._sessions, clock=self._clock
                ).sweep_instance(target)
                with self._sessions() as session:
                    authorize(session, workspace, target)
                return self._result(self._row(operation_id))

    def result(
        self,
        *,
        client_id: UUID,
        request_id: UUID,
        authorize: Callable[[Session, UUID, UUID], None],
    ) -> AddonSettingsSetResult:
        """Read only this client's original receipt on the still-selected profile.

        This does not read current setting values or resolve uncertainty. It is
        safe after response loss: no setter, native request, lease or retry.
        """
        with self._sessions() as session:
            row = session.scalar(
                select(McpAddonSettingsOperationRow).where(
                    McpAddonSettingsOperationRow.client_id == str(client_id),
                    McpAddonSettingsOperationRow.request_id == str(request_id),
                )
            )
            if row is None:
                raise AddonSettingsError("settings_request_not_found")
            workspace, target, _generation = self._selection(
                session, row.session_generation
            )
            if (str(workspace), str(target)) != (row.workspace_id, row.target_id):
                raise AddonSettingsError("settings_stale_session")
            current = active_session(session)
            if (
                current is None
                or current.container_id != row.container_id
                or current.runtime_revision_id != row.runtime_revision_id
                or current.source_id != row.development_source_id
            ):
                raise AddonSettingsError("settings_stale_target")
            authorize(session, workspace, target)
            return self._result(row, replayed=True)

    def contain(self, target_id: UUID, operation_id: UUID) -> AddonSettingsSetResult:
        """Web-admin-only exact stopped-original containment, never a retry."""
        row = self._row(str(operation_id))
        if row.target_id != str(target_id) or row.state != "ambiguous":
            raise AddonSettingsError("settings_receipt_unavailable")
        records = [
            v
            for v in self._development._broker.managed_container_inventory().containers
            if v.managed_instance_id == str(target_id)
        ]
        if (
            len(records) != 1
            or records[0].container_id != row.container_id
            or records[0].runtime_state is not ContainerRuntimeState.STOPPED
        ):
            raise AddonSettingsError("settings_original_not_stopped")
        with self._sessions.begin() as session:
            current = session.get(McpAddonSettingsOperationRow, row.id)
            assert current is not None
            current.state = "contained"
            current.failure_code = "settings_original_stopped"
        return self._result(self._row(row.id))
