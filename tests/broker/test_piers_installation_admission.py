"""Preview uses the existing broker authority and durable operation ownership."""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.broker.reviewed_installations import ReviewedWorkerError
from kodi_lab.runtime.preview import recorded_acknowledgement
from kodi_lab.storage.repositories import RuntimeRecipeInstallationRepository
from tests.broker.test_reviewed_runtime_installation_operation import backend as backend
from tests.runtime.test_ostree_installation import acknowledgement
from tests.runtime.test_recipe_installation import factory as factory


def test_missing_ack_never_launches(backend: tuple[Any, Any]) -> None:
    worker, containers = backend
    with pytest.raises(ReviewedWorkerError, match="acknowledgement"):
        worker.launch("piers", uuid4())
    assert containers.created is None


def test_old_worker_contract_rejects_preview(
    backend: tuple[Any, Any],
    factory: sessionmaker[Session],
) -> None:
    worker, containers = backend
    worker._session_factory = factory
    with pytest.raises(ReviewedWorkerError, match="contract"):
        worker.launch("piers", uuid4(), preview_acknowledgement=acknowledgement())
    assert containers.created is None


@pytest.mark.parametrize("display_candidate", [False, True])
def test_durable_admission_before_offline_worker_launch(
    backend: tuple[Any, Any],
    factory: sessionmaker[Session],
    display_candidate: bool,
) -> None:
    worker, containers = backend
    worker._session_factory = factory
    api = worker._client().api
    inspect = api.inspect_image
    api.inspect_image = lambda image: {
        **inspect(image),
        "Config": {"Labels": {"io.kodi-lab.reviewed-ostree-worker": "v1"}},
    }
    operation = uuid4()
    from tests.runtime.test_piers_installation_pipeline import admitted

    ack = acknowledgement()
    if display_candidate:
        from kodi_lab.application.runtime_installations import (
            RuntimeInstallationService,
            preview_acknowledgement,
        )
        from kodi_lab.runtime.preview import PreviewAcknowledgement
        from tests.application.test_piers_installation_admission import broker

        ack = PreviewAcknowledgement.model_validate(
            preview_acknowledgement(display_candidate=True)
        )
        result = RuntimeInstallationService(factory, broker()).start_preview(
            development_mode=True, acknowledgement=ack, display_candidate=True
        )
        operation = UUID(result.operation_id)
    else:
        operation = admitted(factory)
    assert worker.launch("piers", operation, preview_acknowledgement=ack)
    assert recorded_acknowledgement(factory, operation) == ack
    assert containers.created["command"] == [
        "install",
        "piers",
        "--operation-id",
        str(operation),
    ]
    assert containers.created["network_mode"] == "none"
    assert containers.created["read_only"] is True
    assert containers.created["cap_drop"] == ["ALL"]
    assert set(containers.created["cap_add"]) == {"SETUID", "SETGID"}
    with factory() as session:
        row = RuntimeRecipeInstallationRepository(session).get(operation)
        assert row is not None and row.state.value == "pending"
    assert not worker.launch("piers", operation, preview_acknowledgement=ack)
