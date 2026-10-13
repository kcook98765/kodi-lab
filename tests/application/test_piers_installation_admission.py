"""State-owner Preview admission stays separate from production Web routes."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.runtime_installations import (
    REQUIRED_OPERATIONS,
    RuntimeInstallationService,
    RuntimeInstallationUnavailable,
)
from kodi_lab.broker.operations import ReviewedInstallationLaunchResult
from kodi_lab.runtime.preview import recorded_acknowledgement
from kodi_lab.storage.models import RuntimeRecipeInstallationRow
from kodi_lab.storage.repositories import RuntimeRecipeInstallationRepository
from tests.runtime.test_ostree_installation import acknowledgement
from tests.runtime.test_piers_installation_pipeline import admitted
from tests.runtime.test_recipe_installation import factory as factory


def broker() -> Any:
    def launch(**kwargs: Any) -> ReviewedInstallationLaunchResult:
        return ReviewedInstallationLaunchResult(
            operation_id=kwargs["operation_id"],
            generation="piers",
            newly_accepted=True,
        )

    return SimpleNamespace(
        capabilities=lambda: SimpleNamespace(operations=REQUIRED_OPERATIONS),
        active_reviewed_runtime_installation=lambda: SimpleNamespace(
            operation_id=None, lock_held=False
        ),
        observe_reviewed_runtime_installation=lambda operation: SimpleNamespace(
            state="missing", lock_held=False
        ),
        launch_reviewed_runtime_installation=launch,
    )


def test_home_mode_never_admits_preview(factory: sessionmaker[Session]) -> None:
    with pytest.raises(RuntimeInstallationUnavailable):
        RuntimeInstallationService(factory, broker()).start_preview(
            development_mode=False, acknowledgement=acknowledgement()
        )
    with factory() as session:
        assert RuntimeRecipeInstallationRepository(session).active("piers") is None


def test_owner_admission_precedes_broker_and_can_retry_pending_handoff(
    factory: sessionmaker[Session],
) -> None:
    from uuid import UUID

    operation = admitted(factory)
    result = RuntimeInstallationService(factory, broker()).start_preview(
        development_mode=True, acknowledgement=acknowledgement()
    )
    assert result.operation_id == str(operation)
    assert (
        recorded_acknowledgement(factory, UUID(result.operation_id))
        == acknowledgement()
    )


def test_rejected_worker_marks_owner_operation_failed(
    factory: sessionmaker[Session],
) -> None:
    backend = broker()

    def reject(**kwargs: Any) -> Any:
        raise RuntimeError("old broker")

    backend.launch_reviewed_runtime_installation = reject
    with pytest.raises(RuntimeInstallationUnavailable):
        RuntimeInstallationService(factory, backend).start_preview(
            development_mode=True, acknowledgement=acknowledgement()
        )
    with factory() as session:
        row = session.scalar(
            select(RuntimeRecipeInstallationRow).where(
                RuntimeRecipeInstallationRow.generation == "piers"
            )
        )
        assert row is not None and row.state.value == "failed"


def test_lost_response_does_not_fail_running_exact_worker(
    factory: sessionmaker[Session],
) -> None:
    backend = broker()

    def ambiguous(**kwargs: Any) -> Any:
        raise RuntimeError("response lost")

    backend.launch_reviewed_runtime_installation = ambiguous
    backend.observe_reviewed_runtime_installation = lambda operation: SimpleNamespace(
        state="running", lock_held=True
    )
    result = RuntimeInstallationService(factory, backend).start_preview(
        development_mode=True, acknowledgement=acknowledgement()
    )
    assert result.newly_accepted is False
