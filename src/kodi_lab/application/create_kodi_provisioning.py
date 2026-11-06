"""One explicit provision child through existing lifecycle authority and receipts."""

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
    ProvisionManagedContainerResult,
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
from kodi_lab.storage.create_kodi_provisioning import (
    capture_provision,
    claim_provision,
    finish_provision,
    planned_provision,
)
from kodi_lab.storage.development_sessions import require_session_admission


class CreateKodiProvisioningService:
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
        require_create_runtime(session, op, self._installer)
        if not registration_matches(session, op):
            raise CreateTransitionError("canonical_registration_missing")

    def _container(self, op: CreateOperation, container_id: str) -> None:
        records = self._inventory().containers
        matches = [r for r in records if r.managed_instance_id == str(op.target_id)]
        if (
            len(matches) != 1
            or matches[0].container_id != container_id
            or matches[0].runtime_revision_id != str(op.runtime_revision_id)
            or matches[0].image_id != op.image_id
            or matches[0].runtime_state != "stopped"
            or any(r.runtime_state not in {"stopped", "running"} for r in records)
        ):
            raise CreateTransitionError("provision_inventory_changed")

    def provision(self, operation_id: UUID, *, version: int) -> CreateOperation:
        found = self._repository.get(operation_id)
        if (
            found is None
            or found.version != version
            or found.stage is not CreateStage.PROVISIONING
        ):
            raise CreateTransitionError("stale_provision_context")
        current: CreateOperation = found
        if current.checkpoint not in {
            CreateCheckpoint.ACCEPTED,
            CreateCheckpoint.PLANNED,
        }:
            return current
        if current.checkpoint is CreateCheckpoint.ACCEPTED:
            current = self._repository.plan(current.id, version=current.version)
        planned = current
        assert planned.provision_operation_id is not None
        claim_digest: str | None = None

        def admission(session: Session) -> None:
            op = planned_provision(session, planned.id, planned.version)
            self._binding(session, op)
            records = self._inventory().containers
            if any(
                r.managed_instance_id == str(op.target_id)
                or r.runtime_state not in {"stopped", "running"}
                for r in records
            ):
                raise CreateTransitionError("provision_requires_absent_container")
            require_session_admission(
                session,
                records,
                target_id=str(op.target_id),
                revision_id=str(op.runtime_revision_id),
            )

        def claimed(session: Session, handle: RunningLifecycleOperation) -> None:
            nonlocal current, claim_digest
            current = claim_provision(
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
            result: ProvisionManagedContainerResult,
        ) -> None:
            nonlocal current
            self._binding(session, current)
            if (
                result.managed_instance_id,
                result.runtime_revision_id,
                result.image_id,
                result.runtime_state,
                result.created,
            ) != (
                str(current.target_id),
                str(current.runtime_revision_id),
                current.image_id,
                "stopped",
                True,
            ):
                raise CreateTransitionError("provision_reply_changed")
            self._container(current, result.container_id)
            current = capture_provision(
                session,
                current.id,
                version=current.version,
                child_id=handle.operation_id,
                token_digest=handle.token_digest,
                owned_revision=handle.runtime_state_revision,
                container_id=result.container_id,
            )

        def terminal(
            session: Session, child: ManagedInstanceLifecycleOperation
        ) -> None:
            nonlocal current
            if child.state is ManagedInstanceLifecycleOperationState.SUCCEEDED:
                self._binding(session, current)
                if current.container_id is None:
                    raise CreateTransitionError("provision_result_not_captured")
                self._container(current, current.container_id)
            current = finish_provision(
                session,
                current.id,
                version=current.version,
                child_id=child.id,
            )

        try:
            self._lifecycle.provision(
                planned.target_id,
                operation_id=planned.provision_operation_id,
                precondition=admission,
                on_claim=claimed,
                on_result=captured,
                on_terminal=terminal,
            )
        except BaseException as error:
            persisted = self._repository.get(planned.id)
            assert persisted is not None
            if (
                claim_digest is not None
                and persisted.checkpoint is CreateCheckpoint.DISPATCHED
                and self._repository.owns_provision_claim(planned.id, claim_digest)
            ):
                persisted = self._repository.note_uncertain(
                    persisted.id,
                    version=persisted.version,
                    reason="provision_unconfirmed",
                )
            elif (
                persisted.checkpoint is CreateCheckpoint.PLANNED
                and persisted.version == planned.version
            ):
                persisted = self._repository.refuse(
                    persisted.id,
                    version=persisted.version,
                    reason="provision_admission_refused",
                )
            if not isinstance(error, Exception):
                raise
            return persisted
        # A status read never drives another stage, Start, or a new child.
        persisted = self._repository.get(planned.id)
        assert persisted is not None
        return persisted
