"""Closed, operation-owned bridges for reviewed runtime assembly and validation."""

from __future__ import annotations

import ipaddress
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from docker.errors import APIError, NotFound  # type: ignore[import-untyped]
from docker.types import IPAMConfig, IPAMPool  # type: ignore[import-untyped]

from kodi_lab.config import Settings

Purpose = Literal["acquisition", "validation"]
_RESOURCE = "io.kodi-lab.resource"
_OPERATION = "io.kodi-lab.operation-id"
_PURPOSE = "io.kodi-lab.purpose"
_SCHEMA = "io.kodi-lab.temporary-network-schema"
_SCHEMA_VALUE = "reviewed-runtime-v1"
_HOST_ROUTES = Path("/tmp/kodi-lab-host-ipv4-route")


class TemporaryNetworkError(RuntimeError):
    """A safe failure code, with no Docker text or host network inventory."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class TemporaryNetwork:
    id: str
    name: str
    operation_id: UUID
    purpose: Purpose
    subnet: str | None


def _name(operation_id: UUID, purpose: Purpose) -> str:
    return f"kodi-lab-recipe-{purpose}-{operation_id.hex}"


def _labels(operation_id: UUID, purpose: Purpose) -> dict[str, str]:
    return {
        _RESOURCE: f"recipe-{purpose}-network",
        _OPERATION: str(operation_id),
        _PURPOSE: purpose,
        _SCHEMA: _SCHEMA_VALUE,
    }


def _host_routes(path: Path | None = None) -> tuple[ipaddress.IPv4Network, ...]:
    """Read the fixed host-route bind in workers, or native procfs in CLI use."""

    source = path or (
        _HOST_ROUTES if _HOST_ROUTES.exists() else Path("/proc/net/route")
    )
    try:
        lines = source.read_text().splitlines()
        if not lines or not lines[0].startswith("Iface\t"):
            raise ValueError("invalid route header")
        routes: list[ipaddress.IPv4Network] = []
        for line in lines[1:]:
            fields = line.split()
            if len(fields) < 8:
                raise ValueError("invalid route row")
            destination = int.from_bytes(bytes.fromhex(fields[1]), "little")
            mask = int.from_bytes(bytes.fromhex(fields[7]), "little")
            if mask == 0:
                continue  # The default route is not a claimed local prefix.
            routes.append(
                ipaddress.IPv4Network(
                    (
                        str(ipaddress.IPv4Address(destination)),
                        str(ipaddress.IPv4Address(mask)),
                    ),
                    strict=False,
                )
            )
        return tuple(routes)
    except (OSError, ValueError) as error:
        raise TemporaryNetworkError(
            "temporary_network_route_inventory_unavailable"
        ) from error


def _network_inventory(client: Any) -> list[Mapping[str, Any]]:
    try:
        networks = client.api.networks()
        if not isinstance(networks, list) or len(networks) > 512:
            raise ValueError("network inventory bound")
        return networks
    except Exception as error:
        raise TemporaryNetworkError(
            "temporary_network_inventory_unavailable"
        ) from error


def _subnets(network: Mapping[str, Any]) -> tuple[ipaddress.IPv4Network, ...]:
    ipam = network.get("IPAM") or {}
    configs = ipam.get("Config") if isinstance(ipam, dict) else None
    if configs is None:
        return ()
    if not isinstance(configs, list):
        raise TemporaryNetworkError("temporary_network_inventory_unavailable")
    result = []
    for config in configs:
        if not isinstance(config, dict):
            raise TemporaryNetworkError("temporary_network_inventory_unavailable")
        subnet = config.get("Subnet")
        if subnet:
            try:
                parsed = ipaddress.ip_network(subnet, strict=False)
            except ValueError as error:
                raise TemporaryNetworkError(
                    "temporary_network_inventory_unavailable"
                ) from error
            if isinstance(parsed, ipaddress.IPv4Network):
                result.append(parsed)
    return tuple(result)


def _available_children(
    client: Any, settings: Settings, *, routes: tuple[ipaddress.IPv4Network, ...]
) -> tuple[ipaddress.IPv4Network, ...]:
    assert settings.temporary_network_pool is not None
    pool = ipaddress.IPv4Network(settings.temporary_network_pool)
    children = tuple(pool.subnets(new_prefix=settings.temporary_network_child_prefix))
    occupied = tuple(
        subnet for network in _network_inventory(client) for subnet in _subnets(network)
    )
    # A host route in any part of the administrator's reservation is a bad
    # configuration. Other Docker bridges may occupy individual children.
    if any(pool.overlaps(route) for route in routes) or any(
        pool.subnet_of(network) for network in occupied
    ):
        raise TemporaryNetworkError("temporary_network_pool_conflict")
    return tuple(
        child
        for child in children
        if not any(child.overlaps(item) for item in (*routes, *occupied))
    )


def preflight_temporary_networks(
    client: Any, settings: Settings, *, route_path: Path | None = None
) -> None:
    """Fail before a costly pull when no currently usable bridge subnet exists."""

    if settings.temporary_network_pool is not None:
        routes = _host_routes(route_path)
        if not _available_children(client, settings, routes=routes):
            raise TemporaryNetworkError("temporary_network_pool_exhausted")
        return
    occupied = tuple(
        subnet for network in _network_inventory(client) for subnet in _subnets(network)
    )
    try:
        configured = client.api.info().get("DefaultAddressPools")
        if not configured:
            # The Engine may not report custom daemon pools. Guessing its
            # built-ins could falsely reject a host with configured capacity.
            return
        routes = _host_routes(route_path)
        pools = tuple(
            (item["Base"], item["Size"]) if isinstance(item, dict) else item
            for item in configured
        )
        candidates = (
            child
            for base, size in pools
            for child in ipaddress.IPv4Network(base).subnets(new_prefix=int(size))
        )
        if any(
            not any(child.overlaps(item) for item in (*routes, *occupied))
            for child in candidates
        ):
            return
    except (KeyError, TypeError, ValueError) as error:
        raise TemporaryNetworkError(
            "temporary_network_inventory_unavailable"
        ) from error
    raise TemporaryNetworkError("temporary_network_capacity_unavailable")


def _docker_creation_code(error: BaseException, purpose: Purpose) -> str:
    detail = str(getattr(error, "explanation", ""))
    if isinstance(error, APIError) and (
        "could not find an available, non-overlapping" in detail.lower()
        and "address pool" in detail.lower()
    ):
        return "docker_address_pool_exhausted"
    return f"{purpose}_network_creation_failed"


def _require_identity(network: Mapping[str, Any], lease: TemporaryNetwork) -> None:
    if (
        network.get("Id") != lease.id
        or network.get("Name") != lease.name
        or network.get("Driver") != "bridge"
        or network.get("Scope") != "local"
        or network.get("Internal") is not (lease.purpose == "validation")
        or network.get("Attachable") is not False
        or network.get("EnableIPv6") is not False
        or network.get("Labels") != _labels(lease.operation_id, lease.purpose)
        or (
            lease.subnet is not None
            and tuple(str(s) for s in _subnets(network)) != (lease.subnet,)
        )
    ):
        raise TemporaryNetworkError("temporary_network_identity_mismatch")


def create_temporary_network(
    client: Any,
    settings: Settings,
    *,
    operation_id: UUID,
    purpose: Purpose,
    route_path: Path | None = None,
) -> TemporaryNetwork:
    """Allocate at most 256 explicit children; Docker atomically rejects races."""

    name = _name(operation_id, purpose)
    labels = _labels(operation_id, purpose)
    routes = _host_routes(route_path) if settings.temporary_network_pool else ()
    children: tuple[ipaddress.IPv4Network | None, ...] = (
        _available_children(client, settings, routes=routes)
        if settings.temporary_network_pool
        else (None,)
    )
    if not children:
        raise TemporaryNetworkError("temporary_network_pool_exhausted")
    for child in children:
        options: dict[str, Any] = {
            "driver": "bridge",
            "internal": purpose == "validation",
            "attachable": False,
            "enable_ipv6": False,
            "check_duplicate": True,
            "labels": labels,
        }
        if child is not None:
            options["ipam"] = IPAMConfig(pool_configs=[IPAMPool(subnet=str(child))])
        try:
            created = client.networks.create(name, **options)
        except APIError as error:
            detail = str(getattr(error, "explanation", "")).lower()
            if (
                child is not None
                and "pool overlaps with other one on this address space" in detail
            ):
                continue
            raise TemporaryNetworkError(
                _docker_creation_code(error, purpose)
            ) from error
        except Exception as error:
            raise TemporaryNetworkError(f"{purpose}_network_creation_failed") from error
        lease = TemporaryNetwork(
            created.id, name, operation_id, purpose, str(child) if child else None
        )
        try:
            _require_identity(client.api.inspect_network(created.id), lease)
        except Exception as error:
            raise TemporaryNetworkError(
                "temporary_network_identity_mismatch"
            ) from error
        return lease
    raise TemporaryNetworkError("temporary_network_pool_exhausted")


def remove_exact_temporary_network(client: Any, lease: TemporaryNetwork) -> bool:
    """An absent exact ID is already clean; a substituted identity is untouched."""

    try:
        observed = client.api.inspect_network(lease.id)
    except NotFound:
        return False
    _require_identity(observed, lease)
    if observed.get("Containers"):
        raise TemporaryNetworkError("temporary_network_cleanup_failed")
    try:
        client.api.remove_network(lease.id)
    except NotFound:
        return False
    except Exception as error:
        raise TemporaryNetworkError("temporary_network_cleanup_failed") from error
    return True


def reclaim_terminal_orphans(
    client: Any, *, terminal: Callable[[UUID], bool]
) -> tuple[str, ...]:
    """Reclaim only labeled terminal-operation bridges with zero container refs."""

    reclaimed: list[str] = []
    for item in _network_inventory(client):
        labels = item.get("Labels") or {}
        if not isinstance(labels, dict) or labels.get(_SCHEMA) != _SCHEMA_VALUE:
            continue
        purpose = labels.get(_PURPOSE)
        if purpose not in ("acquisition", "validation"):
            continue
        try:
            operation_id = UUID(labels[_OPERATION])
        except (KeyError, TypeError, ValueError):
            continue
        lease = TemporaryNetwork(
            item.get("Id", ""),
            _name(operation_id, purpose),
            operation_id,
            purpose,
            None,
        )
        try:
            observed = client.api.inspect_network(lease.id)
            _require_identity(observed, lease)
        except (NotFound, TemporaryNetworkError):
            continue
        if not terminal(operation_id) or observed.get("Containers"):
            continue
        containers = client.api.containers(all=True)
        if not isinstance(containers, list) or len(containers) > 512:
            raise TemporaryNetworkError("temporary_network_inventory_unavailable")
        referenced = False
        for container in containers:
            detail = client.api.inspect_container(container["Id"])
            attached = (detail.get("NetworkSettings") or {}).get("Networks") or {}
            if lease.name in attached or any(
                endpoint.get("NetworkID") == lease.id for endpoint in attached.values()
            ):
                referenced = True
                break
            worker_labels = (detail.get("Config") or {}).get("Labels") or {}
            if (
                worker_labels.get("io.kodi-lab.installation-operation-id")
                == str(operation_id)
                and (detail.get("State") or {}).get("Running") is True
            ):
                referenced = True
                break
        if not referenced and remove_exact_temporary_network(client, lease):
            reclaimed.append(lease.id)
    return tuple(reclaimed)
