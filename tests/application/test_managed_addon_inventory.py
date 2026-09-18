from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.addon_inventory import (
    AddonInventoryQueryStatus,
    AddonInventoryUnavailableReason,
    KodiAddonProvenanceClassification,
    KodiAddonProvenanceEvidenceStatus,
    ManagedInstanceAddonInventoryService,
)
from kodi_lab.application.errors import ManagedInstanceNotFoundError
from kodi_lab.broker.docker_inventory import (
    ContainerHealthState,
    ContainerRuntimeState,
    KodiProcessState,
)
from kodi_lab.broker.errors import ManagedRuntimeObservationClientError
from kodi_lab.broker.operations import (
    ManagedContainerInventoryEntry,
    ManagedContainerInventoryResult,
    ObserveManagedKodiRuntimeResult,
)
from kodi_lab.kodi.addons import (
    KodiAddonInventoryLimitExceededError,
    KodiAddonInventoryMalformedDataError,
    KodiAddonInventoryRecord,
    KodiAddonInventoryResult,
    KodiAddonInventoryUnsupportedResponseError,
)
from kodi_lab.kodi.readiness import JsonRpcUnavailableError, KodiApplicationIdentity
from kodi_lab.storage.engine import create_session_factory
from kodi_lab.storage.metadata import Base
from kodi_lab.storage.models import (
    AddonZipArtifactRow,
    AddonZipInstallationEvidenceRow,
    AddonZipInstallationOperationRow,
    DevelopmentSourceDeploymentOperationRow,
    DevelopmentSourceDeploymentRow,
    DevelopmentSourceRow,
    ManagedInstanceRow,
    RuntimeDefinitionRow,
    RuntimeExecutionProfileRow,
    RuntimeRevisionRow,
    TargetRow,
)

INSTANCE_ID = UUID("81000000-0000-4000-8000-000000000001")
REVISION_ID = UUID("81000000-0000-4000-8000-000000000002")
OTHER_REVISION_ID = UUID("81000000-0000-4000-8000-000000000003")
DEFINITION_ID = UUID("81000000-0000-4000-8000-000000000004")
SOURCE_ID = UUID("81000000-0000-4000-8000-000000000005")
OPERATION_ID = UUID("81000000-0000-4000-8000-000000000006")
ZIP_ARTIFACT_ID = UUID("81000000-0000-4000-8000-000000000007")
ZIP_OPERATION_ID = UUID("81000000-0000-4000-8000-000000000008")
CONTAINER_ID = "a" * 64
NOW = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)


@pytest.fixture
def factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_engine(f"sqlite:///{tmp_path / 'inventory.db'}")
    Base.metadata.create_all(engine)
    result = create_session_factory(engine)
    with result.begin() as session:
        session.add(
            RuntimeDefinitionRow(
                id=str(DEFINITION_ID), slug="kodi-21.3", kodi_version="21.3"
            )
        )
        for revision_id, number in ((REVISION_ID, 1), (OTHER_REVISION_ID, 2)):
            session.add(
                RuntimeRevisionRow(
                    id=str(revision_id),
                    runtime_definition_id=str(DEFINITION_ID),
                    revision_number=number,
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
            TargetRow(
                id=str(INSTANCE_ID),
                name="inventory instance",
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
    yield result
    engine.dispose()


class FakeBroker:
    def __init__(self) -> None:
        self.container: ManagedContainerInventoryEntry | None = self._entry()
        self.process_state = KodiProcessState.RUNNING
        self.observe_error = False
        self.observe_calls = 0
        self.observed_revision_id = REVISION_ID

    def _entry(
        self,
        *,
        state: ContainerRuntimeState = ContainerRuntimeState.RUNNING,
        health: ContainerHealthState = ContainerHealthState.HEALTHY,
    ) -> ManagedContainerInventoryEntry:
        return ManagedContainerInventoryEntry(
            managed_instance_id=str(INSTANCE_ID),
            container_id=CONTAINER_ID,
            image_id="sha256:" + "b" * 64,
            runtime_state=state,
            health_state=health,
        )

    def managed_container_inventory(self) -> ManagedContainerInventoryResult:
        return ManagedContainerInventoryResult(
            containers=[] if self.container is None else [self.container]
        )

    def observe_managed_kodi_runtime(
        self, *, managed_instance_id: str, expected_container_id: str
    ) -> ObserveManagedKodiRuntimeResult:
        self.observe_calls += 1
        assert managed_instance_id == str(INSTANCE_ID)
        assert expected_container_id == CONTAINER_ID
        if self.observe_error:
            raise ManagedRuntimeObservationClientError(
                "untrusted_runtime", "untrusted runtime", request_id="test"
            )
        return ObserveManagedKodiRuntimeResult(
            managed_instance_id=managed_instance_id,
            runtime_revision_id=str(self.observed_revision_id),
            container_id=expected_container_id,
            runtime_state=ContainerRuntimeState.RUNNING,
            process_state=self.process_state,
            ipv4_address="10.250.0.8",
            jsonrpc_port=8080,
        )


class FakeAdapter:
    def __init__(self, result: KodiAddonInventoryResult | BaseException) -> None:
        self.result = result

    def get_inventory(self) -> KodiAddonInventoryResult:
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def inventory_result(version: str = "1.2.3") -> KodiAddonInventoryResult:
    return KodiAddonInventoryResult(
        identity=KodiApplicationIdentity("Kodi", 21, 3, "stable"),
        items=(
            KodiAddonInventoryRecord(
                addon_id="plugin.video.example",
                name="Example",
                version=version,
                enabled=False,
                kodi_type="xbmc.python.pluginsource",
            ),
        ),
    )


def service(
    factory: sessionmaker[Session],
    broker: FakeBroker,
    result: KodiAddonInventoryResult | BaseException | None = None,
) -> ManagedInstanceAddonInventoryService:
    observed = inventory_result() if result is None else result
    return ManagedInstanceAddonInventoryService(
        factory,
        broker,
        adapter_factory=lambda _observation, _profile: FakeAdapter(observed),
        now=lambda: NOW,
    )


def add_evidence(
    factory: sessionmaker[Session],
    *,
    confidence: str = "verified_at_last_operation",
    version: str = "1.2.3",
    runtime_revision_id: UUID = REVISION_ID,
    container_id: str = CONTAINER_ID,
) -> None:
    with factory.begin() as session:
        session.add(
            DevelopmentSourceRow(
                id=str(SOURCE_ID),
                logical_name="example source",
                source_kind="local_directory",
                relative_locator="example",
                addon_id="plugin.video.example",
                observed_name="Example",
                observed_version=version,
                extension_types_json='["xbmc.python.pluginsource"]',
                registered_at=NOW.isoformat(),
                validated_at=NOW.isoformat(),
            )
        )
        session.add(
            DevelopmentSourceDeploymentOperationRow(
                id=str(OPERATION_ID),
                original_managed_instance_id=str(INSTANCE_ID),
                managed_instance_id=str(INSTANCE_ID),
                development_source_id=str(SOURCE_ID),
                source_logical_name="example source",
                addon_id="plugin.video.example",
                runtime_revision_id=str(runtime_revision_id),
                request_kind="copy_sync",
                requested_at=NOW.isoformat(),
                state="succeeded",
                phase="finalize",
                result_classification="initial_deployment",
                new_count=1,
                changed_count=0,
                unchanged_count=0,
                destination_only_count=0,
                transferred_bytes=1,
                expected_container_id=container_id,
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
                addon_id="plugin.video.example",
                mode="copy",
                last_successful_operation_id=str(OPERATION_ID),
                source_manifest_digest="sha256:" + "3" * 64,
                verified_destination_digest="sha256:" + "4" * 64,
                runtime_revision_id=str(runtime_revision_id),
                trusted_container_id=container_id,
                observed_source_name="Example",
                observed_source_version=version,
                verified_at=NOW.isoformat(),
                confidence=confidence,
            )
        )


def add_zip_evidence(
    factory: sessionmaker[Session],
    *,
    confidence: str = "verified_at_last_operation",
    version: str = "1.2.3",
    runtime_revision_id: UUID = REVISION_ID,
    container_id: str = CONTAINER_ID,
    include_evidence: bool = True,
) -> None:
    with factory.begin() as session:
        session.add(
            AddonZipArtifactRow(
                id=str(ZIP_ARTIFACT_ID),
                logical_label="uploaded example",
                sha256_digest="sha256:" + "5" * 64,
                compressed_size=100,
                total_uncompressed_bytes=200,
                member_count=2,
                addon_id="plugin.video.example",
                addon_name="Example",
                addon_version=version,
                extension_classification="python_video_plugin",
                dependencies_json="[]",
                payload_manifest_digest="sha256:" + "6" * 64,
                imported_at=NOW.isoformat(),
                origin="uploaded_zip",
                validation_state="accepted",
            )
        )
        session.add(
            AddonZipInstallationOperationRow(
                id=str(ZIP_OPERATION_ID),
                original_managed_instance_id=str(INSTANCE_ID),
                managed_instance_id=str(INSTANCE_ID),
                artifact_id=str(ZIP_ARTIFACT_ID),
                addon_id="plugin.video.example",
                addon_version=version,
                runtime_revision_id=str(runtime_revision_id),
                requested_at=NOW.isoformat(),
                state="succeeded",
                phase="finalize",
                expected_container_id=container_id,
                application_started=True,
                stopped_readback_matched=True,
                final_readback_matched=True,
                recognition_enabled=False,
                dependency_baseline_json="[]",
                dependency_post_json="[]",
                started_at=NOW.isoformat(),
                heartbeat_at=NOW.isoformat(),
                completed_at=NOW.isoformat(),
                reconciliation_required=False,
            )
        )
        if include_evidence:
            session.add(
                AddonZipInstallationEvidenceRow(
                    managed_instance_id=str(INSTANCE_ID),
                    original_managed_instance_id=str(INSTANCE_ID),
                    artifact_id=str(ZIP_ARTIFACT_ID),
                    installation_operation_id=str(ZIP_OPERATION_ID),
                    runtime_revision_id=str(runtime_revision_id),
                    trusted_container_id=container_id,
                    addon_id="plugin.video.example",
                    addon_version=version,
                    payload_manifest_digest="sha256:" + "6" * 64,
                    verified_at=NOW.isoformat(),
                    confidence=confidence,
                    result="installed_recognized_disabled",
                )
            )


def test_exact_running_managed_instance_returns_complete_live_inventory(
    factory: sessionmaker[Session],
) -> None:
    broker = FakeBroker()
    result = service(factory, broker).get(INSTANCE_ID)
    assert result.query_status is AddonInventoryQueryStatus.AVAILABLE
    assert result.complete is True
    assert result.observed_at == NOW
    assert result.items[0].enabled is False
    assert broker.observe_calls == 1


def test_missing_instance_is_typed_not_found(factory: sessionmaker[Session]) -> None:
    with pytest.raises(ManagedInstanceNotFoundError):
        service(factory, FakeBroker()).get(UUID(int=99))


@pytest.mark.parametrize(
    ("state", "health", "reason"),
    [
        (
            ContainerRuntimeState.STOPPED,
            ContainerHealthState.NOT_CONFIGURED,
            AddonInventoryUnavailableReason.KODI_NOT_RUNNING,
        ),
        (
            ContainerRuntimeState.RUNNING,
            ContainerHealthState.STARTING,
            AddonInventoryUnavailableReason.KODI_NOT_READY,
        ),
        (
            ContainerRuntimeState.RUNNING,
            ContainerHealthState.UNHEALTHY,
            AddonInventoryUnavailableReason.KODI_NOT_READY,
        ),
    ],
)
def test_stopped_or_unhealthy_instance_never_reaches_jsonrpc(
    factory: sessionmaker[Session],
    state: ContainerRuntimeState,
    health: ContainerHealthState,
    reason: AddonInventoryUnavailableReason,
) -> None:
    broker = FakeBroker()
    broker.container = broker._entry(state=state, health=health)
    result = service(factory, broker).get(INSTANCE_ID)
    assert result.unavailable_reason is reason
    assert result.items == []
    assert broker.observe_calls == 0


def test_unprovisioned_is_bounded_and_does_not_observe(
    factory: sessionmaker[Session],
) -> None:
    broker = FakeBroker()
    broker.container = None
    result = service(factory, broker).get(INSTANCE_ID)
    assert result.unavailable_reason is (
        AddonInventoryUnavailableReason.INSTANCE_NOT_PROVISIONED
    )
    assert result.complete is False
    assert broker.observe_calls == 0


def test_untrusted_network_or_container_observation_fails_closed(
    factory: sessionmaker[Session],
) -> None:
    broker = FakeBroker()
    broker.observe_error = True
    result = service(factory, broker).get(INSTANCE_ID)
    assert result.unavailable_reason is AddonInventoryUnavailableReason.KODI_NOT_READY


def test_observed_absent_kodi_process_is_not_running(
    factory: sessionmaker[Session],
) -> None:
    broker = FakeBroker()
    broker.process_state = KodiProcessState.NOT_RUNNING
    result = service(factory, broker).get(INSTANCE_ID)
    assert result.unavailable_reason is (
        AddonInventoryUnavailableReason.KODI_NOT_RUNNING
    )


def test_wrong_runtime_revision_never_constructs_network_adapter(
    factory: sessionmaker[Session],
) -> None:
    broker = FakeBroker()
    broker.observed_revision_id = OTHER_REVISION_ID
    result = service(factory, broker).get(INSTANCE_ID)
    assert result.unavailable_reason is AddonInventoryUnavailableReason.KODI_NOT_READY


def test_verified_deployment_is_precise_historical_attribution(
    factory: sessionmaker[Session],
) -> None:
    add_evidence(factory)
    provenance = service(factory, FakeBroker()).get(INSTANCE_ID).items[0].provenance
    assert provenance.classification is (
        KodiAddonProvenanceClassification.DEVELOPMENT_SOURCE_AT_LAST_VERIFIED_COPY
    )
    assert provenance.evidence_status is (
        KodiAddonProvenanceEvidenceStatus.LAST_OPERATION_VERIFIED
    )
    assert provenance.development_source_logical_name == "example source"
    assert provenance.verified_at == NOW


@pytest.mark.parametrize(
    ("confidence", "evidence_version", "runtime_revision", "container_id", "status"),
    [
        (
            "verified_at_last_operation",
            "9.9.9",
            REVISION_ID,
            CONTAINER_ID,
            KodiAddonProvenanceEvidenceStatus.STALE,
        ),
        (
            "stale",
            "1.2.3",
            REVISION_ID,
            CONTAINER_ID,
            KodiAddonProvenanceEvidenceStatus.STALE,
        ),
        (
            "unknown",
            "1.2.3",
            REVISION_ID,
            CONTAINER_ID,
            KodiAddonProvenanceEvidenceStatus.UNKNOWN,
        ),
        (
            "verified_at_last_operation",
            "1.2.3",
            OTHER_REVISION_ID,
            CONTAINER_ID,
            KodiAddonProvenanceEvidenceStatus.STALE,
        ),
        (
            "verified_at_last_operation",
            "1.2.3",
            REVISION_ID,
            "c" * 64,
            KodiAddonProvenanceEvidenceStatus.STALE,
        ),
    ],
)
def test_mismatched_or_invalidated_evidence_never_claims_source(
    factory: sessionmaker[Session],
    confidence: str,
    evidence_version: str,
    runtime_revision: UUID,
    container_id: str,
    status: KodiAddonProvenanceEvidenceStatus,
) -> None:
    add_evidence(
        factory,
        confidence=confidence,
        version=evidence_version,
        runtime_revision_id=runtime_revision,
        container_id=container_id,
    )
    provenance = service(factory, FakeBroker()).get(INSTANCE_ID).items[0].provenance
    assert provenance.classification is KodiAddonProvenanceClassification.UNKNOWN
    assert provenance.evidence_status is status


def test_attachment_or_matching_id_without_deployment_is_unknown(
    factory: sessionmaker[Session],
) -> None:
    provenance = service(factory, FakeBroker()).get(INSTANCE_ID).items[0].provenance
    assert provenance.classification is KodiAddonProvenanceClassification.UNKNOWN
    assert provenance.evidence_status is KodiAddonProvenanceEvidenceStatus.UNKNOWN


def test_exact_retained_zip_evidence_is_precise_historical_attribution(
    factory: sessionmaker[Session],
) -> None:
    add_zip_evidence(factory)
    provenance = service(factory, FakeBroker()).get(INSTANCE_ID).items[0].provenance
    assert provenance.classification is (
        KodiAddonProvenanceClassification.PRIOR_VERIFIED_ZIP_INSTALL
    )
    assert provenance.evidence_status is (
        KodiAddonProvenanceEvidenceStatus.LAST_OPERATION_VERIFIED
    )
    assert provenance.addon_zip_artifact_id == ZIP_ARTIFACT_ID
    assert provenance.addon_zip_logical_label == "uploaded example"


@pytest.mark.parametrize(
    ("confidence", "version", "revision", "container"),
    [
        ("stale", "1.2.3", REVISION_ID, CONTAINER_ID),
        ("verified_at_last_operation", "9.9.9", REVISION_ID, CONTAINER_ID),
        ("verified_at_last_operation", "1.2.3", OTHER_REVISION_ID, CONTAINER_ID),
        ("verified_at_last_operation", "1.2.3", REVISION_ID, "c" * 64),
    ],
)
def test_stale_or_wrong_zip_evidence_is_unknown(
    factory: sessionmaker[Session],
    confidence: str,
    version: str,
    revision: UUID,
    container: str,
) -> None:
    add_zip_evidence(
        factory,
        confidence=confidence,
        version=version,
        runtime_revision_id=revision,
        container_id=container,
    )
    provenance = service(factory, FakeBroker()).get(INSTANCE_ID).items[0].provenance
    assert provenance.classification is KodiAddonProvenanceClassification.UNKNOWN


def test_uploaded_artifact_without_install_evidence_is_unknown(
    factory: sessionmaker[Session],
) -> None:
    add_zip_evidence(factory, include_evidence=False)
    provenance = service(factory, FakeBroker()).get(INSTANCE_ID).items[0].provenance
    assert provenance.classification is KodiAddonProvenanceClassification.UNKNOWN


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (
            JsonRpcUnavailableError("unavailable"),
            AddonInventoryUnavailableReason.JSONRPC_UNAVAILABLE,
        ),
        (
            KodiAddonInventoryUnsupportedResponseError("unsupported"),
            AddonInventoryUnavailableReason.UNSUPPORTED_RESPONSE,
        ),
        (
            KodiAddonInventoryMalformedDataError("malformed"),
            AddonInventoryUnavailableReason.MALFORMED_ADDON_DATA,
        ),
        (
            KodiAddonInventoryLimitExceededError("too many"),
            AddonInventoryUnavailableReason.INVENTORY_LIMIT_EXCEEDED,
        ),
    ],
)
def test_adapter_failures_map_to_safe_whole_inventory_outcomes(
    factory: sessionmaker[Session],
    error: BaseException,
    reason: AddonInventoryUnavailableReason,
) -> None:
    result = service(factory, FakeBroker(), error).get(INSTANCE_ID)
    assert result.query_status is AddonInventoryQueryStatus.UNAVAILABLE
    assert result.unavailable_reason is reason
    assert result.complete is False
    assert result.items == []
