"""Durable intent and fixed transitions without any runtime or network service."""

from dataclasses import replace
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, text

from kodi_lab.domain.create_kodi import (
    CreateAcceptance,
    CreateConflict,
    CreateOutcome,
    CreateStage,
    CreateTransitionError,
)
from kodi_lab.domain.create_kodi import (
    CreateCheckpoint as Checkpoint,
)
from kodi_lab.runtime.recipes import load_installation_recipe, recipe_target_id
from tests.create_kodi_support import (
    FakeChildren,
    accept,
    continue_one,
    database,
    reach,
    service,
)


def test_explicit_approval_replay_conflict_and_canonical_lookup(tmp_path: Path) -> None:
    app = service(database(tmp_path / "db"))
    reviewed = load_installation_recipe("omega")
    request = uuid4()

    def submit(
        request_id: UUID = request,
        generation: str = "omega",
        recipe_digest: str = reviewed.digest,
        owner_approved: bool = True,
    ) -> CreateAcceptance:
        return app.accept(
            request_id=request_id,
            generation=generation,
            recipe_digest=recipe_digest,
            owner_approved=owner_approved,
        )

    with pytest.raises(ValueError, match="approval required"):
        submit(owner_approved=False)
    with pytest.raises(ValueError, match="prerelease approval"):
        submit(generation="piers")
    assert app.for_target(recipe_target_id(reviewed)) is None
    first, second = submit(), submit()
    assert not first.replayed and second.replayed
    assert first.operation == second.operation
    assert app.for_request(request) == first.operation
    assert app.for_request(uuid4()) is None
    assert app.for_target(first.operation.target_id) == first.operation
    with pytest.raises(CreateConflict, match="request_inputs_conflict") as conflict:
        submit(recipe_digest="sha256:" + "c" * 64)
    assert conflict.value.operation_id == first.operation.id
    with pytest.raises(CreateConflict, match="request_inputs_conflict"):
        submit(generation="nexus")
    with pytest.raises(CreateConflict, match="canonical_create_exists") as conflict:
        submit(request_id=uuid4())
    assert conflict.value.operation_id == first.operation.id
    assert app.get(uuid4()) is None


def test_new_request_rejects_changed_recipe_but_replay_survives_upgrade(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = service(database(tmp_path / "db"))
    with pytest.raises(ValueError, match="approved recipe"):
        app.accept(
            request_id=uuid4(),
            generation="omega",
            owner_approved=True,
            recipe_digest="sha256:" + "0" * 64,
        )
    op = accept(app)

    def unavailable(_generation: str) -> None:
        raise AssertionError("replay must use accepted immutable recipe")

    monkeypatch.setattr(
        "kodi_lab.application.create_kodi.load_installation_recipe", unavailable
    )
    replay = app.accept(
        request_id=op.request_id,
        generation=op.generation,
        recipe_digest=op.recipe_digest,
        owner_approved=True,
    )
    assert replay.replayed and replay.operation == op


@pytest.mark.parametrize("stage", list(CreateStage))
@pytest.mark.parametrize(
    "interruption",
    [
        "accepted",
        "planned",
        "checkpoint_only",
        "response_lost",
        "receipt_lost",
        "confirmed",
        "advance",
    ],
)
def test_interruptions_reopen_and_never_repeat_dispatch(
    tmp_path: Path,
    stage: CreateStage,
    interruption: str,
) -> None:
    url = database(tmp_path / "db")
    app, children = service(url), FakeChildren()
    op = reach(app, accept(app), children, stage)
    repo = app.repository
    if interruption != "accepted":
        op = repo.plan(op.id, version=op.version)
    if interruption not in {"accepted", "planned"}:
        claim = repo.claim_dispatch(op.id, version=op.version)
        op = claim.operation
    if interruption in {"response_lost", "receipt_lost", "confirmed", "advance"}:
        result = children.execute(claim)
        if interruption == "receipt_lost":
            assert op.child_id is not None
            del children.receipts[op.child_id]
        elif interruption != "response_lost":
            op = repo.record_result(op.id, version=op.version, result=result)
    if interruption == "advance":
        # Simulate a crash during the next-stage transaction, before commit.
        def crash(_session: object) -> None:
            raise RuntimeError("simulated crash")

        event.listen(repo._sessions, "before_commit", crash)
        with pytest.raises(RuntimeError, match="simulated crash"):
            repo.advance(op.id, version=op.version)
        event.remove(repo._sessions, "before_commit", crash)
    reopened = service(url)
    assert reopened.get(op.id) == op
    original_child = op.child_id
    if interruption in {"checkpoint_only", "receipt_lost"}:
        calls = list(children.calls)
        attention = continue_one(reopened, op.id, children)
        assert attention.checkpoint is Checkpoint.NEEDS_ATTENTION
        assert attention.outcome is CreateOutcome.UNCERTAIN
        assert attention.child_id == original_child
        assert attention.dispatched_at == op.dispatched_at is not None
        assert children.calls == calls
        for _ in range(3):
            assert continue_one(reopened, op.id, children) == attention
        # Late evidence must not silently resolve an already uncertain parent.
        with pytest.raises(CreateTransitionError, match="invalid_checkpoint"):
            reopened.repository.record_result(
                op.id, version=attention.version, result=children.result(op)
            )
        assert reopened.get(op.id) == attention
        return
    for _ in range(20):
        op = continue_one(reopened, op.id, children)
        if op.checkpoint is Checkpoint.COMPLETE:
            break
    assert op.checkpoint is Checkpoint.COMPLETE
    assert len(children.calls) == len(set(children.calls)) == 5
    assert children.running
    children.running = False  # A later manual Stop cannot undo historical completion.
    assert continue_one(service(url), op.id, children) == op
    assert not children.running and len(children.calls) == 5
    assert accept(service(url), op.request_id) == op


def test_before_acceptance_and_failed_commit_have_no_accepted_operation(
    tmp_path: Path,
) -> None:
    url = database(tmp_path / "db")
    app = service(url)
    request = uuid4()
    reviewed = load_installation_recipe("omega")
    assert service(url).for_target(recipe_target_id(reviewed)) is None

    def crash(_session: object) -> None:
        raise RuntimeError("before acceptance commit")

    event.listen(app.repository._sessions, "before_commit", crash)
    with pytest.raises(RuntimeError, match="before acceptance commit"):
        accept(app, request)
    event.remove(app.repository._sessions, "before_commit", crash)
    assert service(url).for_target(recipe_target_id(reviewed)) is None
    op = accept(service(url), request)
    assert op.version == 0 and op.installation_operation_id is None


def test_status_reads_only_select_even_when_dispatched_or_uncertain(
    tmp_path: Path,
) -> None:
    app = service(database(tmp_path / "db"))
    op = accept(app)
    op = app.repository.plan(op.id, version=op.version)
    op = app.repository.claim_dispatch(op.id, version=op.version).operation
    statements: list[str] = []
    with app.repository._sessions() as session:
        engine = session.get_bind()

    def record(
        _c: object, _cu: object, statement: str, _p: object, _ctx: object, _many: object
    ) -> None:
        statements.append(statement.lstrip().split()[0].upper())

    event.listen(engine, "before_cursor_execute", record)
    for _ in range(4):
        assert app.get(op.id) == op
        assert app.for_target(op.target_id) == op
    event.remove(engine, "before_cursor_execute", record)
    assert statements and set(statements) == {"SELECT"}
    assert app.repository.get(op.id) == op
    # The isolated database has no child/lifecycle tables at all.
    with app.repository._sessions() as session:
        tables = session.scalars(
            text("SELECT name FROM sqlite_master WHERE type='table'")
        ).all()
    assert tables == ["create_kodi_operations"]


@pytest.mark.parametrize("boundary", ["planning", "dispatch", "confirmation"])
def test_uncommitted_commands_leave_original_boundary(
    tmp_path: Path,
    boundary: str,
) -> None:
    app = service(database(tmp_path / "db"))
    op = accept(app)
    repo = app.repository
    children = FakeChildren()
    if boundary in {"dispatch", "confirmation"}:
        op = repo.plan(op.id, version=op.version)
    if boundary == "confirmation":
        claim = repo.claim_dispatch(op.id, version=op.version)
        op = claim.operation
        children.execute(claim)

    def crash(_session: object) -> None:
        raise RuntimeError("uncommitted command")

    event.listen(repo._sessions, "before_commit", crash)
    with pytest.raises(RuntimeError, match="uncommitted command"):
        if boundary == "planning":
            repo.plan(op.id, version=op.version)
        elif boundary == "dispatch":
            repo.claim_dispatch(op.id, version=op.version)
        else:
            repo.record_result(op.id, version=op.version, result=children.result(op))
    event.remove(repo._sessions, "before_commit", crash)
    assert app.get(op.id) == op
    # A rolled-back dispatch is provably undispatched. An external effect whose
    # parent confirmation rolled back is recovered through its exact receipt.
    for _ in range(20):
        op = continue_one(app, op.id, children)
        if op.checkpoint is Checkpoint.COMPLETE:
            break
    assert op.checkpoint is Checkpoint.COMPLETE and len(children.calls) == 5


@pytest.mark.parametrize(
    "outcome", [CreateOutcome.FAILED, CreateOutcome.REFUSED, CreateOutcome.UNCERTAIN]
)
def test_non_success_and_refusal_are_terminal(
    tmp_path: Path, outcome: CreateOutcome
) -> None:
    app = service(database(tmp_path / "db"))
    op = accept(app)
    repo = app.repository
    op = repo.plan(op.id, version=op.version)
    op = repo.claim_dispatch(op.id, version=op.version).operation
    result = replace(
        FakeChildren().result(op), outcome=outcome, reason="fake_child_stopped"
    )
    terminal = repo.record_result(op.id, version=op.version, result=result)
    assert terminal.outcome is outcome and terminal.runtime_revision_id is None
    assert accept(app, op.request_id) == terminal
    for command in (repo.plan, repo.claim_dispatch, repo.advance):
        with pytest.raises(CreateTransitionError, match="invalid_checkpoint"):
            command(op.id, version=terminal.version)
    op2 = accept(app, generation="nexus")
    refused = repo.refuse(op2.id, version=op2.version, reason="unsafe_selection")
    assert refused.outcome is CreateOutcome.REFUSED and refused.child_id is None


@pytest.mark.parametrize("stage", list(CreateStage))
def test_stale_and_mismatched_results_rollback(
    tmp_path: Path, stage: CreateStage
) -> None:
    app = service(database(tmp_path / "db"))
    children = FakeChildren()
    op = reach(app, accept(app), children, stage)
    repo = app.repository
    with pytest.raises(CreateTransitionError, match="invalid_checkpoint"):
        repo.advance(op.id, version=op.version)
    op = repo.plan(op.id, version=op.version)
    with pytest.raises(CreateTransitionError, match="stale_version"):
        repo.claim_dispatch(op.id, version=op.version - 1)
    op = repo.claim_dispatch(op.id, version=op.version).operation
    with pytest.raises(CreateTransitionError, match="invalid_checkpoint"):
        repo.claim_dispatch(op.id, version=op.version)
    valid = children.result(op)
    invalid = [
        replace(valid, stage=cast(CreateStage, str(valid.stage))),
        replace(valid, outcome=cast(CreateOutcome, str(valid.outcome))),
        replace(valid, runtime_revision_id=cast(UUID, str(valid.runtime_revision_id))),
        replace(valid, child_id=uuid4()),
        replace(valid, target_id=uuid4()),
        replace(
            valid,
            stage=CreateStage.STARTUP
            if stage is not CreateStage.STARTUP
            else CreateStage.READINESS,
        ),
        replace(valid, recipe_digest="sha256:" + "c" * 64),
        replace(valid, runtime_revision_id=None),
        replace(valid, image_id="wrong"),
        replace(valid, reason="not_success"),
    ]
    if stage is not CreateStage.INSTALLATION:
        invalid.extend(
            [
                replace(valid, runtime_revision_id=uuid4()),
                replace(valid, image_id="sha256:" + "d" * 64),
            ]
        )
    if stage in {CreateStage.STARTUP, CreateStage.READINESS}:
        invalid.extend(
            [
                replace(valid, container_id="c" * 64),
                replace(valid, runtime_state_revision=0),
            ]
        )
    if stage is CreateStage.PROVISIONING:
        invalid.extend(
            [
                replace(valid, container_id=None),
                replace(valid, runtime_state_revision=None),
            ]
        )
    for result in invalid:
        with pytest.raises(CreateTransitionError):
            repo.record_result(op.id, version=op.version, result=result)
        assert app.get(op.id) == op
    confirmed = repo.record_result(op.id, version=op.version, result=valid)
    assert confirmed.checkpoint is Checkpoint.CONFIRMED
