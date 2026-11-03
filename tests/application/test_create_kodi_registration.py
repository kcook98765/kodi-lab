"""Canonical bootstrap versus exact read-only reuse, never download or repin."""

from uuid import UUID

import pytest
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application import create_kodi_registration as module
from kodi_lab.domain.create_kodi import (
    CreateCheckpoint,
    CreateOutcome,
    CreateStage,
    CreateTransitionError,
)
from kodi_lab.runtime.install import bootstrap_recipe_instance
from kodi_lab.runtime.recipes import ReviewedRecipe, load_installation_recipe
from kodi_lab.storage.models import (
    ManagedInstanceRow,
    RuntimeRecipeInstallationRow,
    TargetRow,
)
from tests.application.test_create_kodi_installation import factory as factory
from tests.application.test_create_kodi_installation import schema as schema
from tests.create_kodi_provisioning_support import Environment, Interrupted
from tests.runtime.test_recipe_installation import _ready


@pytest.mark.parametrize("fresh", [False, True])
def test_registration_checkpoint_before_helper_and_exact_result(
    factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    fresh: bool,
) -> None:
    env = Environment(factory, fresh=fresh)
    helper = bootstrap_recipe_instance
    calls = []

    def checked(
        sessions: sessionmaker[Session], reviewed: ReviewedRecipe, revision: UUID
    ) -> dict[str, str]:
        current = env.repo.get(env.op.id)
        assert current and current.checkpoint is CreateCheckpoint.DISPATCHED
        assert current.dispatched_at and current.runtime_revision_id == revision
        calls.append(current.child_id)
        return helper(sessions, reviewed, revision)

    monkeypatch.setattr(module, "bootstrap_recipe_instance", checked)
    try:
        op = env.registration.register(env.op.id, version=env.op.version)
        assert (
            op.checkpoint is CreateCheckpoint.CONFIRMED
            and op.stage is CreateStage.REGISTRATION
        )
        assert env.registration.register(op.id, version=op.version) == op
        assert len(calls) == 1 and env.broker.provisions == 0
        assert len(env.broker.launches) == int(fresh)
        with factory() as session:
            instance = session.get(ManagedInstanceRow, str(op.target_id))
            assert instance and instance.runtime_revision_id == str(
                op.runtime_revision_id
            )
    finally:
        env.close()


def test_preexisting_exact_registration_is_confirmed_without_dispatch(
    factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = Environment(factory)
    try:
        assert env.op.runtime_revision_id is not None
        bootstrap_recipe_instance(
            factory, load_installation_recipe("omega"), env.op.runtime_revision_id
        )
        with factory.begin() as session:
            target = session.get(TargetRow, str(env.op.target_id))
            assert target
            target.name = "Living room"

        def forbidden(*_args: object) -> None:
            pytest.fail("read-only reuse must not invoke bootstrap")

        monkeypatch.setattr(module, "bootstrap_recipe_instance", forbidden)
        op = env.registration.register(env.op.id, version=env.op.version)
        assert op.stage is CreateStage.PROVISIONING and op.dispatched_at is None
        assert op.provision_operation_id is None and env.broker.launches == []
        with factory() as session:
            target = session.get(TargetRow, str(op.target_id))
            assert target and target.name == "Living room"
    finally:
        env.close()


@pytest.mark.parametrize("boundary", ["before", "partial", "after"])
def test_registration_interruption_fences_mutation_and_allows_only_exact_confirmation(
    factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    env = Environment(factory)
    helper = bootstrap_recipe_instance
    calls = 0

    def interrupted(
        sessions: sessionmaker[Session], reviewed: ReviewedRecipe, revision: UUID
    ) -> dict[str, str]:
        nonlocal calls
        calls += 1
        if boundary == "after":
            helper(sessions, reviewed, revision)
        elif boundary == "partial":
            # The existing helper commits Target before creating ManagedInstance.
            monkeypatch.setattr(
                "kodi_lab.runtime.install.ManagedInstanceService.create",
                lambda *_a, **_kw: (_ for _ in ()).throw(Interrupted()),
            )
            helper(sessions, reviewed, revision)
        raise Interrupted()

    monkeypatch.setattr(module, "bootstrap_recipe_instance", interrupted)
    try:
        with pytest.raises(Interrupted):
            env.registration.register(env.op.id, version=env.op.version)
        op = env.repo.get(env.op.id)
        assert op and op.outcome is CreateOutcome.UNCERTAIN
        assert env.registration.register(op.id, version=op.version) == op
        if boundary == "after":
            confirmed = env.registration.confirm(op.id, version=op.version)
            assert confirmed.checkpoint is CreateCheckpoint.CONFIRMED
            assert confirmed.dispatched_at == op.dispatched_at
        else:
            with pytest.raises(CreateTransitionError, match="not_verified"):
                env.registration.confirm(op.id, version=op.version)
            assert env.repo.get(op.id) == op
        assert calls == 1 and env.broker.provisions == 0
    finally:
        env.close()


@pytest.mark.parametrize("conflict", ["pin", "image", "recipe"])
def test_changed_binding_never_repins_or_downloads(
    factory: sessionmaker[Session],
    conflict: str,
) -> None:
    env = Environment(factory)
    try:
        assert env.op.runtime_revision_id is not None
        if conflict == "pin":
            bootstrap_recipe_instance(
                factory, load_installation_recipe("omega"), env.op.runtime_revision_id
            )
            other = _ready(factory, generation="omega")
            with factory.begin() as session:
                source = session.get(RuntimeRecipeInstallationRow, str(other))
                instance = session.get(ManagedInstanceRow, str(env.op.target_id))
                assert source and source.runtime_revision_id and instance
                instance.runtime_revision_id = source.runtime_revision_id
        elif conflict == "image":
            env.broker.image_present = False
        else:
            with factory.begin() as session:
                source = session.get(
                    RuntimeRecipeInstallationRow, str(env.op.installation_operation_id)
                )
                assert source
                source.recipe_digest = "sha256:" + "c" * 64
        with pytest.raises(CreateTransitionError):
            env.registration.register(env.op.id, version=env.op.version)
        assert env.repo.get(env.op.id) == env.op
        assert env.broker.launches == [] and env.broker.provisions == 0
    finally:
        env.close()
