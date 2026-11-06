"""Existing Create/lifecycle/admission services with fake acquisition and Docker."""

import json
from pathlib import Path
from typing import Any, cast
from uuid import UUID

from pydantic import SecretStr
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.create_kodi import CreateKodiService
from kodi_lab.application.create_kodi_installation import CreateKodiInstallationService
from kodi_lab.application.create_kodi_provisioning import CreateKodiProvisioningService
from kodi_lab.application.create_kodi_registration import CreateKodiRegistrationService
from kodi_lab.application.create_kodi_runner import CreateKodiRunner
from kodi_lab.application.create_kodi_start import CreateKodiStartService
from kodi_lab.application.readiness_tasks import ReadinessTaskRegistry
from kodi_lab.application.runtime_installations import RuntimeInstallationService
from kodi_lab.application.runtime_lifecycle import ManagedRuntimeLifecycleService
from kodi_lab.application.runtime_status import ManagedRuntimeStatusService
from kodi_lab.broker.docker_inventory import (
    ContainerHealthState,
    ContainerRuntimeState,
    ManagedContainerRecord,
    StartManagedContainerRecord,
)
from kodi_lab.broker.errors import ClientTransportError
from kodi_lab.broker.operations import (
    ObserveManagedKodiRuntimeResult,
    StartManagedContainerResult,
    StopManagedContainerResult,
)
from kodi_lab.storage.create_kodi import CreateKodiRepository
from tests.application.test_managed_start import Broker as NativeBroker
from tests.application.test_managed_start import NativeReady
from tests.create_kodi_provisioning_support import Broker as ProvisionBroker
from tests.create_kodi_provisioning_support import Interrupted
from tests.create_kodi_readiness_support import Transport
from tests.create_kodi_readiness_support import service as native_service
from tests.create_kodi_support import accept
from tests.runtime.test_recipe_installation import _ready


class Broker(ProvisionBroker):
    def __init__(
        self, sessions: sessionmaker[Session], repo: CreateKodiRepository
    ) -> None:
        super().__init__(sessions, repo)
        self.admission = self.capacity
        self.starts = self.stops = 0
        self.pause = False
        self.finish = None
        self.proof: SecretStr | None = None
        self.start_fault = ""
        self.mutation_log: Path | None = None

    def _record(self, action: str) -> None:
        if self.mutation_log:
            op = self.repo.get(self.parent) if self.parent else None
            assert op and op.child_id
            with self.mutation_log.open("a") as file:
                file.write(json.dumps([action, str(op.child_id)]) + "\n")

    def launch_reviewed_runtime_installation(self, **kwargs: Any) -> Any:
        self._record("installation")
        return super().launch_reviewed_runtime_installation(**kwargs)

    def provision_managed_container(self, **kwargs: Any) -> Any:
        self._record("provision")
        return super().provision_managed_container(**kwargs)

    def start_managed_container(self, **kwargs: Any) -> StartManagedContainerResult:
        self._record("start")
        if self.start_fault == "before_dispatch":
            raise Interrupted()
        result = NativeBroker.start_managed_container(cast(Any, self), **kwargs)
        if self.start_fault == "lost_response":
            raise ClientTransportError("fake lost Start response")
        return result

    def dispatch(self, target: str) -> StartManagedContainerRecord:
        return NativeBroker.dispatch(cast(Any, self), target)

    def stop_managed_container(self, **kwargs: Any) -> StopManagedContainerResult:
        return NativeBroker.stop_managed_container(cast(Any, self), **kwargs)

    def observe_managed_kodi_runtime(
        self, **kwargs: Any
    ) -> ObserveManagedKodiRuntimeResult:
        return NativeBroker.observe_managed_kodi_runtime(cast(Any, self), **kwargs)


class Environment:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        cached: bool = False,
        operation_id: UUID | None = None,
        generation: str = "omega",
        start_after_setup: bool = True,
        preview_approved: bool = False,
    ) -> None:
        self.sessions, self.repo = sessions, CreateKodiRepository(sessions)
        self.broker = Broker(sessions, self.repo)
        self.tasks = ReadinessTaskRegistry(max_workers=1)
        self.transport = Transport()
        self.installer = RuntimeInstallationService(sessions, self.broker)
        self.install = CreateKodiInstallationService(self.repo, self.installer)
        self.registration = CreateKodiRegistrationService(
            sessions, self.repo, self.installer
        )
        self.lifecycle = ManagedRuntimeLifecycleService(
            sessions,
            self.broker,
            self.tasks,
            ManagedRuntimeStatusService(sessions, self.broker, self.tasks),
            coordinator_factory=lambda *_: NativeReady(),
        )
        self.provision = CreateKodiProvisioningService(
            self.repo,
            self.installer,
            self.lifecycle,
            self.broker.managed_container_inventory,
        )
        self.start = CreateKodiStartService(
            self.repo,
            self.installer,
            self.lifecycle,
            self.broker.managed_container_inventory,
        )
        self.native = native_service(
            sessions, self.repo, cast(Any, self.broker), self.transport
        )
        self.runner = CreateKodiRunner(
            self.repo,
            self.install,
            self.registration,
            self.provision,
            self.start,
            self.native,
        )
        if cached and operation_id is None:
            _ready(sessions, generation=generation)
        if operation_id is None:
            self.op = accept(
                CreateKodiService(self.repo),
                generation=generation,
                start_after_setup=start_after_setup,
                preview_approved=preview_approved,
            )
        else:
            existing = self.repo.get(operation_id)
            assert existing is not None
            self.op = existing
            if existing.container_id:
                self.broker.records = [
                    ManagedContainerRecord(
                        str(existing.target_id),
                        existing.container_id,
                        str(existing.image_id),
                        ContainerRuntimeState.STOPPED,
                        ContainerHealthState.HEALTHY,
                        str(existing.runtime_revision_id),
                    )
                ]
        self.broker.parent = self.op.id

    def step(self) -> None:
        self.op = self.runner.continue_once(self.op.id, version=self.op.version)

    def close(self) -> None:
        self.tasks.close()
