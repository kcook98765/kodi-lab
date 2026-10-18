from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import create_engine, update
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.development_source_enablements import (
    DevelopmentSourceEnablementConflictError,
    DevelopmentSourceEnablementOperationCoordinator,
    DevelopmentSourceEnablementService,
)
from kodi_lab.application.mutations import MUTATION_LEASE_DURATION
from kodi_lab.application.reconciliation import StaleOperationReconciliationDispatcher
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
    DevelopmentSourceEnablementOperationState,
    DevelopmentSourceEnablementResult,
    ManagedInstanceMutationKind,
)
from kodi_lab.kodi.addons import (
    KodiAddonDetailsRecord,
    KodiAddonEnablementAmbiguousError,
    KodiAddonEnableRejectedError,
    KodiAddonInventoryRecord,
    KodiAddonInventoryResult,
    KodiReportedAddonDependency,
)
from kodi_lab.kodi.readiness import KodiApplicationIdentity
from kodi_lab.storage.engine import create_session_factory
from kodi_lab.storage.metadata import Base
from kodi_lab.storage.models import (
    DevelopmentSourceDeploymentOperationRow,
    DevelopmentSourceDeploymentRow,
    DevelopmentSourceEnablementOperationRow,
    DevelopmentSourceRow,
    ManagedInstanceDevelopmentSourceRow,
    ManagedInstanceRow,
    RuntimeDefinitionRow,
    RuntimeExecutionProfileRow,
    RuntimeRevisionRow,
    TargetRow,
)
from kodi_lab.storage.repositories import (
    DevelopmentSourceEnablementOperationRepository,
    GlobalHeavyOperationLeaseRepository,
    ManagedInstanceMutationLeaseRepository,
)

INSTANCE_ID = UUID("e8100000-0000-4000-8000-000000000001")
SOURCE_ID = UUID("e8100000-0000-4000-8000-000000000002")
REVISION_ID = UUID("e8100000-0000-4000-8000-000000000003")
DEFINITION_ID = UUID("e8100000-0000-4000-8000-000000000004")
DEPLOYMENT_OPERATION_ID = UUID("e8100000-0000-4000-8000-000000000005")
CONTAINER_ID = "a" * 64
NOW = datetime(2026, 9, 17, 16, 0, tzinfo=UTC)
ADDON_ID = "plugin.video.example"
VERSION = "1.2.3"


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_engine(f"sqlite:///{tmp_path / 'enablements.db'}")
    Base.metadata.create_all(engine)
    with create_session_factory(engine).begin() as session:
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
                name="enable instance",
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
                id=str(DEPLOYMENT_OPERATION_ID),
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
                last_successful_operation_id=str(DEPLOYMENT_OPERATION_ID),
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
    result = create_session_factory(engine)
    source = tmp_path / "source"
    source.mkdir()
    _write_descriptor(source)
    yield result
    engine.dispose()


def _write_descriptor(
    source: Path,
    *,
    extension: str = "xbmc.python.pluginsource",
    dependency_xml: str = "",
) -> None:
    (source / "addon.xml").write_text(
        f'<addon id="{ADDON_ID}" name="Example" version="{VERSION}">'
        f'{dependency_xml}<extension point="{extension}"/>'
        "</addon>",
        encoding="utf-8",
    )


class FakeBroker:
    def __init__(self) -> None:
        self.destination_matches = True
        self.verify_calls = 0

    def managed_container_inventory(self) -> ManagedContainerInventoryResult:
        return ManagedContainerInventoryResult(
            containers=[
                ManagedContainerInventoryEntry(
                    managed_instance_id=str(INSTANCE_ID),
                    container_id=CONTAINER_ID,
                    image_id="sha256:" + "5" * 64,
                    runtime_state=ContainerRuntimeState.RUNNING,
                    health_state=ContainerHealthState.HEALTHY,
                )
            ]
        )

    def observe_managed_kodi_runtime(
        self, *, managed_instance_id: str, expected_container_id: str
    ) -> ObserveManagedKodiRuntimeResult:
        assert managed_instance_id == str(INSTANCE_ID)
        assert expected_container_id == CONTAINER_ID
        return ObserveManagedKodiRuntimeResult(
            managed_instance_id=managed_instance_id,
            runtime_revision_id=str(REVISION_ID),
            container_id=expected_container_id,
            runtime_state=ContainerRuntimeState.RUNNING,
            process_state=KodiProcessState.RUNNING,
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
        if not self.destination_matches:
            raise DevelopmentSourceDeploymentClientError(
                "destination_mismatch", "destination mismatch", request_id="test"
            )
        return VerifyAttachedDevelopmentSourceDestinationResult(
            managed_instance_id=managed_instance_id,
            development_source_id=development_source_id,
            runtime_revision_id=str(REVISION_ID),
            container_id=expected_container_id,
            addon_id=ADDON_ID,
            addon_version=VERSION,
            matched=True,
        )


class FakeInventoryAdapter:
    def __init__(self, items: list[KodiAddonInventoryRecord]) -> None:
        self.items = items

    def get_inventory(self) -> KodiAddonInventoryResult:
        return KodiAddonInventoryResult(
            identity=KodiApplicationIdentity("Kodi", 21, 3, "stable"),
            items=tuple(self.items),
        )


class FakeEnableAdapter:
    def __init__(self) -> None:
        self.enabled = False
        self.dependencies: tuple[KodiReportedAddonDependency, ...] = ()
        self.enable_calls: list[str] = []
        self.enable_error: BaseException | None = None
        self.post_enabled: bool | None = None
        self.kodi_type = "xbmc.python.pluginsource"

    def get_addon_details(self, addon_id: str) -> KodiAddonDetailsRecord:
        assert addon_id == ADDON_ID
        state = self.enabled
        if self.enable_calls and self.post_enabled is not None:
            state = self.post_enabled
        return KodiAddonDetailsRecord(
            addon_id=ADDON_ID,
            name="Example",
            version=VERSION,
            enabled=state,
            kodi_type=self.kodi_type,
            dependencies=self.dependencies,
        )

    def enable(self, addon_id: str) -> None:
        self.enable_calls.append(addon_id)
        if self.enable_error is not None:
            raise self.enable_error
        self.enabled = True


def _target(*, enabled: bool = False) -> KodiAddonInventoryRecord:
    return KodiAddonInventoryRecord(
        addon_id=ADDON_ID,
        name="Example",
        version=VERSION,
        enabled=enabled,
        kodi_type="xbmc.python.pluginsource",
    )


def _dependency(
    dependency_id: str, *, enabled: bool, version: str = "1.0.0"
) -> KodiAddonInventoryRecord:
    return KodiAddonInventoryRecord(
        addon_id=dependency_id,
        name=dependency_id,
        version=version,
        enabled=enabled,
        kodi_type="xbmc.python.module",
    )


def _service(
    factory: sessionmaker[Session],
    tmp_path: Path,
    broker: FakeBroker,
    inventory: list[KodiAddonInventoryRecord],
    adapter: FakeEnableAdapter,
    *,
    hook: object | None = None,
) -> DevelopmentSourceEnablementService:
    def after_enable() -> None:
        if isinstance(hook, BaseException):
            raise hook

    return DevelopmentSourceEnablementService(
        factory,
        broker,
        DevelopmentSourceValidator(tmp_path),
        inventory_adapter_factory=lambda _observation, _profile: FakeInventoryAdapter(
            inventory
        ),
        enable_adapter_factory=lambda _observation, _profile: adapter,
        after_enable_hook=None if hook is None else after_enable,
    )


def test_enable_runs_all_preconditions_and_persists_success(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    broker = FakeBroker()
    adapter = FakeEnableAdapter()
    result = _service(factory, tmp_path, broker, [_target()], adapter).enable(
        INSTANCE_ID, SOURCE_ID
    )
    assert result.state is DevelopmentSourceEnablementOperationState.SUCCEEDED
    assert result.result_classification is DevelopmentSourceEnablementResult.ENABLED
    assert result.prior_enabled is False
    assert result.result_enabled is True
    assert result.enable_performed is True
    assert adapter.enable_calls == [ADDON_ID]
    assert broker.verify_calls == 1
    with factory() as session:
        assert (
            ManagedInstanceMutationLeaseRepository(session).current(INSTANCE_ID) is None
        )
        assert GlobalHeavyOperationLeaseRepository(session).current() is None


def test_already_enabled_is_idempotent_after_all_preconditions(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    broker = FakeBroker()
    adapter = FakeEnableAdapter()
    adapter.enabled = True
    result = _service(
        factory, tmp_path, broker, [_target(enabled=True)], adapter
    ).enable(INSTANCE_ID, SOURCE_ID)
    assert (
        result.result_classification
        is DevelopmentSourceEnablementResult.ALREADY_ENABLED
    )
    assert result.enable_performed is False
    assert adapter.enable_calls == []
    assert broker.verify_calls == 1


@pytest.mark.parametrize(
    ("confidence", "reason"),
    [
        ("stale", "deployment_evidence_stale"),
        ("unknown", "deployment_evidence_unknown"),
    ],
)
def test_noncurrent_deployment_evidence_blocks_before_verification_or_enable(
    factory: sessionmaker[Session],
    tmp_path: Path,
    confidence: str,
    reason: str,
) -> None:
    with factory.begin() as session:
        session.execute(
            update(DevelopmentSourceDeploymentRow).values(confidence=confidence)
        )
    broker = FakeBroker()
    adapter = FakeEnableAdapter()
    result = _service(factory, tmp_path, broker, [_target()], adapter).enable(
        INSTANCE_ID, SOURCE_ID
    )
    assert result.failure_reason == reason
    assert adapter.enable_calls == []
    assert broker.verify_calls == 0


def test_destination_drift_blocks_without_enable(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    broker = FakeBroker()
    broker.destination_matches = False
    adapter = FakeEnableAdapter()
    result = _service(factory, tmp_path, broker, [_target()], adapter).enable(
        INSTANCE_ID, SOURCE_ID
    )
    assert result.failure_reason == "deployment_verification_failed"
    assert adapter.enable_calls == []


def test_current_source_version_must_still_match_copied_identity(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    descriptor = tmp_path / "source" / "addon.xml"
    descriptor.write_text(
        descriptor.read_text(encoding="utf-8").replace(VERSION, "2.0.0"),
        encoding="utf-8",
    )
    broker = FakeBroker()
    adapter = FakeEnableAdapter()
    result = _service(factory, tmp_path, broker, [_target()], adapter).enable(
        INSTANCE_ID, SOURCE_ID
    )
    assert result.failure_reason == "deployment_evidence_stale"
    assert adapter.enable_calls == []
    assert broker.verify_calls == 0


@pytest.mark.parametrize(
    ("inventory", "reason"),
    [
        ([], "addon_not_recognized"),
        (
            [
                KodiAddonInventoryRecord(
                    addon_id=ADDON_ID,
                    name="Example",
                    version="9.9.9",
                    enabled=False,
                    kodi_type="xbmc.python.pluginsource",
                )
            ],
            "addon_version_mismatch",
        ),
    ],
)
def test_exact_recognition_is_required(
    factory: sessionmaker[Session],
    tmp_path: Path,
    inventory: list[KodiAddonInventoryRecord],
    reason: str,
) -> None:
    adapter = FakeEnableAdapter()
    result = _service(factory, tmp_path, FakeBroker(), inventory, adapter).enable(
        INSTANCE_ID, SOURCE_ID
    )
    assert result.failure_reason == reason
    assert adapter.enable_calls == []


@pytest.mark.parametrize(
    ("present", "enabled", "optional", "reason"),
    [
        (False, False, False, "dependency_missing"),
        (True, False, False, "dependency_disabled"),
        (False, False, True, None),
        (True, True, False, None),
    ],
)
def test_dependency_preflight_blocks_hidden_dependency_mutations(
    factory: sessionmaker[Session],
    tmp_path: Path,
    present: bool,
    enabled: bool,
    optional: bool,
    reason: str | None,
) -> None:
    dependency_id = "script.module.required"
    optional_text = "true" if optional else "false"
    _write_descriptor(
        tmp_path / "source",
        dependency_xml=(
            f'<requires><import addon="{dependency_id}" '
            f'optional="{optional_text}"/></requires>'
        ),
    )
    adapter = FakeEnableAdapter()
    adapter.dependencies = (KodiReportedAddonDependency(dependency_id, None, optional),)
    inventory = [_target()]
    if present:
        inventory.append(_dependency(dependency_id, enabled=enabled))
    result = _service(factory, tmp_path, FakeBroker(), inventory, adapter).enable(
        INSTANCE_ID, SOURCE_ID
    )
    assert result.failure_reason == reason
    if reason is None:
        assert result.result_classification is DevelopmentSourceEnablementResult.ENABLED
        assert adapter.enable_calls == [ADDON_ID]
    else:
        assert (
            result.result_classification
            is DevelopmentSourceEnablementResult.DEPENDENCY_BLOCKED
        )
        assert result.blocked_dependency_id == dependency_id
        assert adapter.enable_calls == []


def test_required_version_below_minimum_blocks_enable(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    dependency_id = "script.module.versioned"
    _write_descriptor(
        tmp_path / "source",
        dependency_xml=(
            f'<requires><import addon="{dependency_id}" version="2.0.0"/></requires>'
        ),
    )
    adapter = FakeEnableAdapter()
    adapter.dependencies = (KodiReportedAddonDependency(dependency_id, "2.0.0", False),)
    result = _service(
        factory,
        tmp_path,
        FakeBroker(),
        [_target(), _dependency(dependency_id, enabled=True, version="1.9.0")],
        adapter,
    ).enable(INSTANCE_ID, SOURCE_ID)
    assert result.failure_reason == "dependency_incompatible"
    assert adapter.enable_calls == []


def test_dependency_declaration_disagreement_fails_closed(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    _write_descriptor(
        tmp_path / "source",
        dependency_xml=('<requires><import addon="script.module.source"/></requires>'),
    )
    adapter = FakeEnableAdapter()
    adapter.dependencies = (
        KodiReportedAddonDependency("script.module.kodi", None, False),
    )
    result = _service(factory, tmp_path, FakeBroker(), [_target()], adapter).enable(
        INSTANCE_ID, SOURCE_ID
    )
    assert result.failure_reason == "dependency_state_unknown"
    assert adapter.enable_calls == []


def test_kodi_neutral_dependency_version_sentinels_mean_no_constraint(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    dependency_id = "script.module.required"
    _write_descriptor(
        tmp_path / "source",
        dependency_xml=(f'<requires><import addon="{dependency_id}"/></requires>'),
    )
    adapter = FakeEnableAdapter()
    adapter.dependencies = (
        KodiReportedAddonDependency(dependency_id, "0.0.0", False, minversion="0.0.0"),
    )
    result = _service(factory, tmp_path, FakeBroker(), [_target()], adapter).enable(
        INSTANCE_ID, SOURCE_ID
    )

    assert result.failure_reason == "dependency_missing"
    assert adapter.enable_calls == []


def test_builtin_without_version_is_not_misclassified_as_missing(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    _write_descriptor(
        tmp_path / "source",
        dependency_xml='<requires><import addon="xbmc.python"/></requires>',
    )
    adapter = FakeEnableAdapter()
    adapter.dependencies = (KodiReportedAddonDependency("xbmc.python", None, False),)
    result = _service(factory, tmp_path, FakeBroker(), [_target()], adapter).enable(
        INSTANCE_ID, SOURCE_ID
    )
    assert result.result_classification is DevelopmentSourceEnablementResult.ENABLED


def test_python_interpreter_default_runtime_is_rejected_before_enable(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    with factory.begin() as session:
        session.execute(
            update(RuntimeExecutionProfileRow).values(
                python_bytecode_policy="interpreter_default"
            )
        )
    adapter = FakeEnableAdapter()
    result = _service(factory, tmp_path, FakeBroker(), [_target()], adapter).enable(
        INSTANCE_ID, SOURCE_ID
    )
    assert result.failure_reason == "runtime_bytecode_policy_incompatible"
    assert adapter.enable_calls == []


def test_nonpython_safe_addon_does_not_require_suppression(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    _write_descriptor(tmp_path / "source", extension="xbmc.addon.metadata")
    with factory.begin() as session:
        session.execute(
            update(RuntimeExecutionProfileRow).values(
                python_bytecode_policy="interpreter_default"
            )
        )
        session.execute(
            update(DevelopmentSourceRow).values(
                extension_types_json='["xbmc.addon.metadata"]'
            )
        )
    adapter = FakeEnableAdapter()
    adapter.kodi_type = "xbmc.addon.metadata"
    result = _service(factory, tmp_path, FakeBroker(), [_target()], adapter).enable(
        INSTANCE_ID, SOURCE_ID
    )
    assert result.result_classification is DevelopmentSourceEnablementResult.ENABLED


def test_service_addon_is_rejected_before_enable(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    _write_descriptor(tmp_path / "source", extension="xbmc.service")
    adapter = FakeEnableAdapter()
    result = _service(factory, tmp_path, FakeBroker(), [_target()], adapter).enable(
        INSTANCE_ID, SOURCE_ID
    )
    assert result.failure_reason == "auto_start_addon_unsupported"
    assert adapter.enable_calls == []


def test_valid_kodi_rejection_is_bounded_failure(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    adapter = FakeEnableAdapter()
    adapter.enable_error = KodiAddonEnableRejectedError("secret raw Kodi error")
    result = _service(factory, tmp_path, FakeBroker(), [_target()], adapter).enable(
        INSTANCE_ID, SOURCE_ID
    )
    assert result.failure_reason == "kodi_enable_rejected"
    assert "secret" not in result.model_dump_json()


def test_ambiguous_mutation_requires_reconciliation(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    adapter = FakeEnableAdapter()
    adapter.enable_error = KodiAddonEnablementAmbiguousError("timeout")
    result = _service(factory, tmp_path, FakeBroker(), [_target()], adapter).enable(
        INSTANCE_ID, SOURCE_ID
    )
    assert (
        result.state
        is DevelopmentSourceEnablementOperationState.RECONCILIATION_REQUIRED
    )
    assert result.failure_reason == "enablement_reconciliation_required"


def test_crash_after_accepted_mutation_requires_reconciliation(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    adapter = FakeEnableAdapter()
    result = _service(
        factory,
        tmp_path,
        FakeBroker(),
        [_target()],
        adapter,
        hook=RuntimeError("fault after accepted mutation"),
    ).enable(INSTANCE_ID, SOURCE_ID)
    assert (
        result.state
        is DevelopmentSourceEnablementOperationState.RECONCILIATION_REQUIRED
    )
    assert result.failure_reason == "enablement_reconciliation_required"


def test_ok_without_enabled_postcondition_is_failure(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    adapter = FakeEnableAdapter()
    adapter.post_enabled = False
    result = _service(factory, tmp_path, FakeBroker(), [_target()], adapter).enable(
        INSTANCE_ID, SOURCE_ID
    )
    assert result.state is DevelopmentSourceEnablementOperationState.FAILED
    assert result.result_enabled is False
    assert result.failure_reason == "kodi_enable_rejected"


def test_retention_keeps_latest_twenty_and_never_prunes_reconciliation(
    factory: sessionmaker[Session],
) -> None:
    with factory.begin() as session:
        for index in range(22):
            at = (NOW + timedelta(seconds=index)).isoformat()
            session.add(
                DevelopmentSourceEnablementOperationRow(
                    id=str(UUID(int=index + 1)),
                    original_managed_instance_id=str(INSTANCE_ID),
                    managed_instance_id=str(INSTANCE_ID),
                    development_source_id=str(SOURCE_ID),
                    source_logical_name="Example source",
                    addon_id=ADDON_ID,
                    addon_version=VERSION,
                    runtime_revision_id=str(REVISION_ID),
                    requested_at=at,
                    started_at=at,
                    completed_at=at,
                    state="succeeded",
                    phase="finalize",
                    result_classification="already_enabled",
                    prior_enabled=True,
                    result_enabled=True,
                    enable_performed=False,
                    reconciliation_required=False,
                )
            )
        session.add(
            DevelopmentSourceEnablementOperationRow(
                id=str(UUID(int=100)),
                original_managed_instance_id=str(INSTANCE_ID),
                managed_instance_id=str(INSTANCE_ID),
                development_source_id=str(SOURCE_ID),
                source_logical_name="Example source",
                addon_id=ADDON_ID,
                addon_version=VERSION,
                runtime_revision_id=str(REVISION_ID),
                requested_at=(NOW + timedelta(seconds=30)).isoformat(),
                started_at=(NOW + timedelta(seconds=30)).isoformat(),
                completed_at=(NOW + timedelta(seconds=31)).isoformat(),
                state="reconciliation_required",
                phase="finalize",
                result_classification="reconciliation_required",
                prior_enabled=False,
                result_enabled=None,
                enable_performed=True,
                failure_reason="enablement_reconciliation_required",
                reconciliation_required=True,
            )
        )
    with factory.begin() as session:
        removed = DevelopmentSourceEnablementOperationRepository(
            session
        ).prune_terminal(INSTANCE_ID, retain=20)
    assert removed == 2
    with factory() as session:
        retained = DevelopmentSourceEnablementOperationRepository(
            session
        ).list_for_original_instance(INSTANCE_ID)
    assert len(retained) == 21
    assert sum(item.reconciliation_required for item in retained) == 1


def test_stale_preflight_fails_without_reconciliation_and_releases_lease(
    factory: sessionmaker[Session],
) -> None:
    clock = Clock()
    coordinator = DevelopmentSourceEnablementOperationCoordinator(factory, clock=clock)
    handle = coordinator.begin(
        INSTANCE_ID,
        SOURCE_ID,
        source_logical_name="Example source",
        addon_id=ADDON_ID,
        addon_version=VERSION,
        runtime_revision_id=REVISION_ID,
    )
    clock.now += MUTATION_LEASE_DURATION + timedelta(seconds=1)
    swept = StaleOperationReconciliationDispatcher(factory, clock=clock).sweep_instance(
        INSTANCE_ID
    )
    assert swept.terminalized_operations == [handle.operation_id]
    with factory() as session:
        operation = DevelopmentSourceEnablementOperationRepository(session).get(
            handle.operation_id
        )
        lease = ManagedInstanceMutationLeaseRepository(session).current(INSTANCE_ID)
        heavy = GlobalHeavyOperationLeaseRepository(session).current()
    assert operation is not None
    assert operation.state is DevelopmentSourceEnablementOperationState.FAILED
    assert operation.failure_reason == "enablement_failed_before_mutation"
    assert operation.reconciliation_required is False
    assert lease is None
    assert heavy is None


def test_stale_postmutation_enablement_requires_reconciliation(
    factory: sessionmaker[Session],
) -> None:
    clock = Clock()
    coordinator = DevelopmentSourceEnablementOperationCoordinator(factory, clock=clock)
    handle = coordinator.begin(
        INSTANCE_ID,
        SOURCE_ID,
        source_logical_name="Example source",
        addon_id=ADDON_ID,
        addon_version=VERSION,
        runtime_revision_id=REVISION_ID,
    )
    coordinator.mark_mutation_started(handle)
    clock.now += MUTATION_LEASE_DURATION + timedelta(seconds=1)
    swept = StaleOperationReconciliationDispatcher(factory, clock=clock).sweep_instance(
        INSTANCE_ID
    )
    assert swept.terminalized_operations == [handle.operation_id]
    with factory() as session:
        operation = DevelopmentSourceEnablementOperationRepository(session).get(
            handle.operation_id
        )
        lease = ManagedInstanceMutationLeaseRepository(session).current(INSTANCE_ID)
    assert operation is not None
    assert (
        operation.state
        is DevelopmentSourceEnablementOperationState.RECONCILIATION_REQUIRED
    )
    assert operation.enable_performed is True
    assert lease is None


@pytest.mark.parametrize(
    "kind",
    [
        ManagedInstanceMutationKind.LIFECYCLE,
        ManagedInstanceMutationKind.RESET,
        ManagedInstanceMutationKind.SNAPSHOT_CREATE,
        ManagedInstanceMutationKind.SNAPSHOT_RESTORE,
        ManagedInstanceMutationKind.DEVELOPMENT_SOURCE_DEPLOYMENT,
        ManagedInstanceMutationKind.DEVELOPMENT_SOURCE_ATTACHMENT,
        ManagedInstanceMutationKind.INSTANCE_DELETE,
        ManagedInstanceMutationKind.RUNTIME_REASSIGNMENT,
        ManagedInstanceMutationKind.DEVELOPMENT_SOURCE_ENABLEMENT,
    ],
)
def test_enablement_conflicts_with_every_instance_mutation_kind(
    factory: sessionmaker[Session], kind: ManagedInstanceMutationKind
) -> None:
    blocking_operation = UUID(int=999)
    with factory.begin() as session:
        leases = ManagedInstanceMutationLeaseRepository(session)
        leases.reserve(INSTANCE_ID, kind=kind, operation_id=blocking_operation)
        assert leases.claim(
            INSTANCE_ID,
            operation_id=blocking_operation,
            token_digest="f" * 64,
            now=NOW,
            expires_at=NOW + MUTATION_LEASE_DURATION,
        )
    coordinator = DevelopmentSourceEnablementOperationCoordinator(
        factory, clock=lambda: NOW
    )
    with pytest.raises(DevelopmentSourceEnablementConflictError):
        coordinator.begin(
            INSTANCE_ID,
            SOURCE_ID,
            source_logical_name="Example source",
            addon_id=ADDON_ID,
            addon_version=VERSION,
            runtime_revision_id=REVISION_ID,
        )


@pytest.mark.parametrize("installed", ["5.4.16", "5.4.17", "6.0.0"])
def test_numeric_minimum_and_omega_gui_allow_skin_enable(
    factory: sessionmaker[Session], tmp_path: Path, installed: str
) -> None:
    dependency_id = "plugin.video.example.helper"
    _write_descriptor(
        tmp_path / "source",
        extension="xbmc.gui.skin",
        dependency_xml=(
            '<requires><import addon="xbmc.gui" version="5.17.0"/>'
            f'<import addon="{dependency_id}" version="5.4.16"/></requires>'
        ),
    )
    with factory.begin() as session:
        session.execute(
            update(DevelopmentSourceRow).values(
                extension_types_json='["xbmc.gui.skin"]'
            )
        )
    adapter = FakeEnableAdapter()
    adapter.kodi_type = "xbmc.gui.skin"
    adapter.dependencies = (
        KodiReportedAddonDependency("xbmc.gui", "5.17.0", False, "5.17.0"),
        KodiReportedAddonDependency(dependency_id, "5.4.16", False, "5.4.16"),
    )
    target = KodiAddonInventoryRecord(
        ADDON_ID, "Example", VERSION, False, "xbmc.gui.skin"
    )
    broker = FakeBroker()
    result = _service(
        factory,
        tmp_path,
        broker,
        [target, _dependency(dependency_id, enabled=True, version=installed)],
        adapter,
    ).enable(INSTANCE_ID, SOURCE_ID)
    assert result.result_classification is DevelopmentSourceEnablementResult.ENABLED
    assert result.result_enabled is True
    assert adapter.enable_calls == [ADDON_ID]
    assert broker.verify_calls >= 1
    assert all(item.status.value == "present_enabled" for item in result.dependencies)


def test_dependency_install_then_enable_retains_prior_failure(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    dependency_id = "script.module.example"
    _write_descriptor(
        tmp_path / "source",
        dependency_xml=(
            f'<requires><import addon="{dependency_id}" version="2.0.0"/></requires>'
        ),
    )
    adapter = FakeEnableAdapter()
    adapter.dependencies = (KodiReportedAddonDependency(dependency_id, "2.0.0", False),)
    inventory = [_target()]
    service = _service(factory, tmp_path, FakeBroker(), inventory, adapter)
    blocked = service.enable(INSTANCE_ID, SOURCE_ID)
    assert blocked.failure_reason == "dependency_missing"
    assert adapter.enable_calls == []
    inventory.append(_dependency(dependency_id, enabled=True, version="2.1.0"))
    enabled = service.enable(INSTANCE_ID, SOURCE_ID)
    assert enabled.result_classification is DevelopmentSourceEnablementResult.ENABLED
    assert enabled.operation_id != blocked.operation_id
    assert adapter.enable_calls == [ADDON_ID]
    with factory() as session:
        original = session.get(
            DevelopmentSourceEnablementOperationRow, str(blocked.operation_id)
        )
        assert original is not None
        assert original.state is DevelopmentSourceEnablementOperationState.FAILED
        assert original.failure_reason == "dependency_missing"
