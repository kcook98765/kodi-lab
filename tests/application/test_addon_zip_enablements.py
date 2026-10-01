from __future__ import annotations

import json
import os
import zipfile
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import update
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.addon_zip_enablements import (
    AddonZipEnablementError,
    AddonZipEnablementOperationCoordinator,
    AddonZipEnablementService,
)
from kodi_lab.application.addon_zip_installations import (
    invalidate_addon_zip_installation_evidence,
)
from kodi_lab.application.mutations import MUTATION_LEASE_DURATION
from kodi_lab.application.reconciliation import StaleOperationReconciliationDispatcher
from kodi_lab.application.snapshot_operations import (
    SnapshotCreateConflictError,
    SnapshotCreateOperationCoordinator,
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
    VerifyAddonZipEnablementDestinationResult,
)
from kodi_lab.development.addon_zip import (
    create_upload_stage,
    finish_upload_stage,
    validate_and_publish_addon_zip,
    write_upload_chunk,
)
from kodi_lab.domain import (
    AddonZipEnablementOperationState,
    AddonZipEnablementResult,
    AddonZipInstallationConfidence,
    AddonZipPayloadVerificationOutcome,
)
from kodi_lab.kodi.addons import (
    KodiAddonDetailsRecord,
    KodiAddonEnablementAmbiguousError,
    KodiAddonEnableRejectedError,
    KodiAddonInventoryRecord,
    KodiAddonInventoryResult,
    KodiReportedAddonDependency,
)
from kodi_lab.kodi.readiness import JsonRpcUnavailableError, KodiApplicationIdentity
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.storage.metadata import Base
from kodi_lab.storage.models import (
    AddonZipArtifactRow,
    AddonZipEnablementOperationRow,
    AddonZipInstallationEvidenceRow,
    AddonZipInstallationOperationRow,
    ManagedInstanceRow,
    RuntimeDefinitionRow,
    RuntimeExecutionProfileRow,
    RuntimeRevisionRow,
    TargetRow,
)
from kodi_lab.storage.repositories import (
    AddonZipEnablementOperationRepository,
    AddonZipInstallationEvidenceRepository,
    ManagedInstanceMutationLeaseRepository,
)

INSTANCE_ID = UUID("e8250000-0000-4000-8000-000000000001")
ARTIFACT_ID = UUID("e8250000-0000-4000-8000-000000000002")
INSTALL_ID = UUID("e8250000-0000-4000-8000-000000000003")
REVISION_ID = UUID("e8250000-0000-4000-8000-000000000004")
DEFINITION_ID = UUID("e8250000-0000-4000-8000-000000000005")
OTHER_REVISION_ID = UUID("e8250000-0000-4000-8000-000000000006")
CONTAINER_ID = "a" * 64
ADDON_ID = "plugin.video.uploaded"
VERSION = "1.2.3"
NOW = datetime(2026, 9, 17, 21, 0, tzinfo=UTC)


@pytest.fixture
def factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'zip-enable.db'}")
    Base.metadata.create_all(engine)
    result = create_session_factory(engine)
    with result.begin() as session:
        session.add(
            RuntimeDefinitionRow(
                id=str(DEFINITION_ID), slug="zip-enable", kodi_version="21.3"
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
            RuntimeRevisionRow(
                id=str(OTHER_REVISION_ID),
                runtime_definition_id=str(DEFINITION_ID),
                revision_number=2,
                source_kind="test",
                source_reference="test-other",
                content_digest="sha256:" + "6" * 64,
                manifest_reference="test-other",
                manifest_digest="sha256:" + "7" * 64,
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
            RuntimeExecutionProfileRow(
                runtime_revision_id=str(OTHER_REVISION_ID),
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
                name="ZIP enable instance",
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
                logical_label="Uploaded enable test",
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


def target(
    *,
    enabled: bool = False,
    version: str = VERSION,
    kodi_type: str = "xbmc.python.pluginsource",
) -> KodiAddonInventoryRecord:
    return KodiAddonInventoryRecord(ADDON_ID, "Uploaded", version, enabled, kodi_type)


def dependency(dependency_id: str, *, enabled: bool) -> KodiAddonInventoryRecord:
    return KodiAddonInventoryRecord(
        dependency_id, dependency_id, "1.0.0", enabled, "xbmc.python.module"
    )


class FakeBroker:
    def __init__(self) -> None:
        self.container_id = CONTAINER_ID
        self.runtime_state = ContainerRuntimeState.RUNNING
        self.health_state = ContainerHealthState.HEALTHY
        self.process_state = KodiProcessState.RUNNING
        self.observed_revision = REVISION_ID
        self.verify_calls = 0
        self.verify_matched = True
        self.verify_error = False
        self.verify_identity_field: str | None = None

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

    def verify_addon_zip_enablement_destination(
        self,
        *,
        managed_instance_id: str,
        artifact_id: str,
        enablement_operation_id: str,
        expected_container_id: str,
    ) -> VerifyAddonZipEnablementDestinationResult:
        self.verify_calls += 1
        if self.verify_error:
            raise ClientError("injected verifier failure")
        values = {
            "managed_instance_id": managed_instance_id,
            "artifact_id": artifact_id,
            "enablement_operation_id": enablement_operation_id,
            "runtime_revision_id": str(REVISION_ID),
            "container_id": expected_container_id,
            "addon_id": ADDON_ID,
            "addon_version": VERSION,
        }
        if self.verify_identity_field is not None:
            values[self.verify_identity_field] = str(UUID(int=17))
        return VerifyAddonZipEnablementDestinationResult(
            **values,
            destination_state=(
                "exact_payload_present"
                if self.verify_matched
                else "partial_or_unexpected"
            ),
            matched=self.verify_matched,
        )


class FakeInventory:
    def __init__(self, items: list[KodiAddonInventoryRecord]) -> None:
        self.items = items
        self.calls = 0
        self.error: BaseException | None = None

    def get_inventory(self) -> KodiAddonInventoryResult:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return KodiAddonInventoryResult(
            KodiApplicationIdentity("Kodi", 21, 3, "stable"), tuple(self.items)
        )


class FakeEnableAdapter:
    def __init__(self) -> None:
        self.enabled = False
        self.details_version = VERSION
        self.kodi_type = "xbmc.python.pluginsource"
        self.dependencies: tuple[KodiReportedAddonDependency, ...] = ()
        self.enable_calls: list[str] = []
        self.enable_error: BaseException | None = None
        self.post_enabled: bool | None = None
        self.post_identity_wrong = False

    def get_addon_details(self, addon_id: str) -> KodiAddonDetailsRecord:
        enabled = self.enabled
        if self.enable_calls and self.post_enabled is not None:
            enabled = self.post_enabled
        return KodiAddonDetailsRecord(
            addon_id=(
                "plugin.video.wrong"
                if self.post_identity_wrong and self.enable_calls
                else ADDON_ID
            ),
            name="Uploaded",
            version=self.details_version,
            enabled=enabled,
            kodi_type=self.kodi_type,
            dependencies=self.dependencies,
        )

    def enable(self, addon_id: str) -> None:
        self.enable_calls.append(addon_id)
        if self.enable_error is not None:
            raise self.enable_error
        self.enabled = True


def service(
    factory: sessionmaker[Session],
    broker: FakeBroker,
    inventory: FakeInventory,
    adapter: FakeEnableAdapter,
    *,
    after_enable_hook: object | None = None,
) -> AddonZipEnablementService:
    def hook() -> None:
        if isinstance(after_enable_hook, BaseException):
            raise after_enable_hook

    return AddonZipEnablementService(
        factory,
        broker,
        inventory_adapter_factory=lambda _observation, _profile: inventory,
        enable_adapter_factory=lambda _observation, _profile: adapter,
        after_enable_hook=None if after_enable_hook is None else hook,
    )


def mutate(
    factory: sessionmaker[Session], model: type[object], **values: object
) -> None:
    with factory.begin() as session:
        session.execute(update(model).values(**values))


def test_enable_success_uses_exact_authority_and_retains_install_evidence(
    factory: sessionmaker[Session],
) -> None:
    broker = FakeBroker()
    adapter = FakeEnableAdapter()
    result = service(factory, broker, FakeInventory([target()]), adapter).enable(
        INSTANCE_ID, ARTIFACT_ID
    )
    assert result.state is AddonZipEnablementOperationState.SUCCEEDED
    assert result.result is AddonZipEnablementResult.ENABLED
    assert result.payload_verification is AddonZipPayloadVerificationOutcome.MATCHED
    assert result.prior_enabled is False and result.result_enabled is True
    assert result.enable_performed is True
    assert adapter.enable_calls == [ADDON_ID]
    assert broker.verify_calls == 1
    with factory() as session:
        evidence = AddonZipInstallationEvidenceRepository(session).get(
            INSTANCE_ID, ADDON_ID
        )
        assert evidence is not None
        assert (
            evidence.confidence
            is AddonZipInstallationConfidence.VERIFIED_AT_LAST_OPERATION
        )
        assert (
            ManagedInstanceMutationLeaseRepository(session).current(INSTANCE_ID) is None
        )


def test_already_enabled_runs_payload_and_dependency_checks_without_mutation(
    factory: sessionmaker[Session],
) -> None:
    broker = FakeBroker()
    adapter = FakeEnableAdapter()
    adapter.enabled = True
    inventory = FakeInventory([target(enabled=True)])
    result = service(factory, broker, inventory, adapter).enable(
        INSTANCE_ID, ARTIFACT_ID
    )
    assert result.result is AddonZipEnablementResult.ALREADY_ENABLED
    assert result.enable_performed is False
    assert broker.verify_calls == 1 and inventory.calls == 1
    assert adapter.enable_calls == []


def test_uploaded_but_never_installed_is_rejected_without_operation(
    factory: sessionmaker[Session],
) -> None:
    with factory.begin() as session:
        session.query(AddonZipInstallationEvidenceRow).delete()
    with pytest.raises(AddonZipEnablementError, match="no successful installation"):
        service(
            factory, FakeBroker(), FakeInventory([target()]), FakeEnableAdapter()
        ).enable(INSTANCE_ID, ARTIFACT_ID)
    with factory() as session:
        assert (
            AddonZipEnablementOperationRepository(session).list_for_original_instance(
                INSTANCE_ID
            )
            == []
        )


@pytest.mark.parametrize(
    ("instance_id", "artifact_id"),
    [(UUID(int=901), ARTIFACT_ID), (INSTANCE_ID, UUID(int=902))],
)
def test_wrong_instance_or_artifact_has_no_enable_authority(
    factory: sessionmaker[Session], instance_id: UUID, artifact_id: UUID
) -> None:
    broker = FakeBroker()
    adapter = FakeEnableAdapter()
    with pytest.raises(AddonZipEnablementError):
        service(factory, broker, FakeInventory([target()]), adapter).enable(
            instance_id, artifact_id
        )
    assert broker.verify_calls == 0 and adapter.enable_calls == []


@pytest.mark.parametrize(
    ("model", "values", "failure"),
    [
        (
            AddonZipInstallationEvidenceRow,
            {"confidence": "stale"},
            "zip_installation_evidence_stale",
        ),
        (
            AddonZipInstallationEvidenceRow,
            {"confidence": "unknown"},
            "zip_installation_evidence_unknown",
        ),
        (
            ManagedInstanceRow,
            {"runtime_revision_id": str(OTHER_REVISION_ID)},
            "zip_installation_lineage_stale",
        ),
        (
            AddonZipInstallationEvidenceRow,
            {"trusted_container_id": "b" * 64},
            "zip_installation_lineage_stale",
        ),
        (
            RuntimeExecutionProfileRow,
            {"python_bytecode_policy": "interpreter_default"},
            "zip_enablement_unsupported",
        ),
        (
            ManagedInstanceRow,
            {"instance_type": "persistent"},
            "zip_enablement_unsupported",
        ),
    ],
)
def test_authority_failures_send_no_enable_request(
    factory: sessionmaker[Session],
    model: type[object],
    values: dict[str, object],
    failure: str,
) -> None:
    mutate(factory, model, **values)
    broker = FakeBroker()
    adapter = FakeEnableAdapter()
    result = service(factory, broker, FakeInventory([target()]), adapter).enable(
        INSTANCE_ID, ARTIFACT_ID
    )
    assert result.failure_reason == failure
    assert adapter.enable_calls == []


@pytest.mark.parametrize(
    ("runtime_state", "health_state", "process_state", "failure"),
    [
        (
            ContainerRuntimeState.STOPPED,
            ContainerHealthState.NOT_CONFIGURED,
            KodiProcessState.NOT_RUNNING,
            "instance_not_running",
        ),
        (
            ContainerRuntimeState.RUNNING,
            ContainerHealthState.UNHEALTHY,
            KodiProcessState.RUNNING,
            "kodi_not_ready",
        ),
        (
            ContainerRuntimeState.RUNNING,
            ContainerHealthState.HEALTHY,
            KodiProcessState.NOT_RUNNING,
            "kodi_not_ready",
        ),
    ],
)
def test_runtime_preconditions_never_auto_start_or_enable(
    factory: sessionmaker[Session],
    runtime_state: ContainerRuntimeState,
    health_state: ContainerHealthState,
    process_state: KodiProcessState,
    failure: str,
) -> None:
    broker = FakeBroker()
    broker.runtime_state = runtime_state
    broker.health_state = health_state
    broker.process_state = process_state
    adapter = FakeEnableAdapter()
    result = service(factory, broker, FakeInventory([target()]), adapter).enable(
        INSTANCE_ID, ARTIFACT_ID
    )
    assert result.failure_reason == failure
    assert broker.verify_calls == 0 and adapter.enable_calls == []


@pytest.mark.parametrize("unknown", [False, True])
def test_payload_changed_or_unknown_invalidates_only_target_evidence(
    factory: sessionmaker[Session], unknown: bool
) -> None:
    broker = FakeBroker()
    broker.verify_matched = False
    broker.verify_error = unknown
    adapter = FakeEnableAdapter()
    result = service(factory, broker, FakeInventory([target()]), adapter).enable(
        INSTANCE_ID, ARTIFACT_ID
    )
    expected = (
        AddonZipPayloadVerificationOutcome.UNKNOWN
        if unknown
        else AddonZipPayloadVerificationOutcome.CHANGED
    )
    expected_confidence = (
        AddonZipInstallationConfidence.UNKNOWN
        if unknown
        else AddonZipInstallationConfidence.STALE
    )
    assert result.payload_verification is expected
    assert adapter.enable_calls == []
    with factory() as session:
        evidence = AddonZipInstallationEvidenceRepository(session).get(
            INSTANCE_ID, ADDON_ID
        )
        assert evidence is not None and evidence.confidence is expected_confidence


@pytest.mark.parametrize(
    ("inventory_target", "details_version", "details_type", "failure"),
    [
        (None, VERSION, "xbmc.python.pluginsource", "addon_not_recognized"),
        (
            target(version="9.9"),
            VERSION,
            "xbmc.python.pluginsource",
            "addon_version_mismatch",
        ),
        (
            target(kodi_type="xbmc.python.module"),
            VERSION,
            "xbmc.python.pluginsource",
            "addon_type_mismatch",
        ),
        (target(), "9.9", "xbmc.python.pluginsource", "addon_version_mismatch"),
        (target(), VERSION, "xbmc.python.module", "addon_type_mismatch"),
    ],
)
def test_exact_recognition_identity_is_required(
    factory: sessionmaker[Session],
    inventory_target: KodiAddonInventoryRecord | None,
    details_version: str,
    details_type: str,
    failure: str,
) -> None:
    adapter = FakeEnableAdapter()
    adapter.details_version = details_version
    adapter.kodi_type = details_type
    items = [] if inventory_target is None else [inventory_target]
    result = service(factory, FakeBroker(), FakeInventory(items), adapter).enable(
        INSTANCE_ID, ARTIFACT_ID
    )
    assert result.failure_reason == failure and adapter.enable_calls == []


@pytest.mark.parametrize(
    ("present", "enabled", "optional", "version", "expected", "blocked"),
    [
        (True, True, False, None, None, False),
        (False, False, False, None, "dependency_missing", True),
        (True, False, False, None, "dependency_disabled", True),
        (False, False, True, None, None, False),
        (True, True, False, "2.0.0", "dependency_version_unverified", True),
    ],
)
def test_zip_dependency_preflight_is_non_mutating(
    factory: sessionmaker[Session],
    present: bool,
    enabled: bool,
    optional: bool,
    version: str | None,
    expected: str | None,
    blocked: bool,
) -> None:
    dependency_id = "script.module.required"
    declarations = [
        {
            "id": dependency_id,
            "version": version,
            "minversion": None,
            "optional": optional,
        }
    ]
    mutate(factory, AddonZipArtifactRow, dependencies_json=json.dumps(declarations))
    adapter = FakeEnableAdapter()
    adapter.dependencies = (
        KodiReportedAddonDependency(dependency_id, version, optional),
    )
    items = [target()]
    if present:
        items.append(dependency(dependency_id, enabled=enabled))
    result = service(factory, FakeBroker(), FakeInventory(items), adapter).enable(
        INSTANCE_ID, ARTIFACT_ID
    )
    if blocked:
        assert result.result is AddonZipEnablementResult.DEPENDENCY_BLOCKED
        assert result.failure_reason == expected
        assert adapter.enable_calls == []
    else:
        assert result.result is AddonZipEnablementResult.ENABLED


def test_dependency_declaration_mismatch_blocks_before_enable(
    factory: sessionmaker[Session],
) -> None:
    mutate(
        factory,
        AddonZipArtifactRow,
        dependencies_json=json.dumps(
            [
                {
                    "id": "script.module.one",
                    "version": None,
                    "minversion": None,
                    "optional": False,
                }
            ]
        ),
    )
    adapter = FakeEnableAdapter()
    adapter.dependencies = (
        KodiReportedAddonDependency("script.module.other", None, False),
    )
    result = service(factory, FakeBroker(), FakeInventory([target()]), adapter).enable(
        INSTANCE_ID, ARTIFACT_ID
    )
    assert result.failure_reason == "dependency_state_unknown"
    assert adapter.enable_calls == []


def test_omega_system_python_dependency_allows_enablement(
    factory: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    """A validated ZIP reaches enablement with Omega's actual dependency fields."""

    payload = BytesIO()
    with zipfile.ZipFile(payload, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            f"{ADDON_ID}/addon.xml",
            f'<addon id="{ADDON_ID}" name="Uploaded" version="{VERSION}">'
            '<requires><import addon="xbmc.python" version="3.0.0" /></requires>'
            '<extension point="xbmc.python.pluginsource" library="default.py">'
            "<provides>video</provides></extension>"
            '<extension point="xbmc.addon.metadata" /></addon>',
        )
        archive.writestr(f"{ADDON_ID}/default.py", "import xbmcplugin\n")
    stage_fd, _stage_path = create_upload_stage(tmp_path, ARTIFACT_ID)
    try:
        total = write_upload_chunk(stage_fd, payload.getvalue(), 0)
        finish_upload_stage(stage_fd, total)
    finally:
        os.close(stage_fd)
    validated = validate_and_publish_addon_zip(tmp_path, ARTIFACT_ID)
    assert validated.addon_id == ADDON_ID
    assert validated.addon_version == VERSION
    assert [
        (item.dependency_id, item.version, item.minversion)
        for item in validated.dependencies
    ] == [("xbmc.python", "3.0.0", None)]

    mutate(
        factory,
        AddonZipArtifactRow,
        dependencies_json=json.dumps(
            [
                {
                    "id": item.dependency_id,
                    "version": item.version,
                    "minversion": item.minversion,
                    "optional": item.optional,
                }
                for item in validated.dependencies
            ]
        ),
    )
    adapter = FakeEnableAdapter()
    adapter.dependencies = (
        KodiReportedAddonDependency("xbmc.python", "3.0.0", False, "3.0.0"),
    )
    result = service(factory, FakeBroker(), FakeInventory([target()]), adapter).enable(
        INSTANCE_ID, ARTIFACT_ID
    )
    assert result.result is AddonZipEnablementResult.ENABLED
    assert adapter.enable_calls == [ADDON_ID]


def test_omega_python_requirement_above_installed_api_blocks_enablement(
    factory: sessionmaker[Session],
) -> None:
    mutate(
        factory,
        AddonZipArtifactRow,
        dependencies_json=json.dumps(
            [
                {
                    "id": "xbmc.python",
                    "version": "3.0.2",
                    "minversion": None,
                    "optional": False,
                }
            ]
        ),
    )
    adapter = FakeEnableAdapter()
    adapter.dependencies = (
        KodiReportedAddonDependency("xbmc.python", "3.0.2", False, "3.0.2"),
    )
    result = service(factory, FakeBroker(), FakeInventory([target()]), adapter).enable(
        INSTANCE_ID, ARTIFACT_ID
    )
    assert result.failure_reason == "dependency_incompatible"
    assert adapter.enable_calls == []


def test_unavailable_dependency_inventory_blocks_before_enable(
    factory: sessionmaker[Session],
) -> None:
    inventory = FakeInventory([target()])
    inventory.error = JsonRpcUnavailableError("unavailable")
    adapter = FakeEnableAdapter()
    result = service(factory, FakeBroker(), inventory, adapter).enable(
        INSTANCE_ID, ARTIFACT_ID
    )
    assert result.failure_reason == "jsonrpc_unavailable"
    assert adapter.enable_calls == []


@pytest.mark.parametrize(
    "failure",
    [
        KodiAddonEnablementAmbiguousError("ambiguous"),
        RuntimeError("transport secret must not persist"),
    ],
)
def test_post_dispatch_ambiguity_requires_reconciliation_without_retry(
    factory: sessionmaker[Session], failure: BaseException
) -> None:
    adapter = FakeEnableAdapter()
    adapter.enable_error = failure
    result = service(factory, FakeBroker(), FakeInventory([target()]), adapter).enable(
        INSTANCE_ID, ARTIFACT_ID
    )
    assert result.state is AddonZipEnablementOperationState.RECONCILIATION_REQUIRED
    assert result.result is AddonZipEnablementResult.RECONCILIATION_REQUIRED
    assert result.failure_reason == "zip_enablement_reconciliation_required"
    assert adapter.enable_calls == [ADDON_ID]
    with factory() as session:
        evidence = AddonZipInstallationEvidenceRepository(session).get(
            INSTANCE_ID, ADDON_ID
        )
        assert evidence is not None
        assert evidence.confidence is AddonZipInstallationConfidence.UNKNOWN
        assert "secret" not in (result.failure_reason or "")


def test_fault_after_dispatch_never_retries_disables_or_rewrites_success(
    factory: sessionmaker[Session],
) -> None:
    adapter = FakeEnableAdapter()
    result = service(
        factory,
        FakeBroker(),
        FakeInventory([target()]),
        adapter,
        after_enable_hook=RuntimeError("post-dispatch fault"),
    ).enable(INSTANCE_ID, ARTIFACT_ID)
    assert result.state is AddonZipEnablementOperationState.RECONCILIATION_REQUIRED
    assert result.enable_performed is True
    assert adapter.enable_calls == [ADDON_ID]


def test_definitive_kodi_rejection_is_failed_not_ambiguous(
    factory: sessionmaker[Session],
) -> None:
    adapter = FakeEnableAdapter()
    adapter.enable_error = KodiAddonEnableRejectedError("rejected")
    result = service(factory, FakeBroker(), FakeInventory([target()]), adapter).enable(
        INSTANCE_ID, ARTIFACT_ID
    )
    assert result.state is AddonZipEnablementOperationState.FAILED
    assert result.result is AddonZipEnablementResult.REJECTED
    assert result.result_enabled is False


def test_untrustworthy_postquery_is_reconciliation_required(
    factory: sessionmaker[Session],
) -> None:
    adapter = FakeEnableAdapter()
    adapter.post_identity_wrong = True
    result = service(factory, FakeBroker(), FakeInventory([target()]), adapter).enable(
        INSTANCE_ID, ARTIFACT_ID
    )
    assert result.state is AddonZipEnablementOperationState.RECONCILIATION_REQUIRED
    assert adapter.enable_calls == [ADDON_ID]


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


@pytest.mark.parametrize("mutation_started", [False, True])
def test_stale_lease_distinguishes_preflight_from_post_dispatch(
    factory: sessionmaker[Session], mutation_started: bool
) -> None:
    clock = Clock()
    coordinator = AddonZipEnablementOperationCoordinator(factory, clock=clock)
    handle = coordinator.begin(
        INSTANCE_ID,
        ARTIFACT_ID,
        INSTALL_ID,
        addon_id=ADDON_ID,
        addon_version=VERSION,
        runtime_revision_id=REVISION_ID,
    )
    if mutation_started:
        coordinator.update(handle, enable_performed=True)
    clock.now += MUTATION_LEASE_DURATION + timedelta(seconds=1)
    swept = StaleOperationReconciliationDispatcher(factory, clock=clock).sweep_instance(
        INSTANCE_ID
    )
    assert swept.terminalized_operations == [handle.operation_id]
    with factory() as session:
        operation = AddonZipEnablementOperationRepository(session).get(
            handle.operation_id
        )
        evidence = AddonZipInstallationEvidenceRepository(session).get(
            INSTANCE_ID, ADDON_ID
        )
        lease = ManagedInstanceMutationLeaseRepository(session).current(INSTANCE_ID)
    assert operation is not None and evidence is not None
    assert lease is None
    if mutation_started:
        assert (
            operation.state is AddonZipEnablementOperationState.RECONCILIATION_REQUIRED
        )
        assert evidence.confidence is AddonZipInstallationConfidence.UNKNOWN
    else:
        assert operation.state is AddonZipEnablementOperationState.FAILED
        assert operation.reconciliation_required is False
        assert (
            evidence.confidence
            is AddonZipInstallationConfidence.VERIFIED_AT_LAST_OPERATION
        )


def test_active_zip_enablement_conflicts_with_snapshot_create(
    factory: sessionmaker[Session],
) -> None:
    coordinator = AddonZipEnablementOperationCoordinator(factory)
    coordinator.begin(
        INSTANCE_ID,
        ARTIFACT_ID,
        INSTALL_ID,
        addon_id=ADDON_ID,
        addon_version=VERSION,
        runtime_revision_id=REVISION_ID,
    )
    with pytest.raises(SnapshotCreateConflictError):
        SnapshotCreateOperationCoordinator(factory).queue(INSTANCE_ID, label=None)


def test_runtime_invalidation_stales_evidence_but_retains_history_authority_separation(
    factory: sessionmaker[Session],
) -> None:
    result = service(
        factory, FakeBroker(), FakeInventory([target()]), FakeEnableAdapter()
    ).enable(INSTANCE_ID, ARTIFACT_ID)
    with factory.begin() as session:
        assert invalidate_addon_zip_installation_evidence(session, INSTANCE_ID) == 1
    with factory() as session:
        history = AddonZipEnablementOperationRepository(session).get(
            result.operation_id
        )
        evidence = AddonZipInstallationEvidenceRepository(session).get(
            INSTANCE_ID, ADDON_ID
        )
    assert (
        history is not None
        and history.state is AddonZipEnablementOperationState.SUCCEEDED
    )
    assert (
        evidence is not None
        and evidence.confidence is AddonZipInstallationConfidence.STALE
    )
    retry_adapter = FakeEnableAdapter()
    retry_adapter.enabled = True
    blocked = service(
        factory,
        FakeBroker(),
        FakeInventory([target(enabled=True)]),
        retry_adapter,
    ).enable(INSTANCE_ID, ARTIFACT_ID)
    assert blocked.failure_reason == "zip_installation_evidence_stale"
    assert retry_adapter.enable_calls == []


def test_retention_keeps_newest_twenty_ordinary_and_all_reconciliation(
    factory: sessionmaker[Session],
) -> None:
    with factory.begin() as session:
        for index in range(22):
            timestamp = NOW + timedelta(seconds=index)
            session.add(
                AddonZipEnablementOperationRow(
                    id=str(UUID(int=1000 + index)),
                    original_managed_instance_id=str(INSTANCE_ID),
                    managed_instance_id=str(INSTANCE_ID),
                    artifact_id=str(ARTIFACT_ID),
                    installation_operation_id=str(INSTALL_ID),
                    addon_id=ADDON_ID,
                    addon_version=VERSION,
                    runtime_revision_id=str(REVISION_ID),
                    expected_container_id=CONTAINER_ID,
                    requested_at=timestamp.isoformat(),
                    started_at=timestamp.isoformat(),
                    completed_at=timestamp.isoformat(),
                    state="succeeded",
                    phase="finalize",
                    result_classification="enabled",
                    payload_verification="matched",
                    prior_enabled=False,
                    result_enabled=True,
                    enable_performed=True,
                    reconciliation_required=False,
                )
            )
        timestamp = NOW + timedelta(seconds=30)
        session.add(
            AddonZipEnablementOperationRow(
                id=str(UUID(int=2000)),
                original_managed_instance_id=str(INSTANCE_ID),
                managed_instance_id=str(INSTANCE_ID),
                artifact_id=str(ARTIFACT_ID),
                installation_operation_id=str(INSTALL_ID),
                addon_id=ADDON_ID,
                addon_version=VERSION,
                runtime_revision_id=str(REVISION_ID),
                expected_container_id=CONTAINER_ID,
                requested_at=timestamp.isoformat(),
                started_at=timestamp.isoformat(),
                completed_at=timestamp.isoformat(),
                state="reconciliation_required",
                phase="finalize",
                result_classification="reconciliation_required",
                payload_verification="matched",
                prior_enabled=False,
                result_enabled=None,
                enable_performed=True,
                failure_reason="zip_enablement_reconciliation_required",
                reconciliation_required=True,
            )
        )
    with factory.begin() as session:
        removed = AddonZipEnablementOperationRepository(session).prune_terminal(
            INSTANCE_ID, retain=20
        )
    assert removed == 2
    with factory() as session:
        retained = AddonZipEnablementOperationRepository(
            session
        ).list_for_original_instance(INSTANCE_ID)
    assert len(retained) == 21
    assert sum(item.reconciliation_required for item in retained) == 1
