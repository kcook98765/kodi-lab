"""Canonical four-version lab bootstrap and selector contract."""

from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.domain import InstanceType
from kodi_lab.runtime.distribution import bootstrap_catalog, load_curated_runtimes
from kodi_lab.runtime.lab import (
    LabWorkflowError,
    _require_canonical,
    assign_packaged_fixture,
    bootstrap_instances,
    canonical_id,
    run_selected,
    select_runtimes,
)
from kodi_lab.storage import metadata
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.storage.repositories import (
    FixtureDefinitionRepository,
    ManagedInstanceRepository,
    RuntimeRevisionRepository,
    TargetRepository,
)


@pytest.fixture
def factory(tmp_path: Path) -> sessionmaker[Session]:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'lab.db'}")
    metadata.create_all(engine)
    return create_session_factory(engine)


def _inspect(image_id: str) -> dict[str, object]:
    return {"Id": image_id, "Os": "linux", "Architecture": "amd64"}


def _catalog(factory: sessionmaker[Session]) -> None:
    assert bootstrap_catalog(factory, _inspect) == 20


def test_selectors_are_closed_stable_and_support_subsets() -> None:
    assert select_runtimes(()) == ("19", "20", "21", "22")
    assert select_runtimes(("22", "19", "19")) == ("19", "22")
    assert len({canonical_id(item) for item in select_runtimes(())}) == 4
    with pytest.raises(LabWorkflowError, match="unknown lab runtime selector"):
        select_runtimes(("23",))


def test_bootstrap_creates_exact_pins_and_repeats(
    factory: sessionmaker[Session],
) -> None:
    _catalog(factory)
    first = bootstrap_instances(factory, acknowledge_preview=True)
    second = bootstrap_instances(factory, acknowledge_preview=True)
    assert all("created" in item for item in first)
    assert all("verified" in item for item in second)
    with factory() as session:
        targets = TargetRepository(session)
        instances = ManagedInstanceRepository(session)
        revisions = RuntimeRevisionRepository(session)
        for selector, runtime in zip(
            select_runtimes(()), load_curated_runtimes(), strict=True
        ):
            target = targets.get(canonical_id(selector))
            instance = instances.get(canonical_id(selector))
            assert target is not None and instance is not None
            assert target.kodi_version == runtime.manifest.kodi_version
            assert instance.runtime_revision_id == runtime.revision.id
            assert instance.instance_type is InstanceType.DISPOSABLE
            assert instance.start_on_host_boot is False
            revision = revisions.get(runtime.revision.id)
            assert revision is not None
            assert (revision.trusted_build_confirmed_at is not None) == (
                selector == "22"
            )
    _require_canonical(factory, load_curated_runtimes())


def test_preview_acknowledgement_is_required_before_target_creation(
    factory: sessionmaker[Session],
) -> None:
    _catalog(factory)
    with pytest.raises(LabWorkflowError, match="Preview requires"):
        bootstrap_instances(factory, acknowledge_preview=False)
    with factory() as session:
        assert TargetRepository(session).list() == []


def test_conflicting_canonical_target_rejects_without_creating_other_pins(
    factory: sessionmaker[Session],
) -> None:
    _catalog(factory)
    runtime = load_curated_runtimes()[2]
    from kodi_lab.runtime.lab import _expected_target

    with factory.begin() as session:
        TargetRepository(session).create(
            replace(_expected_target("21", runtime), name="Conflicting name")
        )
    with pytest.raises(LabWorkflowError, match="canonical lab target conflicts"):
        bootstrap_instances(factory, acknowledge_preview=True)
    with factory() as session:
        assert ManagedInstanceRepository(session).list() == []


def test_conflicting_canonical_instance_is_not_rewritten(
    factory: sessionmaker[Session],
) -> None:
    _catalog(factory)
    bootstrap_instances(factory, acknowledge_preview=True)
    with factory.begin() as session:
        instances = ManagedInstanceRepository(session)
        current = instances.get(canonical_id("20"))
        assert current is not None
        instances.update(replace(current, start_on_host_boot=True))

    with pytest.raises(LabWorkflowError, match="canonical lab instance conflicts"):
        bootstrap_instances(factory, acknowledge_preview=True)
    with factory() as session:
        current = ManagedInstanceRepository(session).get(canonical_id("20"))
        assert current is not None and current.start_on_host_boot is True
        assert len(ManagedInstanceRepository(session).list()) == 4


def test_partial_bootstrap_recovers_missing_instance(
    factory: sessionmaker[Session],
) -> None:
    _catalog(factory)
    from kodi_lab.runtime.lab import _expected_target

    with factory.begin() as session:
        TargetRepository(session).create(
            _expected_target("19", load_curated_runtimes()[0])
        )
    result = bootstrap_instances(factory, acknowledge_preview=True)
    assert len(result) == 4
    with factory() as session:
        assert len(ManagedInstanceRepository(session).list()) == 4


def test_installed_fixture_assignment_uses_packaged_compatibility(
    factory: sessionmaker[Session],
) -> None:
    _catalog(factory)
    bootstrap_instances(factory, acknowledge_preview=True)
    assert (
        assign_packaged_fixture(factory, "19", "small-mixed-v4")
        == "19: fixture small-mixed-v4 assigned"
    )
    with factory() as session:
        assigned = ManagedInstanceRepository(session).get(canonical_id("19"))
        assert assigned is not None
        assert assigned.fixture_definition_id is not None
        fixture = FixtureDefinitionRepository(session).get(
            assigned.fixture_definition_id
        )
        assert fixture is not None and fixture.slug == "small-mixed-v4"
    with pytest.raises(LabWorkflowError, match=r"incompatible with Kodi 20\.5"):
        assign_packaged_fixture(factory, "20", "small-mixed-v4")
    with factory() as session:
        other = ManagedInstanceRepository(session).get(canonical_id("20"))
        assert other is not None and other.fixture_definition_id is None


@pytest.mark.parametrize("failed_selector", ["19", "20", "21", "22"])
@pytest.mark.parametrize("failure", ["readiness failed", "lifecycle failed"])
def test_one_runtime_failure_does_not_skip_other_actions(
    failed_selector: str, failure: str, capsys: pytest.CaptureFixture[str]
) -> None:
    reached: list[str] = []

    def action(selector: str) -> str:
        reached.append(selector)
        if selector == failed_selector:
            raise LabWorkflowError(failure)
        return "ready"

    assert run_selected(select_runtimes(()), action) == 1
    assert reached == list(select_runtimes(()))
    output = capsys.readouterr().out
    assert f"{failed_selector}: failed ({failure})" in output
    assert output.count(": ready") == 3


def test_subset_actions_and_broker_failure_reporting(
    capsys: pytest.CaptureFixture[str],
) -> None:
    reached: list[str] = []

    def action(selector: str) -> str:
        reached.append(selector)
        if selector == "22":
            raise ConnectionError("private broker details must stay out of output")
        return "stopped"

    assert run_selected(select_runtimes(("22", "19")), action) == 1
    assert reached == ["19", "22"]
    output = capsys.readouterr().out
    assert "19: stopped" in output
    assert "22: failed (ConnectionError)" in output
    assert "private broker details" not in output
