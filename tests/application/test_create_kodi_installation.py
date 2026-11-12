"""Real installation adapter, isolated receipts and fake broker/worker effects."""

from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier, Event
from types import SimpleNamespace
from typing import Literal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.create_kodi import CreateKodiService
from kodi_lab.application.create_kodi_installation import CreateKodiInstallationService
from kodi_lab.application.runtime_installations import (
    Generation,
    RuntimeInstallationService,
    RuntimeInstallationView,
)
from kodi_lab.config import Settings
from kodi_lab.domain import RevisionApprovalState
from kodi_lab.domain import RuntimeRecipeInstallationState as State
from kodi_lab.domain.create_kodi import (
    CreateCheckpoint,
    CreateOutcome,
    CreateStage,
    CreateTransitionError,
)
from kodi_lab.host_bootstrap import generate_master_key
from kodi_lab.runtime.install import _commit_ready
from kodi_lab.runtime.preview import PreviewAcknowledgement, admission_evidence
from kodi_lab.runtime.recipes import load_installation_recipe
from kodi_lab.storage.create_kodi import CreateKodiRepository
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.storage.models import RuntimeRecipeInstallationRow, RuntimeRevisionRow
from kodi_lab.storage.repositories import RuntimeRecipeInstallationRepository
from tests.application.test_runtime_installations import FakeBroker, create_row
from tests.create_kodi_support import accept
from tests.efficiency import CurrentSchemaTemplate
from tests.runtime import test_recipe_installation as recipe


@pytest.fixture(scope="module")
def schema(tmp_path_factory: pytest.TempPathFactory) -> CurrentSchemaTemplate:
    return CurrentSchemaTemplate(tmp_path_factory.mktemp("create-install-schema"))


@pytest.fixture
def factory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    schema: CurrentSchemaTemplate,
) -> Iterator[sessionmaker[Session]]:
    state_root = tmp_path / "state"
    state_root.mkdir(mode=0o700)
    generate_master_key(state_root)
    monkeypatch.setenv("KODI_LAB_DATA_ROOT", str(state_root))
    path = tmp_path / "recipe.db"
    schema.clone(path)
    engine = create_database_engine(f"sqlite:///{path}")
    yield create_session_factory(engine)
    engine.dispose()


def finish(factory: sessionmaker[Session], child: UUID) -> None:
    with factory() as session:
        row = session.get(RuntimeRecipeInstallationRow, str(child))
        assert row is not None
        state, generation = row.state, row.generation
    while state is not State.VALIDATING:
        with factory.begin() as session:
            repo = RuntimeRecipeInstallationRepository(session)
            repo.advance(child, expected=state, now=datetime.now(UTC))
            row = repo.get(child)
            assert row is not None
            state = row.state
    if generation == "piers":
        from tests.runtime.test_ostree_installation import assembly, validation

        built = assembly(child)
        _commit_ready(
            factory,
            load_installation_recipe(generation),
            child,
            built,
            validation(built.image_id),
            data_root=Settings().data_root,
        )
    else:
        _commit_ready(
            factory,
            load_installation_recipe(generation),
            child,
            recipe._assembly(generation=generation),
            recipe._validation(generation=generation),
            data_root=Settings().data_root,
        )


class Broker(FakeBroker):
    def __init__(
        self, factory: sessionmaker[Session], repo: CreateKodiRepository
    ) -> None:
        super().__init__()
        self.factory, self.repo = factory, repo
        self.parent: UUID | None = None
        self.workers: dict[str, str] = {}
        self.mode = "ready"
        self.entered, self.release = Event(), Event()

    def observe_reviewed_runtime_installation(
        self, operation_id: str
    ) -> SimpleNamespace:
        return SimpleNamespace(
            state=self.workers.get(operation_id, "missing"), lock_held=self.lock_held
        )

    def launch_reviewed_runtime_installation(
        self,
        *,
        generation: str,
        operation_id: str,
        preview_acknowledgement: PreviewAcknowledgement | None = None,
    ) -> SimpleNamespace:
        assert self.parent is not None
        persisted = self.repo.get(self.parent)
        assert persisted and persisted.checkpoint is CreateCheckpoint.DISPATCHED
        assert persisted.installation_operation_id == UUID(operation_id)
        assert persisted.dispatched_at is not None
        self.launches.append((generation, operation_id))
        self.entered.set()
        if self.mode == "blocked":
            assert self.release.wait(5)
        if self.mode == "no_response":
            raise OSError("response unavailable before any receipt")
        if generation == "piers":
            from kodi_lab.broker.reviewed_installations import (
                DockerReviewedInstallationWorker,
            )
            from tests.runtime.test_ostree_installation import acknowledgement

            assert preview_acknowledgement == acknowledgement()
            # The real privileged worker only reads application-owned admission.
            # Creating it in this double hid a native Create failure.
            DockerReviewedInstallationWorker(
                session_factory=self.factory
            )._admit_preview(UUID(operation_id), preview_acknowledgement)
        else:
            assert preview_acknowledgement is None
            create_row(self.factory, generation, UUID(operation_id))
        self.workers[operation_id] = "running"
        self.operation_id, self.generation = operation_id, generation
        if self.mode == "lost_response":
            raise OSError("response lost after worker acceptance")
        if self.mode in {"ready", "blocked"}:
            finish(self.factory, UUID(operation_id))
            self.workers[operation_id] = "exited"
            self.operation_id = self.generation = None
        if self.mode == "failed":
            with self.factory.begin() as session:
                RuntimeRecipeInstallationRepository(session).fail(
                    UUID(operation_id), code="validation_failed", now=datetime.now(UTC)
                )
            self.workers[operation_id] = "exited"
            self.operation_id = self.generation = None
        return SimpleNamespace(
            operation_id=(str(uuid4()) if self.mode == "wrong_reply" else operation_id),
            generation=generation,
            newly_accepted=True,
        )


def setup(
    factory: sessionmaker[Session],
) -> tuple[
    CreateKodiService,
    CreateKodiInstallationService,
    Broker,
    RuntimeInstallationService,
]:
    repo = CreateKodiRepository(factory)
    broker = Broker(factory, repo)
    installer = RuntimeInstallationService(factory, broker)
    return (
        CreateKodiService(repo),
        CreateKodiInstallationService(repo, installer),
        broker,
        installer,
    )


def test_exact_planned_uuid_checkpoint_success_and_duplicate(
    factory: sessionmaker[Session],
) -> None:
    app, adapter, broker, _ = setup(factory)
    op = accept(app)
    op = app.repository.plan(op.id, version=op.version)
    original = op.installation_operation_id
    broker.parent = op.id
    result = adapter.install(op.id, version=op.version)
    assert result.checkpoint is CreateCheckpoint.CONFIRMED
    assert result.stage is CreateStage.INSTALLATION
    assert result.installation_operation_id == original
    assert result.runtime_revision_id and result.image_id == recipe.IMAGE
    assert adapter.install(op.id, version=result.version) == result
    assert accept(app, op.request_id) == result
    assert broker.launches == [("omega", str(original))]
    assert result.container_id is None  # No registration/provision/start integration.


def test_piers_create_commits_exact_preview_admission_before_broker_dispatch(
    factory: sessionmaker[Session],
) -> None:
    app, adapter, broker, _ = setup(factory)
    op = accept(app, generation="piers", preview_approved=True)
    broker.parent, broker.mode = op.id, "pending"
    result = adapter.install(op.id, version=op.version)
    assert result.checkpoint is CreateCheckpoint.DISPATCHED
    assert result.installation_operation_id is not None
    assert broker.workers == {str(result.installation_operation_id): "running"}
    with factory() as session:
        row = session.get(
            RuntimeRecipeInstallationRow, str(result.installation_operation_id)
        )
        assert row and row.state is State.PENDING
        assert (row.recipe_id, row.recipe_revision, row.recipe_digest) == (
            op.recipe_id,
            op.recipe_revision,
            op.recipe_digest,
        )
        from tests.runtime.test_ostree_installation import acknowledgement

        assert row.attestation_json == admission_evidence(
            acknowledgement(), result.installation_operation_id
        )
    # Passive observation and original intent replay never dispatch a second worker.
    assert adapter.resolve(result.id, version=result.version) == result
    assert accept(app, op.request_id, "piers", preview_approved=True) == result
    assert len(broker.launches) == 1


def test_piers_admission_failure_rolls_back_dispatch_and_can_resume_original(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    app, adapter, broker, _ = setup(factory)
    op = accept(app, generation="piers", preview_approved=True)
    broker.parent, broker.mode = op.id, "pending"
    prepare = adapter._prepare_preview_admission

    def fail(session: Session, current: object) -> None:
        from kodi_lab.domain.create_kodi import CreateOperation

        assert isinstance(current, CreateOperation)
        prepare(session, current)
        raise ValueError("interrupted admission transaction")

    monkeypatch.setattr(adapter, "_prepare_preview_admission", fail)
    refused = adapter.install(op.id, version=op.version)
    assert refused.outcome is CreateOutcome.REFUSED
    assert refused.dispatched_at is None and broker.launches == []
    with factory() as session:
        assert (
            session.get(
                RuntimeRecipeInstallationRow, str(refused.installation_operation_id)
            )
            is None
        )
    monkeypatch.setattr(adapter, "_prepare_preview_admission", prepare)
    resumed = adapter.resume_refused(refused.id, version=refused.version)
    assert resumed.installation_operation_id == refused.installation_operation_id
    assert resumed.checkpoint is CreateCheckpoint.DISPATCHED
    assert len(broker.launches) == 1


@pytest.mark.parametrize("mode", ["lost_response", "no_response", "wrong_reply"])
def test_piers_dispatch_uncertainty_retains_admission_without_relaunch(
    factory: sessionmaker[Session], mode: str
) -> None:
    app, adapter, broker, _ = setup(factory)
    op = accept(app, generation="piers", preview_approved=True)
    broker.parent, broker.mode = op.id, mode
    result = adapter.install(op.id, version=op.version)
    assert result.outcome is CreateOutcome.UNCERTAIN
    child = result.installation_operation_id
    with factory() as session:
        row = session.get(RuntimeRecipeInstallationRow, str(child))
        assert row and row.state is State.PENDING and row.attestation_json
        admission = row.attestation_json
    assert adapter.install(result.id, version=result.version) == result
    adapter.resolve(result.id, version=result.version)
    with factory() as session:
        row = session.get(RuntimeRecipeInstallationRow, str(child))
        assert row and row.attestation_json == admission
    assert len(broker.launches) == 1


@pytest.mark.parametrize("when", ["before_checkpoint", "after_checkpoint"])
def test_pre_dispatch_interruptions_preserve_id_and_uncertainty(
    factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    when: str,
) -> None:
    app, adapter, broker, _ = setup(factory)
    op = accept(app)
    op = app.repository.plan(op.id, version=op.version)
    broker.parent = op.id
    original_claim = app.repository.claim_dispatch

    def crash(operation_id: UUID, *, version: int) -> None:
        if when == "after_checkpoint":
            original_claim(operation_id, version=version)
        raise KeyboardInterrupt("simulated interruption")

    monkeypatch.setattr(app.repository, "claim_dispatch", crash)
    with pytest.raises(KeyboardInterrupt):
        adapter.install(op.id, version=op.version)
    current = app.get(op.id)
    assert current and current.installation_operation_id == op.installation_operation_id
    assert broker.launches == []
    monkeypatch.setattr(app.repository, "claim_dispatch", original_claim)
    if when == "before_checkpoint":
        assert (
            current.outcome is CreateOutcome.REFUSED and current.dispatched_at is None
        )
        done = adapter.resume_refused(current.id, version=current.version)
        assert done.checkpoint is CreateCheckpoint.CONFIRMED
        assert done.installation_operation_id == op.installation_operation_id
    else:
        assert adapter.install(current.id, version=current.version) == current
        current = adapter.resolve(current.id, version=current.version)
        assert current.outcome is CreateOutcome.UNCERTAIN
        assert broker.launches == []


@pytest.mark.parametrize("mode", ["lost_response", "no_response", "wrong_reply"])
def test_lost_and_mismatched_responses_never_dispatch_again(
    factory: sessionmaker[Session],
    mode: str,
) -> None:
    app, adapter, broker, _ = setup(factory)
    op = accept(app)
    broker.parent, broker.mode = op.id, mode
    uncertain = adapter.install(op.id, version=op.version)
    assert uncertain.outcome is CreateOutcome.UNCERTAIN
    assert uncertain.installation_operation_id is not None
    assert adapter.install(op.id, version=uncertain.version) == uncertain
    assert adapter.resolve(op.id, version=uncertain.version) == uncertain
    assert len(broker.launches) == 1
    if mode != "no_response":
        finish(factory, uncertain.installation_operation_id)
        broker.workers[str(uncertain.installation_operation_id)] = "exited"
        broker.operation_id = broker.generation = None
        resolved = adapter.resolve(op.id, version=uncertain.version)
        assert resolved.checkpoint is CreateCheckpoint.CONFIRMED
        assert resolved.installation_operation_id == uncertain.installation_operation_id
        assert resolved.dispatched_at == uncertain.dispatched_at
        assert len(broker.launches) == 1


@pytest.mark.parametrize("planned", [False, True])
def test_verified_cached_reuse_without_dispatch_or_uuid_replacement(
    factory: sessionmaker[Session],
    planned: bool,
) -> None:
    app, adapter, broker, _ = setup(factory)
    op = accept(app)
    if planned:
        op = app.repository.plan(op.id, version=op.version)
    cached = recipe._ready(factory, generation="omega")
    reused = adapter.install(op.id, version=op.version)
    assert reused.stage is CreateStage.REGISTRATION
    assert reused.checkpoint is CreateCheckpoint.ACCEPTED
    assert reused.dispatched_at is None and broker.launches == []
    assert reused.installation_operation_id == (
        op.installation_operation_id if planned else cached
    )
    with factory() as session:
        source = RuntimeRecipeInstallationRepository(session).ready_for_revision(
            reused.runtime_revision_id or uuid4()
        )
        assert (
            source and source.id == str(cached) and source.image_id == reused.image_id
        )
        if planned:
            assert (
                session.get(
                    RuntimeRecipeInstallationRow, str(op.installation_operation_id)
                )
                is None
            )


@pytest.mark.parametrize("defect", ["approval", "attestation", "recipe", "image"])
def test_unverified_cache_refuses_without_launch(
    factory: sessionmaker[Session],
    defect: str,
) -> None:
    app, adapter, broker, _ = setup(factory)
    op = accept(app)
    cached = recipe._ready(factory, generation="omega")
    with factory.begin() as session:
        row = session.get(RuntimeRecipeInstallationRow, str(cached))
        assert row is not None
        if defect == "approval":
            revision = session.get(RuntimeRevisionRow, row.runtime_revision_id)
            assert revision is not None
            revision.approval_state = RevisionApprovalState.NOT_APPROVED
            revision.approved_at = None
        elif defect == "attestation":
            row.attestation_json = "{}"
        elif defect == "recipe":
            row.recipe_digest = "sha256:" + "c" * 64
        else:
            broker.image_present = False
    result = adapter.install(op.id, version=op.version)
    assert result.outcome is CreateOutcome.REFUSED
    assert result.dispatched_at is None and broker.launches == []


def test_unrelated_active_and_interrupted_installations_are_not_adopted(
    factory: sessionmaker[Session],
) -> None:
    app, adapter, broker, installer = setup(factory)
    op = accept(app)
    other = uuid4()
    create_row(factory, "omega", other)
    broker.operation_id, broker.generation = str(other), "omega"
    broker.workers[str(other)] = "running"
    assert installer.observe_create("omega").status == "active"
    refused = adapter.install(op.id, version=op.version)
    assert refused.installation_operation_id is None and refused.dispatched_at is None
    broker.workers[str(other)] = "exited"
    broker.operation_id = broker.generation = None
    assert installer.observe_create("omega").status == "interrupted"
    with factory() as session:
        row = session.get(RuntimeRecipeInstallationRow, str(other))
        assert row and row.state is State.PENDING  # No stateful reconciliation.
    assert broker.launches == []


def test_missing_original_cannot_be_resolved_from_an_unrelated_ready_operation(
    factory: sessionmaker[Session],
) -> None:
    app, adapter, broker, _ = setup(factory)
    op = accept(app)
    broker.parent, broker.mode = op.id, "no_response"
    uncertain = adapter.install(op.id, version=op.version)
    cached = recipe._ready(factory, generation="omega")
    assert cached != uncertain.installation_operation_id
    assert adapter.resolve(op.id, version=uncertain.version) == uncertain
    with pytest.raises(CreateTransitionError, match="resume_not_supported"):
        adapter.resume_refused(op.id, version=uncertain.version)
    assert len(broker.launches) == 1


def test_verified_failure_is_final_and_uncertainty_can_resolve_to_original_failure(
    factory: sessionmaker[Session],
) -> None:
    app, adapter, broker, _ = setup(factory)
    op = accept(app)
    broker.parent, broker.mode = op.id, "lost_response"
    uncertain = adapter.install(op.id, version=op.version)
    child = uncertain.installation_operation_id
    assert child is not None
    with factory.begin() as session:
        RuntimeRecipeInstallationRepository(session).fail(
            child, code="validation_failed", now=datetime.now(UTC)
        )
    broker.workers[str(child)] = "exited"
    broker.operation_id = broker.generation = None
    failed = adapter.resolve(op.id, version=uncertain.version)
    assert failed.outcome is CreateOutcome.FAILED
    assert failed.installation_operation_id == child
    assert adapter.install(op.id, version=failed.version) == failed
    with pytest.raises(CreateTransitionError):
        adapter.resume_refused(op.id, version=failed.version)
    assert len(broker.launches) == 1


def test_concurrent_install_requests_only_one_reaches_broker(
    factory: sessionmaker[Session],
) -> None:
    app, adapter, broker, _ = setup(factory)
    op = app.repository.plan(accept(app).id, version=0)
    broker.parent, broker.mode = op.id, "blocked"
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(adapter.install, op.id, version=op.version)
        try:
            assert broker.entered.wait(5)
            with pytest.raises(CreateTransitionError, match="stale_version"):
                adapter.install(op.id, version=op.version)
            current = app.get(op.id)
            assert (
                current and adapter.install(op.id, version=current.version) == current
            )
        finally:
            broker.release.set()
        assert first.result(timeout=5).checkpoint is CreateCheckpoint.CONFIRMED
    assert len(broker.launches) == 1


@pytest.mark.parametrize("state", ["ready", "interrupted"])
def test_observation_and_parent_reads_are_select_only(
    factory: sessionmaker[Session],
    state: str,
) -> None:
    app, _, broker, installer = setup(factory)
    op = accept(app)
    child = recipe._ready(factory, generation="omega") if state == "ready" else uuid4()
    if state != "ready":
        create_row(factory, "omega", child)
    statements: list[str] = []
    with factory() as session:
        engine = session.get_bind()

    def record(
        _c: object, _cu: object, statement: str, _p: object, _ctx: object, _many: object
    ) -> None:
        statements.append(statement.lstrip().split()[0].upper())

    event.listen(engine, "before_cursor_execute", record)
    for _ in range(3):
        assert app.get(op.id) == op
        assert installer.observe_create("omega", child).status == state
    event.remove(engine, "before_cursor_execute", record)
    assert statements and set(statements) == {"SELECT"}
    assert broker.launches == []


def test_cache_verification_drift_rolls_back_parent_binding(
    factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, adapter, broker, _ = setup(factory)
    op = accept(app)
    recipe._ready(factory, generation="omega")
    checks = 0

    def image_present(_child: str) -> SimpleNamespace:
        nonlocal checks
        checks += 1
        return SimpleNamespace(present=checks == 1)

    monkeypatch.setattr(broker, "reviewed_runtime_image_present", image_present)
    with pytest.raises(CreateTransitionError, match="cached_runtime_not_verified"):
        adapter.install(op.id, version=op.version)
    assert app.get(op.id) == op and broker.launches == []
    assert checks == 2


def test_wrong_receipt_and_other_interrupted_generation_never_launch(
    factory: sessionmaker[Session],
) -> None:
    app, adapter, broker, installer = setup(factory)
    op = accept(app)
    other = uuid4()
    create_row(factory, "nexus", other)
    refused = adapter.install(op.id, version=op.version)
    assert refused.outcome is CreateOutcome.REFUSED and broker.launches == []
    assert installer.observe_create("nexus", other).status == "interrupted"
    # An exact UUID alone does not correlate a receipt for the wrong recipe.
    original = refused.installation_operation_id
    assert original is not None
    with factory.begin() as session:
        RuntimeRecipeInstallationRepository(session).fail(
            other, code="interrupted", now=datetime.now(UTC)
        )
    create_row(factory, "nexus", original)
    with pytest.raises(
        CreateTransitionError, match="undispatched_installation_not_verified"
    ):
        adapter.resume_refused(op.id, version=refused.version)
    assert broker.launches == []


def test_competing_runners_that_both_pass_initial_inspection_claim_once(
    factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, adapter, broker, installer = setup(factory)
    op = app.repository.plan(accept(app).id, version=0)
    broker.parent, broker.mode = op.id, "pending"
    barrier = Barrier(2)
    start = installer.start

    def gated(
        generation: Generation,
        *,
        action: Literal["install", "retry", "reacquire"] = "install",
        operation_id: UUID | None = None,
        before_dispatch: Callable[[UUID], None] | None = None,
    ) -> RuntimeInstallationView:
        barrier.wait(timeout=5)
        # This test double gates the real typed entry point without executing
        # another implementation or bypassing its admission.
        return start(
            generation,
            action=action,
            operation_id=operation_id,
            before_dispatch=before_dispatch,
        )

    monkeypatch.setattr(installer, "start", gated)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(adapter.install, op.id, version=op.version) for _ in range(2)
        ]
        results = [future.result(timeout=10) for future in futures]
    assert all(
        result.id == op.id and result.checkpoint is CreateCheckpoint.DISPATCHED
        for result in results
    )
    assert len(broker.launches) == 1


@pytest.mark.parametrize("generation", ["matrix", "nexus", "omega"])
def test_stable_recipe_pending_child_keeps_exact_identity(
    factory: sessionmaker[Session],
    generation: Generation,
) -> None:
    app, adapter, broker, _ = setup(factory)
    op = accept(app, generation=generation)
    broker.parent, broker.mode = op.id, "pending"
    active = adapter.install(op.id, version=op.version)
    assert active.checkpoint is CreateCheckpoint.DISPATCHED
    assert active.installation_operation_id is not None
    assert broker.launches == [(generation, str(active.installation_operation_id))]
    assert adapter.resolve(op.id, version=active.version) == active


def test_cached_reuse_rejects_unexpected_worker_for_planned_uuid(
    factory: sessionmaker[Session],
) -> None:
    app, adapter, broker, _ = setup(factory)
    op = app.repository.plan(accept(app).id, version=0)
    recipe._ready(factory, generation="omega")
    broker.workers[str(op.installation_operation_id)] = "exited"
    with pytest.raises(CreateTransitionError, match="cached_runtime_not_verified"):
        adapter.install(op.id, version=op.version)
    assert app.get(op.id) == op and broker.launches == []
