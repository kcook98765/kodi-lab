"""Explicit, bounded native observations; never dispatch a Kodi mutation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from threading import BoundedSemaphore
from typing import Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.create_kodi_registration import (
    registration_matches,
    require_create_runtime,
)
from kodi_lab.application.reconciliation import ManagedInstanceReconciliationService
from kodi_lab.application.runtime_installations import RuntimeInstallationService
from kodi_lab.broker.operations import (
    ManagedContainerInventoryResult,
    ObserveManagedKodiRuntimeResult,
)
from kodi_lab.domain import (
    ManagedInstanceLifecycleOperationState,
    RuntimeExecutionProfile,
)
from kodi_lab.domain.create_kodi import (
    CreateCheckpoint,
    CreateOperation,
    CreateStage,
    CreateTransitionError,
)
from kodi_lab.kodi.readiness import (
    JsonRpcState,
    KodiReadinessCoordinator,
    ReadinessResult,
    UsabilityState,
)
from kodi_lab.storage.create_kodi import CreateKodiRepository
from kodi_lab.storage.create_kodi_start import require_start_receipt
from kodi_lab.storage.managed_activity import read_resource_policy
from kodi_lab.storage.models import (
    GlobalHeavyOperationLeaseRow,
    ManagedAgentLifecycleRequestRow,
    ManagedInstanceLifecycleOperationRow,
    ManagedInstanceMutationLeaseRow,
    ManagedInstanceRow,
)
from kodi_lab.storage.repositories import RuntimeExecutionProfileRepository
from kodi_lab.storage.retained_development import (
    require_runtime_reservations_idle,
    retained_inventory,
    retained_targets,
)

# Match the existing readiness registry's per-process bound. Synchronous probes
# hold no SQLite writer or lifecycle lease while waiting for native JSON-RPC.
_PROBES = BoundedSemaphore(4)


class _NativeBroker(Protocol):
    def managed_container_inventory(self) -> ManagedContainerInventoryResult: ...

    def observe_managed_kodi_runtime(
        self, *, managed_instance_id: str, expected_container_id: str
    ) -> ObserveManagedKodiRuntimeResult: ...


class _NativeCoordinator(Protocol):
    def wait_until_usable(
        self, managed_instance_id: str, *, cancelled: Callable[[], bool] | None = None
    ) -> ReadinessResult: ...


type CoordinatorFactory = Callable[
    [_NativeBroker, RuntimeExecutionProfile], _NativeCoordinator
]


@dataclass(frozen=True)
class CreateReadinessInspection:
    operation: CreateOperation
    result: ReadinessResult | None


class _ExactNativeBroker:
    """The coordinator sees only observations fenced to the original container."""

    def __init__(
        self, service: CreateKodiReadinessService, op: CreateOperation
    ) -> None:
        self._service, self._op = service, op

    def managed_container_inventory(self) -> ManagedContainerInventoryResult:
        return self._service._context(self._op)[1]

    def observe_managed_kodi_runtime(
        self, *, managed_instance_id: str, expected_container_id: str
    ) -> ObserveManagedKodiRuntimeResult:
        if (managed_instance_id, expected_container_id) != (
            str(self._op.target_id),
            self._op.container_id,
        ):
            raise CreateTransitionError("readiness_observation_target_changed")
        self._service._context(self._op)
        observed = self._service._broker.observe_managed_kodi_runtime(
            managed_instance_id=managed_instance_id,
            expected_container_id=expected_container_id,
        )
        if (
            observed.managed_instance_id,
            observed.container_id,
            observed.runtime_revision_id,
            observed.runtime_state,
        ) != (
            managed_instance_id,
            self._op.container_id,
            str(self._op.runtime_revision_id),
            "running",
        ):
            raise CreateTransitionError("readiness_observation_identity_changed")
        return observed


class CreateKodiReadinessService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        repository: CreateKodiRepository,
        installer: RuntimeInstallationService,
        broker: _NativeBroker,
        *,
        coordinator_factory: CoordinatorFactory = KodiReadinessCoordinator,
    ) -> None:
        self._sessions, self._repository = sessions, repository
        self._installer, self._broker = installer, broker
        self._coordinator_factory = coordinator_factory
        self._reconciliation = ManagedInstanceReconciliationService(sessions)

    def _verify(
        self, session: Session, op: CreateOperation
    ) -> tuple[RuntimeExecutionProfile, ManagedContainerInventoryResult]:
        require_start_receipt(session, op)
        reviewed = require_create_runtime(session, op, self._installer)
        if not registration_matches(session, op):
            raise CreateTransitionError("canonical_registration_missing")
        instance = session.get(ManagedInstanceRow, str(op.target_id))
        if (
            instance is None
            or instance.runtime_state_revision != op.runtime_state_revision
            or instance.runtime_state != "running"
        ):
            raise CreateTransitionError("readiness_projection_changed")
        require_runtime_reservations_idle(session, str(op.target_id))
        if (
            not retained_targets(session)
            and read_resource_policy(session).max_running != 1
        ):
            raise CreateTransitionError("create_readiness_requires_one_active_policy")
        active = session.scalar(
            select(ManagedInstanceLifecycleOperationRow.id).where(
                ManagedInstanceLifecycleOperationRow.original_managed_instance_id
                == str(op.target_id),
                ManagedInstanceLifecycleOperationRow.state
                == ManagedInstanceLifecycleOperationState.RUNNING,
            )
        )
        agent = session.scalar(
            select(ManagedAgentLifecycleRequestRow.id).where(
                ManagedAgentLifecycleRequestRow.original_instance_id
                == str(op.target_id),
                ManagedAgentLifecycleRequestRow.state.in_(
                    ("pending", "dispatched", "ambiguous")
                ),
            )
        )
        if (
            session.get(ManagedInstanceMutationLeaseRow, str(op.target_id)) is not None
            or session.get(GlobalHeavyOperationLeaseRow, 1) is not None
            or active is not None
            or agent is not None
            or self._reconciliation.status(op.target_id, read_session=session).required
        ):
            raise CreateTransitionError("readiness_recovery_or_mutation_pending")
        assert op.runtime_revision_id is not None
        profile = RuntimeExecutionProfileRepository(session).get(op.runtime_revision_id)
        if profile != RuntimeExecutionProfile(
            runtime_revision_id=op.runtime_revision_id,
            **reviewed.recipe.execution_profile.model_dump(),
        ):
            raise CreateTransitionError("readiness_profile_changed")
        assert profile is not None
        inventory = self._broker.managed_container_inventory()
        if retained_targets(session):
            retained_inventory(session, inventory.containers)
        own = [
            r
            for r in inventory.containers
            if r.managed_instance_id == str(op.target_id)
        ]
        if (
            len(own) != 1
            or (
                own[0].container_id,
                own[0].runtime_revision_id,
                own[0].image_id,
                own[0].runtime_state,
            )
            != (op.container_id, str(op.runtime_revision_id), op.image_id, "running")
            or any(
                r.runtime_state != "stopped"
                for r in inventory.containers
                if r.managed_instance_id != str(op.target_id)
            )
        ):
            raise CreateTransitionError("readiness_inventory_changed")
        return profile, inventory

    def _admit(self, session: Session, op: CreateOperation) -> None:
        self._verify(session, op)

    def _context(
        self, op: CreateOperation
    ) -> tuple[RuntimeExecutionProfile, ManagedContainerInventoryResult]:
        # Passive reads never call this path; it belongs to the explicit probe.
        current = self._repository.get(op.id)
        if current != op:
            raise CreateTransitionError("stale_readiness_probe")
        with self._sessions() as session:
            return self._verify(session, op)

    def advance(self, operation_id: UUID, *, version: int) -> CreateOperation:
        return self._repository.prepare_readiness(
            operation_id, version=version, verify=self._admit
        )

    def inspect(self, operation_id: UUID, *, version: int) -> CreateReadinessInspection:
        op = self._repository.get(operation_id)
        if op is None or op.version != version or op.stage is not CreateStage.READINESS:
            raise CreateTransitionError("stale_readiness_context")
        if op.checkpoint is CreateCheckpoint.COMPLETE:
            return CreateReadinessInspection(op, None)
        if not _PROBES.acquire(blocking=False):
            raise CreateTransitionError("native_readiness_busy")
        try:
            if op.checkpoint is CreateCheckpoint.ACCEPTED:
                op = self._repository.plan(op.id, version=op.version)
            op = self._repository.claim_readiness_probe(
                op.id, version=op.version, verify=self._admit
            )
            profile, _ = self._context(op)
            result = self._coordinator_factory(
                _ExactNativeBroker(self, op), profile
            ).wait_until_usable(
                str(op.target_id),
                cancelled=lambda: self._repository.get(op.id) != op,
            )
            if result.usability_state is not UsabilityState.USABLE:
                # A timeout/protocol error is an observation, never a mutation
                # failure. Keep the same stage/key available for explicit recheck.
                return CreateReadinessInspection(op, result)
            if (
                result.container_state != "running"
                or result.process_state != "running"
                or result.jsonrpc_state is not JsonRpcState.RESPONSIVE
                or result.failure_reason is not None
                or result.last_observed_failure is not None
                or result.identity is None
                or result.identity.name != profile.expected_application_name
                or result.identity.kodi_release != profile.kodi_release
            ):
                raise CreateTransitionError("native_readiness_result_invalid")
            completed = self._repository.complete_readiness(
                op.id, version=op.version, verify=self._admit
            )
            return CreateReadinessInspection(completed, result)
        finally:
            _PROBES.release()
