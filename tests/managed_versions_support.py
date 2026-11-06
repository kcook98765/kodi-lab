"""Approved catalog, real SQLite authority/lifecycle/admission, fixture-only runtime."""

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.managed_agent_lifecycle import ManagedAgentLifecycleService
from kodi_lab.application.managed_resources import ManagedResourceService
from kodi_lab.application.managed_versions import (
    ManagedVersionLifecycleService,
    Version,
)
from kodi_lab.application.mcp_administration import (
    FULL_LAB_CONTROL_PRESET_ID,
    McpAdministrationService,
    McpAuthenticationService,
    McpPrincipal,
)
from kodi_lab.application.mcp_workspace_development import BoundedIntervalRateLimiter
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
from kodi_lab.broker.operations import (
    DeprovisionManagedContainerResult,
    ProvisionManagedContainerResult,
)
from kodi_lab.config import Settings
from kodi_lab.domain import RuntimeExecutionProfile
from kodi_lab.kodi.readiness import KodiApplicationIdentity, ReadinessResult
from kodi_lab.mcp.authorization import McpAuthorizationService
from kodi_lab.mcp.registry import build_mcp_tool_registry
from kodi_lab.storage.models import (
    ManagedCleanResetRow,
    ManagedInstanceMutationLeaseRow,
    ManagedInstanceRow,
    ManagedVersionOperationRow,
    RuntimeContainerArtifactRow,
)
from kodi_lab.storage.repositories import TargetRepository
from tests.application.test_managed_start import Broker as NativeBroker
from tests.application.test_managed_start import NativeReady
from tests.application.test_runtime_installations import (
    FakeBroker as InstallationBroker,
)
from tests.runtime.test_recipe_installation import _ready
from tests.storage.test_repositories import make_target


class ProcessLoss(BaseException):
    pass


def readiness(_broker: object, profile: RuntimeExecutionProfile) -> Any:
    release = profile.kodi_release
    value = replace(
        NativeReady().wait_until_usable(),
        identity=KodiApplicationIdentity(
            "Kodi",
            release.major,
            release.minor,
            release.channel.value,
            release.prerelease_ordinal,
        ),
    )

    def wait(*_args: object, **_kwargs: object) -> ReadinessResult:
        return value

    return SimpleNamespace(wait_until_usable=wait)


class Broker(NativeBroker, InstallationBroker):
    def __init__(self, factory: sessionmaker[Session], root: Path) -> None:
        self.factory, self.root = factory, root
        NativeBroker.__init__(
            self,
            factory,
            ManagedContainerRecord(
                str(uuid4()),
                "a" * 64,
                "sha256:" + "b" * 64,
                ContainerRuntimeState.STOPPED,
                ContainerHealthState.HEALTHY,
                str(uuid4()),
            ),
        )
        InstallationBroker.__init__(self)
        self.records = []
        self.worker_state = "missing"
        self.provisions = 0
        self.removals = 0
        self.fault = ""
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.remote_error: BaseException | None = None
        self.playback_active: bool | None = False
        self.snapshot_data_root: Path | None = None

    def deprovision_managed_container(
        self, **kwargs: Any
    ) -> DeprovisionManagedContainerResult:
        target = kwargs["managed_instance_id"]
        record = next(r for r in self.records if r.managed_instance_id == target)
        assert record.container_id == kwargs["expected_container_id"]
        assert record.runtime_revision_id == kwargs["expected_runtime_revision_id"]
        assert record.runtime_state == kwargs["expected_runtime_state"] == "stopped"
        self.removals += 1
        self.records.remove(record)
        profile = self.root / target / "settings.xml"
        profile.unlink()
        profile.parent.rmdir()
        if self.fault == "lost_remove_response":
            raise ClientTransportError("fake lost removal reply")
        return DeprovisionManagedContainerResult(
            managed_instance_id=target,
            runtime_revision_id=str(record.runtime_revision_id),
            removed_container_id=record.container_id,
            removed=True,
        )

    def provision_managed_container(
        self, **kwargs: Any
    ) -> ProvisionManagedContainerResult:
        managed_instance_id = str(kwargs["managed_instance_id"])
        with self.factory() as session:
            lease = session.get(ManagedInstanceMutationLeaseRow, managed_instance_id)
            instance = session.get(ManagedInstanceRow, managed_instance_id)
            assert lease is not None and instance is not None
            phase = (
                session.query(ManagedVersionOperationRow.phase)
                .filter_by(provision_operation_id=lease.operation_id)
                .scalar()
            )
            if phase is None:
                phase = (
                    session.query(ManagedCleanResetRow.phase)
                    .filter_by(provision_id=lease.operation_id)
                    .scalar()
                )
            assert phase == "provisioning"
            artifact = session.get(
                RuntimeContainerArtifactRow, instance.runtime_revision_id
            )
            assert artifact is not None
            record = ManagedContainerRecord(
                managed_instance_id,
                uuid4().hex * 2,
                artifact.image_id,
                ContainerRuntimeState.STOPPED,
                ContainerHealthState.HEALTHY,
                instance.runtime_revision_id,
            )
        if self.fault == "process_loss":
            raise ProcessLoss()

        def dispatch() -> ProvisionedManagedContainerRecord:
            if self.fault == "safe_provision_failure":
                from kodi_lab.broker.errors import RuntimeImageUnavailableError

                raise RuntimeImageUnavailableError(
                    "fake image unavailable before create"
                )
            self.provisions += 1
            self.records.append(record)
            profile = self.root / managed_instance_id / "settings.xml"
            profile.parent.mkdir(parents=True)
            profile.write_text("clean fixture Kodi profile\n")
            if self.fault == "uncertain_provision":
                raise ClientTransportError("fake lost Docker reply")
            return ProvisionedManagedContainerRecord(
                record.managed_instance_id,
                str(record.runtime_revision_id),
                record.container_id,
                record.image_id,
                record.runtime_state,
                True,
            )

        result = self.admission.run_provision(
            instance_id=managed_instance_id,
            revision_id=str(record.runtime_revision_id),
            dispatch=dispatch,
        )
        if self.fault == "lost_provision_response":
            raise ClientTransportError("fake lost broker reply")
        return ProvisionManagedContainerResult(
            **{
                key: getattr(result, key)
                for key in ProvisionManagedContainerResult.model_fields
            }
        )


class Environment:
    def __init__(self, factory: sessionmaker[Session], root: Path) -> None:
        self.factory, self.root = factory, root
        self.broker = Broker(factory, root / "profiles")
        self.tasks = ReadinessTaskRegistry()
        self.status = ManagedRuntimeStatusService(factory, self.broker, self.tasks)
        self.lifecycle = ManagedRuntimeLifecycleService(
            factory,
            self.broker,
            self.tasks,
            self.status,
            coordinator_factory=readiness,
        )
        self.resources = ManagedResourceService(
            factory,
            self.broker,
            self.lifecycle,
            playback_factory=lambda *_: SimpleNamespace(playback_active=lambda: False),
        )
        self.agent = ManagedAgentLifecycleService(
            factory,
            self.resources,
            self.lifecycle,
            self.status,
            lock_root=root / "locks",
            rate_limiter=BoundedIntervalRateLimiter(0),
        )
        self.installer = RuntimeInstallationService(
            factory, self.broker, data_root=Settings().data_root
        )
        self.service = self.new_service()
        self.admin = McpAdministrationService(factory)
        self.authorization = McpAuthorizationService(factory, build_mcp_tool_registry())
        self.preset = self.admin.create_custom_preset(
            display_name="Version lifecycle fixture",
            template_preset_id=FULL_LAB_CONTROL_PRESET_ID,
            permissions=frozenset({"managed.versions"}),
        )
        self.principal = self.actor("Version actor")

    def new_service(self) -> ManagedVersionLifecycleService:
        return ManagedVersionLifecycleService(
            self.factory,
            self.lifecycle,
            self.resources,
            self.status,
            self.installer,
            self.agent,
            self.broker.managed_container_inventory,
            data_root=Settings().data_root,
            rate_limiter=BoundedIntervalRateLimiter(0),
        )

    def actor(self, name: str) -> McpPrincipal:
        client = self.admin.create_client(display_name=name, preset_id=self.preset.id)
        credential = self.admin.issue_credential(client.id)
        principal = McpAuthenticationService(self.factory).authenticate(
            credential.token
        )
        assert principal is not None
        return principal

    def prepare(
        self, version: Version = "omega", *, grant: bool = True, cached: bool = True
    ) -> UUID:
        slot = self.service.slot(version)
        if cached:
            if version == "piers":
                from tests.runtime.test_ostree_installation import ready

                ready(self.factory)
            else:
                _ready(self.factory, generation=version)
        with self.factory.begin() as session:
            TargetRepository(session).create(
                replace(
                    make_target(version + " Lab"),
                    id=slot.canonical_target_id,
                    kodi_version=slot.kodi_version,
                )
            )
        self.admin.assign_target_preset(slot.canonical_target_id, self.preset.id)
        if grant:
            self.admin.grant_target(self.principal.client_id, slot.canonical_target_id)
        return slot.canonical_target_id

    def authorize(self, session: Session, target: UUID) -> None:
        self.authorization.recheck_managed_versions(
            session, self.principal, "managed_start", target
        )

    def invoke(
        self,
        action: Any = "start",
        version: Version = "omega",
        request_id: UUID | None = None,
    ) -> Any:
        return self.service.invoke(
            client_id=self.principal.client_id,
            request_id=request_id or uuid4(),
            version=version,
            action=action,
            authorize=lambda session, target: (
                self.authorization.recheck_managed_versions(
                    session, self.principal, "managed_" + action, target
                )
            ),
        )

    def wait(self, target: UUID) -> None:
        with self.tasks._lock:
            future = self.tasks._entries[target].future
        if future is not None:
            future.result(timeout=5)

    def close(self) -> None:
        self.tasks.close()
