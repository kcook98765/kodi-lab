"""Sealed fake runtime evidence and real lifecycle/capacity with fake Docker."""

from dataclasses import replace
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.create_kodi import CreateKodiService
from kodi_lab.application.create_kodi_installation import CreateKodiInstallationService
from kodi_lab.application.create_kodi_provisioning import CreateKodiProvisioningService
from kodi_lab.application.create_kodi_registration import CreateKodiRegistrationService
from kodi_lab.application.readiness_tasks import ReadinessTaskRegistry
from kodi_lab.application.runtime_installations import RuntimeInstallationService
from kodi_lab.application.runtime_lifecycle import ManagedRuntimeLifecycleService
from kodi_lab.application.runtime_status import ManagedRuntimeStatusService
from kodi_lab.broker.docker_inventory import (
    ContainerHealthState,
    ContainerRuntimeState,
    ManagedContainerRecord,
    ProvisionedManagedContainerRecord,
)
from kodi_lab.broker.errors import ClientTransportError
from kodi_lab.broker.managed_capacity import ManagedCapacityAdmission
from kodi_lab.broker.operations import (
    ManagedContainerInventoryEntry,
    ManagedContainerInventoryResult,
    ProvisionManagedContainerResult,
)
from kodi_lab.domain.create_kodi import CreateCheckpoint, CreateOperation
from kodi_lab.storage.create_kodi import CreateKodiRepository
from kodi_lab.storage.models import ManagedInstanceMutationLeaseRow
from tests.application.test_create_kodi_installation import Broker as InstallationBroker
from tests.create_kodi_support import accept
from tests.runtime.test_recipe_installation import _ready


class Interrupted(BaseException):
    pass


class Broker(InstallationBroker):
    def __init__(
        self, factory: sessionmaker[Session], repo: CreateKodiRepository
    ) -> None:
        super().__init__(factory, repo)
        self.records: list[ManagedContainerRecord] = []
        self.capacity = ManagedCapacityAdmission(factory, lambda: tuple(self.records))
        self.provisions = 0
        self.fault = ""
        self.dispatch_log: Path | None = None

    def managed_container_inventory(
        self, **_kwargs: Any
    ) -> ManagedContainerInventoryResult:
        op = self.repo.get(self.parent) if self.parent else None
        if self.fault == "replacement" and op and op.container_id and self.records:
            self.records[0] = replace(self.records[0], container_id="e" * 64)
        return ManagedContainerInventoryResult(
            containers=[
                ManagedContainerInventoryEntry(
                    managed_instance_id=r.managed_instance_id,
                    container_id=r.container_id,
                    image_id=r.image_id,
                    runtime_revision_id=r.runtime_revision_id,
                    runtime_state=r.runtime_state,
                    health_state=r.health_state,
                )
                for r in self.records
            ]
        )

    def provision_managed_container(
        self, *, managed_instance_id: str
    ) -> ProvisionManagedContainerResult:
        assert self.parent is not None
        op = self.repo.get(self.parent)
        assert op and op.checkpoint is CreateCheckpoint.DISPATCHED
        with self.factory() as session:
            lease = session.get(ManagedInstanceMutationLeaseRow, managed_instance_id)
            assert lease and lease.operation_id == str(op.provision_operation_id)
        if self.fault == "before_dispatch":
            raise Interrupted()

        def dispatch() -> ProvisionedManagedContainerRecord:
            self.provisions += 1
            if self.dispatch_log:
                with self.dispatch_log.open("a") as file:
                    file.write(str(op.provision_operation_id) + "\n")
            record = ManagedContainerRecord(
                managed_instance_id,
                "a" * 64,
                str(op.image_id),
                ContainerRuntimeState.STOPPED,
                ContainerHealthState.NOT_CONFIGURED,
                str(op.runtime_revision_id),
            )
            self.records.append(record)
            return ProvisionedManagedContainerRecord(
                managed_instance_id,
                str(op.runtime_revision_id),
                record.container_id,
                record.image_id,
                record.runtime_state,
                True,
            )

        result = self.capacity.run_provision(
            instance_id=managed_instance_id,
            revision_id=str(op.runtime_revision_id),
            dispatch=dispatch,
        )
        if self.fault == "lost_response":
            raise ClientTransportError("fake lost reply")
        reply = ProvisionManagedContainerResult(
            managed_instance_id=managed_instance_id,
            runtime_revision_id=result.runtime_revision_id,
            container_id=result.container_id,
            image_id=result.image_id,
            runtime_state=result.runtime_state,
            created=result.created,
        )
        if self.fault == "wrong_container":
            reply = reply.model_copy(update={"container_id": "b" * 64})
        if self.fault == "wrong_image":
            reply = reply.model_copy(update={"image_id": "sha256:" + "c" * 64})
        if self.fault == "reused_container":
            reply = reply.model_copy(update={"created": False})
        return reply

    def observe_managed_kodi_runtime(self, **_kwargs: Any) -> Any:
        raise AssertionError("Provision must not run native readiness")

    def start_managed_container(self, **_kwargs: Any) -> Any:
        raise AssertionError("Start is outside milestone 2B")

    def stop_managed_container(self, **_kwargs: Any) -> Any:
        raise AssertionError("Stop is outside milestone 2B")

    def deprovision_managed_container(self, **_kwargs: Any) -> Any:
        raise AssertionError("Replacement is outside milestone 2B")


class Environment:
    def __init__(self, sessions: sessionmaker[Session], *, fresh: bool = False) -> None:
        self.sessions = sessions
        self.repo = CreateKodiRepository(sessions)
        self.broker = Broker(sessions, self.repo)
        self.installer = RuntimeInstallationService(sessions, self.broker)
        self.registration = CreateKodiRegistrationService(
            sessions, self.repo, self.installer
        )
        self.readiness = ReadinessTaskRegistry()
        self.lifecycle = ManagedRuntimeLifecycleService(
            sessions,
            self.broker,
            self.readiness,
            ManagedRuntimeStatusService(sessions, self.broker, self.readiness),
        )
        self.provisioning = CreateKodiProvisioningService(
            self.repo,
            self.installer,
            self.lifecycle,
            self.broker.managed_container_inventory,
        )
        if not fresh:
            _ready(sessions, generation="omega")
        app = CreateKodiService(self.repo)
        root = accept(app)
        self.broker.parent = root.id
        installed = CreateKodiInstallationService(self.repo, self.installer).install(
            root.id, version=root.version
        )
        self.op = (
            self.repo.advance(installed.id, version=installed.version)
            if installed.checkpoint is CreateCheckpoint.CONFIRMED
            else installed
        )

    def registered(self) -> CreateOperation:
        op = self.registration.register(self.op.id, version=self.op.version)
        self.op = (
            self.repo.advance(op.id, version=op.version)
            if op.checkpoint is CreateCheckpoint.CONFIRMED
            else op
        )
        return self.op

    def close(self) -> None:
        self.readiness.close()
