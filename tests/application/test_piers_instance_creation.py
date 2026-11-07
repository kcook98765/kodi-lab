"""Preview creation uses real sealed fixture eligibility, not an approval bypass."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.runtime_installations import (
    PreviewActionApproval,
    RuntimeInstallationActionConflict,
    RuntimeInstallationService,
)
from kodi_lab.storage.repositories import (
    ManagedInstanceRepository,
    RuntimeRecipeInstallationRepository,
)
from tests.application.test_piers_installation_admission import broker
from tests.runtime.test_ostree_installation import acknowledgement, ready
from tests.runtime.test_recipe_installation import factory as factory


def test_ready_preview_creates_exact_pin_and_is_idempotent(
    factory: sessionmaker[Session],
) -> None:
    operation = ready(factory)
    backend = broker()
    backend.reviewed_runtime_image_present = lambda _id: SimpleNamespace(present=True)
    service = RuntimeInstallationService(factory, backend)
    view = service.get(operation)
    assert view is not None and view.status == "ready" and view.action == "create"
    with pytest.raises(RuntimeInstallationActionConflict):
        service.create_preview_instance(
            approval=PreviewActionApproval.model_construct(
                action="create", approved=False, acknowledgement=acknowledgement()
            )
        )
    created = service.create_preview_instance(
        approval=PreviewActionApproval(
            action="create", approved=True, acknowledgement=acknowledgement()
        )
    )
    assert created.action == "open" and created.managed_instance_url is not None
    assert (
        service.create_preview_instance(
            approval=PreviewActionApproval(
                action="create", approved=True, acknowledgement=acknowledgement()
            )
        )
        == created
    )
    with factory() as session:
        instances = ManagedInstanceRepository(session).list()
        installation = RuntimeRecipeInstallationRepository(session).get(operation)
        assert len(instances) == 1 and installation is not None
        assert str(instances[0].runtime_revision_id) == installation.runtime_revision_id
        assert instances[0].fixture_definition_id is None


@pytest.mark.parametrize("failure", ["image_missing", "attestation_altered"])
def test_ineligible_preview_never_creates_instance(
    factory: sessionmaker[Session], failure: str
) -> None:
    operation = ready(factory)
    backend = broker()
    backend.reviewed_runtime_image_present = lambda _id: SimpleNamespace(
        present=failure != "image_missing"
    )
    if failure == "attestation_altered":
        with factory.begin() as session:
            row = RuntimeRecipeInstallationRepository(session).get(operation)
            assert row is not None
            row.attestation_json = "{}"
    with pytest.raises(RuntimeInstallationActionConflict):
        RuntimeInstallationService(factory, backend).create_preview_instance(
            approval=PreviewActionApproval(
                action="create", approved=True, acknowledgement=acknowledgement()
            )
        )
    with factory() as session:
        assert ManagedInstanceRepository(session).list() == []


def test_historical_consent_cannot_create_qualified_current_instance(
    factory: sessionmaker[Session],
) -> None:
    from kodi_lab.runtime.preview import PreviewAcknowledgement
    from kodi_lab.runtime.recipes import HISTORICAL_PREVIEW_INSTALLATION_DIGEST

    operation = ready(factory)
    backend = broker()
    backend.reviewed_runtime_image_present = lambda _id: SimpleNamespace(present=True)
    service = RuntimeInstallationService(factory, backend)
    view = service.get(operation)
    assert view is not None and view.status == "ready" and view.action == "create"
    old = {
        **acknowledgement().model_dump(),
        "recipe_id": "piers-22.0-rc1-r1",
        "recipe_revision": 1,
        "recipe_digest": HISTORICAL_PREVIEW_INSTALLATION_DIGEST,
    }
    with pytest.raises(
        RuntimeInstallationActionConflict, match="acknowledgement differs"
    ):
        service.create_preview_instance(
            approval=PreviewActionApproval(
                action="create",
                approved=True,
                acknowledgement=PreviewAcknowledgement.model_validate(old),
            ),
        )
    with factory() as session:
        assert ManagedInstanceRepository(session).list() == []


def test_missing_image_requires_exact_reacquire_approval(
    factory: sessionmaker[Session],
) -> None:
    operation = ready(factory)
    backend = broker()
    backend.reviewed_runtime_image_present = lambda _id: SimpleNamespace(present=False)
    service = RuntimeInstallationService(factory, backend)
    current = service.get(operation)
    assert current is not None and current.action == "reacquire"
    with pytest.raises(RuntimeInstallationActionConflict):
        service.start_preview(
            action="reacquire",
            approval=PreviewActionApproval(
                action="install", approved=True, acknowledgement=acknowledgement()
            ),
        )
    result = service.start_preview(
        action="reacquire",
        approval=PreviewActionApproval(
            action="reacquire", approved=True, acknowledgement=acknowledgement()
        ),
    )
    assert result.operation_id != str(operation)
    with factory() as session:
        original = RuntimeRecipeInstallationRepository(session).get(operation)
        assert original is not None and original.state.value == "ready"
        assert ManagedInstanceRepository(session).list() == []
