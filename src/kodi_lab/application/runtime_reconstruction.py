"""Administrator-only, exact stopped-mismatch runtime reconstruction."""

from __future__ import annotations

import hashlib
import secrets
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from threading import Lock
from typing import Protocol, cast
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.addon_zip_installations import (
    invalidate_addon_zip_installation_evidence,
)
from kodi_lab.application.development_source_deployments import (
    invalidate_development_source_deployments,
)
from kodi_lab.application.reconciliation import (
    ManagedInstanceReconciliationRequiredError,
    ManagedInstanceReconciliationService,
    ReconciliationAction,
)
from kodi_lab.application.reset import (
    DisposableInstanceResetService,
    ResetError,
    ResetReconciliationRequiredError,
    _ResetBroker,
)
from kodi_lab.application.reset_activity import (
    ResetActivityRegistry,
    ResetActivitySnapshot,
    ResetActivityState,
)
from kodi_lab.broker.docker_inventory import ContainerRuntimeState
from kodi_lab.broker.errors import ClientProtocolError, ClientTransportError
from kodi_lab.broker.operations import (
    ManagedContainerInventoryEntry,
    ManagedContainerInventoryResult,
    ProvisionManagedContainerResult,
    ReconstructionDestinationResult,
    RemoveMismatchedManagedContainerResult,
)
from kodi_lab.domain import InstanceType, ManagedInstanceMutationKind
from kodi_lab.fixtures.package import load_packaged_fixture
from kodi_lab.storage.models import (
    ManagedInstanceMutationLeaseRow,
    ManagedInstanceRow,
    ManagedRuntimeReconstructionRow,
)
from kodi_lab.storage.repositories import (
    FixtureDefinitionRepository,
    ManagedInstanceMutationLeaseRepository,
    ManagedInstanceRepository,
)

LEASE_DURATION = timedelta(minutes=10)
UNCERTAIN_ERRORS = (ClientTransportError, ClientProtocolError)


class ReconstructionConflictError(RuntimeError):
    """The exact mismatch or administrator confirmation is no longer current."""


class ReconstructionLeaseLostError(RuntimeError):
    """The worker no longer owns the reconstruction lease."""


class _Broker(Protocol):
    def managed_container_inventory(self) -> ManagedContainerInventoryResult: ...

    def reconstruction_destination(
        self, *, managed_instance_id: str
    ) -> ReconstructionDestinationResult: ...

    def remove_mismatched_managed_container(
        self,
        *,
        managed_instance_id: str,
        operation_id: str,
        lease_token_digest: str,
        owned_runtime_state_revision: int,
        old_container_id: str,
        old_image_id: str,
        old_runtime_revision_id: str,
        desired_runtime_revision_id: str,
    ) -> RemoveMismatchedManagedContainerResult: ...

    def provision_managed_container(
        self, *, managed_instance_id: str
    ) -> ProvisionManagedContainerResult: ...


class ReconstructionEligibility(BaseModel):
    model_config = ConfigDict(extra="forbid")

    eligible: bool
    reason: str = Field(max_length=256)
    observation_token: str | None = None
    desired_runtime_revision_id: UUID | None = None


class ReconstructionStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation_id: UUID
    managed_instance_id: UUID
    state: str
    phase: str
    requested_at: datetime
    completed_at: datetime | None
    old_writable_state_discarded: bool
    fresh_container_created: bool
    failure_reason: str | None
    reconciliation_state: str | None = None


class ReconstructionAccepted(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: ReconstructionStatus
    status_url: str


def _timestamp(value: datetime) -> str:
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse(value: str | None) -> datetime | None:
    return (
        None if value is None else datetime.fromisoformat(value.replace("Z", "+00:00"))
    )


def _token(
    instance_id: UUID,
    container: ManagedContainerInventoryEntry,
    desired_revision: UUID,
    desired_image_id: str,
    state_revision: int,
) -> str:
    # A comparison token, never independent authority: every field is reobserved.
    parts = (
        str(instance_id),
        container.container_id,
        container.image_id,
        container.runtime_revision_id or "",
        str(desired_revision),
        desired_image_id,
        str(state_revision),
    )
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


class ManagedRuntimeReconstructionCoordinator:
    """Keep the confirmation, lease, and destructive phases independently durable."""

    def __init__(self, session_factory: sessionmaker[Session], broker: _Broker) -> None:
        self._session_factory = session_factory
        self._broker = broker
        self._reconciliation = ManagedInstanceReconciliationService(session_factory)

    def _current_container(self, instance_id: UUID) -> ManagedContainerInventoryEntry:
        try:
            matches = [
                item
                for item in self._broker.managed_container_inventory().containers
                if item.managed_instance_id == str(instance_id)
            ]
        except Exception as error:
            raise ReconstructionConflictError(
                "Managed container ownership cannot be inspected."
            ) from error
        if len(matches) != 1:
            raise ReconstructionConflictError(
                "Exactly one owned managed container must be present."
            )
        return matches[0]

    def _observe(
        self, instance_id: UUID
    ) -> tuple[
        ManagedInstanceRow,
        ManagedContainerInventoryEntry,
        ReconstructionDestinationResult,
    ]:
        try:
            self._reconciliation.require_allowed(
                instance_id, ReconciliationAction.RECONSTRUCTION
            )
        except ManagedInstanceReconciliationRequiredError as error:
            raise ReconstructionConflictError(
                "Another operation requires administrator reconciliation."
            ) from error
        with self._session_factory() as session:
            instance = session.get(ManagedInstanceRow, str(instance_id))
            if (
                instance is None
                or instance.instance_type is not InstanceType.DISPOSABLE
            ):
                raise ReconstructionConflictError(
                    "Only a provisioned disposable managed instance can be "
                    "reconstructed."
                )
            desired = instance.runtime_revision_id
            fixture_id = instance.fixture_definition_id
            state_revision = instance.runtime_state_revision
            if (
                session.get(ManagedInstanceMutationLeaseRow, str(instance_id))
                is not None
            ):
                raise ReconstructionConflictError(
                    "Another managed operation is active."
                )
            if fixture_id is not None:
                fixture = FixtureDefinitionRepository(session).get(UUID(fixture_id))
                if fixture is None:
                    raise ReconstructionConflictError(
                        "Assigned fixture is unavailable."
                    )
                try:
                    package = load_packaged_fixture(fixture.slug)
                except Exception as error:
                    raise ReconstructionConflictError(
                        "Assigned fixture package is unavailable."
                    ) from error
                if package.definition != fixture:
                    raise ReconstructionConflictError(
                        "Assigned fixture package changed."
                    )
            unresolved = session.scalar(
                select(ManagedRuntimeReconstructionRow.id).where(
                    ManagedRuntimeReconstructionRow.managed_instance_id
                    == str(instance_id),
                    ManagedRuntimeReconstructionRow.state == "reconciliation_required",
                )
            )
            if unresolved is not None:
                raise ReconstructionConflictError(
                    "Earlier reconstruction requires administrator reconciliation."
                )
        container = self._current_container(instance_id)
        if container.runtime_state is not ContainerRuntimeState.STOPPED:
            raise ReconstructionConflictError("The managed container must be stopped.")
        if container.runtime_revision_id is None:
            raise ReconstructionConflictError(
                "The observed container revision is missing or invalid."
            )
        if container.runtime_revision_id == desired:
            raise ReconstructionConflictError(
                "The managed container already matches its pin."
            )
        try:
            destination = self._broker.reconstruction_destination(
                managed_instance_id=str(instance_id)
            )
        except Exception as error:
            raise ReconstructionConflictError(
                "The approved pinned runtime image or network is unavailable."
            ) from error
        if destination.desired_runtime_revision_id != desired:
            raise ReconstructionConflictError("The desired runtime pin changed.")
        with self._session_factory() as session:
            refreshed = session.get(ManagedInstanceRow, str(instance_id))
            if (
                refreshed is None
                or refreshed.runtime_revision_id != desired
                or refreshed.runtime_state_revision != state_revision
            ):
                raise ReconstructionConflictError(
                    "Managed runtime state changed during inspection."
                )
            session.expunge(refreshed)
        return refreshed, container, destination

    def eligibility(self, instance_id: UUID) -> ReconstructionEligibility:
        try:
            instance, container, destination = self._observe(instance_id)
        except ReconstructionConflictError as error:
            return ReconstructionEligibility(eligible=False, reason=str(error)[:256])
        return ReconstructionEligibility(
            eligible=True,
            reason="Stopped managed runtime differs from its approved persisted pin.",
            observation_token=_token(
                instance_id,
                container,
                UUID(instance.runtime_revision_id),
                destination.desired_image_id,
                instance.runtime_state_revision,
            ),
            desired_runtime_revision_id=UUID(instance.runtime_revision_id),
        )

    def queue(
        self, instance_id: UUID, observation_token: str
    ) -> ReconstructionAccepted:
        instance, container, destination = self._observe(instance_id)
        expected = _token(
            instance_id,
            container,
            UUID(instance.runtime_revision_id),
            destination.desired_image_id,
            instance.runtime_state_revision,
        )
        if not secrets.compare_digest(expected, observation_token):
            raise ReconstructionConflictError(
                "Confirmation is stale. Inspect the managed runtime again."
            )
        operation_id = uuid4()
        try:
            with self._session_factory.begin() as session:
                current = session.get(ManagedInstanceRow, str(instance_id))
                if (
                    current is None
                    or current.runtime_revision_id != instance.runtime_revision_id
                    or current.runtime_state_revision != instance.runtime_state_revision
                ):
                    raise ReconstructionConflictError("Managed runtime state changed.")
                lease_repo = ManagedInstanceMutationLeaseRepository(session)
                lease_repo.reserve(
                    instance_id,
                    kind=ManagedInstanceMutationKind.RECONSTRUCTION,
                    operation_id=operation_id,
                )
                session.refresh(current)
                row = ManagedRuntimeReconstructionRow(
                    id=str(operation_id),
                    managed_instance_id=str(instance_id),
                    old_container_id=container.container_id,
                    old_image_id=container.image_id,
                    old_runtime_revision_id=container.runtime_revision_id,
                    desired_runtime_revision_id=instance.runtime_revision_id,
                    desired_image_id=destination.desired_image_id,
                    confirmed_runtime_state_revision=instance.runtime_state_revision,
                    owned_runtime_state_revision=current.runtime_state_revision,
                    old_discarded=False,
                    state="queued",
                    phase="confirmed",
                    requested_at=_timestamp(datetime.now(UTC)),
                )
                session.add(row)
        except IntegrityError as error:
            raise ReconstructionConflictError(
                "Another managed operation is active."
            ) from error
        operation = self.get(operation_id)
        assert operation is not None
        return ReconstructionAccepted(
            operation=self.to_status(operation),
            status_url=(
                f"/api/instances/{instance_id}/reconstruction-operations/{operation_id}"
            ),
        )

    def get(self, operation_id: UUID) -> ManagedRuntimeReconstructionRow | None:
        with self._session_factory() as session:
            row = session.get(ManagedRuntimeReconstructionRow, str(operation_id))
            if row is not None:
                session.expunge(row)
            return row

    def latest_for_instance(
        self, instance_id: UUID
    ) -> ManagedRuntimeReconstructionRow | None:
        with self._session_factory() as session:
            row = session.scalar(
                select(ManagedRuntimeReconstructionRow)
                .where(
                    ManagedRuntimeReconstructionRow.managed_instance_id
                    == str(instance_id)
                )
                .order_by(ManagedRuntimeReconstructionRow.requested_at.desc())
                .limit(1)
            )
            if row is not None:
                session.expunge(row)
            return row

    def queued(self) -> list[UUID]:
        with self._session_factory() as session:
            return [
                UUID(row.id)
                for row in session.scalars(
                    select(ManagedRuntimeReconstructionRow).where(
                        ManagedRuntimeReconstructionRow.state == "queued"
                    )
                )
            ]

    def acquire(self, operation_id: UUID) -> str | None:
        token_digest = hashlib.sha256(secrets.token_bytes(32)).hexdigest()
        now = datetime.now(UTC)
        with self._session_factory.begin() as session:
            row = session.get(ManagedRuntimeReconstructionRow, str(operation_id))
            if row is None or row.state != "queued":
                return None
            if not ManagedInstanceMutationLeaseRepository(session).claim(
                UUID(row.managed_instance_id),
                operation_id=operation_id,
                token_digest=token_digest,
                now=now,
                expires_at=now + LEASE_DURATION,
            ):
                return None
            row.state = "running"
            row.phase = "preflight"
            row.started_at = _timestamp(now)
            row.heartbeat_at = _timestamp(now)
            row.lease_token_digest = token_digest
            row.lease_expires_at = _timestamp(now + LEASE_DURATION)
        return token_digest

    def phase(
        self,
        operation_id: UUID,
        token_digest: str,
        phase: str,
        *,
        old_discarded: bool | None = None,
        fresh_container_id: str | None = None,
    ) -> None:
        now = datetime.now(UTC)
        with self._session_factory.begin() as session:
            row = session.get(ManagedRuntimeReconstructionRow, str(operation_id))
            if (
                row is None
                or row.state != "running"
                or row.lease_token_digest != token_digest
            ):
                raise ReconstructionLeaseLostError("Reconstruction lease was lost.")
            renewed = ManagedInstanceMutationLeaseRepository(session).renew(
                UUID(row.managed_instance_id),
                operation_id=operation_id,
                token_digest=token_digest,
                now=now,
                expires_at=now + LEASE_DURATION,
            )
            if not renewed:
                raise ReconstructionLeaseLostError("Reconstruction lease was lost.")
            row.phase = phase
            row.heartbeat_at = _timestamp(now)
            row.lease_expires_at = _timestamp(now + LEASE_DURATION)
            if old_discarded is not None:
                row.old_discarded = old_discarded
            if fresh_container_id is not None:
                row.fresh_container_id = fresh_container_id

    def finish(
        self,
        operation_id: UUID,
        token_digest: str,
        *,
        state: str,
        reason: str | None = None,
    ) -> None:
        now = datetime.now(UTC)
        with self._session_factory.begin() as session:
            row = session.get(ManagedRuntimeReconstructionRow, str(operation_id))
            if (
                row is None
                or row.state != "running"
                or row.lease_token_digest != token_digest
            ):
                raise ReconstructionLeaseLostError("Reconstruction lease was lost.")
            if not ManagedInstanceMutationLeaseRepository(session).release(
                UUID(row.managed_instance_id),
                operation_id=operation_id,
                token_digest=token_digest,
            ):
                raise ReconstructionLeaseLostError("Reconstruction lease was lost.")
            row.state = state
            if state == "succeeded":
                row.phase = "succeeded"
            row.completed_at = _timestamp(now)
            row.failure_reason = reason[:256] if reason else None
            row.lease_token_digest = None
            row.lease_expires_at = None

    def reconciliation_state(self, row: ManagedRuntimeReconstructionRow) -> str:
        try:
            matches = [
                item
                for item in self._broker.managed_container_inventory().containers
                if item.managed_instance_id == row.managed_instance_id
            ]
        except Exception:
            return "observation_unavailable"
        if not matches:
            return "unprovisioned"
        if len(matches) != 1:
            return "ambiguous_ownership"
        item = matches[0]
        if (
            item.container_id == row.old_container_id
            and item.image_id == row.old_image_id
            and item.runtime_revision_id == row.old_runtime_revision_id
            and item.runtime_state is ContainerRuntimeState.STOPPED
        ):
            return "exact_old_container"
        if (
            row.fresh_container_id is not None
            and item.container_id == row.fresh_container_id
            and item.image_id == row.desired_image_id
            and item.runtime_revision_id == row.desired_runtime_revision_id
            and item.runtime_state is ContainerRuntimeState.STOPPED
        ):
            return "expected_fresh_stopped"
        return "ambiguous_ownership"

    def resolve_interrupted(
        self, operation_id: UUID, expected_state: str
    ) -> ReconstructionStatus:
        """Close uncertainty only after a serial broker read proves a safe state."""

        row = self.get(operation_id)
        if row is None or row.state != "reconciliation_required":
            raise ReconstructionConflictError(
                "There is no interrupted reconstruction to reconcile."
            )
        observed = self.reconciliation_state(row)
        if observed != expected_state or observed not in {
            "exact_old_container",
            "unprovisioned",
            "expected_fresh_stopped",
        }:
            raise ReconstructionConflictError(
                "Reconstruction state is ambiguous or changed; "
                "intervention is required."
            )
        with self._session_factory.begin() as session:
            current = session.get(ManagedRuntimeReconstructionRow, str(operation_id))
            instance = session.get(ManagedInstanceRow, row.managed_instance_id)
            lease = session.get(
                ManagedInstanceMutationLeaseRow, row.managed_instance_id
            )
            if (
                current is None
                or current.state != "reconciliation_required"
                or instance is None
                or instance.runtime_revision_id != row.desired_runtime_revision_id
                or lease is not None
            ):
                raise ReconstructionConflictError(
                    "Reconstruction ownership or persisted pin changed."
                )
            if observed != "exact_old_container":
                invalidate_development_source_deployments(
                    session, UUID(row.managed_instance_id)
                )
                invalidate_addon_zip_installation_evidence(
                    session, UUID(row.managed_instance_id)
                )
            current.state = "resolved"
            current.failure_reason = (
                f"Administrator confirmed {observed} after interrupted reconstruction."
            )
            current.old_discarded = observed != "exact_old_container"
        updated = self.get(operation_id)
        assert updated is not None
        return self.to_status(updated)

    def to_status(self, row: ManagedRuntimeReconstructionRow) -> ReconstructionStatus:
        requested_at = _parse(row.requested_at)
        assert requested_at is not None
        return ReconstructionStatus(
            operation_id=UUID(row.id),
            managed_instance_id=UUID(row.managed_instance_id),
            state=row.state,
            phase=row.phase,
            requested_at=requested_at,
            completed_at=_parse(row.completed_at),
            old_writable_state_discarded=row.old_discarded,
            fresh_container_created=row.fresh_container_id is not None,
            failure_reason=row.failure_reason,
            reconciliation_state=(
                self.reconciliation_state(row)
                if row.state == "reconciliation_required"
                else None
            ),
        )


class ManagedRuntimeReconstructionExecutor:
    """One asynchronous worker per accepted operation; leases are authoritative."""

    def __init__(
        self,
        coordinator: ManagedRuntimeReconstructionCoordinator,
        session_factory: sessionmaker[Session],
        broker: _Broker,
    ) -> None:
        self._coordinator = coordinator
        self._session_factory = session_factory
        self._broker = broker
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="reconstruct")
        self._guard = Lock()
        self._accepting = True

    def start(self) -> None:
        for operation_id in self._coordinator.queued():
            self.submit(operation_id)

    def submit(self, operation_id: UUID) -> None:
        with self._guard:
            if not self._accepting:
                raise ReconstructionConflictError(
                    "Reconstruction executor is shutting down."
                )
            self._pool.submit(self._execute, operation_id)

    def close(self) -> None:
        with self._guard:
            self._accepting = False
        self._pool.shutdown(wait=True)

    def _execute(self, operation_id: UUID) -> None:
        digest = self._coordinator.acquire(operation_id)
        if digest is None:
            return
        row = self._coordinator.get(operation_id)
        assert row is not None
        try:
            self._run(row, digest)
        except ReconstructionLeaseLostError:
            return
        except UNCERTAIN_ERRORS:
            with suppress(ReconstructionLeaseLostError):
                self._coordinator.finish(
                    operation_id,
                    digest,
                    state="reconciliation_required",
                    reason="Broker dispatch outcome requires read-only reconciliation.",
                )
        except ResetReconciliationRequiredError:
            with suppress(ReconstructionLeaseLostError):
                self._coordinator.finish(
                    operation_id,
                    digest,
                    state="reconciliation_required",
                    reason="Fixture postcondition needs administrator inspection.",
                )
        except Exception:
            latest = self._coordinator.get(operation_id)
            assert latest is not None
            phase = latest.phase
            observation = self._coordinator.reconciliation_state(latest)
            uncertain = (
                (phase == "removing_old" and observation != "exact_old_container")
                or (
                    phase in {"provisioning", "applying_fixture"}
                    and observation not in {"unprovisioned", "expected_fresh_stopped"}
                )
                or phase == "old_absence_proven"
            )
            with suppress(ReconstructionLeaseLostError):
                self._coordinator.finish(
                    operation_id,
                    digest,
                    state="reconciliation_required" if uncertain else "failed",
                    reason=(
                        "Reconstruction postcondition needs administrator inspection."
                        if uncertain
                        else f"Reconstruction failed during {phase}."
                    ),
                )

    def _run(self, row: ManagedRuntimeReconstructionRow, digest: str) -> None:
        operation_id = UUID(row.id)
        instance_id = UUID(row.managed_instance_id)
        container = self._coordinator._current_container(instance_id)
        if (
            container.container_id != row.old_container_id
            or container.image_id != row.old_image_id
            or container.runtime_revision_id != row.old_runtime_revision_id
            or container.runtime_state is not ContainerRuntimeState.STOPPED
        ):
            raise ReconstructionConflictError("Confirmed stopped container changed.")
        destination = self._broker.reconstruction_destination(
            managed_instance_id=row.managed_instance_id
        )
        if destination.desired_runtime_revision_id != row.desired_runtime_revision_id:
            raise ReconstructionConflictError("Persisted runtime pin changed.")
        if destination.desired_image_id != row.desired_image_id:
            raise ReconstructionConflictError("Pinned runtime image changed.")
        self._coordinator.phase(operation_id, digest, "removing_old")
        result = self._broker.remove_mismatched_managed_container(
            managed_instance_id=row.managed_instance_id,
            operation_id=row.id,
            lease_token_digest=digest,
            owned_runtime_state_revision=row.owned_runtime_state_revision,
            old_container_id=row.old_container_id,
            old_image_id=row.old_image_id,
            old_runtime_revision_id=row.old_runtime_revision_id,
            desired_runtime_revision_id=row.desired_runtime_revision_id,
        )
        if not result.removed or result.removed_container_id != row.old_container_id:
            raise ReconstructionConflictError("Broker removal proof changed.")
        self._coordinator.phase(
            operation_id, digest, "old_absence_proven", old_discarded=True
        )
        with self._session_factory.begin() as session:
            invalidate_development_source_deployments(session, instance_id)
            invalidate_addon_zip_installation_evidence(session, instance_id)
        self._coordinator.phase(operation_id, digest, "provisioning")
        destination = self._broker.reconstruction_destination(
            managed_instance_id=row.managed_instance_id
        )
        if (
            destination.desired_runtime_revision_id != row.desired_runtime_revision_id
            or destination.desired_image_id != row.desired_image_id
        ):
            raise ReconstructionConflictError(
                "Pinned runtime changed before provision."
            )
        fresh = self._broker.provision_managed_container(
            managed_instance_id=row.managed_instance_id
        )
        if (
            fresh.container_id == row.old_container_id
            or not fresh.created
            or fresh.runtime_revision_id != row.desired_runtime_revision_id
            or fresh.image_id != row.desired_image_id
            or fresh.runtime_state is not ContainerRuntimeState.STOPPED
        ):
            raise ReconstructionConflictError(
                "Fresh container identity or state changed."
            )
        observed_fresh = self._coordinator._current_container(instance_id)
        if (
            observed_fresh.container_id != fresh.container_id
            or observed_fresh.image_id != fresh.image_id
            or observed_fresh.image_id != row.desired_image_id
            or observed_fresh.runtime_revision_id != row.desired_runtime_revision_id
            or observed_fresh.runtime_state is not ContainerRuntimeState.STOPPED
        ):
            raise ReconstructionConflictError("Fresh container verification changed.")
        self._coordinator.phase(
            operation_id,
            digest,
            "fresh_stopped_verified",
            fresh_container_id=fresh.container_id,
        )
        with self._session_factory() as session:
            instance = ManagedInstanceRepository(session).get(instance_id)
            if (
                instance is None
                or str(instance.runtime_revision_id) != row.desired_runtime_revision_id
            ):
                raise ReconstructionConflictError("Managed instance or pin changed.")
            fixture_assigned = instance.fixture_definition_id is not None
        if fixture_assigned:
            self._coordinator.phase(operation_id, digest, "applying_fixture")
            fixture_service = DisposableInstanceResetService(
                self._session_factory,
                cast(_ResetBroker, self._broker),
                activity=_ReconstructionFixtureActivity(
                    self._coordinator, operation_id, digest
                ),
            )
            try:
                fixture_service.initialize_assigned_fixture_on_fresh_container(
                    instance_id, fresh.container_id
                )
            except ResetReconciliationRequiredError:
                raise
            except ResetError as error:
                if (
                    self._coordinator.reconciliation_state(
                        self._coordinator.get(operation_id) or row
                    )
                    != "expected_fresh_stopped"
                ):
                    raise ReconstructionConflictError(
                        "Fixture failure left uncertain state."
                    ) from error
                raise
        self._coordinator.finish(operation_id, digest, state="succeeded")


class _ReconstructionFixtureActivity:
    """Renew reconstruction ownership at the trusted reset fixture stages."""

    def __init__(
        self,
        coordinator: ManagedRuntimeReconstructionCoordinator,
        operation_id: UUID,
        token_digest: str,
    ) -> None:
        self._coordinator = coordinator
        self._operation_id = operation_id
        self._token_digest = token_digest
        self._local = ResetActivityRegistry()

    def _renew(self) -> None:
        self._coordinator.phase(
            self._operation_id, self._token_digest, "applying_fixture"
        )

    def begin(self, instance_id: UUID) -> ResetActivitySnapshot:
        self._renew()
        return self._local.begin(instance_id)

    def transition(
        self, instance_id: UUID, generation: int, state: ResetActivityState
    ) -> ResetActivitySnapshot:
        self._renew()
        return self._local.transition(instance_id, generation, state)

    def succeed(self, instance_id: UUID, generation: int) -> ResetActivitySnapshot:
        self._renew()
        return self._local.succeed(instance_id, generation)

    def fail(
        self,
        instance_id: UUID,
        generation: int,
        *,
        failure_phase: str,
        failure_reason: str,
        reconciliation_required: bool,
    ) -> ResetActivitySnapshot:
        self._renew()
        return self._local.fail(
            instance_id,
            generation,
            failure_phase=failure_phase,
            failure_reason=failure_reason,
            reconciliation_required=reconciliation_required,
        )
