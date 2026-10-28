"""Canonical acquisition cannot be shadowed by a sealed development variant."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.runtime_installations import RuntimeInstallationService
from kodi_lab.domain import RevisionApprovalState
from kodi_lab.runtime.install import bootstrap_recipe_instance
from kodi_lab.runtime.recipes import load_reviewed_recipe
from kodi_lab.storage.models import RuntimeRecipeInstallationRow, RuntimeRevisionRow
from kodi_lab.storage.repositories import RuntimeRecipeInstallationRepository
from tests.application.test_development_runtime_selection import installed
from tests.application.test_runtime_installations import FakeBroker
from tests.runtime import test_recipe_installation as recipe_fixture
from tests.runtime.test_recipe_installation import factory as factory


def test_canonical_and_r4_each_use_their_own_reviewed_identity(
    factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canonical = load_reviewed_recipe("omega")
    canonical_operation = recipe_fixture._ready(factory, generation="omega")
    with factory() as db:
        row = RuntimeRecipeInstallationRepository(db).get(canonical_operation)
        assert row is not None and row.runtime_revision_id is not None
        canonical_revision = UUID(row.runtime_revision_id)
    bootstrap_recipe_instance(factory, canonical, canonical_revision)
    _sessions, development_target = installed(factory, tmp_path, monkeypatch)
    service = RuntimeInstallationService(
        factory, FakeBroker(), data_root=tmp_path / "state"
    )
    normal = service.list()[2]
    assert normal.operation_id == str(canonical_operation)
    assert normal.status == "ready" and normal.action == "open"
    assert normal.managed_instance_name == "Omega 21 Lab"
    records = service.records()
    assert len(records) == 2
    development = next(
        item for item in records if item.operation_id != str(canonical_operation)
    )
    assert development.status == "ready"
    assert development.managed_instance_name == "Omega 21 Lab · experimental noVNC"
    assert development.managed_instance_url == f"/instances/{development_target}"
    assert development.action == "none"
    assert development.technical_details is not None
    assert development.technical_details["recipe_revision"] == "4"
    with factory() as db:
        row = db.get(RuntimeRecipeInstallationRow, development.operation_id)
        assert row is not None
        assert not service._eligible_ready(row)  # Ordinary new-pin policy is unchanged.
    assert service.get(UUID(development.operation_id or "")).status == "ready"  # type: ignore[union-attr]

    # A newer failure of the actual canonical recipe is never hidden by success.
    with factory.begin() as db:
        operation = uuid4()
        repo = RuntimeRecipeInstallationRepository(db)
        repo.create(
            operation_id=operation,
            recipe_id=canonical.recipe.recipe_id,
            recipe_revision=canonical.recipe.recipe_revision,
            recipe_digest=canonical.digest,
            generation="omega",
            now=datetime.now(UTC) + timedelta(seconds=1),
        )
        repo.fail(operation, code="validation_failed", now=datetime.now(UTC))
    assert service.list()[2].status == "failed"
    assert service.list()[2].operation_id == str(operation)


@pytest.mark.parametrize("defect", ["approval", "attestation", "recipe"])
def test_development_record_never_hides_a_real_rejection(
    factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    defect: str,
) -> None:
    installed(factory, tmp_path, monkeypatch)
    with factory.begin() as db:
        row = db.scalar(select(RuntimeRecipeInstallationRow))
        assert row is not None
        operation = UUID(row.id)
        if defect == "approval":
            revision = db.get(RuntimeRevisionRow, row.runtime_revision_id)
            assert revision is not None
            revision.approval_state = RevisionApprovalState.NOT_APPROVED
            revision.approved_at = None
        elif defect == "attestation":
            row.attestation_json = "{}"
        else:
            row.recipe_digest = "sha256:" + "f" * 64
    service = RuntimeInstallationService(
        factory, FakeBroker(), data_root=tmp_path / "state"
    )
    record = service.get(operation)
    assert record is not None
    assert record.status == "unavailable" and record.action == "none"
    assert record.operation_id == str(operation)
    assert service.records()[0].status == "unavailable"
    assert service.list()[2].status == "not_installed"


def test_unreviewed_variant_never_links_to_canonical_when_broker_is_unavailable(
    factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canonical = load_reviewed_recipe("omega")
    canonical_operation = recipe_fixture._ready(factory, generation="omega")
    with factory() as db:
        row = RuntimeRecipeInstallationRepository(db).get(canonical_operation)
        assert row is not None and row.runtime_revision_id is not None
        revision = UUID(row.runtime_revision_id)
    bootstrap_recipe_instance(factory, canonical, revision)
    installed(factory, tmp_path, monkeypatch)
    with factory.begin() as db:
        row = db.scalar(
            select(RuntimeRecipeInstallationRow).where(
                RuntimeRecipeInstallationRow.recipe_revision == 4
            )
        )
        assert row is not None
        row.recipe_digest = "sha256:" + "f" * 64
        operation = row.id
    broker = FakeBroker()
    monkeypatch.setattr(
        broker, "capabilities", lambda: (_ for _ in ()).throw(ConnectionError())
    )
    records = RuntimeInstallationService(factory, broker).records()
    rejected = next(record for record in records if record.operation_id == operation)
    assert rejected.managed_instance_url is None
    assert rejected.action == "none" and rejected.status == "unavailable"
    assert rejected.unavailable_code == "recorded_recipe_unavailable"
    assert rejected.technical_details is not None
    assert rejected.technical_details["recipe_revision"] == "4"


def test_history_cursor_keeps_an_old_failure_discoverable_after_256_records(
    factory: sessionmaker[Session],
) -> None:
    recipe = load_reviewed_recipe("omega")
    oldest = uuid4()
    # Equal timestamps deliberately exercise the secondary immutable-ID cursor.
    operations = sorted([oldest, *(uuid4() for _ in range(256))], key=str)
    oldest = operations[0]
    now = datetime.now(UTC)
    with factory.begin() as db:
        repo = RuntimeRecipeInstallationRepository(db)
        for operation in operations:
            repo.create(
                operation_id=operation,
                recipe_id=recipe.recipe.recipe_id,
                recipe_revision=recipe.recipe.recipe_revision,
                recipe_digest=recipe.digest,
                generation="omega",
                now=now,
            )
            repo.fail(operation, code="validation_failed", now=datetime.now(UTC))
    service = RuntimeInstallationService(factory, FakeBroker())
    first = service.records()
    assert len(first) == 256 and str(oldest) not in {x.operation_id for x in first}
    assert first[-1].operation_id is not None
    second = service.records(before=UUID(first[-1].operation_id))
    assert len(second) == 1 and second[0].operation_id == str(oldest)
    assert second[0].status == "failed"
    assert service.records(before=uuid4()) == []
