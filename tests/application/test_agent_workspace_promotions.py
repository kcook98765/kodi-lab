from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.addon_inventory import AddonInventoryQueryStatus
from kodi_lab.application.agent_workspace_cleanup import AgentWorkspaceCleanupService
from kodi_lab.application.agent_workspace_promotions import (
    AgentWorkspacePromotionConflictError,
    AgentWorkspacePromotionService,
)
from kodi_lab.application.agent_workspaces import (
    AgentWorkspaceConflictError,
    AgentWorkspaceService,
)
from kodi_lab.application.mcp_promotion_requests import (
    BoundedPromotionPreparationLimiter,
    McpPromotionRequestError,
    McpPromotionRequestService,
)
from kodi_lab.application.mcp_workspace_development import (
    McpWorkspaceDevelopmentService,
)
from kodi_lab.application.mcp_workspace_mutations import McpWorkspaceCreationService
from kodi_lab.application.promotion_verifications import (
    PromotionVerificationError,
    PromotionVerificationService,
)
from kodi_lab.application.reconciliation import (
    ManagedInstanceReconciliationService,
    StaleOperationReconciliationDispatcher,
)
from kodi_lab.broker.development_sources import (
    DevelopmentSourceDeploymentStateError,
    SqliteDevelopmentSourceDeploymentResolver,
)
from kodi_lab.broker.docker_inventory import ContainerRuntimeState
from kodi_lab.broker.kodi_logs import classify_kodi_log_window
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
    McpClientRow,
    McpDevelopmentRequestRow,
    McpPresetRow,
    McpWorkspaceDeploymentOperationRow,
    McpWorkspaceExecutionOperationRow,
    McpWorkspaceValidationRow,
    PromotionVerificationLogEvidenceRow,
    PromotionVerificationOperationRow,
    RuntimeDefinitionRow,
    RuntimeExecutionProfileRow,
    RuntimeRevisionRow,
    TargetRow,
)
from kodi_lab.storage.repositories import (
    AgentWorkspacePromotionHandoffRepository,
    DevelopmentSourceRepository,
)

SOURCE_ID = UUID("82000000-0000-4000-8000-000000000001")
TARGET_ID = UUID("82000000-0000-4000-8000-000000000002")
RUNTIME_ID = UUID("82000000-0000-4000-8000-000000000003")
REVISION_ID = UUID("82000000-0000-4000-8000-000000000004")
DEPLOYMENT_ID = UUID("82000000-0000-4000-8000-000000000005")
ENABLEMENT_ID = UUID("82000000-0000-4000-8000-000000000006")
SMOKE_ID = UUID("82000000-0000-4000-8000-000000000007")
MCP_PRESET_ID = UUID("82000000-0000-4000-8000-000000000008")
MCP_CLIENT_ID = UUID("82000000-0000-4000-8000-000000000009")
CONTAINER_ID = "a" * 64
SOURCE_DIGEST = "sha256:" + "b" * 64
DESTINATION_DIGEST = "sha256:" + "c" * 64
NOW = datetime(2026, 9, 18, 18, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


def _verification_publisher(factory: sessionmaker[Session], clock: Clock) -> Any:
    service = PromotionVerificationService(
        factory,
        cast(Any, object()),
        cast(Any, object()),
        clock=clock,
    )
    return service.publish_durable


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
        verification_publisher=_verification_publisher(factory, clock),
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


class _NoVerificationMutationBroker:
    stop_calls = 0

    def managed_container_inventory(self) -> Any:
        raise AssertionError("container inventory must not run")

    def capture_promotion_kodi_log_cursor(self, **_kwargs: object) -> Any:
        raise AssertionError("log cursor must not run")

    def stop_managed_container(self, **_kwargs: object) -> None:
        self.stop_calls += 1
        raise AssertionError("stop must not run")

    def copy_sync_promoted_development_source(self, **_kwargs: object) -> Any:
        raise AssertionError("sync must not run")

    def start_managed_container(self, **_kwargs: object) -> None:
        raise AssertionError("start must not run")

    def read_promotion_kodi_log_window(self, **_kwargs: object) -> Any:
        raise AssertionError("log window must not run")


class _NoVerificationInventory:
    def get(self, _instance_id: UUID) -> Any:
        raise AssertionError("Kodi inventory must not run")


def _promotion_with_durable_child(
    harness: Harness,
    verification: PromotionVerificationService,
    *,
    starter: Any,
) -> AgentWorkspacePromotionService:
    return AgentWorkspacePromotionService(
        harness.factory,
        data_root=harness.data_root,
        development_root=harness.source.parent,
        clock=harness.clock,
        verification_publisher=verification.publish_durable,
        verification_starter=starter,
    )


def test_promotion_service_requires_publication_dependency_before_mutation(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)

    with pytest.raises(TypeError, match="verification_publisher"):
        AgentWorkspacePromotionService(  # type: ignore[call-arg]
            harness.factory,
            data_root=harness.data_root,
            development_root=harness.source.parent,
        )
    with pytest.raises(ValueError, match="verification_publisher is required"):
        AgentWorkspacePromotionService(
            harness.factory,
            data_root=harness.data_root,
            development_root=harness.source.parent,
            verification_publisher=None,  # type: ignore[arg-type]
        )

    assert harness.source.joinpath("main.py").read_text() == "print('old')\n"
    with harness.factory() as session:
        assert session.scalar(select(AgentWorkspacePromotionOperationRow)) is None


def test_handoff_reservation_failure_rolls_back_source_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _harness(tmp_path)
    monkeypatch.setattr(
        AgentWorkspacePromotionHandoffRepository,
        "reserve_for_verification",
        lambda _repository, *_args, **_kwargs: False,
    )
    _edit(harness)
    plan = harness.promotions.prepare(TARGET_ID, harness.workspace_id)

    result = harness.promotions.approve_and_execute(
        TARGET_ID, harness.workspace_id, plan.plan_id
    )

    assert result.state.value == "failed"
    assert harness.workspaces.get(harness.workspace_id).state.value == "ready"
    assert harness.source.joinpath("main.py").read_text() == "print('old')\n"
    with harness.factory() as session:
        assert session.scalar(select(PromotionVerificationOperationRow)) is None
        source = session.get(
            AgentWorkspacePromotionOperationRow, str(result.operation_id)
        )
        assert source is not None and source.state.value != "succeeded"


def test_double_publication_reuses_sole_child_and_reservation(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    verification = PromotionVerificationService(
        harness.factory,
        cast(Any, object()),
        cast(Any, object()),
        clock=harness.clock,
    )
    published: list[UUID] = []

    def publish_twice(session: Session, operation_id: UUID) -> UUID:
        first = verification.publish_durable(session, operation_id)
        second = verification.publish_durable(session, operation_id)
        assert second == first
        published.extend((first, second))
        return first

    promotions = AgentWorkspacePromotionService(
        harness.factory,
        data_root=harness.data_root,
        development_root=harness.source.parent,
        verification_publisher=publish_twice,
        clock=harness.clock,
    )
    _edit(harness)
    plan = promotions.prepare(TARGET_ID, harness.workspace_id)
    result = promotions.approve_and_execute(
        TARGET_ID, harness.workspace_id, plan.plan_id
    )

    assert result.state.value == "succeeded"
    assert published[0] == published[1]
    with harness.factory() as session:
        rows = list(session.scalars(select(PromotionVerificationOperationRow)))
        handoff = session.scalar(select(AgentWorkspacePromotionHandoffRow))
        assert len(rows) == 1
        assert handoff is not None
        assert handoff.verification_operation_id == rows[0].id


def test_queue_and_run_cannot_fabricate_missing_child_after_source_success(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    _edit(harness)
    plan = harness.promotions.prepare(TARGET_ID, harness.workspace_id)
    source = harness.promotions.approve_and_execute(
        TARGET_ID, harness.workspace_id, plan.plan_id
    )
    with harness.factory.begin() as session:
        child = session.scalar(select(PromotionVerificationOperationRow))
        assert child is not None
        session.execute(
            update(AgentWorkspacePromotionHandoffRow).values(
                verification_operation_id=None,
                expected_addon_id=None,
                expected_name=None,
                expected_version=None,
                expected_enabled=None,
            )
        )
        session.execute(delete(ManagedInstanceMutationLeaseRow))
        session.execute(delete(GlobalHeavyOperationLeaseRow))
        session.execute(delete(PromotionVerificationOperationRow))
        session.execute(
            delete(DevelopmentSourceDeploymentOperationRow).where(
                DevelopmentSourceDeploymentOperationRow.id
                == child.deployment_operation_id
            )
        )
    verification = PromotionVerificationService(
        harness.factory,
        cast(Any, object()),
        cast(Any, object()),
        clock=harness.clock,
    )

    with pytest.raises(PromotionVerificationError) as missing:
        verification.queue_and_run(source.operation_id)

    assert missing.value.code == "promotion_verification_missing"
    with harness.factory() as session:
        assert session.scalar(select(PromotionVerificationOperationRow)) is None


def test_starter_begin_failure_is_durable_and_source_success_is_unchanged(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    broker = _NoVerificationMutationBroker()

    def fail_start(phase: str) -> None:
        if phase == "starter_begin":
            raise RuntimeError("audit injected starter failure")

    verification = PromotionVerificationService(
        harness.factory,
        broker,
        _NoVerificationInventory(),
        clock=harness.clock,
        sleep=lambda _seconds: None,
        failure_injector=fail_start,
    )
    promotions = _promotion_with_durable_child(
        harness, verification, starter=verification.queue_and_run
    )
    _edit(harness)
    plan = promotions.prepare(TARGET_ID, harness.workspace_id)

    source_result = promotions.approve_and_execute(
        TARGET_ID, harness.workspace_id, plan.plan_id
    )

    assert source_result.state.value == "succeeded"
    assert harness.workspaces.get(harness.workspace_id).state.value == "promoted"
    assert broker.stop_calls == 0
    with harness.factory() as session:
        rows = list(session.scalars(select(PromotionVerificationOperationRow)))
        handoff = session.scalar(select(AgentWorkspacePromotionHandoffRow))
        assert len(rows) == 1
        assert rows[0].state.value == "failed"
        assert rows[0].failure_code == "promotion_verification_start_failed"
        assert handoff is not None
        assert handoff.verification_operation_id == rows[0].id
        assert handoff.consumed_at is None
    repeated = verification.queue_and_run(source_result.operation_id)
    assert str(repeated.verification_id) == rows[0].id


def test_child_creation_failure_cannot_commit_source_success(tmp_path: Path) -> None:
    harness = _harness(tmp_path)

    def fail_publication(_session: Session, _operation_id: UUID) -> UUID:
        raise RuntimeError("audit injected child creation failure")

    promotions = AgentWorkspacePromotionService(
        harness.factory,
        data_root=harness.data_root,
        development_root=harness.source.parent,
        clock=harness.clock,
        verification_publisher=fail_publication,
        verification_starter=lambda _operation_id: None,
    )
    _edit(harness)
    plan = promotions.prepare(TARGET_ID, harness.workspace_id)

    result = promotions.approve_and_execute(
        TARGET_ID, harness.workspace_id, plan.plan_id
    )

    assert result.state.value == "failed"
    assert harness.workspaces.get(harness.workspace_id).state.value == "ready"
    assert harness.source.joinpath("main.py").read_text() == "print('old')\n"
    with harness.factory() as session:
        assert (
            session.scalar(
                select(func.count()).select_from(PromotionVerificationOperationRow)
            )
            == 0
        )
        source = session.get(
            AgentWorkspacePromotionOperationRow, str(result.operation_id)
        )
        assert source is not None and source.state.value != "succeeded"


@pytest.mark.parametrize("failure_kind", ["target_drift", "handoff_invalid"])
def test_preflight_rejection_is_durable_before_target_stop(
    tmp_path: Path, failure_kind: str
) -> None:
    harness = _harness(tmp_path)
    broker = _NoVerificationMutationBroker()
    verification = PromotionVerificationService(
        harness.factory,
        broker,
        _NoVerificationInventory(),
        clock=harness.clock,
        sleep=lambda _seconds: None,
    )
    promotions = _promotion_with_durable_child(
        harness, verification, starter=lambda _operation_id: None
    )
    _edit(harness)
    plan = promotions.prepare(TARGET_ID, harness.workspace_id)
    source_result = promotions.approve_and_execute(
        TARGET_ID, harness.workspace_id, plan.plan_id
    )
    with harness.factory.begin() as session:
        if failure_kind == "target_drift":
            session.execute(
                update(DevelopmentSourceDeploymentRow).values(
                    verified_destination_digest="sha256:" + "9" * 64
                )
            )
        else:
            session.execute(
                update(AgentWorkspacePromotionHandoffRow).values(
                    trusted_container_id="f" * 64
                )
            )

    result = verification.queue_and_run(source_result.operation_id)

    assert result.state.value == "failed"
    assert result.failure_code == (
        "promotion_verification_preflight_drift"
        if failure_kind == "target_drift"
        else "promotion_verification_handoff_invalid"
    )
    assert broker.stop_calls == 0
    with harness.factory() as session:
        assert (
            session.scalar(
                select(func.count()).select_from(PromotionVerificationOperationRow)
            )
            == 1
        )
        handoff = session.scalar(select(AgentWorkspacePromotionHandoffRow))
        source = session.get(
            AgentWorkspacePromotionOperationRow, str(source_result.operation_id)
        )
        assert handoff is not None and handoff.verification_operation_id is not None
        assert handoff.consumed_at is None
        assert source is not None and source.state.value == "succeeded"


def test_published_child_process_boundary_recovers_exactly_once(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    verification = PromotionVerificationService(
        harness.factory,
        _NoVerificationMutationBroker(),
        _NoVerificationInventory(),
        clock=harness.clock,
        sleep=lambda _seconds: None,
    )
    promotions = _promotion_with_durable_child(
        harness, verification, starter=lambda _operation_id: None
    )
    _edit(harness)
    plan = promotions.prepare(TARGET_ID, harness.workspace_id)
    source_result = promotions.approve_and_execute(
        TARGET_ID, harness.workspace_id, plan.plan_id
    )

    assert verification.sweep_startup() == 1
    assert verification.sweep_startup() == 0
    result = verification.get_for_promotion(source_result.operation_id)
    assert result.state.value == "failed"
    assert result.failure_code == "promotion_verification_interrupted"
    with harness.factory() as session:
        assert (
            session.scalar(
                select(func.count()).select_from(PromotionVerificationOperationRow)
            )
            == 1
        )
        handoff = session.scalar(select(AgentWorkspacePromotionHandoffRow))
        assert handoff is not None
        assert handoff.verification_operation_id == str(result.verification_id)
        assert handoff.consumed_at is None
        assert session.scalar(select(ManagedInstanceMutationLeaseRow)) is None
        assert session.scalar(select(GlobalHeavyOperationLeaseRow)) is None


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
        verification_rows = list(
            session.scalars(select(PromotionVerificationOperationRow))
        )
        assert deployment is not None and deployment.confidence.value == "stale"
        assert handoff is not None
        assert handoff.target_instance_id == str(TARGET_ID)
        assert handoff.prior_deployment_operation_id == str(DEPLOYMENT_ID)
        assert handoff.prior_source_digest == SOURCE_DIGEST
        assert handoff.prior_destination_digest == DESTINATION_DIGEST
        assert len(verification_rows) == 1
        assert handoff.verification_operation_id == verification_rows[0].id
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
            == 1
        )
        assert (
            session.scalar(
                select(func.count()).select_from(GlobalHeavyOperationLeaseRow)
            )
            == 1
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

    cleanup = AgentWorkspaceCleanupService(
        harness.factory,
        data_root=harness.data_root,
        clock=harness.clock,
    )
    queued_cleanup = cleanup.queue(harness.workspace_id)
    cleanup_lease = cleanup.acquire(queued_cleanup.operation_id)
    assert cleanup_lease is not None
    cleanup.run(cleanup_lease)
    assert cleanup.get(queued_cleanup.operation_id).state.value == "succeeded"
    retained_workspace = harness.workspaces.get(harness.workspace_id)
    assert retained_workspace.state.value == "promoted"
    assert retained_workspace.bytes_state.value == "cleaned"
    assert harness.source.joinpath("main.py").read_text() == "print('promoted')\n"
    with harness.factory() as session:
        source_operation = session.get(
            AgentWorkspacePromotionOperationRow, str(source_result.operation_id)
        )
        assert source_operation is not None
        assert source_operation.backup_artifact_id is not None
        assert (
            harness.data_root
            / "source-promotions"
            / str(source_result.operation_id)
            / f"backup-{source_operation.backup_artifact_id}"
        ).is_dir()
        assert (
            session.get(
                PromotionVerificationLogEvidenceRow, str(verification.verification_id)
            )
            is not None
        )


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
    boundary_secret = "status-boundary-secret-" + "z" * 42_000
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
            if log_result == "target_errors":
                return classify_kodi_log_window(
                    (
                        f"api_key={boundary_secret} ERROR "
                        "plugin.video.example dependency failed\n"
                    ).encode(),
                    "plugin.video.example",
                )
            return SimpleNamespace(
                classification=log_result,
                target_error_count=0,
                unrelated_warning_count=0,
                snippets=[],
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
    rendered_status = verification.model_dump_json()
    assert "status-boundary-secret" not in rendered_status
    assert "z" * 32 not in rendered_status
    if log_result == "target_errors":
        assert "[REDACTED]" in rendered_status
    with harness.factory() as session:
        retained = session.get(
            AgentWorkspacePromotionOperationRow, str(source_result.operation_id)
        )
        assert retained is not None and retained.state.value == "succeeded"
        evidence = session.get(
            PromotionVerificationLogEvidenceRow, str(verification.verification_id)
        )
        assert evidence is not None
        assert "status-boundary-secret" not in evidence.target_error_snippets_json
        assert "z" * 32 not in evidence.target_error_snippets_json
        if log_result == "target_errors":
            assert "[REDACTED]" in evidence.target_error_snippets_json


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
        verification_publisher=_verification_publisher(harness.factory, harness.clock),
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
        verification_publisher=_verification_publisher(harness.factory, harness.clock),
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


def _seed_mcp_promotion_evidence(
    harness: Harness,
    *,
    validation: bool = True,
    deployment: bool = True,
    execution: bool = True,
) -> tuple[UUID, UUID]:
    workspace = harness.workspaces.get(harness.workspace_id)
    deployment_id = uuid4()
    execution_id = uuid4()
    timestamp = NOW.isoformat(timespec="microseconds").replace("+00:00", "Z")
    with harness.factory.begin() as session:
        session.add(
            McpPresetRow(
                id=str(MCP_PRESET_ID),
                display_name="Promotion test preset",
                builtin=False,
                assignable=True,
            )
        )
        session.flush()
        session.add(
            McpClientRow(
                id=str(MCP_CLIENT_ID),
                display_name="Promotion requester",
                enabled=True,
                preset_id=str(MCP_PRESET_ID),
                created_at=timestamp,
                last_used_at=None,
                disabled_at=None,
            )
        )
        if validation:
            session.add(
                McpWorkspaceValidationRow(
                    id=str(uuid4()),
                    workspace_id=str(harness.workspace_id),
                    development_source_id=str(SOURCE_ID),
                    generation=workspace.generation,
                    classification="passed",
                    addon_id="plugin.video.example",
                    addon_version="1.0.0",
                    extension_types_json='["xbmc.python.pluginsource"]',
                    validated_at=timestamp,
                )
            )
        if deployment:
            session.add(
                McpWorkspaceDeploymentOperationRow(
                    id=str(deployment_id),
                    client_id=str(MCP_CLIENT_ID),
                    request_id=str(uuid4()),
                    workspace_id=str(harness.workspace_id),
                    development_source_id=str(SOURCE_ID),
                    target_id=str(TARGET_ID),
                    runtime_revision_id=str(REVISION_ID),
                    generation=workspace.generation,
                    addon_id="plugin.video.example",
                    addon_version="1.0.0",
                    state="succeeded",
                    phase="finalize",
                    mutation_started=True,
                    target_stopped=True,
                    sync_started=True,
                    sync_completed=True,
                    target_started=True,
                    inventory_classification="matched",
                    addon_enabled=True,
                    result_classification="deployed",
                    source_manifest_digest="sha256:" + "d" * 64,
                    verified_destination_digest="sha256:" + "f" * 64,
                    failure_code=None,
                    requested_at=timestamp,
                    started_at=timestamp,
                    completed_at=timestamp,
                )
            )
            session.execute(
                update(DevelopmentSourceDeploymentRow)
                .where(
                    DevelopmentSourceDeploymentRow.managed_instance_id
                    == str(TARGET_ID),
                    DevelopmentSourceDeploymentRow.development_source_id
                    == str(SOURCE_ID),
                )
                .values(confidence="stale")
            )
        if execution:
            session.add(
                McpWorkspaceExecutionOperationRow(
                    id=str(execution_id),
                    client_id=str(MCP_CLIENT_ID),
                    request_id=str(uuid4()),
                    workspace_id=str(harness.workspace_id),
                    development_source_id=str(SOURCE_ID),
                    target_id=str(TARGET_ID),
                    deployment_id=str(deployment_id),
                    generation=workspace.generation,
                    addon_id="plugin.video.example",
                    addon_version="1.0.0",
                    state="succeeded",
                    phase="finalize",
                    invocation_dispatched=True,
                    execution_classification="directory_result",
                    item_count=0,
                    result_classification="passed",
                    failure_code=None,
                    requested_at=timestamp,
                    started_at=timestamp,
                    completed_at=timestamp,
                )
            )
    return deployment_id, execution_id


def _request_pruning_services(
    harness: Harness,
) -> tuple[
    McpWorkspaceCreationService,
    McpWorkspaceDevelopmentService,
    McpPromotionRequestService,
]:
    creation = McpWorkspaceCreationService(
        harness.factory,
        harness.workspaces,
        lambda _operation_id: None,
        clock=harness.clock,
    )
    development = McpWorkspaceDevelopmentService(
        harness.factory,
        cast(Any, object()),
        cast(Any, object()),
        harness.workspaces,
        data_root=harness.data_root,
        clock=harness.clock,
    )
    promotion = McpPromotionRequestService(
        harness.factory, harness.promotions, clock=harness.clock
    )
    return creation, development, promotion


def _old_request_timestamp(harness: Harness) -> str:
    return (
        (harness.clock.now - timedelta(days=31))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def test_mcp_promotion_request_replays_one_phase9_plan_and_web_approval(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    _edit(harness, add=False)
    _seed_mcp_promotion_evidence(harness)
    service = McpPromotionRequestService(
        harness.factory, harness.promotions, clock=harness.clock
    )
    request_id = uuid4()

    requested = service.request(
        client_id=MCP_CLIENT_ID,
        request_id=request_id,
        workspace_id=harness.workspace_id,
        expected_generation=1,
        target_id=TARGET_ID,
    )
    restarted = McpPromotionRequestService(
        harness.factory, harness.promotions, clock=harness.clock
    )
    replayed = restarted.request(
        client_id=MCP_CLIENT_ID,
        request_id=request_id,
        workspace_id=harness.workspace_id,
        expected_generation=1,
        target_id=TARGET_ID,
    )

    assert replayed == requested
    assert requested.review_state == "reviewable"
    assert requested.web_review_required is True
    assert requested.changed_file_count == 1
    assert requested.modified_file_count == 1
    with harness.factory() as session:
        assert (
            session.scalar(
                select(func.count()).select_from(AgentWorkspacePromotionPlanRow)
            )
            == 1
        )
        assert (
            session.scalar(select(func.count()).select_from(McpDevelopmentRequestRow))
            == 1
        )
    review = harness.promotions.get_plan(
        TARGET_ID, harness.workspace_id, requested.promotion_plan_id
    )
    assert review.originating_actor == "mcp_client"
    assert review.originating_mcp_client_id == MCP_CLIENT_ID
    assert review.originating_mcp_client_name == "Promotion requester"
    assert review.validation_classification == "passed"
    assert review.deployment_classification == "succeeded"
    assert review.execution_classification == "succeeded"

    approved = harness.promotions.approve_and_execute(
        TARGET_ID, harness.workspace_id, requested.promotion_plan_id
    )
    assert approved.state.value == "succeeded"
    projection = service.projection(harness.workspace_id)
    assert projection.promotion_operation_id == approved.operation_id
    assert projection.verification_id is not None
    assert projection.verification_state == "queued"
    assert projection.final_classification == "source_promoted_verification_queued"
    old = _old_request_timestamp(harness)
    with harness.factory.begin() as session:
        session.execute(
            update(McpDevelopmentRequestRow)
            .where(McpDevelopmentRequestRow.request_id == str(request_id))
            .values(created_at=old, updated_at=old)
        )
        service._prune_and_require_capacity(session, MCP_CLIENT_ID)
        assert session.scalar(select(McpDevelopmentRequestRow)) is not None

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
                        version="1.0.0",
                        enabled=True,
                        kodi_type="xbmc.python.pluginsource",
                    )
                ],
            )

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
                new_count=0,
                changed_count=1,
                unchanged_count=1,
                destination_only_count=0,
                transferred_bytes=20,
            )

        def start_managed_container(self, **_kwargs: object) -> None:
            self.state = ContainerRuntimeState.RUNNING

        def read_promotion_kodi_log_window(self, **_kwargs: object) -> Any:
            return SimpleNamespace(
                classification="clean",
                target_error_count=0,
                unrelated_warning_count=0,
                snippets=[],
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
    ).queue_and_run(approved.operation_id)
    assert verification.state.value == "succeeded"
    verified_projection = service.projection(harness.workspace_id)
    assert verified_projection.verification_state == "succeeded"
    assert verified_projection.final_classification == "source_promoted_and_verified"
    with harness.factory.begin() as session:
        session.execute(
            update(McpDevelopmentRequestRow)
            .where(McpDevelopmentRequestRow.request_id == str(request_id))
            .values(created_at=old, updated_at=old)
        )
        service._prune_and_require_capacity(session, MCP_CLIENT_ID)
        assert session.scalar(select(McpDevelopmentRequestRow)) is None


def test_mcp_promotion_post_marker_replay_is_ambiguous_and_mapping_is_retained(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    _edit(harness, add=False)
    _seed_mcp_promotion_evidence(harness)
    service = McpPromotionRequestService(
        harness.factory, harness.promotions, clock=harness.clock
    )
    request_id = uuid4()
    requested = service.request(
        client_id=MCP_CLIENT_ID,
        request_id=request_id,
        workspace_id=harness.workspace_id,
        expected_generation=1,
        target_id=TARGET_ID,
    )
    old = (
        (harness.clock.now - timedelta(days=31))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )
    with harness.factory.begin() as session:
        session.execute(
            update(McpDevelopmentRequestRow)
            .where(McpDevelopmentRequestRow.request_id == str(request_id))
            .values(created_at=old)
        )
        service._prune_and_require_capacity(session, MCP_CLIENT_ID)
        assert (
            session.scalar(select(func.count()).select_from(McpDevelopmentRequestRow))
            == 1
        )
        session.execute(
            update(AgentWorkspacePromotionPlanRow)
            .where(
                AgentWorkspacePromotionPlanRow.id == str(requested.promotion_plan_id)
            )
            .values(state="preparing")
        )

    restarted = McpPromotionRequestService(
        harness.factory, harness.promotions, clock=harness.clock
    )
    with pytest.raises(McpPromotionRequestError, match="promotion_request_ambiguous"):
        restarted.request(
            client_id=MCP_CLIENT_ID,
            request_id=request_id,
            workspace_id=harness.workspace_id,
            expected_generation=1,
            target_id=TARGET_ID,
        )


def test_active_promotion_mapping_survives_every_cross_operation_pruner_and_replays(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    _edit(harness, add=False)
    _seed_mcp_promotion_evidence(harness)
    creation, development, promotion = _request_pruning_services(harness)
    request_id = uuid4()
    requested = promotion.request(
        client_id=MCP_CLIENT_ID,
        request_id=request_id,
        workspace_id=harness.workspace_id,
        expected_generation=1,
        target_id=TARGET_ID,
    )
    old = _old_request_timestamp(harness)
    with harness.factory.begin() as session:
        session.execute(
            update(McpDevelopmentRequestRow)
            .where(McpDevelopmentRequestRow.request_id == str(request_id))
            .values(created_at=old, updated_at=old)
        )
        creation._prune_expired_requests(session, harness.clock.now)
        assert (
            session.scalar(select(func.count()).select_from(McpDevelopmentRequestRow))
            == 1
        )
    with harness.factory.begin() as session:
        development._prune_and_require_capacity(
            session, MCP_CLIENT_ID, harness.clock.now
        )
        assert (
            session.scalar(select(func.count()).select_from(McpDevelopmentRequestRow))
            == 1
        )
    with harness.factory.begin() as session:
        promotion._prune_and_require_capacity(session, MCP_CLIENT_ID)
        assert (
            session.scalar(select(func.count()).select_from(McpDevelopmentRequestRow))
            == 1
        )

    restarted = McpPromotionRequestService(
        harness.factory, harness.promotions, clock=harness.clock
    )
    assert (
        restarted.request(
            client_id=MCP_CLIENT_ID,
            request_id=request_id,
            workspace_id=harness.workspace_id,
            expected_generation=1,
            target_id=TARGET_ID,
        )
        == requested
    )
    with harness.factory() as session:
        assert (
            session.scalar(
                select(func.count()).select_from(AgentWorkspacePromotionPlanRow)
            )
            == 1
        )


def test_ambiguous_development_mappings_survive_cross_operation_pruners(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    deployment_id, execution_id = _seed_mcp_promotion_evidence(harness)
    creation, development, promotion = _request_pruning_services(harness)
    deployment_request = uuid4()
    execution_request = uuid4()
    missing_request = uuid4()
    old = _old_request_timestamp(harness)
    with harness.factory.begin() as session:
        session.execute(
            update(McpWorkspaceDeploymentOperationRow)
            .where(McpWorkspaceDeploymentOperationRow.id == str(deployment_id))
            .values(
                request_id=str(deployment_request),
                state="reconciliation_required",
                failure_code="deployment_ambiguous",
            )
        )
        session.execute(
            update(McpWorkspaceExecutionOperationRow)
            .where(McpWorkspaceExecutionOperationRow.id == str(execution_id))
            .values(
                request_id=str(execution_request),
                state="ambiguous",
                execution_classification="ambiguous",
                failure_code="execution_ambiguous",
            )
        )
        session.add_all(
            [
                McpDevelopmentRequestRow(
                    id=str(uuid4()),
                    client_id=str(MCP_CLIENT_ID),
                    request_id=str(deployment_request),
                    operation_kind="workspace_deploy",
                    parameter_fingerprint="a" * 64,
                    development_source_id=str(SOURCE_ID),
                    workspace_id=str(harness.workspace_id),
                    creation_operation_id=None,
                    operation_id=str(deployment_id),
                    state="allocated",
                    created_at=old,
                    updated_at=old,
                ),
                McpDevelopmentRequestRow(
                    id=str(uuid4()),
                    client_id=str(MCP_CLIENT_ID),
                    request_id=str(execution_request),
                    operation_kind="workspace_execute",
                    parameter_fingerprint="b" * 64,
                    development_source_id=str(SOURCE_ID),
                    workspace_id=str(harness.workspace_id),
                    creation_operation_id=None,
                    operation_id=str(execution_id),
                    state="allocated",
                    created_at=old,
                    updated_at=old,
                ),
                McpDevelopmentRequestRow(
                    id=str(uuid4()),
                    client_id=str(MCP_CLIENT_ID),
                    request_id=str(missing_request),
                    operation_kind="workspace_deploy",
                    parameter_fingerprint="c" * 64,
                    development_source_id=str(SOURCE_ID),
                    workspace_id=str(harness.workspace_id),
                    creation_operation_id=None,
                    operation_id=str(uuid4()),
                    state="allocated",
                    created_at=old,
                    updated_at=old,
                ),
            ]
        )

    with harness.factory.begin() as session:
        promotion._prune_and_require_capacity(session, MCP_CLIENT_ID)
    with harness.factory.begin() as session:
        creation._prune_expired_requests(session, harness.clock.now)
        development._prune_and_require_capacity(
            session, MCP_CLIENT_ID, harness.clock.now
        )
    with harness.factory() as session:
        retained = set(
            session.scalars(select(McpDevelopmentRequestRow.request_id)).all()
        )
    assert retained == {
        str(deployment_request),
        str(execution_request),
        str(missing_request),
    }


def test_web_promotion_authority_survives_originating_client_disable(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    _edit(harness, add=False)
    _seed_mcp_promotion_evidence(harness)
    service = McpPromotionRequestService(
        harness.factory, harness.promotions, clock=harness.clock
    )
    requested = service.request(
        client_id=MCP_CLIENT_ID,
        request_id=uuid4(),
        workspace_id=harness.workspace_id,
        expected_generation=1,
        target_id=TARGET_ID,
    )
    with harness.factory.begin() as session:
        session.execute(
            update(McpClientRow)
            .where(McpClientRow.id == str(MCP_CLIENT_ID))
            .values(
                enabled=False,
                disabled_at=NOW.isoformat(timespec="microseconds").replace(
                    "+00:00", "Z"
                ),
            )
        )

    approved = harness.promotions.approve_and_execute(
        TARGET_ID, harness.workspace_id, requested.promotion_plan_id
    )
    assert approved.state.value == "succeeded"
    assert service.projection(harness.workspace_id).verification_state == "queued"


@pytest.mark.parametrize(
    ("validation", "deployment", "execution", "expected"),
    (
        (False, True, True, "validation_required"),
        (True, False, False, "deployment_required"),
        (True, True, False, "execution_required"),
    ),
)
def test_mcp_promotion_request_requires_exact_slice4_evidence(
    tmp_path: Path,
    validation: bool,
    deployment: bool,
    execution: bool,
    expected: str,
) -> None:
    harness = _harness(tmp_path)
    _edit(harness, add=False)
    _seed_mcp_promotion_evidence(
        harness,
        validation=validation,
        deployment=deployment,
        execution=execution,
    )
    service = McpPromotionRequestService(
        harness.factory, harness.promotions, clock=harness.clock
    )
    with pytest.raises(McpPromotionRequestError, match=expected):
        service.request(
            client_id=MCP_CLIENT_ID,
            request_id=uuid4(),
            workspace_id=harness.workspace_id,
            expected_generation=1,
            target_id=TARGET_ID,
        )


def test_mcp_promotion_request_conflict_expiry_rejection_and_source_drift(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    _edit(harness, add=False)
    _seed_mcp_promotion_evidence(harness)
    service = McpPromotionRequestService(
        harness.factory, harness.promotions, clock=harness.clock
    )
    request_id = uuid4()
    service.request(
        client_id=MCP_CLIENT_ID,
        request_id=request_id,
        workspace_id=harness.workspace_id,
        expected_generation=1,
        target_id=TARGET_ID,
    )
    with pytest.raises(McpPromotionRequestError, match="request_id_conflict"):
        service.request(
            client_id=MCP_CLIENT_ID,
            request_id=request_id,
            workspace_id=harness.workspace_id,
            expected_generation=2,
            target_id=TARGET_ID,
        )
    with pytest.raises(McpPromotionRequestError, match="promotion_already_requested"):
        service.request(
            client_id=MCP_CLIENT_ID,
            request_id=uuid4(),
            workspace_id=harness.workspace_id,
            expected_generation=1,
            target_id=TARGET_ID,
        )

    harness.clock.now += timedelta(minutes=16)
    assert (
        service.request(
            client_id=MCP_CLIENT_ID,
            request_id=request_id,
            workspace_id=harness.workspace_id,
            expected_generation=1,
            target_id=TARGET_ID,
        ).review_state
        == "expired"
    )
    second = service.request(
        client_id=MCP_CLIENT_ID,
        request_id=uuid4(),
        workspace_id=harness.workspace_id,
        expected_generation=1,
        target_id=TARGET_ID,
    )
    rejected = harness.promotions.reject(
        TARGET_ID, harness.workspace_id, second.promotion_plan_id
    )
    assert rejected.state.value == "invalidated"

    service = McpPromotionRequestService(
        harness.factory, harness.promotions, clock=harness.clock
    )
    third = service.request(
        client_id=MCP_CLIENT_ID,
        request_id=uuid4(),
        workspace_id=harness.workspace_id,
        expected_generation=1,
        target_id=TARGET_ID,
    )
    harness.source.joinpath("main.py").write_text(
        "print('external change')\n", encoding="utf-8"
    )
    result = harness.promotions.approve_and_execute(
        TARGET_ID, harness.workspace_id, third.promotion_plan_id
    )
    assert result.state.value == "failed"
    assert result.failure_code == "promotion_source_overlap"
    assert harness.workspaces.get(harness.workspace_id).state.value == "ready"
    assert harness.source.joinpath("main.py").read_text(encoding="utf-8") == (
        "print('external change')\n"
    )


def test_promotion_preparation_limiter_is_two_per_ten_minutes_and_bounded() -> None:
    now = [0.0]
    limiter = BoundedPromotionPreparationLimiter(maximum_keys=2, clock=lambda: now[0])
    assert limiter.allow("one") is True
    assert limiter.allow("one") is True
    assert limiter.allow("one") is False
    assert limiter.allow("two") is True
    assert limiter.allow("three") is False
    assert limiter.key_count == 2
    now[0] = 601.0
    assert limiter.allow("three") is True
    assert limiter.key_count == 1
