"""Docker's empty command encodings preserve exact entrypoint authority."""

from __future__ import annotations

from typing import Any, cast

import pytest

from kodi_lab.broker.docker_inventory import DockerManagedContainerAdapter
from kodi_lab.broker.errors import ProvisioningConflictError

from .test_provision_managed_container import (
    CONTAINER_ID,
    container_data,
    image_data,
    spec,
)


@pytest.mark.parametrize(
    "image_command,container_command",
    [
        (None, []),
        ([], None),
        (None, None),
        ([], []),
    ],
)
def test_empty_command_encodings_are_equivalent(
    image_command: object, container_command: object
) -> None:
    actual = container_data()
    image = cast(dict[str, Any], image_data()["Config"])
    image["Cmd"] = image_command
    cast(dict[str, Any], actual["Config"])["Cmd"] = container_command
    DockerManagedContainerAdapter._validate_provisioned_container(
        actual,
        expected_container_id=CONTAINER_ID,
        spec=spec(),
        image_config=image,
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("Cmd", ["/bin/sh"]),
        ("Cmd", ""),
        ("Cmd", [""]),
        ("Entrypoint", []),
        ("Entrypoint", None),
        ("Healthcheck", {"Test": ["NONE"]}),
    ],
)
def test_empty_command_normalization_never_accepts_execution_substitution(
    field: str, value: object
) -> None:
    actual = container_data()
    image = cast(dict[str, Any], image_data()["Config"])
    image["Cmd"] = None
    config = cast(dict[str, Any], actual["Config"])
    config["Cmd"] = []
    config[field] = value
    with pytest.raises(ProvisioningConflictError):
        DockerManagedContainerAdapter._validate_provisioned_container(
            actual,
            expected_container_id=CONTAINER_ID,
            spec=spec(),
            image_config=image,
        )
