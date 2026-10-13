"""Existing durable worker state machine dispatches RC1 and fails closed.

Provider/validator doubles here exercise transactions, not live acquisition.
"""

from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.config import Settings
from kodi_lab.runtime.install import (
    RecipeInstallationError,
    _create_operation,
    install_reviewed_runtime,
)
from kodi_lab.runtime.preview import admission_evidence
from kodi_lab.runtime.recipes import load_installation_recipe
from kodi_lab.runtime.validation import RecipeValidationError
from kodi_lab.storage.repositories import (
    ManagedInstanceRepository,
    RuntimeRecipeInstallationRepository,
)
from tests.runtime.test_ostree_installation import acknowledgement, assembly, validation
from tests.runtime.test_recipe_installation import factory as factory


def admitted(factory: sessionmaker[Session]) -> UUID:
    operation = uuid4()
    _create_operation(factory, load_installation_recipe("piers"), operation)
    with factory.begin() as session:
        row = RuntimeRecipeInstallationRepository(session).get(operation)
        assert row
        row.attestation_json = admission_evidence(acknowledgement(), operation)
    return operation


def dependencies(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch, operation: UUID
) -> tuple[Any, list[str]]:
    built = assembly(operation)
    phases: list[str] = []
    client = SimpleNamespace(
        close=lambda: None,
        api=SimpleNamespace(
            inspect_image=lambda image: {
                "Config": {"Labels": {"io.kodi-lab.operation-id": operation.hex}}
            }
        ),
        images=SimpleNamespace(remove=lambda *args, **kwargs: None),
    )
    monkeypatch.setattr("docker.from_env", lambda **kwargs: client)
    monkeypatch.setattr(
        "kodi_lab.runtime.install._installation_lock", lambda path: nullcontext()
    )
    monkeypatch.setattr(
        "kodi_lab.runtime.install._database_call",
        lambda settings, action: action(factory),
    )
    monkeypatch.setattr(
        "kodi_lab.runtime.install.preflight_temporary_networks", lambda *args: None
    )

    def build(op: UUID, settings: Settings, on_acquired: Any) -> Any:
        on_acquired()
        return built.assembly, built.acquisition

    monkeypatch.setattr("kodi_lab.runtime.ostree_worker.assemble_in_worker", build)
    monkeypatch.setattr(
        "kodi_lab.runtime.ostree_validation.validate_ostree_image",
        lambda *args, **kwargs: validation(built.image_id),
    )
    return client, phases


def test_provider_uses_existing_phases_and_seals_ready(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    operation = admitted(factory)
    _client, phases = dependencies(factory, monkeypatch, operation)
    outcome = install_reviewed_runtime(
        "piers", settings=Settings(), operation_id=operation, report=phases.append
    )
    assert outcome.operation_id == operation
    assert phases == ["acquiring", "assembling", "validating", "ready"]
    with factory() as session:
        row = RuntimeRecipeInstallationRepository(session).get(operation)
        assert row
        assert row.state.value == "ready" and row.runtime_revision_id == str(
            outcome.runtime_revision_id
        )
        assert "hmac-sha256:" in (row.attestation_json or "")
        assert ManagedInstanceRepository(session).list() == []


@pytest.mark.parametrize(
    "failure", ["acquisition", "assembly", "validation", "cancellation"]
)
def test_failure_never_creates_ready_and_retry_reacquires(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    operation = admitted(factory)
    _client, phases = dependencies(factory, monkeypatch, operation)

    def fail(*args: Any, **kwargs: Any) -> Any:
        if failure == "cancellation":
            raise KeyboardInterrupt
        if failure == "validation":
            raise RecipeValidationError("failed")
        if failure == "assembly":
            args[2]()
        raise RuntimeError("failed")

    if failure == "validation":
        monkeypatch.setattr(
            "kodi_lab.runtime.ostree_validation.validate_ostree_image", fail
        )
    else:
        monkeypatch.setattr("kodi_lab.runtime.ostree_worker.assemble_in_worker", fail)
    with pytest.raises(RecipeInstallationError):
        install_reviewed_runtime(
            "piers", settings=Settings(), operation_id=operation, report=phases.append
        )
    with factory() as session:
        row = RuntimeRecipeInstallationRepository(session).get(operation)
        assert row
        assert row.state.value == "failed" and row.runtime_revision_id is None
    retry = admitted(factory)
    _client, phases = dependencies(factory, monkeypatch, retry)
    assert not install_reviewed_runtime(
        "piers", settings=Settings(), operation_id=retry, report=phases.append
    ).reused


def test_preview_without_broker_admission_never_acquires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(RecipeInstallationError, match="admission"):
        install_reviewed_runtime("piers", settings=Settings())


def test_validation_and_cleanup_failure_remains_failed(
    factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operation = admitted(factory)
    client, phases = dependencies(factory, monkeypatch, operation)

    def fail_validation(*args: Any, **kwargs: Any) -> Any:
        raise RecipeValidationError("invalid runtime")

    def fail_cleanup(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("image removal failed")

    client.images.remove = fail_cleanup
    monkeypatch.setattr(
        "kodi_lab.runtime.ostree_validation.validate_ostree_image", fail_validation
    )
    with pytest.raises(RecipeInstallationError):
        install_reviewed_runtime(
            "piers", settings=Settings(), operation_id=operation, report=phases.append
        )
    with factory() as session:
        row = RuntimeRecipeInstallationRepository(session).get(operation)
        assert row is not None and row.state.value == "failed"
        assert row.failure_code == "installation_cleanup_failed"
