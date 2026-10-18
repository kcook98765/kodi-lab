from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, delete, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.development_source_smoke_tests import (
    DevelopmentSourceSmokeTestConflictError,
    DevelopmentSourceSmokeTestOperationCoordinator,
    DevelopmentSourceSmokeTestService,
    DevelopmentSourceSmokeTestStatus,
)
from kodi_lab.application.reconciliation import (
    ManagedInstanceReconciliationRequiredError,
    ManagedInstanceReconciliationService,
    ReconciliationAction,
    StaleOperationReconciliationDispatcher,
)
from kodi_lab.broker.docker_inventory import (
    ContainerHealthState,
    ContainerRuntimeState,
    KodiProcessState,
)
from kodi_lab.broker.errors import DevelopmentSourceDeploymentClientError
from kodi_lab.broker.operations import (
    ManagedContainerInventoryEntry,
    ManagedContainerInventoryResult,
    ObserveManagedKodiRuntimeResult,
    VerifyAttachedDevelopmentSourceDestinationResult,
)
from kodi_lab.development import DevelopmentSourceValidator
from kodi_lab.domain import (
    DevelopmentSourceDeploymentConfidence,
    DevelopmentSourceSmokeTestExecutionResult,
    DevelopmentSourceSmokeTestOperationPhase,
    DevelopmentSourceSmokeTestOperationState,
    DevelopmentSourceSmokeTestOverallResult,
    DevelopmentSourceSmokeTestPostProvenance,
    ManagedInstanceMutationKind,
)
from kodi_lab.kodi.addons import (
    KodiAddonDetailsRecord,
    KodiAddonInventoryRecord,
    KodiAddonInventoryResult,
    KodiAddonSmokeJsonRpcError,
    KodiAddonSmokeResult,
    KodiAddonSmokeTimeoutError,
    KodiReportedAddonDependency,
)
from kodi_lab.kodi.readiness import KodiApplicationIdentity
from kodi_lab.storage.engine import create_session_factory
from kodi_lab.storage.metadata import Base
from kodi_lab.storage.models import (
    DevelopmentSourceDeploymentOperationRow,
    DevelopmentSourceDeploymentRow,
    DevelopmentSourceRow,
    DevelopmentSourceSmokeTestOperationRow,
    ManagedInstanceDevelopmentSourceRow,
    ManagedInstanceMutationLeaseRow,
    ManagedInstanceRow,
    RuntimeDefinitionRow,
    RuntimeExecutionProfileRow,
    RuntimeRevisionRow,
    TargetRow,
)
from kodi_lab.storage.repositories import (
    DevelopmentSourceDeploymentRepository,
    DevelopmentSourceSmokeTestOperationRepository,
    GlobalHeavyOperationLeaseRepository,
    ManagedInstanceMutationLeaseRepository,
)

INSTANCE_ID = UUID("a8100000-0000-4000-8000-000000000001")
SOURCE_ID = UUID("a8100000-0000-4000-8000-000000000002")
REVISION_ID = UUID("a8100000-0000-4000-8000-000000000003")
DEFINITION_ID = UUID("a8100000-0000-4000-8000-000000000004")
COPY_OPERATION_ID = UUID("a8100000-0000-4000-8000-000000000005")
CONTAINER_ID = "a" * 64
NOW = datetime(2026, 9, 17, 16, 0, tzinfo=UTC)
ADDON_ID = "plugin.video.example"
VERSION = "1.2.3"


@pytest.fixture
def factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_engine(f"sqlite:///{tmp_path / 'smoke.db'}")
    with create_session_factory(engine).begin() as session:
        Base.metadata.create_all(engine)
        session.add(
            RuntimeDefinitionRow(
                id=str(DEFINITION_ID), slug="kodi-21.3", kodi_version="21.3"
            )
        )
        session.add(
            RuntimeRevisionRow(
                id=str(REVISION_ID),
                runtime_definition_id=str(DEFINITION_ID),
                revision_number=1,
                source_kind="official_package",
                source_reference="test",
                content_digest="sha256:" + "1" * 64,
                manifest_reference="test",
                manifest_digest="sha256:" + "2" * 64,
                built_at=NOW.isoformat(),
                approval_state="approved",
                approved_at=NOW.isoformat(),
                requires_trusted_build_confirmation=False,
            )
        )
        session.add(
            RuntimeExecutionProfileRow(
                runtime_revision_id=str(REVISION_ID),
                runtime_kind="kodi",
                runtime_uid=1000,
                runtime_gid=1000,
                python_bytecode_policy="suppress",
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
        session.add(
            TargetRow(
                id=str(INSTANCE_ID),
                name="smoke instance",
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
                runtime_state="running",
                start_on_host_boot=False,
            )
        )
        session.add(
            DevelopmentSourceRow(
                id=str(SOURCE_ID),
                logical_name="Example source",
                source_kind="local_directory",
                relative_locator="source",
                addon_id=ADDON_ID,
                observed_name="Example",
                observed_version=VERSION,
                extension_types_json='["xbmc.python.pluginsource"]',
                registered_at=NOW.isoformat(),
                validated_at=NOW.isoformat(),
            )
        )
        session.add(
            ManagedInstanceDevelopmentSourceRow(
                managed_instance_id=str(INSTANCE_ID),
                development_source_id=str(SOURCE_ID),
                attached_at=NOW.isoformat(),
            )
        )
        session.add(
            DevelopmentSourceDeploymentOperationRow(
                id=str(COPY_OPERATION_ID),
                original_managed_instance_id=str(INSTANCE_ID),
                managed_instance_id=str(INSTANCE_ID),
                development_source_id=str(SOURCE_ID),
                source_logical_name="Example source",
                addon_id=ADDON_ID,
                runtime_revision_id=str(REVISION_ID),
                request_kind="copy_sync",
                requested_at=NOW.isoformat(),
                state="succeeded",
                phase="finalize",
                result_classification="initial_deployment",
                new_count=2,
                changed_count=0,
                unchanged_count=0,
                destination_only_count=0,
                transferred_bytes=64,
                expected_container_id=CONTAINER_ID,
                started_at=NOW.isoformat(),
                heartbeat_at=NOW.isoformat(),
                completed_at=NOW.isoformat(),
                reconciliation_required=False,
            )
        )
        session.add(
            DevelopmentSourceDeploymentRow(
                managed_instance_id=str(INSTANCE_ID),
                development_source_id=str(SOURCE_ID),
                addon_id=ADDON_ID,
                mode="copy",
                last_successful_operation_id=str(COPY_OPERATION_ID),
                source_manifest_digest="sha256:" + "3" * 64,
                verified_destination_digest="sha256:" + "4" * 64,
                runtime_revision_id=str(REVISION_ID),
                trusted_container_id=CONTAINER_ID,
                observed_source_name="Example",
                observed_source_version=VERSION,
                verified_at=NOW.isoformat(),
                confidence="verified_at_last_operation",
            )
        )
    source = tmp_path / "source"
    source.mkdir()
    write_descriptor(source)
    yield create_session_factory(engine)
    engine.dispose()


def write_descriptor(source: Path, dependency_xml: str = "") -> None:
    (source / "addon.xml").write_text(
        f'<addon id="{ADDON_ID}" name="Example" version="{VERSION}">'
        f"{dependency_xml}"
        '<extension point="xbmc.python.pluginsource" library="main.py">'
        "<provides>video</provides></extension>"
        '<extension point="xbmc.addon.metadata"/>'
        "</addon>",
        encoding="utf-8",
    )
    (source / "main.py").write_text("pass\n", encoding="utf-8")


class FakeBroker:
    def __init__(self) -> None:
        self.runtime_state = ContainerRuntimeState.RUNNING
        self.health_state = ContainerHealthState.HEALTHY
        self.process_state = KodiProcessState.RUNNING
        self.observed_revision = str(REVISION_ID)
        self.verify_outcomes: list[str] = ["match", "match"]
        self.verify_calls = 0

    def managed_container_inventory(self) -> ManagedContainerInventoryResult:
        return ManagedContainerInventoryResult(
            containers=[
                ManagedContainerInventoryEntry(
                    managed_instance_id=str(INSTANCE_ID),
                    container_id=CONTAINER_ID,
                    image_id="sha256:" + "5" * 64,
                    runtime_state=self.runtime_state,
                    health_state=self.health_state,
                )
            ]
        )

    def observe_managed_kodi_runtime(
        self, *, managed_instance_id: str, expected_container_id: str
    ) -> ObserveManagedKodiRuntimeResult:
        return ObserveManagedKodiRuntimeResult(
            managed_instance_id=managed_instance_id,
            runtime_revision_id=self.observed_revision,
            container_id=expected_container_id,
            runtime_state=self.runtime_state,
            process_state=self.process_state,
            ipv4_address="10.250.0.8",
            jsonrpc_port=8080,
        )

    def verify_attached_development_source_destination(
        self,
        *,
        managed_instance_id: str,
        development_source_id: str,
        expected_container_id: str,
    ) -> VerifyAttachedDevelopmentSourceDestinationResult:
        self.verify_calls += 1
        outcome = self.verify_outcomes.pop(0)
        if outcome != "match":
            code = (
                "development_destination_mismatch"
                if outcome == "mismatch"
                else "development_destination_verification_failed"
            )
            raise DevelopmentSourceDeploymentClientError(
                code, "bounded verifier failure", request_id="test"
            )
        return VerifyAttachedDevelopmentSourceDestinationResult(
            managed_instance_id=managed_instance_id,
            development_source_id=development_source_id,
            runtime_revision_id=str(REVISION_ID),
            container_id=expected_container_id,
            addon_id=ADDON_ID,
            addon_version=VERSION,
            matched=True,
            video_plugin_descriptor_eligible=True,
        )


class FakeInventory:
    def __init__(self, items: list[KodiAddonInventoryRecord]) -> None:
        self.items = items

    def get_inventory(self) -> KodiAddonInventoryResult:
        return KodiAddonInventoryResult(
            identity=KodiApplicationIdentity("Kodi", 21, 3, "stable"),
            items=tuple(self.items),
        )


class FakeDetails:
    def __init__(self) -> None:
        self.enabled = True
        self.version = VERSION
        self.kodi_type = "xbmc.python.pluginsource"
        self.dependencies: tuple[KodiReportedAddonDependency, ...] = ()

    def get_addon_details(self, addon_id: str) -> KodiAddonDetailsRecord:
        assert addon_id == ADDON_ID
        return KodiAddonDetailsRecord(
            addon_id=ADDON_ID,
            name="Example",
            version=self.version,
            enabled=self.enabled,
            kodi_type=self.kodi_type,
            dependencies=self.dependencies,
        )


class FakeSmoke:
    def __init__(self, outcome: KodiAddonSmokeResult | BaseException) -> None:
        self.outcome = outcome
        self.calls: list[str] = []

    def invoke(self, addon_id: str) -> KodiAddonSmokeResult:
        self.calls.append(addon_id)
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


def target(*, enabled: bool = True, version: str = VERSION) -> KodiAddonInventoryRecord:
    return KodiAddonInventoryRecord(
        addon_id=ADDON_ID,
        name="Example",
        version=version,
        enabled=enabled,
        kodi_type="xbmc.python.pluginsource",
    )


def dependency(
    dependency_id: str, *, enabled: bool, version: str = "1.0.0"
) -> KodiAddonInventoryRecord:
    return KodiAddonInventoryRecord(
        addon_id=dependency_id,
        name=dependency_id,
        version=version,
        enabled=enabled,
        kodi_type="xbmc.python.module",
    )


def run_smoke(
    factory: sessionmaker[Session],
    tmp_path: Path,
    broker: FakeBroker,
    smoke: FakeSmoke,
    *,
    inventory: list[KodiAddonInventoryRecord] | None = None,
    details: FakeDetails | None = None,
) -> tuple[
    DevelopmentSourceSmokeTestStatus, DevelopmentSourceSmokeTestOperationCoordinator
]:
    reconciliation = ManagedInstanceReconciliationService(factory, broker=broker)
    coordinator = DevelopmentSourceSmokeTestOperationCoordinator(
        factory, clock=lambda: NOW, reconciliation=reconciliation
    )
    queued = coordinator.queue(INSTANCE_ID, SOURCE_ID)
    lease = coordinator.acquire(queued.operation_id)
    assert lease is not None
    details = details or FakeDetails()
    service = DevelopmentSourceSmokeTestService(
        factory,
        broker,
        DevelopmentSourceValidator(tmp_path),
        coordinator,
        lease,
        inventory_adapter_factory=lambda _o, _p: FakeInventory(
            [target()] if inventory is None else inventory
        ),
        details_adapter_factory=lambda _o, _p: details,
        smoke_adapter_factory=lambda _o, _p: smoke,
    )
    return service.run(), coordinator


@pytest.mark.parametrize(("count", "duration"), [(0, 12), (1, 34)])
def test_pass_requires_fixed_invocation_and_matching_post_readback(
    factory: sessionmaker[Session], tmp_path: Path, count: int, duration: int
) -> None:
    broker = FakeBroker()
    smoke = FakeSmoke(KodiAddonSmokeResult(count, duration))
    result, _ = run_smoke(factory, tmp_path, broker, smoke)
    assert result.state is DevelopmentSourceSmokeTestOperationState.SUCCEEDED
    assert result.overall_result is DevelopmentSourceSmokeTestOverallResult.PASSED
    assert (
        result.execution_result
        is DevelopmentSourceSmokeTestExecutionResult.DIRECTORY_RESULT
    )
    assert (
        result.post_execution_provenance
        is DevelopmentSourceSmokeTestPostProvenance.MATCHED
    )
    assert result.returned_item_count == count
    assert result.invocation_duration_ms == duration
    assert smoke.calls == [ADDON_ID]
    assert broker.verify_calls == 2
    with factory() as session:
        evidence = DevelopmentSourceDeploymentRepository(session).get(
            INSTANCE_ID, SOURCE_ID
        )
        assert evidence is not None
        assert (
            evidence.confidence
            is DevelopmentSourceDeploymentConfidence.VERIFIED_AT_LAST_OPERATION
        )
        assert GlobalHeavyOperationLeaseRepository(session).current() is None


def test_correlated_execution_error_remains_separate_from_provenance(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    result, _ = run_smoke(
        factory, tmp_path, FakeBroker(), FakeSmoke(KodiAddonSmokeJsonRpcError(19))
    )
    assert (
        result.overall_result
        is DevelopmentSourceSmokeTestOverallResult.EXECUTION_FAILED
    )
    assert (
        result.execution_result
        is DevelopmentSourceSmokeTestExecutionResult.JSONRPC_ERROR
    )
    assert (
        result.post_execution_provenance
        is DevelopmentSourceSmokeTestPostProvenance.MATCHED
    )
    assert result.invocation_duration_ms == 19
    with factory() as session:
        evidence = DevelopmentSourceDeploymentRepository(session).get(
            INSTANCE_ID, SOURCE_ID
        )
        assert evidence is not None
        assert (
            evidence.confidence
            is DevelopmentSourceDeploymentConfidence.VERIFIED_AT_LAST_OPERATION
        )


def test_timeout_is_ambiguous_blocks_second_smoke_and_never_adopts_match(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    broker = FakeBroker()
    result, coordinator = run_smoke(
        factory, tmp_path, broker, FakeSmoke(KodiAddonSmokeTimeoutError(10_000))
    )
    assert (
        result.state is DevelopmentSourceSmokeTestOperationState.RECONCILIATION_REQUIRED
    )
    assert result.overall_result is DevelopmentSourceSmokeTestOverallResult.TIMEOUT
    assert result.execution_result is DevelopmentSourceSmokeTestExecutionResult.TIMEOUT
    assert (
        result.post_execution_provenance
        is DevelopmentSourceSmokeTestPostProvenance.UNKNOWN
    )
    assert broker.verify_calls == 1
    with factory() as session:
        evidence = DevelopmentSourceDeploymentRepository(session).get(
            INSTANCE_ID, SOURCE_ID
        )
        assert evidence is not None
        assert evidence.confidence is DevelopmentSourceDeploymentConfidence.UNKNOWN
    with pytest.raises(ManagedInstanceReconciliationRequiredError):
        coordinator.queue(INSTANCE_ID, SOURCE_ID)
    reconciliation = ManagedInstanceReconciliationService(factory, broker=broker)
    status = reconciliation.status(INSTANCE_ID)
    assert ReconciliationAction.STOP not in status.blocked_actions
    broker.runtime_state = ContainerRuntimeState.STOPPED
    recovered = reconciliation.status(INSTANCE_ID)
    assert recovered.blocking is False
    historical = coordinator.get(result.operation_id)
    assert historical is not None
    assert (
        historical.state
        is DevelopmentSourceSmokeTestOperationState.RECONCILIATION_REQUIRED
    )


def test_source_tree_change_marks_stale_without_adoption(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    broker = FakeBroker()
    broker.verify_outcomes = ["match", "mismatch"]
    result, _ = run_smoke(
        factory, tmp_path, broker, FakeSmoke(KodiAddonSmokeResult(0, 7))
    )
    assert (
        result.overall_result
        is DevelopmentSourceSmokeTestOverallResult.SOURCE_TREE_CHANGED
    )
    assert (
        result.post_execution_provenance
        is DevelopmentSourceSmokeTestPostProvenance.CHANGED
    )
    with factory() as session:
        evidence = DevelopmentSourceDeploymentRepository(session).get(
            INSTANCE_ID, SOURCE_ID
        )
        assert evidence is not None
        assert evidence.confidence is DevelopmentSourceDeploymentConfidence.STALE
        assert evidence.verified_destination_digest == "sha256:" + "4" * 64


def test_post_verifier_unavailable_is_not_a_pass(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    broker = FakeBroker()
    broker.verify_outcomes = ["match", "unavailable"]
    result, _ = run_smoke(
        factory, tmp_path, broker, FakeSmoke(KodiAddonSmokeResult(1, 7))
    )
    assert (
        result.state is DevelopmentSourceSmokeTestOperationState.RECONCILIATION_REQUIRED
    )
    assert (
        result.overall_result
        is DevelopmentSourceSmokeTestOverallResult.RECONCILIATION_REQUIRED
    )
    assert (
        result.post_execution_provenance
        is DevelopmentSourceSmokeTestPostProvenance.UNKNOWN
    )
    with factory() as session:
        evidence = DevelopmentSourceDeploymentRepository(session).get(
            INSTANCE_ID, SOURCE_ID
        )
        assert evidence is not None
        assert evidence.confidence is DevelopmentSourceDeploymentConfidence.UNKNOWN


def test_unexpected_post_dispatch_failure_is_crash_equivalent(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    result, _ = run_smoke(
        factory, tmp_path, FakeBroker(), FakeSmoke(RuntimeError("simulated crash"))
    )
    assert (
        result.state is DevelopmentSourceSmokeTestOperationState.RECONCILIATION_REQUIRED
    )
    assert (
        result.overall_result
        is DevelopmentSourceSmokeTestOverallResult.RECONCILIATION_REQUIRED
    )
    assert (
        result.execution_result is DevelopmentSourceSmokeTestExecutionResult.AMBIGUOUS
    )
    assert (
        result.post_execution_provenance
        is DevelopmentSourceSmokeTestPostProvenance.UNKNOWN
    )


def test_abandoned_dispatched_smoke_is_terminalized_without_resume(
    factory: sessionmaker[Session],
) -> None:
    coordinator = DevelopmentSourceSmokeTestOperationCoordinator(
        factory, clock=lambda: NOW
    )
    queued = coordinator.queue(INSTANCE_ID, SOURCE_ID)
    lease = coordinator.acquire(queued.operation_id)
    assert lease is not None
    coordinator.set_expected_container(lease, CONTAINER_ID)
    coordinator.mark_invocation_dispatched(lease)
    expired = "2026-09-17T15:59:59.000000Z"
    with factory.begin() as session:
        session.execute(
            update(DevelopmentSourceSmokeTestOperationRow).values(
                lease_expires_at=expired
            )
        )
        session.execute(
            update(ManagedInstanceMutationLeaseRow).values(lease_expires_at=expired)
        )
    sweep = StaleOperationReconciliationDispatcher(
        factory, clock=lambda: NOW
    ).sweep_instance(INSTANCE_ID)
    assert sweep.released_reservations == 1
    operation = coordinator.get(queued.operation_id)
    assert operation is not None
    assert (
        operation.state
        is DevelopmentSourceSmokeTestOperationState.RECONCILIATION_REQUIRED
    )
    assert (
        operation.execution_result
        is DevelopmentSourceSmokeTestExecutionResult.AMBIGUOUS
    )
    assert operation.invocation_dispatched is True
    with factory() as session:
        assert (
            ManagedInstanceMutationLeaseRepository(session).current(INSTANCE_ID) is None
        )
        evidence = DevelopmentSourceDeploymentRepository(session).get(
            INSTANCE_ID, SOURCE_ID
        )
        assert evidence is not None
        assert evidence.confidence is DevelopmentSourceDeploymentConfidence.UNKNOWN


@pytest.mark.parametrize(
    ("present", "enabled", "optional", "version", "reason"),
    [
        (False, False, False, None, "dependency_missing"),
        (True, False, False, None, "dependency_disabled"),
        (False, False, True, None, None),
        (True, True, False, None, None),
        (True, True, False, "2.0.0", "dependency_incompatible"),
        (True, True, False, "1.0.0", None),
        (True, True, False, "0.9.0", None),
        (True, True, False, "1.0.0~beta1", "dependency_version_unverified"),
    ],
)
def test_dependency_preflight_never_invokes_when_blocked(
    factory: sessionmaker[Session],
    tmp_path: Path,
    present: bool,
    enabled: bool,
    optional: bool,
    version: str | None,
    reason: str | None,
) -> None:
    dependency_id = "script.module.required"
    optional_text = "true" if optional else "false"
    version_text = "" if version is None else f' version="{version}"'
    write_descriptor(
        tmp_path / "source",
        '<requires><import addon="script.module.required"'
        f'{version_text} optional="{optional_text}"/></requires>',
    )
    details = FakeDetails()
    details.dependencies = (
        KodiReportedAddonDependency(dependency_id, version, optional),
    )
    items = [target()]
    if present:
        items.append(dependency(dependency_id, enabled=enabled))
    smoke = FakeSmoke(KodiAddonSmokeResult(0, 1))
    result, _ = run_smoke(
        factory, tmp_path, FakeBroker(), smoke, inventory=items, details=details
    )
    assert result.failure_reason == reason
    if reason is None:
        assert smoke.calls == [ADDON_ID]
    else:
        assert (
            result.overall_result
            is DevelopmentSourceSmokeTestOverallResult.DEPENDENCY_BLOCKED
        )
        assert smoke.calls == []


def test_dependency_declaration_mismatch_blocks_without_invocation(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    write_descriptor(
        tmp_path / "source",
        '<requires><import addon="script.module.source"/></requires>',
    )
    details = FakeDetails()
    details.dependencies = (
        KodiReportedAddonDependency("script.module.kodi", None, False),
    )
    smoke = FakeSmoke(KodiAddonSmokeResult(0, 1))
    result, _ = run_smoke(factory, tmp_path, FakeBroker(), smoke, details=details)
    assert result.failure_reason == "dependency_state_unknown"
    assert smoke.calls == []


@pytest.mark.parametrize(
    ("mutation",),
    [
        (ManagedInstanceMutationKind.LIFECYCLE,),
        (ManagedInstanceMutationKind.RESET,),
        (ManagedInstanceMutationKind.SNAPSHOT_CREATE,),
        (ManagedInstanceMutationKind.SNAPSHOT_RESTORE,),
        (ManagedInstanceMutationKind.FIXTURE_ASSIGNMENT,),
        (ManagedInstanceMutationKind.RUNTIME_REASSIGNMENT,),
        (ManagedInstanceMutationKind.DEVELOPMENT_SOURCE_DEPLOYMENT,),
        (ManagedInstanceMutationKind.DEVELOPMENT_SOURCE_ATTACHMENT,),
        (ManagedInstanceMutationKind.DEVELOPMENT_SOURCE_ENABLEMENT,),
        (ManagedInstanceMutationKind.INSTANCE_DELETE,),
    ],
)
def test_smoke_conflicts_with_every_instance_mutation(
    factory: sessionmaker[Session], mutation: ManagedInstanceMutationKind
) -> None:
    with factory.begin() as session:
        ManagedInstanceMutationLeaseRepository(session).reserve(
            INSTANCE_ID, kind=mutation, operation_id=uuid4()
        )
    coordinator = DevelopmentSourceSmokeTestOperationCoordinator(
        factory, clock=lambda: NOW
    )
    with pytest.raises(DevelopmentSourceSmokeTestConflictError):
        coordinator.queue(INSTANCE_ID, SOURCE_ID)


def test_active_smoke_blocks_snapshot_create_and_second_smoke(
    factory: sessionmaker[Session],
) -> None:
    coordinator = DevelopmentSourceSmokeTestOperationCoordinator(
        factory, clock=lambda: NOW
    )
    coordinator.queue(INSTANCE_ID, SOURCE_ID)
    with pytest.raises(DevelopmentSourceSmokeTestConflictError):
        coordinator.queue(INSTANCE_ID, SOURCE_ID)
    with pytest.raises(IntegrityError), factory.begin() as session:
        ManagedInstanceMutationLeaseRepository(session).reserve(
            INSTANCE_ID,
            kind=ManagedInstanceMutationKind.SNAPSHOT_CREATE,
            operation_id=uuid4(),
        )


@pytest.mark.parametrize(
    ("setup", "reason"),
    [
        ("not_attached", "source_not_attached"),
        ("not_deployed", "source_not_deployed"),
        ("stale", "deployment_evidence_stale"),
        ("unknown", "deployment_evidence_unknown"),
        ("interpreter_default", "runtime_bytecode_policy_incompatible"),
        ("stopped", "instance_not_running"),
        ("wrong_runtime", "kodi_not_ready"),
        ("not_recognized", "addon_not_recognized"),
        ("disabled", "addon_disabled"),
        ("version_mismatch", "addon_version_mismatch"),
        ("destination_mismatch", "source_tree_changed"),
        ("destination_unavailable", "deployment_verification_failed"),
    ],
)
def test_precondition_failures_never_dispatch(
    factory: sessionmaker[Session],
    tmp_path: Path,
    setup: str,
    reason: str,
) -> None:
    broker = FakeBroker()
    items = [target()]
    if setup == "not_attached":
        with factory.begin() as session:
            session.execute(delete(ManagedInstanceDevelopmentSourceRow))
    elif setup == "not_deployed":
        with factory.begin() as session:
            session.execute(delete(DevelopmentSourceDeploymentRow))
    elif setup in {"stale", "unknown"}:
        with factory.begin() as session:
            session.execute(
                update(DevelopmentSourceDeploymentRow).values(confidence=setup)
            )
    elif setup == "interpreter_default":
        with factory.begin() as session:
            session.execute(
                update(RuntimeExecutionProfileRow).values(
                    python_bytecode_policy="interpreter_default"
                )
            )
    elif setup == "stopped":
        broker.runtime_state = ContainerRuntimeState.STOPPED
    elif setup == "wrong_runtime":
        broker.observed_revision = str(UUID(int=999))
    elif setup == "not_recognized":
        items = []
    elif setup == "disabled":
        items = [target(enabled=False)]
    elif setup == "version_mismatch":
        items = [target(version="9.9.9")]
    elif setup == "destination_mismatch":
        broker.verify_outcomes = ["mismatch"]
    elif setup == "destination_unavailable":
        broker.verify_outcomes = ["unavailable"]
    smoke = FakeSmoke(KodiAddonSmokeResult(0, 1))
    result, _ = run_smoke(factory, tmp_path, broker, smoke, inventory=items)
    assert result.failure_reason == reason
    assert (
        result.execution_result
        is DevelopmentSourceSmokeTestExecutionResult.NOT_ATTEMPTED
    )
    assert smoke.calls == []
    if setup == "destination_unavailable":
        with factory() as session:
            evidence = DevelopmentSourceDeploymentRepository(session).get(
                INSTANCE_ID, SOURCE_ID
            )
            assert evidence is not None
            assert (
                evidence.confidence
                is DevelopmentSourceDeploymentConfidence.VERIFIED_AT_LAST_OPERATION
            )


def test_history_survives_instance_deletion_and_never_contains_returned_content(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    result, _ = run_smoke(
        factory, tmp_path, FakeBroker(), FakeSmoke(KodiAddonSmokeResult(1, 2))
    )
    with factory.begin() as session:
        DevelopmentSourceSmokeTestOperationRepository(session).clear_live_instance(
            INSTANCE_ID
        )
        session.execute(delete(ManagedInstanceRow))
    with factory() as session:
        operation = DevelopmentSourceSmokeTestOperationRepository(session).get(
            result.operation_id
        )
        assert operation is not None
        assert operation.managed_instance_id is None
        assert operation.original_managed_instance_id == INSTANCE_ID
        assert operation.returned_item_count == 1
        assert "label" not in repr(operation).lower()
        assert "plugin://" not in repr(operation)


def test_retention_keeps_latest_twenty_ordinary_and_all_reconciliation_history(
    factory: sessionmaker[Session],
) -> None:
    with factory.begin() as session:
        for index in range(22):
            session.add(
                DevelopmentSourceSmokeTestOperationRow(
                    id=str(UUID(int=1000 + index)),
                    original_managed_instance_id=str(INSTANCE_ID),
                    managed_instance_id=str(INSTANCE_ID),
                    development_source_id=str(SOURCE_ID),
                    source_logical_name="Example source",
                    addon_id=ADDON_ID,
                    addon_version=VERSION,
                    runtime_revision_id=str(REVISION_ID),
                    requested_at=f"2026-09-17T16:00:{index:02d}.000000Z",
                    started_at=NOW.isoformat(),
                    completed_at=NOW.isoformat(),
                    state=DevelopmentSourceSmokeTestOperationState.SUCCEEDED,
                    phase=DevelopmentSourceSmokeTestOperationPhase.FINALIZE,
                    overall_result=DevelopmentSourceSmokeTestOverallResult.PASSED,
                    execution_result=DevelopmentSourceSmokeTestExecutionResult.DIRECTORY_RESULT,
                    post_execution_provenance=DevelopmentSourceSmokeTestPostProvenance.MATCHED,
                    invocation_dispatched=True,
                    returned_item_count=0,
                    reconciliation_required=False,
                )
            )
        session.add(
            DevelopmentSourceSmokeTestOperationRow(
                id=str(UUID(int=2000)),
                original_managed_instance_id=str(INSTANCE_ID),
                managed_instance_id=str(INSTANCE_ID),
                development_source_id=str(SOURCE_ID),
                source_logical_name="Example source",
                addon_id=ADDON_ID,
                addon_version=VERSION,
                runtime_revision_id=str(REVISION_ID),
                requested_at="2026-09-17T15:59:00.000000Z",
                started_at=NOW.isoformat(),
                completed_at=NOW.isoformat(),
                state=DevelopmentSourceSmokeTestOperationState.RECONCILIATION_REQUIRED,
                phase=DevelopmentSourceSmokeTestOperationPhase.FINALIZE,
                overall_result=DevelopmentSourceSmokeTestOverallResult.TIMEOUT,
                execution_result=DevelopmentSourceSmokeTestExecutionResult.TIMEOUT,
                post_execution_provenance=DevelopmentSourceSmokeTestPostProvenance.UNKNOWN,
                invocation_dispatched=True,
                reconciliation_required=True,
            )
        )
        repository = DevelopmentSourceSmokeTestOperationRepository(session)
        assert repository.prune_terminal(INSTANCE_ID, retain=20) == 2
    with factory() as session:
        retained = DevelopmentSourceSmokeTestOperationRepository(
            session
        ).list_for_original_instance(INSTANCE_ID)
    assert len(retained) == 21
    assert (
        sum(
            item.state is DevelopmentSourceSmokeTestOperationState.SUCCEEDED
            for item in retained
        )
        == 20
    )
    assert any(item.reconciliation_required for item in retained)
