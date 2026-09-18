from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.activity import (
    ManagedInstanceActivityKind,
    ManagedInstanceActivityService,
)
from kodi_lab.application.addon_zip_smoke_tests import (
    AddonZipSmokeTestOperationCoordinator,
    AddonZipSmokeTestService,
    AddonZipSmokeTestStatus,
)
from kodi_lab.application.reconciliation import (
    ManagedInstanceReconciliationRequiredError,
    ManagedInstanceReconciliationService,
    ReconciliationAction,
)
from kodi_lab.broker.docker_inventory import (
    ContainerHealthState,
    ContainerRuntimeState,
    KodiProcessState,
)
from kodi_lab.broker.errors import ClientError
from kodi_lab.broker.operations import (
    ManagedContainerInventoryEntry,
    ManagedContainerInventoryResult,
    ObserveManagedKodiRuntimeResult,
    VerifyAddonZipSmokeDestinationResult,
)
from kodi_lab.domain import (
    AddonZipInstallationConfidence,
    AddonZipSmokeTestExecutionResult,
    AddonZipSmokeTestOperationState,
    AddonZipSmokeTestOverallResult,
    AddonZipSmokeTestPostProvenance,
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
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.storage.metadata import Base
from kodi_lab.storage.models import (
    AddonZipArtifactRow,
    AddonZipInstallationEvidenceRow,
    AddonZipInstallationOperationRow,
    ManagedInstanceRow,
    RuntimeDefinitionRow,
    RuntimeExecutionProfileRow,
    RuntimeRevisionRow,
    TargetRow,
)
from kodi_lab.storage.repositories import (
    AddonZipInstallationEvidenceRepository,
    AddonZipSmokeTestOperationRepository,
    ManagedInstanceMutationLeaseRepository,
)

INSTANCE_ID = UUID("f8260000-0000-4000-8000-000000000001")
ARTIFACT_ID = UUID("f8260000-0000-4000-8000-000000000002")
INSTALL_ID = UUID("f8260000-0000-4000-8000-000000000003")
REVISION_ID = UUID("f8260000-0000-4000-8000-000000000004")
DEFINITION_ID = UUID("f8260000-0000-4000-8000-000000000005")
CONTAINER_ID = "a" * 64
ADDON_ID = "plugin.video.uploaded"
VERSION = "1.2.3"
NOW = datetime(2026, 9, 17, 22, 0, tzinfo=UTC)


@pytest.fixture
def factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'zip-smoke.db'}")
    Base.metadata.create_all(engine)
    result = create_session_factory(engine)
    with result.begin() as session:
        session.add(
            RuntimeDefinitionRow(
                id=str(DEFINITION_ID), slug="zip-smoke", kodi_version="21.3"
            )
        )
        session.flush()
        session.add(
            RuntimeRevisionRow(
                id=str(REVISION_ID),
                runtime_definition_id=str(DEFINITION_ID),
                revision_number=1,
                source_kind="test",
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
        session.flush()
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
                name="ZIP smoke instance",
                target_type="managed",
                kodi_version="21.3",
                availability="available",
                health="healthy",
                connection_state="connected",
            )
        )
        session.flush()
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
            AddonZipArtifactRow(
                id=str(ARTIFACT_ID),
                logical_label="Uploaded smoke test",
                sha256_digest="sha256:" + "3" * 64,
                compressed_size=100,
                total_uncompressed_bytes=200,
                member_count=2,
                addon_id=ADDON_ID,
                addon_name="Uploaded Test",
                addon_version=VERSION,
                extension_classification="python_video_plugin",
                dependencies_json="[]",
                payload_manifest_digest="sha256:" + "4" * 64,
                imported_at=NOW.isoformat(),
                origin="uploaded_zip",
                validation_state="accepted",
            )
        )
        session.flush()
        session.add(
            AddonZipInstallationOperationRow(
                id=str(INSTALL_ID),
                original_managed_instance_id=str(INSTANCE_ID),
                managed_instance_id=str(INSTANCE_ID),
                artifact_id=str(ARTIFACT_ID),
                addon_id=ADDON_ID,
                addon_version=VERSION,
                runtime_revision_id=str(REVISION_ID),
                requested_at=NOW.isoformat(),
                state="succeeded",
                phase="finalize",
                expected_container_id=CONTAINER_ID,
                application_started=True,
                stopped_readback_matched=True,
                final_readback_matched=True,
                recognition_enabled=False,
                started_at=NOW.isoformat(),
                heartbeat_at=NOW.isoformat(),
                completed_at=NOW.isoformat(),
                reconciliation_required=False,
            )
        )
        session.flush()
        session.add(
            AddonZipInstallationEvidenceRow(
                managed_instance_id=str(INSTANCE_ID),
                original_managed_instance_id=str(INSTANCE_ID),
                artifact_id=str(ARTIFACT_ID),
                installation_operation_id=str(INSTALL_ID),
                runtime_revision_id=str(REVISION_ID),
                trusted_container_id=CONTAINER_ID,
                addon_id=ADDON_ID,
                addon_version=VERSION,
                payload_manifest_digest="sha256:" + "4" * 64,
                verified_at=NOW.isoformat(),
                confidence="verified_at_last_operation",
                result="installed_recognized_disabled",
            )
        )
    yield result
    engine.dispose()


class FakeBroker:
    def __init__(self) -> None:
        self.container_id = CONTAINER_ID
        self.runtime_state = ContainerRuntimeState.RUNNING
        self.health_state = ContainerHealthState.HEALTHY
        self.process_state = KodiProcessState.RUNNING
        self.observed_revision = REVISION_ID
        self.verify_outcomes = ["matched", "matched"]
        self.verify_calls = 0
        self.wrong_identity = False
        self.wrong_identity_on_call: int | None = None

    def managed_container_inventory(self) -> ManagedContainerInventoryResult:
        return ManagedContainerInventoryResult(
            containers=[
                ManagedContainerInventoryEntry(
                    managed_instance_id=str(INSTANCE_ID),
                    container_id=self.container_id,
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
            runtime_revision_id=str(self.observed_revision),
            container_id=expected_container_id,
            runtime_state=self.runtime_state,
            process_state=self.process_state,
            ipv4_address="10.250.0.8",
            jsonrpc_port=8080,
        )

    def verify_addon_zip_smoke_destination(
        self,
        *,
        managed_instance_id: str,
        artifact_id: str,
        smoke_operation_id: str,
        expected_container_id: str,
    ) -> VerifyAddonZipSmokeDestinationResult:
        self.verify_calls += 1
        outcome = self.verify_outcomes.pop(0)
        if outcome == "unknown":
            raise ClientError("injected verifier failure")
        return VerifyAddonZipSmokeDestinationResult(
            managed_instance_id=managed_instance_id,
            artifact_id=artifact_id,
            smoke_operation_id=(
                str(UUID(int=17))
                if self.wrong_identity
                or self.wrong_identity_on_call == self.verify_calls
                else smoke_operation_id
            ),
            runtime_revision_id=str(REVISION_ID),
            container_id=expected_container_id,
            addon_id=ADDON_ID,
            addon_version=VERSION,
            destination_state=(
                "exact_payload_present"
                if outcome == "matched"
                else "partial_or_unexpected"
            ),
            matched=outcome == "matched",
        )


class FakeInventory:
    def __init__(self, items: list[KodiAddonInventoryRecord]) -> None:
        self.items = items
        self.calls = 0

    def get_inventory(self) -> KodiAddonInventoryResult:
        self.calls += 1
        return KodiAddonInventoryResult(
            KodiApplicationIdentity("Kodi", 21, 3, "stable"), tuple(self.items)
        )


class FakeDetails:
    def __init__(self) -> None:
        self.enabled = True
        self.dependencies: tuple[KodiReportedAddonDependency, ...] = ()

    def get_addon_details(self, addon_id: str) -> KodiAddonDetailsRecord:
        return KodiAddonDetailsRecord(
            addon_id=addon_id,
            name="Uploaded",
            version=VERSION,
            enabled=self.enabled,
            kodi_type="xbmc.python.pluginsource",
            dependencies=self.dependencies,
        )


class FakeSmoke:
    def __init__(self, *, count: int = 0, error: BaseException | None = None) -> None:
        self.count = count
        self.error = error
        self.calls: list[str] = []

    def invoke(self, addon_id: str) -> KodiAddonSmokeResult:
        self.calls.append(addon_id)
        if self.error is not None:
            raise self.error
        return KodiAddonSmokeResult(duration_ms=17, item_count=self.count)


def target(*, enabled: bool = True, version: str = VERSION) -> KodiAddonInventoryRecord:
    return KodiAddonInventoryRecord(
        ADDON_ID, "Uploaded", version, enabled, "xbmc.python.pluginsource"
    )


def run_smoke(
    factory: sessionmaker[Session],
    broker: FakeBroker,
    inventory: FakeInventory,
    details: FakeDetails,
    smoke: FakeSmoke,
    *,
    after_dispatch_hook: object | None = None,
) -> AddonZipSmokeTestStatus:
    coordinator = AddonZipSmokeTestOperationCoordinator(factory)
    queued = coordinator.queue(INSTANCE_ID, ARTIFACT_ID)
    lease = coordinator.acquire(queued.operation_id)
    assert lease is not None
    service = AddonZipSmokeTestService(
        factory,
        broker,
        coordinator,
        lease,
        inventory_adapter_factory=lambda _runtime, _profile: inventory,
        details_adapter_factory=lambda _runtime, _profile: details,
        smoke_adapter_factory=lambda _runtime, _profile: smoke,
        after_dispatch_hook=(
            after_dispatch_hook if callable(after_dispatch_hook) else None
        ),
    )
    return service.run()


@pytest.mark.parametrize("count", [0, 1])
def test_empty_and_one_item_results_pass_without_returned_content(
    factory: sessionmaker[Session], count: int
) -> None:
    broker = FakeBroker()
    smoke = FakeSmoke(count=count)
    result = run_smoke(factory, broker, FakeInventory([target()]), FakeDetails(), smoke)

    assert result.state is AddonZipSmokeTestOperationState.SUCCEEDED
    assert result.overall_result is AddonZipSmokeTestOverallResult.PASSED
    assert result.execution_result is AddonZipSmokeTestExecutionResult.DIRECTORY_RESULT
    assert result.post_execution_provenance is AddonZipSmokeTestPostProvenance.MATCHED
    assert result.returned_item_count == count
    assert result.model_dump().keys().isdisjoint({"items", "files", "plugin_uri"})
    assert broker.verify_calls == 2
    assert smoke.calls == [ADDON_ID]


def test_correlated_execution_error_with_exact_tree_preserves_evidence(
    factory: sessionmaker[Session],
) -> None:
    result = run_smoke(
        factory,
        FakeBroker(),
        FakeInventory([target()]),
        FakeDetails(),
        FakeSmoke(error=KodiAddonSmokeJsonRpcError(12)),
    )

    assert result.overall_result is AddonZipSmokeTestOverallResult.EXECUTION_FAILED
    assert result.execution_result is AddonZipSmokeTestExecutionResult.JSONRPC_ERROR
    assert result.post_execution_provenance is AddonZipSmokeTestPostProvenance.MATCHED
    with factory() as session:
        evidence = AddonZipInstallationEvidenceRepository(session).get(
            INSTANCE_ID, ADDON_ID
        )
    assert evidence is not None
    assert (
        evidence.confidence is AddonZipInstallationConfidence.VERIFIED_AT_LAST_OPERATION
    )


def test_successful_execution_with_installed_tree_mutation_marks_stale(
    factory: sessionmaker[Session],
) -> None:
    broker = FakeBroker()
    broker.verify_outcomes = ["matched", "changed"]
    result = run_smoke(
        factory, broker, FakeInventory([target()]), FakeDetails(), FakeSmoke(count=1)
    )

    assert (
        result.overall_result is AddonZipSmokeTestOverallResult.INSTALLED_TREE_CHANGED
    )
    assert result.post_execution_provenance is AddonZipSmokeTestPostProvenance.CHANGED
    with factory() as session:
        evidence = AddonZipInstallationEvidenceRepository(session).get(
            INSTANCE_ID, ADDON_ID
        )
    assert evidence is not None
    assert evidence.confidence is AddonZipInstallationConfidence.STALE


def test_addon_data_only_mutation_may_pass_when_installed_tree_matches(
    factory: sessionmaker[Session],
) -> None:
    data_write = {"performed": False}
    result = run_smoke(
        factory,
        FakeBroker(),
        FakeInventory([target()]),
        FakeDetails(),
        FakeSmoke(),
        after_dispatch_hook=lambda: data_write.update(performed=True),
    )
    assert data_write["performed"] is True
    assert result.overall_result is AddonZipSmokeTestOverallResult.PASSED


def test_history_activity_is_bounded_and_private(
    factory: sessionmaker[Session],
) -> None:
    result = run_smoke(
        factory,
        FakeBroker(),
        FakeInventory([target()]),
        FakeDetails(),
        FakeSmoke(count=1),
    )
    activity = ManagedInstanceActivityService(factory).list_for_live_instance(
        INSTANCE_ID
    )
    item = next(
        entry
        for entry in activity
        if entry.kind is ManagedInstanceActivityKind.ADDON_ZIP_SMOKE_TEST
    )
    assert item.operation_uuid == result.operation_id
    serialized = item.model_dump_json()
    for forbidden in (
        "plugin://",
        "label",
        "path",
        "raw_json",
        "kodi_error",
        CONTAINER_ID,
    ):
        assert forbidden not in serialized


@pytest.mark.parametrize(
    ("outcome", "expected", "confidence"),
    [
        (
            "changed",
            AddonZipSmokeTestOverallResult.INSTALLED_TREE_CHANGED,
            AddonZipInstallationConfidence.STALE,
        ),
        (
            "unknown",
            AddonZipSmokeTestOverallResult.RECONCILIATION_REQUIRED,
            AddonZipInstallationConfidence.UNKNOWN,
        ),
    ],
)
def test_pre_execution_tree_verification_blocks_and_transitions_evidence(
    factory: sessionmaker[Session],
    outcome: str,
    expected: AddonZipSmokeTestOverallResult,
    confidence: AddonZipInstallationConfidence,
) -> None:
    broker = FakeBroker()
    broker.verify_outcomes = [outcome]
    smoke = FakeSmoke()
    result = run_smoke(factory, broker, FakeInventory([target()]), FakeDetails(), smoke)
    assert result.overall_result is expected
    assert result.execution_result is AddonZipSmokeTestExecutionResult.NOT_ATTEMPTED
    assert smoke.calls == []
    with factory() as session:
        evidence = AddonZipInstallationEvidenceRepository(session).get(
            INSTANCE_ID, ADDON_ID
        )
    assert evidence is not None and evidence.confidence is confidence


def test_wrong_correlation_is_unknown_and_never_invokes(
    factory: sessionmaker[Session],
) -> None:
    broker = FakeBroker()
    broker.wrong_identity = True
    smoke = FakeSmoke()
    result = run_smoke(factory, broker, FakeInventory([target()]), FakeDetails(), smoke)
    assert (
        result.overall_result is AddonZipSmokeTestOverallResult.RECONCILIATION_REQUIRED
    )
    assert smoke.calls == []


def test_post_execution_verifier_unavailable_requires_reconciliation(
    factory: sessionmaker[Session],
) -> None:
    broker = FakeBroker()
    broker.verify_outcomes = ["matched", "unknown"]
    result = run_smoke(
        factory, broker, FakeInventory([target()]), FakeDetails(), FakeSmoke(count=1)
    )
    assert result.state is AddonZipSmokeTestOperationState.RECONCILIATION_REQUIRED
    assert result.execution_result is AddonZipSmokeTestExecutionResult.DIRECTORY_RESULT
    assert result.post_execution_provenance is AddonZipSmokeTestPostProvenance.UNKNOWN


def test_crash_after_dispatch_is_ambiguous_and_not_retried(
    factory: sessionmaker[Session],
) -> None:
    smoke = FakeSmoke()

    def crash() -> None:
        raise RuntimeError("injected crash after durable dispatch")

    result = run_smoke(
        factory,
        FakeBroker(),
        FakeInventory([target()]),
        FakeDetails(),
        smoke,
        after_dispatch_hook=crash,
    )
    assert result.state is AddonZipSmokeTestOperationState.RECONCILIATION_REQUIRED
    assert result.execution_result is AddonZipSmokeTestExecutionResult.AMBIGUOUS
    assert smoke.calls == []


@pytest.mark.parametrize(
    ("enabled", "observed_revision", "container_id", "reason"),
    [
        (False, REVISION_ID, CONTAINER_ID, "addon_disabled"),
        (True, UUID(int=18), CONTAINER_ID, "kodi_not_ready"),
        (True, REVISION_ID, "b" * 64, "zip_installation_lineage_stale"),
    ],
)
def test_target_and_exact_runtime_preconditions_block(
    factory: sessionmaker[Session],
    enabled: bool,
    observed_revision: UUID,
    container_id: str,
    reason: str,
) -> None:
    broker = FakeBroker()
    broker.observed_revision = observed_revision
    broker.container_id = container_id
    result = run_smoke(
        factory,
        broker,
        FakeInventory([target(enabled=enabled)]),
        FakeDetails(),
        FakeSmoke(),
    )
    assert result.execution_result is AddonZipSmokeTestExecutionResult.NOT_ATTEMPTED
    assert result.failure_reason == reason


@pytest.mark.parametrize(
    ("dependencies_json", "reported", "inventory_items", "reason"),
    [
        (
            '[{"id":"script.module.required","minversion":null,"optional":false,"version":null}]',
            (KodiReportedAddonDependency("script.module.required", None, False, None),),
            [],
            "dependency_missing",
        ),
        (
            '[{"id":"script.module.required","minversion":null,"optional":false,"version":null}]',
            (KodiReportedAddonDependency("script.module.required", None, False, None),),
            [
                KodiAddonInventoryRecord(
                    "script.module.required",
                    "Required",
                    "1.0.0",
                    False,
                    "xbmc.python.module",
                )
            ],
            "dependency_disabled",
        ),
        (
            '[{"id":"script.module.required","minversion":"1.0.0","optional":false,"version":null}]',
            (
                KodiReportedAddonDependency(
                    "script.module.required", None, False, "1.0.0"
                ),
            ),
            [
                KodiAddonInventoryRecord(
                    "script.module.required",
                    "Required",
                    "1.0.0",
                    True,
                    "xbmc.python.module",
                )
            ],
            "dependency_version_unverified",
        ),
    ],
)
def test_dependency_preflight_blocks_without_invocation(
    factory: sessionmaker[Session],
    dependencies_json: str,
    reported: tuple[KodiReportedAddonDependency, ...],
    inventory_items: list[KodiAddonInventoryRecord],
    reason: str,
) -> None:
    with factory.begin() as session:
        artifact = session.get(AddonZipArtifactRow, str(ARTIFACT_ID))
        assert artifact is not None
        artifact.dependencies_json = dependencies_json
    details = FakeDetails()
    details.dependencies = reported
    smoke = FakeSmoke()
    result = run_smoke(
        factory,
        FakeBroker(),
        FakeInventory([target(), *inventory_items]),
        details,
        smoke,
    )
    assert result.overall_result is AddonZipSmokeTestOverallResult.DEPENDENCY_BLOCKED
    assert result.blocked_dependency_reason == reason
    assert smoke.calls == []


def test_optional_missing_dependency_is_allowed(
    factory: sessionmaker[Session],
) -> None:
    with factory.begin() as session:
        artifact = session.get(AddonZipArtifactRow, str(ARTIFACT_ID))
        assert artifact is not None
        artifact.dependencies_json = (
            '[{"id":"script.module.optional","minversion":null,'
            '"optional":true,"version":null}]'
        )
    details = FakeDetails()
    details.dependencies = (
        KodiReportedAddonDependency("script.module.optional", None, True, None),
    )
    result = run_smoke(
        factory, FakeBroker(), FakeInventory([target()]), details, FakeSmoke()
    )
    assert result.overall_result is AddonZipSmokeTestOverallResult.PASSED


@pytest.mark.parametrize(
    ("confidence", "reason"),
    [
        (AddonZipInstallationConfidence.STALE, "zip_installation_evidence_stale"),
        (AddonZipInstallationConfidence.UNKNOWN, "zip_installation_evidence_unknown"),
    ],
)
def test_stale_or_unknown_installation_evidence_blocks_before_verification(
    factory: sessionmaker[Session],
    confidence: AddonZipInstallationConfidence,
    reason: str,
) -> None:
    with factory.begin() as session:
        evidence = session.get(
            AddonZipInstallationEvidenceRow, (str(INSTANCE_ID), ADDON_ID)
        )
        assert evidence is not None
        evidence.confidence = confidence
    broker = FakeBroker()
    result = run_smoke(
        factory, broker, FakeInventory([target()]), FakeDetails(), FakeSmoke()
    )
    assert result.failure_reason == reason
    assert broker.verify_calls == 0


def test_timeout_requires_reconciliation_blocks_second_smoke_and_stop_is_allowed(
    factory: sessionmaker[Session],
) -> None:
    broker = FakeBroker()
    result = run_smoke(
        factory,
        broker,
        FakeInventory([target()]),
        FakeDetails(),
        FakeSmoke(error=KodiAddonSmokeTimeoutError(10_000)),
    )
    assert result.state is AddonZipSmokeTestOperationState.RECONCILIATION_REQUIRED
    assert result.overall_result is AddonZipSmokeTestOverallResult.TIMEOUT
    assert result.execution_result is AddonZipSmokeTestExecutionResult.TIMEOUT
    assert result.post_execution_provenance is AddonZipSmokeTestPostProvenance.UNKNOWN
    with factory() as session:
        evidence = AddonZipInstallationEvidenceRepository(session).get(
            INSTANCE_ID, ADDON_ID
        )
        historical = AddonZipSmokeTestOperationRepository(session).get(
            result.operation_id
        )
        lease = ManagedInstanceMutationLeaseRepository(session).current(INSTANCE_ID)
    assert evidence is not None
    assert evidence.confidence is AddonZipInstallationConfidence.UNKNOWN
    assert historical is not None and historical.reconciliation_required
    assert lease is None

    coordinator = AddonZipSmokeTestOperationCoordinator(
        factory,
        reconciliation=ManagedInstanceReconciliationService(factory, broker=broker),
    )
    with pytest.raises(ManagedInstanceReconciliationRequiredError):
        coordinator.queue(INSTANCE_ID, ARTIFACT_ID)
    status = ManagedInstanceReconciliationService(
        factory, broker=broker
    ).require_allowed(INSTANCE_ID, ReconciliationAction.STOP)
    assert status.blocking is True
    assert ReconciliationAction.STOP not in status.blocked_actions

    broker.runtime_state = ContainerRuntimeState.STOPPED
    status = ManagedInstanceReconciliationService(factory, broker=broker).status(
        INSTANCE_ID
    )
    assert status.required is True
    assert status.blocking is False
    with factory() as session:
        still_unresolved = AddonZipSmokeTestOperationRepository(session).get(
            result.operation_id
        )
    assert still_unresolved is not None and still_unresolved.reconciliation_required
