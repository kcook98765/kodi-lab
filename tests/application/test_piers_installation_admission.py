"""State-owner Preview admission stays separate from production Web routes."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.runtime_installations import (
    REQUIRED_OPERATIONS,
    PreviewActionApproval,
    RuntimeInstallationActionConflict,
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


def test_missing_explicit_action_approval_never_admits_preview(
    factory: sessionmaker[Session],
) -> None:
    with pytest.raises(RuntimeInstallationActionConflict):
        RuntimeInstallationService(factory, broker()).start_preview(
            approval=PreviewActionApproval.model_construct(
                action="install", approved=False, acknowledgement=acknowledgement()
            )
        )
    with factory() as session:
        assert RuntimeRecipeInstallationRepository(session).active("piers") is None


def test_pending_handoff_requires_recovery_instead_of_new_install(
    factory: sessionmaker[Session],
) -> None:
    operation = admitted(factory)
    backend = broker()
    backend.launch_reviewed_runtime_installation = lambda **_kw: pytest.fail(
        "Redispatched pending installation"
    )
    with pytest.raises(RuntimeInstallationActionConflict):
        RuntimeInstallationService(factory, backend).start_preview(
            approval=PreviewActionApproval(
                action="install", approved=True, acknowledgement=acknowledgement()
            )
        )
    assert recorded_acknowledgement(factory, operation) == acknowledgement()


def test_rejected_worker_marks_owner_operation_failed(
    factory: sessionmaker[Session],
) -> None:
    backend = broker()

    def reject(**kwargs: Any) -> Any:
        raise RuntimeError("old broker")

    backend.launch_reviewed_runtime_installation = reject
    with pytest.raises(RuntimeInstallationUnavailable):
        RuntimeInstallationService(factory, backend).start_preview(
            approval=PreviewActionApproval(
                action="install", approved=True, acknowledgement=acknowledgement()
            )
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
        approval=PreviewActionApproval(
            action="install", approved=True, acknowledgement=acknowledgement()
        )
    )
    assert result.newly_accepted is False


@pytest.mark.parametrize("defect", ["approval", "action", "nested_recipe"])
def test_forged_python_models_cannot_bypass_service_admission(
    factory: sessionmaker[Session], defect: str
) -> None:
    ack = acknowledgement()
    approval = PreviewActionApproval(
        action="install", approved=True, acknowledgement=ack
    )
    if defect == "approval":
        approval = approval.model_copy(update={"approved": False})
    elif defect == "action":
        approval = approval.model_copy(update={"action": "create"})
    else:
        approval = approval.model_copy(
            update={"acknowledgement": ack.model_copy(update={"release": "22.0"})}
        )
    backend = broker()
    backend.launch_reviewed_runtime_installation = lambda **_kw: pytest.fail(
        "Launched without exact consent"
    )
    with pytest.raises(RuntimeInstallationActionConflict):
        RuntimeInstallationService(factory, backend).start_preview(approval=approval)
    with factory() as db:
        assert RuntimeRecipeInstallationRepository(db).active("piers") is None


@pytest.mark.parametrize("state", ["worker", "lock", "capability"])
def test_valid_consent_does_not_bypass_broker_admission(
    factory: sessionmaker[Session], state: str
) -> None:
    from kodi_lab.application.runtime_installations import RuntimeInstallationBusy

    backend = broker()
    backend.launch_reviewed_runtime_installation = lambda **_kw: pytest.fail(
        "Bypassed broker admission"
    )
    if state == "capability":
        backend.capabilities = lambda: SimpleNamespace(operations=[])
    else:
        backend.active_reviewed_runtime_installation = lambda: SimpleNamespace(
            operation_id="occupied" if state == "worker" else None,
            lock_held=state == "lock",
        )
    with pytest.raises(
        RuntimeInstallationUnavailable
        if state == "capability"
        else RuntimeInstallationBusy
    ):
        RuntimeInstallationService(factory, backend).start_preview(
            approval=PreviewActionApproval(
                action="install", approved=True, acknowledgement=acknowledgement()
            )
        )


def test_explicit_retry_retains_original_admission_and_binds_a_new_operation(
    factory: sessionmaker[Session],
) -> None:
    from datetime import UTC, datetime

    operation = admitted(factory)
    with factory.begin() as db:
        RuntimeRecipeInstallationRepository(db).fail(
            operation, code="test_failure", now=datetime.now(UTC)
        )
    backend = broker()

    def launch(**kwargs: Any) -> ReviewedInstallationLaunchResult:
        from uuid import UUID

        assert (
            recorded_acknowledgement(factory, UUID(kwargs["operation_id"]))
            == acknowledgement()
        )
        return ReviewedInstallationLaunchResult(
            operation_id=kwargs["operation_id"], generation="piers", newly_accepted=True
        )

    backend.launch_reviewed_runtime_installation = launch
    service = RuntimeInstallationService(factory, backend)
    with pytest.raises(RuntimeInstallationActionConflict):
        service.start_preview(
            action="retry",
            approval=PreviewActionApproval(
                action="install", approved=True, acknowledgement=acknowledgement()
            ),
        )
    result = service.start_preview(
        action="retry",
        approval=PreviewActionApproval(
            action="retry", approved=True, acknowledgement=acknowledgement()
        ),
    )
    assert result.operation_id != str(operation)
    assert recorded_acknowledgement(factory, operation) == acknowledgement()
