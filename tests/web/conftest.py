from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.readiness_tasks import ReadinessTaskRegistry
from kodi_lab.auth.service import WebAuthenticationService
from kodi_lab.broker.docker_inventory import (
    ContainerHealthState,
    ContainerRuntimeState,
    KodiProcessState,
)
from kodi_lab.broker.operations import (
    DeprovisionManagedContainerResult,
    ManagedContainerInventoryEntry,
    ManagedContainerInventoryResult,
    ObserveManagedKodiRuntimeResult,
    ProvisionManagedContainerResult,
    StartManagedContainerResult,
    StopManagedContainerResult,
)
from kodi_lab.config import Settings
from kodi_lab.domain import RuntimeExecutionProfile
from kodi_lab.kodi.readiness import (
    JsonRpcState,
    KodiApplicationIdentity,
    ReadinessResult,
    UsabilityState,
)
from kodi_lab.storage.engine import create_session_factory
from kodi_lab.storage.metadata import Base
from kodi_lab.storage.models import (
    ManagedInstanceRow,
    RuntimeDefinitionRow,
    RuntimeExecutionProfileRow,
    RuntimeRevisionRow,
    TargetRow,
)
from kodi_lab.web.app import create_app

INSTANCE_ID = UUID("00000000-0000-4000-8000-000000000101")
DEFINITION_ID = UUID("00000000-0000-4000-8000-000000000102")
REVISION_ID = UUID("00000000-0000-4000-8000-000000000103")
CONTAINER_ID = "a" * 64
IMAGE_ID = "sha256:" + ("b" * 64)
PASSWORD = "correct horse battery staple"
ORIGIN = {"Origin": "http://127.0.0.1"}


@dataclass
class FakeBroker:
    container: ManagedContainerInventoryEntry | None = None
    process_state: KodiProcessState = KodiProcessState.RUNNING
    calls: list[tuple[str, dict[str, object]]] = field(default_factory=list)
    inventory_error: BaseException | None = None

    def managed_container_inventory(self) -> ManagedContainerInventoryResult:
        if self.inventory_error is not None:
            raise self.inventory_error
        return ManagedContainerInventoryResult(
            containers=[] if self.container is None else [self.container]
        )

    def observe_managed_kodi_runtime(
        self, *, managed_instance_id: str, expected_container_id: str
    ) -> ObserveManagedKodiRuntimeResult:
        assert self.container is not None
        assert expected_container_id == self.container.container_id
        return ObserveManagedKodiRuntimeResult(
            managed_instance_id=managed_instance_id,
            runtime_revision_id=str(REVISION_ID),
            container_id=expected_container_id,
            runtime_state=ContainerRuntimeState.RUNNING,
            process_state=self.process_state,
            ipv4_address="10.250.0.10",
            jsonrpc_port=8080,
        )

    def provision_managed_container(
        self, *, managed_instance_id: str
    ) -> ProvisionManagedContainerResult:
        self.calls.append(("provision", {"managed_instance_id": managed_instance_id}))
        self.container = inventory_entry(ContainerRuntimeState.STOPPED)
        return ProvisionManagedContainerResult(
            managed_instance_id=managed_instance_id,
            runtime_revision_id=str(REVISION_ID),
            container_id=CONTAINER_ID,
            image_id=IMAGE_ID,
            runtime_state=ContainerRuntimeState.STOPPED,
            created=True,
        )

    def start_managed_container(
        self,
        *,
        managed_instance_id: str,
        expected_container_id: str,
        expected_runtime_state: ContainerRuntimeState,
    ) -> StartManagedContainerResult:
        self.calls.append(
            (
                "start",
                {
                    "managed_instance_id": managed_instance_id,
                    "expected_container_id": expected_container_id,
                    "expected_runtime_state": expected_runtime_state,
                },
            )
        )
        self.container = inventory_entry(ContainerRuntimeState.RUNNING)
        return StartManagedContainerResult(
            managed_instance_id=managed_instance_id,
            container_id=expected_container_id,
            runtime_state=ContainerRuntimeState.RUNNING,
            start_performed=True,
        )

    def stop_managed_container(
        self,
        *,
        managed_instance_id: str,
        expected_container_id: str,
        expected_runtime_state: ContainerRuntimeState,
    ) -> StopManagedContainerResult:
        self.calls.append(
            (
                "stop",
                {
                    "managed_instance_id": managed_instance_id,
                    "expected_container_id": expected_container_id,
                    "expected_runtime_state": expected_runtime_state,
                },
            )
        )
        self.container = inventory_entry(ContainerRuntimeState.STOPPED)
        return StopManagedContainerResult(
            managed_instance_id=managed_instance_id,
            container_id=expected_container_id,
            runtime_state=ContainerRuntimeState.STOPPED,
            stop_performed=True,
        )

    def deprovision_managed_container(
        self,
        *,
        managed_instance_id: str,
        expected_container_id: str,
        expected_runtime_revision_id: str,
        expected_runtime_state: Literal[ContainerRuntimeState.STOPPED],
    ) -> DeprovisionManagedContainerResult:
        self.calls.append(
            (
                "deprovision",
                {
                    "managed_instance_id": managed_instance_id,
                    "expected_container_id": expected_container_id,
                    "expected_runtime_revision_id": expected_runtime_revision_id,
                    "expected_runtime_state": expected_runtime_state,
                },
            )
        )
        self.container = None
        return DeprovisionManagedContainerResult(
            managed_instance_id=managed_instance_id,
            removed_container_id=expected_container_id,
            runtime_revision_id=expected_runtime_revision_id,
            removed=True,
        )


def inventory_entry(state: ContainerRuntimeState) -> ManagedContainerInventoryEntry:
    return ManagedContainerInventoryEntry(
        managed_instance_id=str(INSTANCE_ID),
        container_id=CONTAINER_ID,
        image_id=IMAGE_ID,
        runtime_state=state,
        health_state=(
            ContainerHealthState.HEALTHY
            if state is ContainerRuntimeState.RUNNING
            else ContainerHealthState.NOT_CONFIGURED
        ),
    )


class ImmediateCoordinator:
    def wait_until_usable(
        self, managed_instance_id: str, **_kwargs: object
    ) -> ReadinessResult:
        assert managed_instance_id == str(INSTANCE_ID)
        return ReadinessResult(
            container_state=ContainerRuntimeState.RUNNING,
            process_state=KodiProcessState.RUNNING,
            jsonrpc_state=JsonRpcState.RESPONSIVE,
            usability_state=UsabilityState.USABLE,
            failure_reason=None,
            last_observed_failure=None,
            identity=KodiApplicationIdentity("Kodi", 21, 3, "stable"),
            elapsed_seconds=0.1,
            attempts=1,
        )


def coordinator_factory(
    _broker: object, _profile: RuntimeExecutionProfile
) -> ImmediateCoordinator:
    return ImmediateCoordinator()


@pytest.fixture
def database(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_engine(f"sqlite:///{tmp_path / 'web.db'}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    seed_instance(factory)
    yield factory
    engine.dispose()


def seed_instance(factory: sessionmaker[Session]) -> None:
    digest = "sha256:" + ("c" * 64)
    with factory.begin() as session:
        session.add(
            RuntimeDefinitionRow(
                id=str(DEFINITION_ID), slug="kodi-21.3", kodi_version="21.3"
            )
        )
        session.add(
            RuntimeRevisionRow(
                id=str(REVISION_ID),
                runtime_definition_id=str(DEFINITION_ID),
                revision_number=2,
                source_kind="official_package",
                source_reference="test",
                content_digest=digest,
                manifest_reference="runtime/kodi-21.3/manifest.json",
                manifest_digest=digest,
                built_at="2026-09-15T00:00:00.000000Z",
                build_log_reference=None,
                approval_state="approved",
                approved_at="2026-09-15T00:00:00.000000Z",
                approval_note=None,
                requires_trusted_build_confirmation=False,
                trusted_build_confirmed_at=None,
            )
        )
        session.add(
            TargetRow(
                id=str(INSTANCE_ID),
                name="Kodi <script>not markup</script>",
                target_type="managed",
                kodi_version="21.3",
                availability="available",
                health="healthy",
                connection_state="connected",
            )
        )
        session.add(
            ManagedInstanceRow(
                target_id=str(INSTANCE_ID),
                instance_type="disposable",
                runtime_revision_id=str(REVISION_ID),
                runtime_state="stopped",
                start_on_host_boot=False,
                notes=None,
            )
        )
        session.add(
            RuntimeExecutionProfileRow(
                runtime_revision_id=str(REVISION_ID),
                runtime_kind="kodi",
                expected_application_name="Kodi",
                expected_kodi_version="21.3",
                expected_version_tag="stable",
                jsonrpc_port=8080,
                jsonrpc_path="/jsonrpc",
                process_observation="docker_healthcheck",
                readiness_probe="kodi_jsonrpc",
                readiness_timeout_seconds=90,
                poll_interval_seconds=1.0,
                http_timeout_seconds=3.0,
            )
        )


@pytest.fixture
def broker() -> FakeBroker:
    return FakeBroker()


@pytest.fixture
def client(database: sessionmaker[Session], broker: FakeBroker) -> Iterator[TestClient]:
    auth = WebAuthenticationService(database)
    auth.set_password(PASSWORD)
    app = create_app(
        settings=Settings(),
        session_factory=database,
        broker=broker,
        readiness=ReadinessTaskRegistry(max_workers=1),
        auth=auth,
        coordinator_factory=coordinator_factory,
    )
    with TestClient(app, base_url="http://127.0.0.1") as test_client:
        yield test_client


def login(client: TestClient) -> str:
    response = client.post(
        "/login",
        data={"password": PASSWORD},
        headers=ORIGIN,
        follow_redirects=False,
    )
    assert response.status_code == 303
    page = client.get("/")
    assert page.status_code == 200
    match = re.search(r'name="kodi-lab-csrf" content="([^"]+)"', page.text)
    assert match is not None
    return match.group(1)
