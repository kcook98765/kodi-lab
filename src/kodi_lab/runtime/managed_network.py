"""Trusted setup of the one fixed Linux managed-Kodi Docker network."""

from __future__ import annotations

from collections.abc import Mapping
from ipaddress import IPv4Network, ip_network
from typing import Protocol

from docker.errors import NotFound  # type: ignore[import-untyped]
from docker.types import IPAMConfig, IPAMPool  # type: ignore[import-untyped]

from kodi_lab.broker.docker_inventory import (
    MANAGED_LABEL_KEY,
    MANAGED_LABEL_VALUE,
    NETWORK_ID_LABEL_KEY,
    NETWORK_ID_LABEL_VALUE,
    NETWORK_RESOURCE_LABEL_KEY,
    NETWORK_RESOURCE_LABEL_VALUE,
)
from kodi_lab.broker.provisioning import (
    MANAGED_NETWORK_DRIVER,
    MANAGED_NETWORK_NAME,
    MANAGED_NETWORK_SUBNET,
)

_LABELS = {
    MANAGED_LABEL_KEY: MANAGED_LABEL_VALUE,
    NETWORK_RESOURCE_LABEL_KEY: NETWORK_RESOURCE_LABEL_VALUE,
    NETWORK_ID_LABEL_KEY: NETWORK_ID_LABEL_VALUE,
}
_SUBNET = IPv4Network(MANAGED_NETWORK_SUBNET)


class ManagedNetworkSetupError(ValueError):
    """The fixed managed network cannot be safely created or accepted."""


class _NetworkDocker(Protocol):
    def networks(self) -> list[Mapping[str, object]]: ...

    def inspect_network(self, network_id: str) -> Mapping[str, object]: ...

    def create_network(
        self,
        name: str,
        *,
        driver: str,
        ipam: IPAMConfig,
        check_duplicate: bool,
        internal: bool,
        labels: Mapping[str, str],
        enable_ipv6: bool,
        attachable: bool,
    ) -> Mapping[str, object]: ...


def _require_network(value: Mapping[str, object]) -> None:
    ipam = value.get("IPAM")
    configs = ipam.get("Config") if isinstance(ipam, dict) else None
    if (
        value.get("Name") != MANAGED_NETWORK_NAME
        or value.get("Driver") != MANAGED_NETWORK_DRIVER
        or value.get("Internal") is not False
        or value.get("EnableIPv6") is not False
        or value.get("Attachable") is not False
        or value.get("Labels") != _LABELS
        or not isinstance(configs, list)
        or len(configs) != 1
        or not isinstance(configs[0], dict)
        or configs[0].get("Subnet") != MANAGED_NETWORK_SUBNET
    ):
        raise ManagedNetworkSetupError(
            "managed network identity or configuration differs"
        )


def _reject_overlap(
    networks: list[Mapping[str, object]], *, managed_id: str | None
) -> None:
    for network in networks:
        if network.get("Id") == managed_id:
            continue
        ipam = network.get("IPAM")
        configs = ipam.get("Config") if isinstance(ipam, dict) else None
        if configs is None:
            continue
        if not isinstance(configs, list):
            raise ManagedNetworkSetupError("Docker network IPAM inventory is invalid")
        for config in configs:
            if not isinstance(config, dict):
                raise ManagedNetworkSetupError(
                    "Docker network IPAM inventory is invalid"
                )
            subnet = config.get("Subnet")
            if subnet is None:
                continue
            try:
                candidate = ip_network(subnet, strict=False)
            except (TypeError, ValueError) as error:
                raise ManagedNetworkSetupError(
                    "Docker network IPAM inventory is invalid"
                ) from error
            if isinstance(candidate, IPv4Network) and candidate.overlaps(_SUBNET):
                raise ManagedNetworkSetupError(
                    "managed subnet overlaps another Docker network"
                )


def ensure_managed_network(client: _NetworkDocker) -> bool:
    """Verify or create only the fixed labeled network; return True if created."""

    try:
        existing = client.inspect_network(MANAGED_NETWORK_NAME)
    except NotFound:
        existing = None
    if existing is not None:
        _require_network(existing)
    managed_id = existing.get("Id") if existing is not None else None
    if managed_id is not None and not isinstance(managed_id, str):
        raise ManagedNetworkSetupError("managed network identity is invalid")
    _reject_overlap(client.networks(), managed_id=managed_id)
    if existing is not None:
        return False
    client.create_network(
        MANAGED_NETWORK_NAME,
        driver=MANAGED_NETWORK_DRIVER,
        ipam=IPAMConfig(pool_configs=[IPAMPool(subnet=MANAGED_NETWORK_SUBNET)]),
        check_duplicate=True,
        internal=False,
        labels=_LABELS,
        enable_ipv6=False,
        attachable=False,
    )
    _require_network(client.inspect_network(MANAGED_NETWORK_NAME))
    return True
