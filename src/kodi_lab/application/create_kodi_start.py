"""Explicit exact managed Start only. No readiness confirmation or stage runner."""

from collections.abc import Callable
from uuid import UUID

from sqlalchemy.orm import Session

from kodi_lab.application.create_kodi_registration import (
    registration_matches,
    require_create_runtime,
)
from kodi_lab.application.lifecycle_operations import RunningLifecycleOperation
from kodi_lab.application.runtime_installations import RuntimeInstallationService
from kodi_lab.application.runtime_lifecycle import ManagedRuntimeLifecycleService
from kodi_lab.broker.operations import (
    ManagedContainerInventoryResult,
    StartManagedContainerResult,
)
from kodi_lab.domain import (
    ManagedInstanceLifecycleOperation,
    ManagedInstanceLifecycleOperationState,
)
from kodi_lab.domain.create_kodi import (
    CreateCheckpoint,
    CreateOperation,
    CreateStage,
    CreateTransitionError,
)
from kodi_lab.storage.create_kodi import CreateKodiRepository
from kodi_lab.storage.create_kodi_start import (
    capture_start,
    claim_start,
    finish_start,
    planned_start,
    require_provision_receipt,
)
from kodi_lab.storage.managed_activity import read_resource_policy
from kodi_lab.storage.managed_start import ManagedStartBinding
from kodi_lab.storage.models import ManagedInstanceRow
from kodi_lab.storage.retained_development import retained_targets


class CreateKodiStartService:
    def __init__(
        self,
        repository: CreateKodiRepository,
        installer: RuntimeInstallationService,
        lifecycle: ManagedRuntimeLifecycleService,
        inventory: Callable[[], ManagedContainerInventoryResult],
    ) -> None:
        self._repository, self._installer = repository, installer
        self._lifecycle, self._inventory = lifecycle, inventory

    def _binding(self, session: Session, op: CreateOperation) -> None:
        require_provision_receipt(session, op)
        require_create_runtime(session, op, self._installer)
        if not registration_matches(session, op):
            raise CreateTransitionError("canonical_registration_missing")

    def _container(self, op: CreateOperation, state: str) -> None:
        records = self._inventory().containers
        own = [r for r in records if r.managed_instance_id == str(op.target_id)]
        if (
            len(own) != 1
            or (
                own[0].container_id,
                own[0].runtime_revision_id,
                own[0].image_id,
                own[0].runtime_state,
            )
            != (op.container_id, str(op.runtime_revision_id), op.image_id, state)
            or any(
                r.runtime_state != "stopped"
                for r in records
                if r.managed_instance_id != str(op.target_id)
            )
        ):
            raise CreateTransitionError("start_inventory_changed")

    def _stopped(self, session: Session, op: CreateOperation) -> None:
        self._binding(session, op)
        instance = session.get(ManagedInstanceRow, str(op.target_id))
        if (
            instance is None
            or instance.runtime_state_revision != op.runtime_state_revision
        ):
            raise CreateTransitionError("start_projection_changed")
        # Do not silently change legacy multi-runtime policy or pretend the
        # broker can atomically enforce a stricter limit than its own policy.
        if (
            not retained_targets(session)
            and read_resource_policy(session).max_running != 1
        ):
            raise CreateTransitionError("create_start_requires_one_active_policy")
        self._container(op, "stopped")

    def advance(self, operation_id: UUID, *, version: int) -> CreateOperation:
        return self._repository.prepare_startup(
            operation_id, version=version, verify=self._stopped
        )

    def start(self, operation_id: UUID, *, version: int) -> CreateOperation:
        found = self._repository.get(operation_id)
        if (
            found is None
            or found.version != version
            or found.stage is not CreateStage.STARTUP
        ):
            raise CreateTransitionError("stale_start_context")
        current: CreateOperation = found
        if current.checkpoint not in {
            CreateCheckpoint.ACCEPTED,
            CreateCheckpoint.PLANNED,
        }:
            return current
        if current.checkpoint is CreateCheckpoint.ACCEPTED:
            current = self._repository.plan(current.id, version=current.version)
        planned = current
        assert planned.start_operation_id and planned.container_id
        if self._repository.has_start_evidence(planned.id):
            raise CreateTransitionError("start_child_already_recorded")
        claim_digest: str | None = None

        def admission(session: Session) -> None:
            self._stopped(session, planned_start(session, planned.id, planned.version))

        def authorized(session: Session, handle: RunningLifecycleOperation) -> None:
            nonlocal current, claim_digest
            current = claim_start(
                session,
                planned.id,
                version=planned.version,
                child_id=handle.operation_id,
                token_digest=handle.token_digest,
                owned_revision=handle.runtime_state_revision,
            )
            claim_digest = handle.token_digest

        def captured(
            session: Session,
            handle: RunningLifecycleOperation,
            result: StartManagedContainerResult,
        ) -> None:
            nonlocal current
            self._binding(session, current)
            if (
                result.managed_instance_id,
                result.container_id,
                result.runtime_state,
                result.start_performed,
            ) != (str(current.target_id), current.container_id, "running", True):
                raise CreateTransitionError("start_reply_changed")
            self._container(current, "running")
            current = capture_start(
                session,
                current.id,
                version=current.version,
                child_id=handle.operation_id,
                token_digest=handle.token_digest,
                owned_revision=handle.runtime_state_revision,
            )

        def terminal(
            session: Session, child: ManagedInstanceLifecycleOperation
        ) -> None:
            nonlocal current
            if child.state is ManagedInstanceLifecycleOperationState.SUCCEEDED:
                self._binding(session, current)
                self._container(current, "running")
            current = finish_start(
                session, current.id, version=current.version, child_id=child.id
            )

        try:
            self._lifecycle.start_managed(
                planned.target_id,
                operation_id=planned.start_operation_id,
                expected=ManagedStartBinding(
                    str(planned.target_id),
                    str(planned.runtime_revision_id),
                    str(planned.image_id),
                    planned.container_id,
                    "stopped",
                ),
                precondition=admission,
                on_claim=authorized,
                on_result=captured,
                on_terminal=terminal,
            )
        except BaseException as error:
            persisted = self._repository.get(planned.id)
            assert persisted is not None
            if (
                claim_digest is not None
                and persisted.checkpoint is CreateCheckpoint.DISPATCHED
                and self._repository.owns_start_claim(planned.id, claim_digest)
            ):
                persisted = self._repository.note_uncertain(
                    planned.id, version=persisted.version, reason="start_unconfirmed"
                )
            elif (
                persisted.checkpoint is CreateCheckpoint.PLANNED
                and persisted.version == planned.version
            ):
                persisted = self._repository.refuse(
                    planned.id,
                    version=planned.version,
                    reason="start_admission_refused",
                )
            if not isinstance(error, Exception):
                raise
            return persisted
        persisted = self._repository.get(planned.id)
        assert persisted is not None
        return persisted
