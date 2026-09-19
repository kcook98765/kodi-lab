from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.addon_inventory import AddonInventoryQueryStatus
from kodi_lab.application.agent_workspace_promotions import (
    AgentWorkspacePromotionConflictError,
    AgentWorkspacePromotionService,
)
from kodi_lab.application.agent_workspaces import (
    AgentWorkspaceConflictError,
    AgentWorkspaceService,
)
from kodi_lab.application.promotion_verifications import PromotionVerificationService
from kodi_lab.application.reconciliation import (
    ManagedInstanceReconciliationService,
    StaleOperationReconciliationDispatcher,
)
from kodi_lab.broker.development_sources import (
    DevelopmentSourceDeploymentStateError,
    SqliteDevelopmentSourceDeploymentResolver,
)
from kodi_lab.broker.docker_inventory import ContainerRuntimeState
from kodi_lab.domain import DevelopmentSource, DevelopmentSourceKind
from kodi_lab.kodi.readiness import UsabilityState
from kodi_lab.storage import Base
from kodi_lab.storage.engine import create_database_engine
from kodi_lab.storage.models import (
    AgentWorkspacePromotionHandoffRow,
    AgentWorkspacePromotionJournalRow,
    AgentWorkspacePromotionOperationRow,
    AgentWorkspacePromotionPlanRow,
    AgentWorkspaceRow,
    DevelopmentSourceDeploymentOperationRow,
    DevelopmentSourceDeploymentRow,
    DevelopmentSourceEnablementOperationRow,
    DevelopmentSourcePromotionLeaseRow,
    DevelopmentSourceSmokeTestOperationRow,
    GlobalHeavyOperationLeaseRow,
    ManagedInstanceDevelopmentSourceRow,
    ManagedInstanceMutationLeaseRow,
    ManagedInstanceRow,
    PromotionVerificationOperationRow,
    RuntimeDefinitionRow,
    RuntimeExecutionProfileRow,
    RuntimeRevisionRow,
    TargetRow,
)
from kodi_lab.storage.repositories import DevelopmentSourceRepository

SOURCE_ID = UUID("82000000-0000-4000-8000-000000000001")
TARGET_ID = UUID("82000000-0000-4000-8000-000000000002")
RUNTIME_ID = UUID("82000000-0000-4000-8000-000000000003")
REVISION_ID = UUID("82000000-0000-4000-8000-000000000004")
DEPLOYMENT_ID = UUID("82000000-0000-4000-8000-000000000005")
ENABLEMENT_ID = UUID("82000000-0000-4000-8000-000000000006")
SMOKE_ID = UUID("82000000-0000-4000-8000-000000000007")
CONTAINER_ID = "a" * 64
SOURCE_DIGEST = "sha256:" + "b" * 64
DESTINATION_DIGEST = "sha256:" + "c" * 64
NOW = datetime(2026, 9, 18, 18, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


@dataclass
class Harness:
    factory: sessionmaker[Session]
    workspaces: AgentWorkspaceService
    promotions: AgentWorkspacePromotionService
    source: Path
    data_root: Path
    workspace_id: UUID
    clock: Clock


def _harness(tmp_path: Path, *, failure_phase: str | None = None) -> Harness:
    tmp_path.mkdir(parents=True, exist_ok=True)
    engine = create_database_engine(f"sqlite:///{tmp_path / 'promotions.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    development_root = tmp_path / "sources"
    source = development_root / "source"
    source.mkdir(parents=True)
    (source / "addon.xml").write_text(
        '<addon id="plugin.video.example" name="Example" version="1.0.0">'
        '<extension point="xbmc.python.pluginsource" library="main.py" />'
        "</addon>\n",
        encoding="utf-8",
    )
    (source / "main.py").write_text("print('old')\n", encoding="utf-8")
    with factory.begin() as session:
        session.add(
            RuntimeDefinitionRow(
                id=str(RUNTIME_ID), slug="runtime", kodi_version="21.3"
            )
        )
        session.flush()
        session.add(
            RuntimeRevisionRow(
                id=str(REVISION_ID),
                runtime_definition_id=str(RUNTIME_ID),
                revision_number=1,
                source_kind="test",
                source_reference="test",
                content_digest="sha256:" + "d" * 64,
                manifest_reference="test",
                manifest_digest="sha256:" + "e" * 64,
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
                runtime_uid=2468,
                runtime_gid=1357,
                python_bytecode_policy="interpreter_default",
                expected_application_name="Kodi",
                expected_kodi_version="21.3",
                expected_version_tag="stable",
                expected_prerelease_ordinal=None,
                jsonrpc_port=8080,
                jsonrpc_path="/jsonrpc",
                process_observation="docker_healthcheck",
                readiness_probe="kodi_jsonrpc",
                readiness_timeout_seconds=60,
                poll_interval_seconds=1.0,
                http_timeout_seconds=1.0,
            )
        )
        session.add(
            TargetRow(
                id=str(TARGET_ID),
                name="verification target",
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
                target_id=str(TARGET_ID),
                instance_type="disposable",
                runtime_revision_id=str(REVISION_ID),
                # Catalog state is not the authoritative live Docker state.
                runtime_state="stopped",
                start_on_host_boot=False,
            )
        )
        DevelopmentSourceRepository(session).create(
            DevelopmentSource(
                id=SOURCE_ID,
                logical_name="Example source",
                source_kind=DevelopmentSourceKind.LOCAL_DIRECTORY,
                relative_locator="source",
                addon_id="plugin.video.example",
                observed_name="Example",
                observed_version="1.0.0",
                extension_types=("xbmc.python.pluginsource",),
                registered_at=NOW,
                validated_at=NOW,
            )
        )
        session.add(
            ManagedInstanceDevelopmentSourceRow(
                managed_instance_id=str(TARGET_ID),
                development_source_id=str(SOURCE_ID),
                attached_at=NOW.isoformat(),
            )
        )
        session.add(
            DevelopmentSourceDeploymentOperationRow(
                id=str(DEPLOYMENT_ID),
                original_managed_instance_id=str(TARGET_ID),
                managed_instance_id=str(TARGET_ID),
                development_source_id=str(SOURCE_ID),
                source_logical_name="Example source",
                addon_id="plugin.video.example",
                runtime_revision_id=str(REVISION_ID),
                request_kind="copy_sync",
                requested_at=NOW.isoformat(),
                state="succeeded",
                phase="finalize",
                result_classification="unchanged",
                completed_at=NOW.isoformat(),
                reconciliation_required=False,
            )
        )
        session.flush()
        session.add(
            DevelopmentSourceDeploymentRow(
                managed_instance_id=str(TARGET_ID),
                development_source_id=str(SOURCE_ID),
                addon_id="plugin.video.example",
                mode="copy",
                last_successful_operation_id=str(DEPLOYMENT_ID),
                source_manifest_digest=SOURCE_DIGEST,
                verified_destination_digest=DESTINATION_DIGEST,
                runtime_revision_id=str(REVISION_ID),
                trusted_container_id=CONTAINER_ID,
                observed_source_name="Example",
                observed_source_version="1.0.0",
                verified_at=NOW.isoformat(),
                confidence="verified_at_last_operation",
            )
        )
        session.add(
            DevelopmentSourceEnablementOperationRow(
                id=str(ENABLEMENT_ID),
                original_managed_instance_id=str(TARGET_ID),
                managed_instance_id=str(TARGET_ID),
                development_source_id=str(SOURCE_ID),
                source_logical_name="Example source",
                addon_id="plugin.video.example",
                addon_version="1.0.0",
                runtime_revision_id=str(REVISION_ID),
                requested_at=NOW.isoformat(),
                started_at=NOW.isoformat(),
                completed_at=NOW.isoformat(),
                state="succeeded",
                phase="finalize",
                result_classification="enabled",
                prior_enabled=False,
                result_enabled=True,
                enable_performed=True,
                reconciliation_required=False,
            )
        )
        session.add(
            DevelopmentSourceSmokeTestOperationRow(
                id=str(SMOKE_ID),
                original_managed_instance_id=str(TARGET_ID),
                managed_instance_id=str(TARGET_ID),
                development_source_id=str(SOURCE_ID),
                source_logical_name="Example source",
                addon_id="plugin.video.example",
                addon_version="1.0.0",
                runtime_revision_id=str(REVISION_ID),
                requested_at=NOW.isoformat(),
                started_at=NOW.isoformat(),
                completed_at=NOW.isoformat(),
                state="succeeded",
                phase="finalize",
                overall_result="passed",
                execution_result="directory_result",
                post_execution_provenance="matched",
                invocation_dispatched=True,
                invocation_duration_ms=5,
                returned_item_count=1,
                reconciliation_required=False,
            )
        )
    data_root = tmp_path / "data"
    clock = Clock()
    workspaces = AgentWorkspaceService(
        factory,
        data_root=data_root,
        development_root=development_root,
        clock=clock,
    )
    queued = workspaces.queue_creation(SOURCE_ID)
    lease = workspaces.acquire(queued.operation_id)
    assert lease is not None
    workspaces.run(lease)
    assert workspaces.creation_status(queued.operation_id).state.value == "succeeded"

    def inject(phase: str) -> None:
        if phase == failure_phase:
            raise RuntimeError("injected")

    promotions = AgentWorkspacePromotionService(
        factory,
        data_root=data_root,
        development_root=development_root,
        clock=clock,
        uuid_factory=uuid4,
        failure_injector=inject,
    )
    return Harness(
        factory, workspaces, promotions, source, data_root, queued.workspace_id, clock
    )


def _edit(harness: Harness, *, add: bool = True) -> None:
    harness.workspaces.replace_text_file(
        harness.workspace_id,
        expected_generation=0,
        relative_path="main.py",
        content="print('promoted')\n",
    )
    if add:
        harness.workspaces.create_text_file(
            harness.workspace_id,
            expected_generation=1,
            relative_path="new.py",
            content="new = True\n",
        )


def test_success_promotes_workspace_stales_evidence_and_publishes_exact_handoff(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    _edit(harness)

    assert harness.promotions.eligible_targets(harness.workspace_id)[0].id == TARGET_ID
    plan = harness.promotions.prepare(TARGET_ID, harness.workspace_id)
    assert plan.state.value == "reviewable"
    assert plan.expires_at == NOW + timedelta(minutes=15)
    assert plan.current_version == "1.0.0"
    assert plan.promoted_version == "1.0.0"
    assert [change.path for change in plan.changes] == ["main.py", "new.py"]
    assert all(change.after_text for change in plan.changes)

    operation = harness.promotions.approve_and_execute(
        TARGET_ID, harness.workspace_id, plan.plan_id
    )

    assert operation.state.value == "succeeded", operation
    assert operation.application_started
    assert operation.backup_verified
    assert operation.handoff_published
    assert harness.source.joinpath("main.py").read_text() == "print('promoted')\n"
    assert harness.source.joinpath("new.py").read_text() == "new = True\n"
    assert harness.workspaces.get(harness.workspace_id).state.value == "promoted"
    with pytest.raises(AgentWorkspaceConflictError) as immutable:
        harness.workspaces.replace_text_file(
            harness.workspace_id,
            expected_generation=2,
            relative_path="main.py",
            content="again\n",
        )
    assert immutable.value.code == "agent_workspace_not_ready"
    with harness.factory() as session:
        deployment = session.get(
            DevelopmentSourceDeploymentRow, (str(TARGET_ID), str(SOURCE_ID))
        )
        handoff = session.scalar(select(AgentWorkspacePromotionHandoffRow))
        journals = list(
            session.scalars(
                select(AgentWorkspacePromotionJournalRow).order_by(
                    AgentWorkspacePromotionJournalRow.ordinal
                )
            )
        )
        historical = session.get(
            DevelopmentSourceDeploymentOperationRow, str(DEPLOYMENT_ID)
        )
        enablement = session.get(
            DevelopmentSourceEnablementOperationRow, str(ENABLEMENT_ID)
        )
        smoke = session.get(DevelopmentSourceSmokeTestOperationRow, str(SMOKE_ID))
        assert deployment is not None and deployment.confidence.value == "stale"
        assert handoff is not None
        assert handoff.target_instance_id == str(TARGET_ID)
        assert handoff.prior_deployment_operation_id == str(DEPLOYMENT_ID)
        assert handoff.prior_source_digest == SOURCE_DIGEST
        assert handoff.prior_destination_digest == DESTINATION_DIGEST
        assert [item.state.value for item in journals] == ["applied", "applied"]
        assert historical is not None and historical.state.value == "succeeded"
        assert enablement is not None and enablement.state.value == "succeeded"
        assert enablement.result_enabled is True
        assert smoke is not None and smoke.state.value == "succeeded"
        assert smoke.overall_result is not None
        assert smoke.overall_result.value == "passed"
        assert (
            session.scalar(
                select(func.count()).select_from(ManagedInstanceMutationLeaseRow)
            )
            == 0
        )


def test_success_automatically_supports_bound_same_container_verification(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    _edit(harness)
    plan = harness.promotions.prepare(TARGET_ID, harness.workspace_id)
    source_result = harness.promotions.approve_and_execute(
        TARGET_ID, harness.workspace_id, plan.plan_id
    )
    assert source_result.state.value == "succeeded"
    resolver = SqliteDevelopmentSourceDeploymentResolver(
        harness.factory,
        development_root=harness.source.parent,
        data_root=harness.data_root,
    )

    class Inventory:
        def get(self, _instance_id: UUID) -> Any:
            return SimpleNamespace(
                query_status=AddonInventoryQueryStatus.AVAILABLE,
                items=[
                    SimpleNamespace(
                        addon_id="plugin.video.example",
                        name="Example",
                        version="1.0.0",
                        enabled=True,
                        kodi_type="xbmc.python.pluginsource",
                    )
                ],
            )

    class Broker:
        state = ContainerRuntimeState.RUNNING
        stops = 0
        starts = 0

        @staticmethod
        def assert_reserved_unconsumed(verification_id: str) -> None:
            with harness.factory() as session:
                handoff = session.scalar(select(AgentWorkspacePromotionHandoffRow))
                operation = session.get(
                    PromotionVerificationOperationRow, verification_id
                )
                assert handoff is not None
                assert handoff.verification_operation_id == verification_id
                assert handoff.consumed_at is None
                assert operation is not None and not operation.handoff_consumed

        def managed_container_inventory(self) -> Any:
            return SimpleNamespace(
                containers=[
                    SimpleNamespace(
                        managed_instance_id=str(TARGET_ID),
                        container_id=CONTAINER_ID,
                        runtime_state=self.state,
                    )
                ]
            )

        def capture_promotion_kodi_log_cursor(self, **kwargs: object) -> Any:
            self.assert_reserved_unconsumed(str(kwargs["verification_id"]))
            return SimpleNamespace(cursor="opaque")

        def stop_managed_container(self, **_kwargs: object) -> None:
            self.stops += 1
            self.state = ContainerRuntimeState.STOPPED

        def copy_sync_promoted_development_source(self, **kwargs: object) -> Any:
            self.assert_reserved_unconsumed(str(kwargs["verification_operation_id"]))
            authority = resolver.resolve_promotion_verification(
                str(kwargs["verification_operation_id"]),
                str(kwargs["managed_instance_id"]),
                str(kwargs["expected_container_id"]),
            )
            assert authority.addon_id == "plugin.video.example"
            return SimpleNamespace(
                source_manifest_digest="sha256:" + "7" * 64,
                verified_destination_digest="sha256:" + "8" * 64,
                container_id=CONTAINER_ID,
                observed_source_name="Example",
                observed_source_version="1.0.0",
                result_classification="synchronized",
                new_count=1,
                changed_count=1,
                unchanged_count=1,
                destination_only_count=1,
                transferred_bytes=20,
            )

        def start_managed_container(self, **_kwargs: object) -> None:
            self.starts += 1
            self.state = ContainerRuntimeState.RUNNING

        def read_promotion_kodi_log_window(self, **_kwargs: object) -> Any:
            return SimpleNamespace(
                classification="clean",
                target_error_count=0,
                unrelated_warning_count=0,
                snippets=[],
            )

    broker = Broker()
    service = PromotionVerificationService(
        harness.factory,
        broker,
        Inventory(),
        clock=harness.clock,
        sleep=lambda _seconds: None,
        readiness_factory=lambda _broker, _profile: SimpleNamespace(
            wait_until_usable=lambda _instance_id: SimpleNamespace(
                usability_state=UsabilityState.USABLE
            )
        ),
    )
    verification = service.queue_and_run(source_result.operation_id)

    assert verification.state.value == "succeeded"
    assert verification.sync_result.value == "succeeded"
    assert verification.reload_result.value == "succeeded"
    assert verification.inventory_result.value == "succeeded"
    assert verification.log_result.value == "clean"
    assert broker.stops == broker.starts == 1
    with harness.factory() as session:
        source_operation = session.get(
            AgentWorkspacePromotionOperationRow, str(source_result.operation_id)
        )
        retained = session.get(
            PromotionVerificationOperationRow, str(verification.verification_id)
        )
        deployment = session.get(
            DevelopmentSourceDeploymentRow, (str(TARGET_ID), str(SOURCE_ID))
        )
        handoff = session.scalar(select(AgentWorkspacePromotionHandoffRow))
        assert source_operation is not None
        assert source_operation.state.value == "succeeded"
        assert retained is not None and retained.handoff_consumed
        assert deployment is not None
        assert deployment.confidence.value == "verified_at_last_operation"
        assert deployment.source_manifest_digest == "sha256:" + "7" * 64
        assert handoff is not None and handoff.consumed_at is not None
        assert (
            session.scalar(
                select(func.count()).select_from(GlobalHeavyOperationLeaseRow)
            )
            == 0
        )
    repeated = service.queue_and_run(source_result.operation_id)
    assert repeated.verification_id == verification.verification_id
    with pytest.raises(DevelopmentSourceDeploymentStateError):
        resolver.resolve_promotion_verification(
            str(verification.verification_id), str(TARGET_ID), CONTAINER_ID
        )
    with harness.factory() as session:
        assert (
            session.scalar(
                select(func.count()).select_from(PromotionVerificationOperationRow)
            )
            == 1
        )
        assert (
            session.scalar(
                select(func.count()).select_from(DevelopmentSourcePromotionLeaseRow)
            )
            == 0
        )
    with pytest.raises(AgentWorkspacePromotionConflictError) as repeat:
        harness.promotions.approve_and_execute(
            TARGET_ID, harness.workspace_id, plan.plan_id
        )
    assert repeat.value.code == "promotion_plan_not_executable"


@pytest.mark.parametrize(
    ("inventory_status", "version", "enabled", "log_result", "expected"),
    [
        (AddonInventoryQueryStatus.AVAILABLE, "1.0.0", True, "target_errors", "failed"),
        (
            AddonInventoryQueryStatus.AVAILABLE,
            "1.0.0",
            True,
            "incomplete",
            "incomplete",
        ),
        (AddonInventoryQueryStatus.AVAILABLE, "1.0.0", False, "clean", "failed"),
        (AddonInventoryQueryStatus.AVAILABLE, "9.9.9", True, "clean", "failed"),
        (AddonInventoryQueryStatus.UNAVAILABLE, None, None, "clean", "incomplete"),
    ],
)
def test_verification_failures_never_change_source_success(
    tmp_path: Path,
    inventory_status: AddonInventoryQueryStatus,
    version: str | None,
    enabled: bool | None,
    log_result: str,
    expected: str,
) -> None:
    harness = _harness(tmp_path)
    _edit(harness)
    plan = harness.promotions.prepare(TARGET_ID, harness.workspace_id)
    source_result = harness.promotions.approve_and_execute(
        TARGET_ID, harness.workspace_id, plan.plan_id
    )
    source_after_promotion = (harness.source / "main.py").read_bytes()

    class Inventory:
        def get(self, _instance_id: UUID) -> Any:
            items = []
            if version is not None and enabled is not None:
                items.append(
                    SimpleNamespace(
                        addon_id="plugin.video.example",
                        version=version,
                        enabled=enabled,
                        kodi_type="xbmc.python.pluginsource",
                    )
                )
            return SimpleNamespace(query_status=inventory_status, items=items)

    class Broker:
        state = ContainerRuntimeState.RUNNING

        def managed_container_inventory(self) -> Any:
            return SimpleNamespace(
                containers=[
                    SimpleNamespace(
                        managed_instance_id=str(TARGET_ID),
                        container_id=CONTAINER_ID,
                        runtime_state=self.state,
                    )
                ]
            )

        def capture_promotion_kodi_log_cursor(self, **_kwargs: object) -> Any:
            return SimpleNamespace(cursor="opaque")

        def stop_managed_container(self, **_kwargs: object) -> None:
            self.state = ContainerRuntimeState.STOPPED

        def copy_sync_promoted_development_source(self, **_kwargs: object) -> Any:
            return SimpleNamespace(
                source_manifest_digest="sha256:" + "7" * 64,
                verified_destination_digest="sha256:" + "8" * 64,
                container_id=CONTAINER_ID,
                observed_source_name="Example",
                observed_source_version="1.0.0",
                result_classification="synchronized",
                new_count=0,
                changed_count=1,
                unchanged_count=2,
                destination_only_count=1,
                transferred_bytes=20,
            )

        def start_managed_container(self, **_kwargs: object) -> None:
            self.state = ContainerRuntimeState.RUNNING

        def read_promotion_kodi_log_window(self, **_kwargs: object) -> Any:
            return SimpleNamespace(
                classification=log_result,
                target_error_count=1 if log_result == "target_errors" else 0,
                unrelated_warning_count=0,
                snippets=["bounded target error"]
                if log_result == "target_errors"
                else [],
            )

    verification = PromotionVerificationService(
        harness.factory,
        Broker(),
        Inventory(),
        clock=harness.clock,
        sleep=lambda _seconds: None,
        readiness_factory=lambda _broker, _profile: SimpleNamespace(
            wait_until_usable=lambda _instance_id: SimpleNamespace(
                usability_state=UsabilityState.USABLE
            )
        ),
    ).queue_and_run(source_result.operation_id)
    assert verification.state.value == expected
    assert (harness.source / "main.py").read_bytes() == source_after_promotion
    with harness.factory() as session:
        retained = session.get(
            AgentWorkspacePromotionOperationRow, str(source_result.operation_id)
        )
        assert retained is not None and retained.state.value == "succeeded"


@pytest.mark.parametrize(
    ("error_code", "expected_state", "expected_runtime"),
    [
        (
            "promotion_verification_handoff_invalid",
            "failed",
            ContainerRuntimeState.RUNNING,
        ),
        (
            "development_source_apply_unverified",
            "reconciliation_required",
            ContainerRuntimeState.STOPPED,
        ),
    ],
)
def test_sync_failure_boundary_restores_only_when_destination_is_certain(
    tmp_path: Path,
    error_code: str,
    expected_state: str,
    expected_runtime: ContainerRuntimeState,
) -> None:
    harness = _harness(tmp_path)
    _edit(harness)
    plan = harness.promotions.prepare(TARGET_ID, harness.workspace_id)
    source_result = harness.promotions.approve_and_execute(
        TARGET_ID, harness.workspace_id, plan.plan_id
    )

    class SyncError(RuntimeError):
        code = error_code

    class Inventory:
        def get(self, _instance_id: UUID) -> Any:
            raise AssertionError("inventory must not run")

    class Broker:
        state = ContainerRuntimeState.RUNNING

        def managed_container_inventory(self) -> Any:
            return SimpleNamespace(
                containers=[
                    SimpleNamespace(
                        managed_instance_id=str(TARGET_ID),
                        container_id=CONTAINER_ID,
                        runtime_state=self.state,
                    )
                ]
            )

        def capture_promotion_kodi_log_cursor(self, **_kwargs: object) -> Any:
            return SimpleNamespace(cursor="opaque")

        def stop_managed_container(self, **_kwargs: object) -> None:
            self.state = ContainerRuntimeState.STOPPED

        def copy_sync_promoted_development_source(self, **kwargs: object) -> Any:
            verification_id = str(kwargs["verification_operation_id"])
            with harness.factory() as session:
                handoff = session.scalar(select(AgentWorkspacePromotionHandoffRow))
                operation = session.get(
                    PromotionVerificationOperationRow, verification_id
                )
                assert handoff is not None
                assert handoff.verification_operation_id == verification_id
                assert handoff.consumed_at is None
                assert operation is not None and not operation.handoff_consumed
            raise SyncError

        def start_managed_container(self, **_kwargs: object) -> None:
            self.state = ContainerRuntimeState.RUNNING

        def read_promotion_kodi_log_window(self, **_kwargs: object) -> Any:
            raise AssertionError("log read must not run")

    broker = Broker()
    service = PromotionVerificationService(
        harness.factory,
        broker,
        Inventory(),
        clock=harness.clock,
        sleep=lambda _seconds: None,
    )
    verification = service.queue_and_run(source_result.operation_id)
    assert verification.state.value == expected_state
    assert broker.state is expected_runtime
    with harness.factory() as session:
        retained = session.get(
            AgentWorkspacePromotionOperationRow, str(source_result.operation_id)
        )
        verification_row = session.get(
            PromotionVerificationOperationRow, str(verification.verification_id)
        )
        handoff = session.scalar(select(AgentWorkspacePromotionHandoffRow))
        assert retained is not None and retained.state.value == "succeeded"
        assert verification_row is not None and not verification_row.handoff_consumed
        assert handoff is not None
        assert handoff.verification_operation_id == str(verification.verification_id)
        assert handoff.consumed_at is None
    repeated = service.queue_and_run(source_result.operation_id)
    assert repeated.verification_id == verification.verification_id
    with harness.factory() as session:
        assert (
            session.scalar(
                select(func.count()).select_from(PromotionVerificationOperationRow)
            )
            == 1
        )


def test_pre_sync_failure_stays_reserved_unconsumed_and_non_reusable(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    _edit(harness)
    plan = harness.promotions.prepare(TARGET_ID, harness.workspace_id)
    source_result = harness.promotions.approve_and_execute(
        TARGET_ID, harness.workspace_id, plan.plan_id
    )

    class Inventory:
        def get(self, _instance_id: UUID) -> Any:
            raise AssertionError("inventory must not run")

    class Broker:
        def managed_container_inventory(self) -> Any:
            return SimpleNamespace(
                containers=[
                    SimpleNamespace(
                        managed_instance_id=str(TARGET_ID),
                        container_id=CONTAINER_ID,
                        runtime_state=ContainerRuntimeState.RUNNING,
                    )
                ]
            )

        def capture_promotion_kodi_log_cursor(self, **kwargs: object) -> Any:
            verification_id = str(kwargs["verification_id"])
            with harness.factory() as session:
                handoff = session.scalar(select(AgentWorkspacePromotionHandoffRow))
                operation = session.get(
                    PromotionVerificationOperationRow, verification_id
                )
                assert handoff is not None
                assert handoff.verification_operation_id == verification_id
                assert handoff.consumed_at is None
                assert operation is not None and not operation.handoff_consumed
            return SimpleNamespace(cursor="opaque")

        def stop_managed_container(self, **_kwargs: object) -> None:
            raise AssertionError("stop must not run")

        def copy_sync_promoted_development_source(self, **_kwargs: object) -> Any:
            raise AssertionError("sync must not run")

        def start_managed_container(self, **_kwargs: object) -> None:
            raise AssertionError("start must not run")

        def read_promotion_kodi_log_window(self, **_kwargs: object) -> Any:
            raise AssertionError("log read must not run")

    def fail_before_stop(phase: str) -> None:
        if phase == "before_stop":
            raise RuntimeError("injected pre-sync failure")

    service = PromotionVerificationService(
        harness.factory,
        Broker(),
        Inventory(),
        clock=harness.clock,
        sleep=lambda _seconds: None,
        failure_injector=fail_before_stop,
    )
    verification = service.queue_and_run(source_result.operation_id)

    assert verification.state.value == "failed"
    with harness.factory() as session:
        operation = session.get(
            PromotionVerificationOperationRow, str(verification.verification_id)
        )
        handoff = session.scalar(select(AgentWorkspacePromotionHandoffRow))
        assert operation is not None and not operation.handoff_consumed
        assert handoff is not None
        assert handoff.verification_operation_id == str(verification.verification_id)
        assert handoff.consumed_at is None
    repeated = service.queue_and_run(source_result.operation_id)
    assert repeated.verification_id == verification.verification_id
    with harness.factory() as session:
        assert (
            session.scalar(
                select(func.count()).select_from(PromotionVerificationOperationRow)
            )
            == 1
        )


def test_exact_rollback_keeps_workspace_ready_and_consumes_approval(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path, failure_phase="after_first_modification")
    _edit(harness, add=False)
    plan = harness.promotions.prepare(TARGET_ID, harness.workspace_id)

    operation = harness.promotions.approve_and_execute(
        TARGET_ID, harness.workspace_id, plan.plan_id
    )

    assert operation.state.value == "failed"
    assert operation.application_started
    assert not operation.reconciliation_required
    assert harness.source.joinpath("main.py").read_text() == "print('old')\n"
    assert harness.workspaces.get(harness.workspace_id).state.value == "ready"
    with harness.factory() as session:
        persisted = session.get(AgentWorkspacePromotionPlanRow, str(plan.plan_id))
        deployment = session.get(
            DevelopmentSourceDeploymentRow, (str(TARGET_ID), str(SOURCE_ID))
        )
        assert persisted is not None and persisted.state.value == "consumed"
        assert deployment is not None
        assert deployment.confidence.value == "verified_at_last_operation"
        assert session.scalar(select(AgentWorkspacePromotionHandoffRow)) is None
    with pytest.raises(AgentWorkspacePromotionConflictError):
        harness.promotions.approve_and_execute(
            TARGET_ID, harness.workspace_id, plan.plan_id
        )


def test_unproven_rollback_marks_workspace_and_source_evidence_unknown(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    _edit(harness, add=False)

    def corrupt(phase: str) -> None:
        if phase == "after_first_modification":
            harness.source.joinpath("main.py").write_text(
                "external-after-apply\n", encoding="utf-8"
            )
            raise RuntimeError("injected")

    harness.promotions._failure_injector = corrupt
    plan = harness.promotions.prepare(TARGET_ID, harness.workspace_id)
    operation = harness.promotions.approve_and_execute(
        TARGET_ID, harness.workspace_id, plan.plan_id
    )

    assert operation.state.value == "reconciliation_required"
    assert harness.workspaces.get(harness.workspace_id).state.value == (
        "reconciliation_required"
    )
    assert harness.source.joinpath("main.py").read_text() == "external-after-apply\n"
    reconciliation = ManagedInstanceReconciliationService(harness.factory).status(
        TARGET_ID
    )
    assert reconciliation.blocking
    assert reconciliation.operation_kind is not None
    assert reconciliation.operation_kind.value == "agent_workspace_promotion"
    with harness.factory() as session:
        deployment = session.get(
            DevelopmentSourceDeploymentRow, (str(TARGET_ID), str(SOURCE_ID))
        )
        assert deployment is not None and deployment.confidence.value == "unknown"
        assert session.scalar(select(AgentWorkspacePromotionHandoffRow)) is None


def test_expiry_workspace_generation_drift_and_target_drift_fail_closed(
    tmp_path: Path,
) -> None:
    expired = _harness(tmp_path / "expired")
    _edit(expired, add=False)
    plan = expired.promotions.prepare(TARGET_ID, expired.workspace_id)
    expired.clock.now += timedelta(minutes=15)
    with pytest.raises(AgentWorkspacePromotionConflictError) as timeout:
        expired.promotions.approve_and_execute(
            TARGET_ID, expired.workspace_id, plan.plan_id
        )
    assert timeout.value.code == "promotion_plan_not_executable"

    generation = _harness(tmp_path / "generation")
    _edit(generation, add=False)
    plan = generation.promotions.prepare(TARGET_ID, generation.workspace_id)
    with generation.factory.begin() as session:
        session.execute(
            update(AgentWorkspaceRow)
            .where(AgentWorkspaceRow.id == str(generation.workspace_id))
            .values(generation=2)
        )
    operation = generation.promotions.approve_and_execute(
        TARGET_ID, generation.workspace_id, plan.plan_id
    )
    assert operation.state.value == "failed"
    assert not operation.application_started
    assert generation.source.joinpath("main.py").read_text() == "print('old')\n"

    target = _harness(tmp_path / "target")
    _edit(target, add=False)
    plan = target.promotions.prepare(TARGET_ID, target.workspace_id)
    with target.factory.begin() as session:
        session.execute(
            update(TargetRow)
            .where(TargetRow.id == str(TARGET_ID))
            .values(connection_state="disconnected")
        )
    with pytest.raises(AgentWorkspacePromotionConflictError) as drift:
        target.promotions.approve_and_execute(
            TARGET_ID, target.workspace_id, plan.plan_id
        )
    assert drift.value.code == "promotion_target_ineligible"


def test_safe_unrelated_source_drift_remains_executable_but_overlap_does_not(
    tmp_path: Path,
) -> None:
    safe = _harness(tmp_path / "safe")
    _edit(safe, add=False)
    plan = safe.promotions.prepare(TARGET_ID, safe.workspace_id)
    safe.source.joinpath("unrelated.txt").write_text("safe\n", encoding="utf-8")
    operation = safe.promotions.approve_and_execute(
        TARGET_ID, safe.workspace_id, plan.plan_id
    )
    assert operation.state.value == "succeeded"
    assert safe.source.joinpath("unrelated.txt").read_text() == "safe\n"

    overlap = _harness(tmp_path / "overlap")
    _edit(overlap, add=False)
    plan = overlap.promotions.prepare(TARGET_ID, overlap.workspace_id)
    overlap.source.joinpath("main.py").write_text("external\n", encoding="utf-8")
    operation = overlap.promotions.approve_and_execute(
        TARGET_ID, overlap.workspace_id, plan.plan_id
    )
    assert operation.state.value == "failed"
    assert not operation.application_started
    assert overlap.source.joinpath("main.py").read_text() == "external\n"


def test_conflicted_plan_cannot_be_approved(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    _edit(harness, add=False)
    harness.source.joinpath("main.py").write_text("external\n", encoding="utf-8")
    plan = harness.promotions.prepare(TARGET_ID, harness.workspace_id)
    assert plan.state.value == "conflicted"
    assert plan.changes[0].conflict_reason == "overlapping_source_modification"
    with pytest.raises(AgentWorkspacePromotionConflictError) as failure:
        harness.promotions.approve_and_execute(
            TARGET_ID, harness.workspace_id, plan.plan_id
        )
    assert failure.value.code == "promotion_plan_not_executable"


@pytest.mark.parametrize(
    ("application_started", "journal_applied"),
    [(False, False), (True, False), (True, True)],
)
def test_startup_recovery_never_adopts_apparent_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    application_started: bool,
    journal_applied: bool,
) -> None:
    harness = _harness(tmp_path)
    _edit(harness, add=False)
    plan = harness.promotions.prepare(TARGET_ID, harness.workspace_id)
    monkeypatch.setattr(harness.promotions, "_run", lambda *_args: None)
    running = harness.promotions.approve_and_execute(
        TARGET_ID, harness.workspace_id, plan.plan_id
    )
    assert running.state.value == "running"
    if application_started:
        with harness.factory.begin() as session:
            session.execute(
                update(AgentWorkspacePromotionOperationRow)
                .where(
                    AgentWorkspacePromotionOperationRow.id == str(running.operation_id)
                )
                .values(
                    application_started=True,
                    phase="apply",
                    backup_artifact_id=str(uuid4()),
                    backup_verified=True,
                )
            )
            session.execute(
                update(AgentWorkspacePromotionPlanRow)
                .where(AgentWorkspacePromotionPlanRow.id == str(plan.plan_id))
                .values(state="consumed")
            )
            if journal_applied:
                session.execute(
                    update(AgentWorkspacePromotionJournalRow)
                    .where(
                        AgentWorkspacePromotionJournalRow.operation_id
                        == str(running.operation_id),
                        AgentWorkspacePromotionJournalRow.ordinal == 0,
                    )
                    .values(state="applied", applied_device=1, applied_inode=2)
                )
                harness.source.joinpath("main.py").write_text(
                    "print('promoted')\n", encoding="utf-8"
                )

    recovered = AgentWorkspacePromotionService(
        harness.factory,
        data_root=harness.data_root,
        development_root=harness.source.parent,
        clock=harness.clock,
    )
    assert recovered.sweep_startup() == 1
    status = recovered.get_operation(
        TARGET_ID, harness.workspace_id, running.operation_id
    )
    assert status.state.value == (
        "reconciliation_required" if journal_applied else "failed"
    )
    assert harness.workspaces.get(harness.workspace_id).state.value == (
        "reconciliation_required" if journal_applied else "ready"
    )
    with harness.factory() as session:
        deployment = session.get(
            DevelopmentSourceDeploymentRow, (str(TARGET_ID), str(SOURCE_ID))
        )
        assert deployment is not None
        assert deployment.confidence.value == (
            "unknown" if journal_applied else "verified_at_last_operation"
        )
        assert (
            session.scalar(
                select(func.count()).select_from(ManagedInstanceMutationLeaseRow)
            )
            == 0
        )
        assert (
            session.scalar(
                select(func.count()).select_from(GlobalHeavyOperationLeaseRow)
            )
            == 0
        )
        assert (
            session.scalar(
                select(func.count()).select_from(DevelopmentSourcePromotionLeaseRow)
            )
            == 0
        )


def test_startup_recovery_closes_interrupted_preparing_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _harness(tmp_path)
    _edit(harness, add=False)

    def interrupted(*_args: object, **_kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(
        "kodi_lab.application.agent_workspace_promotions.prepare_promotion_review",
        interrupted,
    )
    with pytest.raises(KeyboardInterrupt):
        harness.promotions.prepare(TARGET_ID, harness.workspace_id)
    with harness.factory() as session:
        plan = session.scalar(select(AgentWorkspacePromotionPlanRow))
        assert plan is not None and plan.state.value == "preparing"
        assert session.scalar(select(GlobalHeavyOperationLeaseRow)) is not None

    recovered = AgentWorkspacePromotionService(
        harness.factory,
        data_root=harness.data_root,
        development_root=harness.source.parent,
        clock=harness.clock,
    )
    assert recovered.sweep_startup() == 1
    with harness.factory() as session:
        plan = session.scalar(select(AgentWorkspacePromotionPlanRow))
        assert plan is not None and plan.state.value == "invalidated"
        assert session.scalar(select(GlobalHeavyOperationLeaseRow)) is None


def test_central_stale_dispatcher_closes_expired_promotion_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _harness(tmp_path)
    _edit(harness, add=False)
    plan = harness.promotions.prepare(TARGET_ID, harness.workspace_id)
    monkeypatch.setattr(harness.promotions, "_run", lambda *_args: None)
    running = harness.promotions.approve_and_execute(
        TARGET_ID, harness.workspace_id, plan.plan_id
    )
    assert running.state.value == "running"
    harness.clock.now += timedelta(minutes=16)

    result = StaleOperationReconciliationDispatcher(
        harness.factory, clock=harness.clock
    ).sweep_startup()

    assert running.operation_id in result.terminalized_operations
    status = harness.promotions.get_operation(
        TARGET_ID, harness.workspace_id, running.operation_id
    )
    assert status.state.value == "failed"
    assert status.failure_code == "promotion_lease_expired_before_application"
    assert harness.workspaces.get(harness.workspace_id).state.value == "ready"
    with harness.factory() as session:
        assert session.scalar(select(ManagedInstanceMutationLeaseRow)) is None
        assert session.scalar(select(GlobalHeavyOperationLeaseRow)) is None
        assert session.scalar(select(DevelopmentSourcePromotionLeaseRow)) is None
