"""The fixed managed network is created or accepted only by exact identity."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

import pytest
from docker.errors import NotFound  # type: ignore[import-untyped]

from kodi_lab.runtime.managed_network import (
    ManagedNetworkSetupError,
    ensure_managed_network,
)

NETWORK = {
    "Id": "managed-id",
    "Name": "kodi-lab-managed-v1",
    "Driver": "bridge",
    "Internal": False,
    "EnableIPv6": False,
    "Attachable": False,
    "Labels": {
        "io.kodi-lab.managed": "true",
        "io.kodi-lab.resource": "network",
        "io.kodi-lab.network-id": "managed-v1",
    },
    "IPAM": {"Config": [{"Subnet": "10.250.0.0/24"}]},
}


class FakeDocker:
    def __init__(self, managed: dict[str, Any] | None = None) -> None:
        self.managed = managed
        self.other: list[dict[str, Any]] = []
        self.created: list[dict[str, Any]] = []

    def inspect_network(self, network_id: str) -> dict[str, Any]:
        if network_id != "kodi-lab-managed-v1" or self.managed is None:
            raise NotFound("missing")
        return deepcopy(self.managed)

    def networks(self) -> list[Mapping[str, object]]:
        return [*self.other, *([self.managed] if self.managed else [])]

    def create_network(self, name: str, **kwargs: Any) -> dict[str, Any]:
        self.created.append({"name": name, **kwargs})
        self.managed = deepcopy(NETWORK)
        return {"Id": "managed-id"}


def test_create_then_verify_is_idempotent_and_fixed() -> None:
    docker = FakeDocker()
    assert ensure_managed_network(docker) is True
    assert ensure_managed_network(docker) is False
    assert len(docker.created) == 1
    creation = docker.created[0]
    assert creation["name"] == "kodi-lab-managed-v1"
    assert creation["driver"] == "bridge"
    assert creation["check_duplicate"] is True
    assert creation["internal"] is False
    assert creation["enable_ipv6"] is False
    assert creation["attachable"] is False
    assert creation["labels"] == NETWORK["Labels"]
    assert creation["ipam"]["Config"][0]["Subnet"] == "10.250.0.0/24"


@pytest.mark.parametrize("field", ["Name", "Driver", "Internal", "Labels", "IPAM"])
def test_same_name_impostor_is_never_adopted(field: str) -> None:
    network = deepcopy(NETWORK)
    network[field] = {} if field in {"Labels", "IPAM"} else "wrong"
    docker = FakeDocker(network)
    with pytest.raises(ManagedNetworkSetupError, match="identity or configuration"):
        ensure_managed_network(docker)
    assert docker.created == []


@pytest.mark.parametrize("subnet", ["10.250.0.0/24", "10.250.0.0/16"])
def test_overlap_blocks_creation_or_reuse(subnet: str) -> None:
    for managed in (None, deepcopy(NETWORK)):
        docker = FakeDocker(managed)
        docker.other = [{"Id": "other-id", "IPAM": {"Config": [{"Subnet": subnet}]}}]
        with pytest.raises(ManagedNetworkSetupError, match="overlaps"):
            ensure_managed_network(docker)
        assert docker.created == []


def test_nonoverlapping_network_and_missing_ipam_are_allowed() -> None:
    docker = FakeDocker()
    docker.other = [
        {"Id": "other-id", "IPAM": {"Config": [{"Subnet": "10.251.0.0/24"}]}},
        {"Id": "host-id", "IPAM": {"Config": None}},
    ]
    assert ensure_managed_network(docker) is True


def test_failed_postcreation_identity_check_does_not_accept_network() -> None:
    class WrongCreation(FakeDocker):
        def create_network(self, name: str, **kwargs: Any) -> dict[str, Any]:
            result = super().create_network(name, **kwargs)
            assert self.managed is not None
            self.managed["Labels"] = {}
            return result

    docker = WrongCreation()
    with pytest.raises(ManagedNetworkSetupError, match="identity or configuration"):
        ensure_managed_network(docker)
