"""Reviewed temporary network allocation never accepts caller Docker inputs."""

from __future__ import annotations

import ipaddress
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from docker.errors import APIError, NotFound  # type: ignore[import-untyped]
from pydantic import ValidationError

from kodi_lab.config import Settings
from kodi_lab.runtime.install import _installation_failure_code
from kodi_lab.runtime.recipes import load_reviewed_recipe
from kodi_lab.runtime.temporary_networks import (
    TemporaryNetworkError,
    _host_routes,
    create_temporary_network,
    preflight_temporary_networks,
    reclaim_terminal_orphans,
    remove_exact_temporary_network,
)
from kodi_lab.runtime.validation import RecipeValidationError, validate_reviewed_image


def settings(pool: str | None = "10.252.0.0/20", prefix: int = 24) -> Settings:
    return Settings(
        temporary_network_pool=pool,
        temporary_network_child_prefix=prefix,
        _env_file=None,  # type: ignore[call-arg]
    )


def route_file(path: Path, *subnets: str) -> Path:
    rows = [
        "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\tMTU\tWindow\tIRTT"
    ]
    for text in subnets:
        network = ipaddress.IPv4Network(text)
        destination = network.network_address.packed[::-1].hex().upper()
        mask = network.netmask.packed[::-1].hex().upper()
        rows.append(f"eth0\t{destination}\t00000000\t0001\t0\t0\t0\t{mask}\t0\t0\t0")
    path.write_text("\n".join(rows) + "\n")
    return path


class FakeDocker:
    def __init__(self) -> None:
        self.items: dict[str, dict[str, Any]] = {}
        self.container_items: dict[str, dict[str, Any]] = {}
        self.created: list[dict[str, Any]] = []
        self.removed: list[str] = []
        self.creation_error: Exception | None = None
        self.containers: Any = None
        self.api = SimpleNamespace(
            networks=lambda: list(self.items.values()),
            inspect_network=self._inspect_network,
            remove_network=self._remove,
            info=lambda: {},
            containers=lambda **_kwargs: [{"Id": key} for key in self.container_items],
            inspect_container=lambda id: self.container_items[id],
        )
        self.networks = SimpleNamespace(create=self._create)

    def add(
        self,
        subnet: str,
        *,
        name: str = "other",
        labels: dict[str, str] | None = None,
    ) -> str:
        id = uuid4().hex
        self.items[id] = {
            "Id": id,
            "Name": name,
            "Driver": "bridge",
            "Scope": "local",
            "Internal": False,
            "Attachable": False,
            "EnableIPv6": False,
            "Labels": labels or {},
            "IPAM": {"Config": [{"Subnet": subnet}]},
            "Containers": {},
        }
        return id

    def _create(self, name: str, **options: Any) -> SimpleNamespace:
        if self.creation_error is not None:
            raise self.creation_error
        id = uuid4().hex
        ipam = options.get("ipam") or {"Config": [{"Subnet": "172.18.0.0/16"}]}
        self.items[id] = {
            "Id": id,
            "Name": name,
            "Driver": options["driver"],
            "Scope": "local",
            "Internal": options["internal"],
            "Attachable": options["attachable"],
            "EnableIPv6": options["enable_ipv6"],
            "Labels": options["labels"],
            "IPAM": {"Config": [{"Subnet": ipam["Config"][0]["Subnet"]}]},
            "Containers": {},
        }
        self.created.append({"id": id, "name": name, **options})
        return SimpleNamespace(id=id)

    def _remove(self, id: str) -> None:
        self.removed.append(id)
        del self.items[id]

    def _inspect_network(self, id: str) -> dict[str, Any]:
        try:
            return self.items[id]
        except KeyError as error:
            raise NotFound("network not found") from error


@pytest.fixture
def empty_routes(tmp_path: Path) -> Path:
    return route_file(tmp_path / "route")


def test_configured_pool_and_child_prefix_parse() -> None:
    chosen = settings()
    assert chosen.temporary_network_pool == "10.252.0.0/20"
    assert chosen.temporary_network_child_prefix == 24
    assert settings(None).temporary_network_pool is None


@pytest.mark.parametrize(
    "value", ["10.252.1.1/20", "not-a-cidr", "8.8.8.0/24", "10.252.0.0/33"]
)
def test_invalid_pool_cidr_is_rejected(value: str) -> None:
    with pytest.raises(ValidationError):
        settings(value)


@pytest.mark.parametrize("prefix", [20, 23, 29])
def test_invalid_child_prefix_is_rejected(prefix: int) -> None:
    with pytest.raises(ValidationError):
        settings(prefix=prefix)


def test_route_parser_and_host_route_conflict(tmp_path: Path) -> None:
    routes = route_file(tmp_path / "route", "10.252.1.0/24")
    assert _host_routes(routes) == (ipaddress.IPv4Network("10.252.1.0/24"),)
    with pytest.raises(TemporaryNetworkError, match="temporary_network_pool_conflict"):
        preflight_temporary_networks(FakeDocker(), settings(), route_path=routes)


def test_existing_docker_network_covering_pool_is_rejected(empty_routes: Path) -> None:
    docker = FakeDocker()
    docker.add("10.252.0.0/20")
    with pytest.raises(TemporaryNetworkError, match="temporary_network_pool_conflict"):
        preflight_temporary_networks(docker, settings(), route_path=empty_routes)


def test_occupied_child_is_skipped_and_sequential_operations_reuse_release(
    empty_routes: Path,
) -> None:
    docker = FakeDocker()
    docker.add("10.252.0.0/24")
    first = create_temporary_network(
        docker,
        settings(),
        operation_id=uuid4(),
        purpose="acquisition",
        route_path=empty_routes,
    )
    assert first.subnet == "10.252.1.0/24"
    assert remove_exact_temporary_network(docker, first)
    second = create_temporary_network(
        docker,
        settings(),
        operation_id=uuid4(),
        purpose="validation",
        route_path=empty_routes,
    )
    assert second.subnet == first.subnet
    assert remove_exact_temporary_network(docker, second)
    assert not remove_exact_temporary_network(docker, second)


def test_acquisition_and_validation_have_separate_operation_ownership(
    empty_routes: Path,
) -> None:
    docker = FakeDocker()
    operation = uuid4()
    acquisition = create_temporary_network(
        docker,
        settings(),
        operation_id=operation,
        purpose="acquisition",
        route_path=empty_routes,
    )
    validation = create_temporary_network(
        docker,
        settings(),
        operation_id=operation,
        purpose="validation",
        route_path=empty_routes,
    )
    assert acquisition.name != validation.name
    assert acquisition.subnet != validation.subnet
    assert docker.items[acquisition.id]["Internal"] is False
    assert docker.items[validation.id]["Internal"] is True
    for lease in (acquisition, validation):
        assert docker.items[lease.id]["Labels"] == {
            "io.kodi-lab.resource": f"recipe-{lease.purpose}-network",
            "io.kodi-lab.operation-id": str(operation),
            "io.kodi-lab.purpose": lease.purpose,
            "io.kodi-lab.temporary-network-schema": "reviewed-runtime-v1",
        }
        assert remove_exact_temporary_network(docker, lease)


def test_exact_cleanup_leaves_similar_name_and_managed_network(
    empty_routes: Path,
) -> None:
    docker = FakeDocker()
    unrelated = docker.add("10.250.0.0/24", name="kodi-lab-managed-v1")
    similar = docker.add("10.253.0.0/24", name="kodi-lab-recipe-acquisition-similar")
    lease = create_temporary_network(
        docker,
        settings(),
        operation_id=uuid4(),
        purpose="acquisition",
        route_path=empty_routes,
    )
    assert remove_exact_temporary_network(docker, lease)
    assert unrelated in docker.items and similar in docker.items
    assert docker.removed == [lease.id]


def test_validation_runtime_failure_cleans_exact_network(
    empty_routes: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docker = FakeDocker()
    docker.containers = SimpleNamespace(
        create=lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("failed"))
    )
    monkeypatch.setattr(
        "kodi_lab.runtime.temporary_networks._host_routes",
        lambda _path=None: _host_routes(empty_routes),
    )
    with pytest.raises(RecipeValidationError, match="validation failed"):
        validate_reviewed_image(
            load_reviewed_recipe("nexus"),
            image_id="sha256:" + "a" * 64,
            operation_id=uuid4(),
            client=docker,
            settings=settings(),
        )
    assert len(docker.created) == 1
    assert docker.removed == [docker.created[0]["id"]]


def test_cleanup_rejects_substituted_identity(empty_routes: Path) -> None:
    docker = FakeDocker()
    lease = create_temporary_network(
        docker,
        settings(),
        operation_id=uuid4(),
        purpose="acquisition",
        route_path=empty_routes,
    )
    docker.items[lease.id]["Labels"] = {}
    with pytest.raises(TemporaryNetworkError, match="identity_mismatch"):
        remove_exact_temporary_network(docker, lease)
    assert lease.id in docker.items and docker.removed == []


def test_orphan_reclaim_requires_terminal_operation_and_zero_refs(
    empty_routes: Path,
) -> None:
    docker = FakeDocker()
    orphan = create_temporary_network(
        docker,
        settings(),
        operation_id=uuid4(),
        purpose="acquisition",
        route_path=empty_routes,
    )
    assert reclaim_terminal_orphans(docker, terminal=lambda _id: False) == ()
    docker.container_items["stopped"] = {
        "NetworkSettings": {"Networks": {orphan.name: {"NetworkID": orphan.id}}},
        "State": {"Running": False},
    }
    assert reclaim_terminal_orphans(docker, terminal=lambda _id: True) == ()
    docker.container_items.clear()
    assert reclaim_terminal_orphans(docker, terminal=lambda _id: True) == (orphan.id,)
    assert reclaim_terminal_orphans(docker, terminal=lambda _id: True) == ()


def test_orphan_reclaim_rejects_spoofed_same_name(empty_routes: Path) -> None:
    docker = FakeDocker()
    lease = create_temporary_network(
        docker,
        settings(),
        operation_id=uuid4(),
        purpose="validation",
        route_path=empty_routes,
    )
    docker.items[lease.id]["Labels"]["io.kodi-lab.resource"] = "other"
    assert reclaim_terminal_orphans(docker, terminal=lambda _id: True) == ()
    assert lease.id in docker.items


def test_unconfigured_pool_uses_docker_automatic_allocation() -> None:
    docker = FakeDocker()
    lease = create_temporary_network(
        docker, settings(None), operation_id=uuid4(), purpose="acquisition"
    )
    assert "ipam" not in docker.created[0]
    assert lease.subnet is None
    assert remove_exact_temporary_network(docker, lease)


def test_automatic_preflight_reports_no_capacity(tmp_path: Path) -> None:
    docker = FakeDocker()
    docker.api.info = lambda: {
        "DefaultAddressPools": [
            {"Base": "172.17.0.0/16", "Size": 16},
            {"Base": "192.168.0.0/16", "Size": 20},
        ]
    }
    for number in range(17, 32):
        docker.add(f"172.{number}.0.0/16")
    for number in range(16, 256, 16):
        docker.add(f"192.168.{number}.0/20")
    routes = route_file(tmp_path / "route", "192.168.2.0/24")
    with pytest.raises(
        TemporaryNetworkError, match="temporary_network_capacity_unavailable"
    ):
        preflight_temporary_networks(docker, settings(None), route_path=routes)


def test_unknown_effective_automatic_pools_defer_to_docker(empty_routes: Path) -> None:
    docker = FakeDocker()
    docker.add("172.17.0.0/16")
    preflight_temporary_networks(docker, settings(None), route_path=empty_routes)


def test_actual_docker_pool_error_gets_specific_safe_code() -> None:
    docker = FakeDocker()
    docker.creation_error = APIError(
        "private daemon detail",
        explanation=(
            "could not find an available, non-overlapping IPv4 address pool "
            "among the defaults to assign to the network"
        ),
    )
    with pytest.raises(TemporaryNetworkError) as raised:
        create_temporary_network(
            docker, settings(None), operation_id=uuid4(), purpose="acquisition"
        )
    assert raised.value.code == "docker_address_pool_exhausted"
    assert "private daemon detail" not in str(raised.value)
    assert _installation_failure_code(raised.value) == "docker_address_pool_exhausted"


def test_unknown_docker_error_stays_phase_specific_and_safe() -> None:
    docker = FakeDocker()
    docker.creation_error = APIError(
        "private daemon detail", explanation="unrecognized failure"
    )
    with pytest.raises(TemporaryNetworkError) as raised:
        create_temporary_network(
            docker, settings(None), operation_id=uuid4(), purpose="validation"
        )
    assert raised.value.code == "validation_network_creation_failed"
    assert "private daemon detail" not in str(raised.value)


def test_pool_with_all_children_occupied_fails_closed(empty_routes: Path) -> None:
    docker = FakeDocker()
    for child in ipaddress.IPv4Network("10.252.0.0/20").subnets(new_prefix=24):
        docker.add(str(child))
    with pytest.raises(TemporaryNetworkError, match="temporary_network_pool_exhausted"):
        create_temporary_network(
            docker,
            settings(),
            operation_id=uuid4(),
            purpose="acquisition",
            route_path=empty_routes,
        )
