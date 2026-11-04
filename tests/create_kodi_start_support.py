"""Real Create/lifecycle/authority records; fake Docker and native transport."""

from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import SecretStr
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.create_kodi_start import CreateKodiStartService
from kodi_lab.application.readiness_tasks import ReadinessTaskRegistry
from kodi_lab.application.runtime_installations import (
    REQUIRED_OPERATIONS,
    RuntimeInstallationService,
)
from kodi_lab.application.runtime_lifecycle import ManagedRuntimeLifecycleService
from kodi_lab.application.runtime_status import ManagedRuntimeStatusService
from kodi_lab.broker.docker_inventory import (
    ContainerRuntimeState,
    ManagedContainerRecord,
)
from kodi_lab.broker.errors import ClientTransportError, RemoteBrokerError
from kodi_lab.broker.managed_capacity import ManagedCapacityError
from kodi_lab.broker.operations import StartManagedContainerResult
from kodi_lab.domain.create_kodi import CreateCheckpoint
from kodi_lab.storage.models import DevelopmentSessionPolicyRow
from tests.application.test_managed_start import Broker as NativeBroker
from tests.application.test_managed_start import NativeReady
from tests.create_kodi_provisioning_support import Environment as ProvisionEnvironment
from tests.create_kodi_provisioning_support import Interrupted


class Broker(NativeBroker):
    def __init__(
        self, sessions: sessionmaker[Session], record: ManagedContainerRecord
    ) -> None:
        super().__init__(sessions, record)
        self.calls = 0
        self.fault = ""
        self.before_rpc: Any = None
        self.dispatch_log: Path | None = None

    def capabilities(self) -> Any:
        from types import SimpleNamespace

        return SimpleNamespace(operations=list(REQUIRED_OPERATIONS))

    def active_reviewed_runtime_installation(self) -> Any:
        from types import SimpleNamespace

        return SimpleNamespace(operation_id=None, generation=None, lock_held=False)

    def observe_reviewed_runtime_installation(self, _operation: str) -> Any:
        from types import SimpleNamespace

        return SimpleNamespace(state="missing", lock_held=False)

    def reviewed_runtime_image_present(self, _operation: str) -> Any:
        from types import SimpleNamespace

        return SimpleNamespace(present=True)

    def start_managed_container(
        self,
        *,
        managed_instance_id: str,
        expected_container_id: str,
        expected_runtime_state: ContainerRuntimeState,
        ordinary_start_proof: SecretStr | None = None,
    ) -> StartManagedContainerResult:
        self.calls += 1
        self.proof = ordinary_start_proof
        if self.fault == "before_dispatch":
            raise Interrupted()
        if self.before_rpc:
            self.before_rpc()
        try:
            result = super().start_managed_container(
                managed_instance_id=managed_instance_id,
                expected_container_id=expected_container_id,
                expected_runtime_state=expected_runtime_state,
                ordinary_start_proof=ordinary_start_proof,
            )
        except ManagedCapacityError as error:
            raise RemoteBrokerError(
                error.code, "fake broker admission denied", request_id=None
            ) from error
        if self.dispatch_log:
            with self.dispatch_log.open("a") as file:
                file.write(managed_instance_id + "\n")
        if self.fault == "lost_response":
            raise ClientTransportError("fake lost response")
        if self.fault == "wrong_container":
            return result.model_copy(update={"container_id": "e" * 64})
        if self.fault == "no_start":
            return result.model_copy(update={"start_performed": False})
        return result


class Environment:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        fresh_install: bool = False,
        retained: bool = True,
    ) -> None:
        provision = ProvisionEnvironment(sessions, fresh=fresh_install)
        provision.registered()
        self.op = provision.provisioning.provision(
            provision.op.id, version=provision.op.version
        )
        assert self.op.checkpoint is CreateCheckpoint.CONFIRMED
        self.sessions, self.repo = sessions, provision.repo
        self.broker = Broker(sessions, provision.broker.records[0])
        provision.close()
        if retained:
            with sessions.begin() as session:
                session.add(
                    DevelopmentSessionPolicyRow(
                        id=1,
                        retain_profiles=True,
                        retained_omega_target_id=str(self.op.target_id),
                    )
                )
        self.readiness = ReadinessTaskRegistry()
        self.installer = RuntimeInstallationService(sessions, self.broker)
        self.lifecycle = ManagedRuntimeLifecycleService(
            sessions,
            self.broker,
            self.readiness,
            ManagedRuntimeStatusService(sessions, self.broker, self.readiness),
            coordinator_factory=lambda *_: NativeReady(),
        )
        self.start = CreateKodiStartService(
            self.repo,
            self.installer,
            self.lifecycle,
            self.broker.managed_container_inventory,
        )

    def advance(self) -> None:
        self.op = self.start.advance(self.op.id, version=self.op.version)

    def close(self) -> None:
        self.readiness.close()


def select_stopped(env: Environment, *, incomplete: bool = False) -> None:
    """A pre-existing valid synthetic workspace/session, never created by Start."""
    from datetime import UTC, datetime

    from kodi_lab.domain import AgentWorkspaceState, DevelopmentSourceKind
    from kodi_lab.storage.managed_activity import timestamp
    from kodi_lab.storage.models import (
        AgentWorkspaceRow,
        DevelopmentSessionRow,
        DevelopmentSourceRow,
    )

    now = timestamp(datetime.now(UTC))
    source_id, workspace_id = str(uuid4()), str(uuid4())
    with env.sessions.begin() as session:
        session.add(
            DevelopmentSourceRow(
                id=source_id,
                logical_name="Example",
                source_kind=DevelopmentSourceKind.LOCAL_DIRECTORY,
                relative_locator="plugin.video.example",
                addon_id="plugin.video.example",
                observed_name="Example",
                observed_version="1.0.0",
                extension_types_json="[]",
                registered_at=now,
                validated_at=now,
            )
        )
        session.flush()
        session.add(
            AgentWorkspaceRow(
                id=workspace_id,
                development_source_id=source_id,
                source_addon_id="plugin.video.example",
                source_logical_name="Example",
                source_observed_name="Example",
                source_observed_version="1.0.0",
                state=AgentWorkspaceState.READY,
                baseline_artifact_id=str(uuid4()),
                baseline_manifest_digest="sha256:" + "b" * 64,
                current_tree_digest="sha256:" + "b" * 64,
                capture_started_at=now,
                captured_at=now,
                created_at=now,
                updated_at=now,
            )
        )
        session.flush()
        session.add(
            DevelopmentSessionRow(
                generation=1,
                request_id=str(uuid4()),
                target_id=str(env.op.target_id),
                runtime_revision_id=str(env.op.runtime_revision_id),
                workspace_id=workspace_id,
                source_id=source_id,
                container_id=env.op.container_id,
                phase="reserved" if incomplete else "bound",
                created_at=now,
                updated_at=now,
            )
        )
        session.flush()
        policy = session.get(DevelopmentSessionPolicyRow, 1)
        assert policy
        policy.active_generation = 1
