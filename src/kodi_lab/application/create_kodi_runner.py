"""One explicit fixed-stage continuation, with no scheduler or implicit retry."""

from uuid import UUID

from kodi_lab.application.create_kodi_installation import CreateKodiInstallationService
from kodi_lab.application.create_kodi_provisioning import CreateKodiProvisioningService
from kodi_lab.application.create_kodi_readiness import CreateKodiReadinessService
from kodi_lab.application.create_kodi_registration import CreateKodiRegistrationService
from kodi_lab.application.create_kodi_start import CreateKodiStartService
from kodi_lab.domain.create_kodi import (
    CreateCheckpoint,
    CreateOperation,
    CreateOutcome,
    CreateStage,
    CreateTransitionError,
)
from kodi_lab.storage.create_kodi import CreateKodiRepository


class CreateKodiRunner:
    """Internal caller must explicitly continue the original approved parent.

    Each call plans once, advances once, or calls ONE existing adapter method.
    Adapter-internal checkpoints/claims remain in their existing transactions.
    Only an explicit protected continuation invokes this runner; no startup hook.
    """

    def __init__(
        self,
        repository: CreateKodiRepository,
        installation: CreateKodiInstallationService,
        registration: CreateKodiRegistrationService,
        provisioning: CreateKodiProvisioningService,
        start: CreateKodiStartService,
        readiness: CreateKodiReadinessService,
    ) -> None:
        self._repository = repository
        self._installation, self._registration = installation, registration
        self._provisioning, self._start, self._readiness = (
            provisioning,
            start,
            readiness,
        )

    def continue_once(self, operation_id: UUID, *, version: int) -> CreateOperation:
        op = self._repository.get(operation_id)
        if op is None:
            raise CreateTransitionError("operation_not_found")
        if op.version != version:
            raise CreateTransitionError("stale_version")
        if op.checkpoint is CreateCheckpoint.COMPLETE:
            return op
        try:
            self._step(op)
        except CreateTransitionError:
            # A competing successful claim may have changed the parent while
            # this call entered an adapter. Never repair/reset the winning claim.
            latest = self._repository.get(op.id)
            if latest is None or latest == op:
                raise
        latest = self._repository.get(op.id)
        assert latest is not None
        return latest

    def _step(self, op: CreateOperation) -> None:
        if op.checkpoint is CreateCheckpoint.NEEDS_ATTENTION:
            # These existing explicit methods observe original evidence only.
            # No refusal reset, failed-install retry or lifecycle replay is used.
            if op.outcome is CreateOutcome.UNCERTAIN:
                if op.stage is CreateStage.INSTALLATION:
                    self._installation.resolve(op.id, version=op.version)
                elif op.stage is CreateStage.REGISTRATION:
                    self._registration.confirm(op.id, version=op.version)
            return
        if op.checkpoint is CreateCheckpoint.CONFIRMED:
            if op.stage in {CreateStage.INSTALLATION, CreateStage.REGISTRATION}:
                self._repository.advance(op.id, version=op.version)
            elif op.stage is CreateStage.PROVISIONING:
                self._start.advance(op.id, version=op.version)
            elif op.stage is CreateStage.STARTUP:
                self._readiness.advance(op.id, version=op.version)
            else:
                # Native success needs a fresh 2D observation, never a generic
                # ledger advance of a historical/fabricated Confirmed marker.
                raise CreateTransitionError("native_readiness_confirmation_required")
            return
        if op.checkpoint is CreateCheckpoint.ACCEPTED:
            self._repository.plan(op.id, version=op.version)
            return
        if op.checkpoint is CreateCheckpoint.DISPATCHED:
            if op.stage is CreateStage.INSTALLATION:
                self._installation.resolve(op.id, version=op.version)
            elif op.stage is CreateStage.REGISTRATION:
                self._registration.confirm(op.id, version=op.version)
            elif op.stage in {CreateStage.PROVISIONING, CreateStage.STARTUP}:
                self._repository.inspect_lifecycle_dispatch(op.id, version=op.version)
            else:
                self._readiness.inspect(op.id, version=op.version)
            return
        if op.checkpoint is not CreateCheckpoint.PLANNED:
            raise CreateTransitionError("unsupported_create_checkpoint")
        if op.stage is CreateStage.INSTALLATION:
            self._installation.install(op.id, version=op.version)
        elif op.stage is CreateStage.REGISTRATION:
            self._registration.register(op.id, version=op.version)
        elif op.stage is CreateStage.PROVISIONING:
            self._provisioning.provision(op.id, version=op.version)
        elif op.stage is CreateStage.STARTUP:
            self._start.start(op.id, version=op.version)
        else:
            self._readiness.inspect(op.id, version=op.version)
