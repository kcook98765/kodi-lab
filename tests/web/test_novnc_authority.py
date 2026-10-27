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
