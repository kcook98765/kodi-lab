"""Isolated Create ledger and child doubles. Never imports a live executor."""

from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID, uuid4

from kodi_lab.application.create_kodi import CreateKodiService
from kodi_lab.domain.create_kodi import (
    CreateCheckpoint,
    CreateChildResult,
    CreateDispatchClaim,
    CreateOperation,
    CreateOutcome,
    CreateStage,
)
from kodi_lab.runtime.recipes import load_installation_recipe
from kodi_lab.storage import metadata
from kodi_lab.storage.create_kodi import CreateKodiRepository
from kodi_lab.storage.engine import create_database_engine, create_session_factory

IMAGE = "sha256:" + "a" * 64
CONTAINER = "b" * 64
RUNTIME = UUID("ffdcc9bf-cbc5-4dc9-8b0b-5593ea159cdb")


def database(path: Path) -> str:
    url = f"sqlite:///{path}"
    engine = create_database_engine(url)
    metadata.tables["create_kodi_operations"].create(engine)
    engine.dispose()
    return url


def service(url: str) -> CreateKodiService:
    engine = create_database_engine(url)
    # Use short-lived independent connections, including in spawned processes.
    return CreateKodiService(CreateKodiRepository(create_session_factory(engine)))


def accept(
    app: CreateKodiService,
    request: UUID | None = None,
    generation: str = "omega",
) -> CreateOperation:
    return app.accept(
        request_id=request or uuid4(),
        generation=generation,
        recipe_digest=load_installation_recipe(generation).digest,
        owner_approved=True,
    ).operation


@dataclass
class FakeChildren:
    receipts: dict[UUID, CreateChildResult] = field(default_factory=dict)
    calls: list[UUID] = field(default_factory=list)
    running: bool = False

    def result(self, op: CreateOperation) -> CreateChildResult:
        assert op.child_id is not None
        container = op.stage in {
            CreateStage.PROVISIONING,
            CreateStage.STARTUP,
            CreateStage.READINESS,
        }
        return CreateChildResult(
            stage=op.stage,
            child_id=op.child_id,
            target_id=op.target_id,
            recipe_digest=op.recipe_digest,
            outcome=CreateOutcome.SUCCEEDED,
            runtime_revision_id=RUNTIME,
            image_id=IMAGE,
            container_id=CONTAINER if container else None,
            runtime_state_revision=(4 if op.stage is CreateStage.PROVISIONING else 5)
            if container
            else None,
        )

    def execute(self, claim: CreateDispatchClaim) -> CreateChildResult:
        op = claim.operation
        assert op.checkpoint is CreateCheckpoint.DISPATCHED
        assert op.child_id is not None and op.child_id not in self.calls
        self.calls.append(op.child_id)
        result = self.result(op)
        self.receipts[op.child_id] = result
        if op.stage is CreateStage.STARTUP:
            self.running = True
        return result


def continue_one(
    app: CreateKodiService,
    operation_id: UUID,
    children: FakeChildren,
) -> CreateOperation:
    """Test-only explicit resume driver, not a production runner."""
    repo = app.repository
    op = app.get(operation_id)
    assert op is not None
    if op.checkpoint is CreateCheckpoint.ACCEPTED:
        return repo.plan(op.id, version=op.version)
    if op.checkpoint is CreateCheckpoint.PLANNED:
        claim = repo.claim_dispatch(op.id, version=op.version)
        result = children.execute(claim)
        return repo.record_result(
            claim.operation.id, version=claim.operation.version, result=result
        )
    if op.checkpoint is CreateCheckpoint.DISPATCHED:
        assert op.child_id is not None
        receipt = children.receipts.get(op.child_id)
        if receipt is None:
            return repo.note_uncertain(
                op.id, version=op.version, reason="child_receipt_missing"
            )
        return repo.record_result(op.id, version=op.version, result=receipt)
    if op.checkpoint is CreateCheckpoint.CONFIRMED:
        return repo.advance(op.id, version=op.version)
    return op


def reach(
    app: CreateKodiService,
    op: CreateOperation,
    children: FakeChildren,
    stage: CreateStage,
) -> CreateOperation:
    for _ in range(20):
        if op.stage is stage and op.checkpoint is CreateCheckpoint.ACCEPTED:
            return op
        op = continue_one(app, op.id, children)
    raise AssertionError("failed to reach fixed stage")
