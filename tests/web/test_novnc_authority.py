from dataclasses import replace
from typing import Any

import pytest

from kodi_lab.application.novnc import CatalogViewAuthority
from kodi_lab.broker.novnc import ViewBinding
from kodi_lab.storage.models import (
    ManagedInstanceRow,
    RuntimeContainerArtifactRow,
    RuntimeExecutionProfileRow,
)

from .conftest import IMAGE_ID, INSTANCE_ID, REVISION_ID


def test_catalog_identity_is_fenced_to_revision_epoch_image_and_profile(
    database: Any,
) -> None:
    with database.begin() as db:
        db.add(
            RuntimeContainerArtifactRow(
                runtime_revision_id=str(REVISION_ID), image_id=IMAGE_ID
            )
        )
    pin = ViewBinding("a" * 64, IMAGE_ID, "one-start-incarnation", ())
    resolver = CatalogViewAuthority(database, {INSTANCE_ID: pin})
    bound = resolver.resolve(INSTANCE_ID)
    assert bound.identity[0] == str(REVISION_ID)
    resolver.pins[INSTANCE_ID] = bound
    with database.begin() as db:
        row = db.get(ManagedInstanceRow, str(INSTANCE_ID))
        row.runtime_state_revision += 1
    with pytest.raises(ValueError):
        resolver.resolve(INSTANCE_ID)
    resolver.pins[INSTANCE_ID] = replace(pin, image="sha256:" + "c" * 64)
    with pytest.raises(ValueError):
        resolver.resolve(INSTANCE_ID)
    resolver.pins[INSTANCE_ID] = pin
    with database.begin() as db:
        row = db.get(RuntimeExecutionProfileRow, str(REVISION_ID))
        row.runtime_uid = 0
    with pytest.raises(ValueError):
        resolver.resolve(INSTANCE_ID)


def test_another_targets_bound_workspace_is_preserved_without_blocking_viewer(
    database: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace
    from uuid import uuid4

    with database.begin() as db:
        db.add(
            RuntimeContainerArtifactRow(
                runtime_revision_id=str(REVISION_ID), image_id=IMAGE_ID
            )
        )
    other = SimpleNamespace(target_id=str(uuid4()))
    monkeypatch.setattr("kodi_lab.application.novnc.active_session", lambda _: other)
    pin = ViewBinding("a" * 64, IMAGE_ID, "current-incarnation", ())
    assert CatalogViewAuthority(database, {INSTANCE_ID: pin}).resolve(
        INSTANCE_ID
    ).identity == (
        str(REVISION_ID),
        0,
        None,
    )


@pytest.mark.parametrize("drift", [None, "version", "tag", "ordinal", "uid", "gid"])
def test_trusted_display_descriptor_fences_generation_and_profile(
    database: Any,
    drift: str | None,
) -> None:
    from kodi_lab.runtime.display_capabilities import reviewed_display_capability
    from kodi_lab.runtime.recipes import load_display_candidate_recipe

    capability = reviewed_display_capability(load_display_candidate_recipe("nexus"))
    assert capability is not None
    with database.begin() as db:
        db.add(
            RuntimeContainerArtifactRow(
                runtime_revision_id=str(REVISION_ID), image_id=IMAGE_ID
            )
        )
        profile = db.get(RuntimeExecutionProfileRow, str(REVISION_ID))
        assert profile is not None
        profile.expected_kodi_version = "20.5"
        if drift == "version":
            profile.expected_kodi_version = "21.3"
        elif drift in ("tag", "ordinal"):
            profile.expected_version_tag = "rc"
            profile.expected_prerelease_ordinal = 1
        elif drift == "uid":
            profile.runtime_uid = 0
        elif drift == "gid":
            profile.runtime_gid = 0
    pin = ViewBinding("a" * 64, IMAGE_ID, "one-incarnation", ())
    resolver = CatalogViewAuthority(database, {INSTANCE_ID: pin}, capability=capability)
    if drift is None:
        assert resolver.resolve(INSTANCE_ID).image == IMAGE_ID
    else:
        with pytest.raises(ValueError, match="not admitted"):
            resolver.resolve(INSTANCE_ID)
