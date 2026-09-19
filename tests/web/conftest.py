from __future__ import annotations

import re
import struct
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from uuid import UUID

import pytest
from argon2 import PasswordHasher as Argon2PasswordHasher
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.browser_remote import BrowserRemoteRateLimiter
from kodi_lab.application.readiness_tasks import ReadinessTaskRegistry
from kodi_lab.auth import passwords as password_module
from kodi_lab.auth.service import WebAuthenticationService
from kodi_lab.broker.docker_inventory import (
    ContainerHealthState,
    ContainerRuntimeState,
    KodiProcessState,
)
from kodi_lab.broker.operations import (
    CaptureManagedKodiScreenshotResult,
    CopySyncAttachedDevelopmentSourceResult,
    DeleteManagedSnapshotArtifactResult,
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
from kodi_lab.kodi.addons import KodiAddonInventoryResult
from kodi_lab.kodi.readiness import (
    JsonRpcState,
    KodiApplicationIdentity,
    ReadinessResult,
    UsabilityState,
)
from kodi_lab.kodi.remote import BrowserRemoteAction
from kodi_lab.kodi.screenshots import X11_FRAME_MAGIC
from kodi_lab.snapshot.storage import delete_snapshot_artifact
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
DEFINITION_ID = UUID("3a000000-0000-4000-8000-000000000001")
REVISION_ID = UUID("3a000000-0000-4000-8000-000000000006")
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
    snapshot_data_root: Path | None = None
    addon_inventory_result: KodiAddonInventoryResult = field(
        default_factory=lambda: KodiAddonInventoryResult(
            identity=KodiApplicationIdentity("Kodi", 21, 3, "stable"), items=()
        )
    )
    addon_inventory_error: BaseException | None = None
    screenshot_error: BaseException | None = None
    screenshot_calls: int = 0
    remote_calls: list[BrowserRemoteAction] = field(default_factory=list)
    remote_error: BaseException | None = None

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

    def capture_managed_kodi_screenshot(
        self,
        *,
        managed_instance_id: str,
        expected_container_id: str,
        expected_runtime_revision_id: str,
        capture_id: str,
    ) -> CaptureManagedKodiScreenshotResult:
        self.screenshot_calls += 1
        if self.screenshot_error is not None:
            raise self.screenshot_error
        assert self.snapshot_data_root is not None
        assert managed_instance_id == str(INSTANCE_ID)
        assert expected_container_id == CONTAINER_ID
        assert expected_runtime_revision_id == str(REVISION_ID)
        raw = struct.pack(
            ">8s12I",
            X11_FRAME_MAGIC,
            1280,
            720,
            24,
            32,
            0,
            32,
            1280 * 4,
            4,
            8,
            0x00FF0000,
            0x0000FF00,
            0x000000FF,
        ) + (bytes((0x33, 0x22, 0x11, 0)) * 1280 * 720)
        path = (
            self.snapshot_data_root
            / "screenshots"
            / "staging"
            / capture_id
            / "frame.raw"
        )
        path.write_bytes(raw)
        return CaptureManagedKodiScreenshotResult(
            managed_instance_id=managed_instance_id,
            runtime_revision_id=expected_runtime_revision_id,
            container_id=expected_container_id,
            capture_id=capture_id,
            backend="x11_get_image",
            raw_byte_size=len(raw),
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

    def delete_managed_snapshot_artifact(
        self, *, snapshot_id: str
    ) -> DeleteManagedSnapshotArtifactResult:
        assert self.snapshot_data_root is not None
        snapshots_root = self.snapshot_data_root / "snapshots"
        stat_result = snapshots_root.stat()
        delete_snapshot_artifact(
            data_root=self.snapshot_data_root,
            snapshot_id=UUID(snapshot_id),
            owner_uid=stat_result.st_uid,
            owner_gid=stat_result.st_gid,
        )
        self.calls.append(("delete_snapshot", {"snapshot_id": snapshot_id}))
        return DeleteManagedSnapshotArtifactResult(
            snapshot_id=snapshot_id,
            artifact_absent=True,
            artifact_was_present=True,
        )

    def copy_sync_attached_development_source(
        self,
        *,
        managed_instance_id: str,
        development_source_id: str,
        expected_container_id: str,
        deployment_operation_id: str,
    ) -> CopySyncAttachedDevelopmentSourceResult:
        self.calls.append(
            (
                "copy_sync_development_source",
                {
                    "managed_instance_id": managed_instance_id,
                    "development_source_id": development_source_id,
                    "expected_container_id": expected_container_id,
                    "deployment_operation_id": deployment_operation_id,
                },
            )
        )
        return CopySyncAttachedDevelopmentSourceResult(
            managed_instance_id=managed_instance_id,
            development_source_id=development_source_id,
            deployment_operation_id=deployment_operation_id,
            runtime_revision_id=str(REVISION_ID),
            container_id=expected_container_id,
            result_classification="initial_deployment",
            source_manifest_digest="sha256:" + "1" * 64,
            verified_destination_digest="sha256:" + "2" * 64,
            observed_source_name="Alpha Video",
            observed_source_version="1.2.3",
            new_count=1,
            changed_count=0,
            unchanged_count=0,
            destination_only_count=0,
            transferred_bytes=10,
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


class FakeRemoteAdapter:
    def __init__(self, broker: FakeBroker) -> None:
        self._broker = broker

    def dispatch(self, action: BrowserRemoteAction, _invocation_id: UUID) -> None:
        self._broker.remote_calls.append(action)
        if self._broker.remote_error is not None:
            raise self._broker.remote_error


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


class FakeAddonInventoryAdapter:
    def __init__(self, broker: FakeBroker) -> None:
        self._broker = broker

    def get_inventory(self) -> KodiAddonInventoryResult:
        if self._broker.addon_inventory_error is not None:
            raise self._broker.addon_inventory_error
        return self._broker.addon_inventory_result


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
                runtime_uid=1000,
                runtime_gid=1000,
                python_bytecode_policy="interpreter_default",
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
def data_root(tmp_path: Path) -> Path:
    return tmp_path / "web-data"


@pytest.fixture
def broker(data_root: Path) -> FakeBroker:
    return FakeBroker(snapshot_data_root=data_root)


@pytest.fixture
def client(
    database: sessionmaker[Session],
    broker: FakeBroker,
    data_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    development_root = data_root.parent / "development-root"
    development_root.mkdir(parents=True)
    # Web route tests exercise authentication semantics, not Argon2 cost tuning.
    # Dedicated auth tests continue to instantiate the production-strength KDF.
    monkeypatch.setattr(
        password_module,
        "PasswordHasher",
        lambda **kwargs: Argon2PasswordHasher(
            time_cost=1,
            memory_cost=8,
            parallelism=1,
            hash_len=16,
            salt_len=8,
            **kwargs,
        ),
    )
    auth = WebAuthenticationService(database)
    auth.set_password(PASSWORD)
    app = create_app(
        settings=Settings(
            data_root=data_root,
            development_root=development_root,
        ),
        session_factory=database,
        broker=broker,
        readiness=ReadinessTaskRegistry(max_workers=1),
        auth=auth,
        coordinator_factory=coordinator_factory,
        addon_inventory_adapter_factory=(
            lambda _observation, _profile: FakeAddonInventoryAdapter(broker)
        ),
        browser_remote_adapter_factory=(
            lambda _observation, _profile: FakeRemoteAdapter(broker)
        ),
        browser_remote_rate_limiter=BrowserRemoteRateLimiter(clock=lambda: 100.0),
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
