"""Closed Docker authority for one reviewed runtime installation worker."""

from __future__ import annotations

import fcntl
import hashlib
import logging
import os
import re
import socket
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from threading import Event, Lock
from typing import Any, Literal
from urllib.parse import quote
from uuid import UUID, uuid4

import docker  # type: ignore[import-untyped]
from docker.errors import DockerException, NotFound  # type: ignore[import-untyped]
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.broker.worker_receipts import (
    LOG_TAIL_BYTES,
    RECEIPT_LIMIT,
    WorkerDiagnostics,
    WorkerReceipt,
    WorkerReceipts,
)
from kodi_lab.config import Settings
from kodi_lab.domain import RuntimeRecipeInstallationState
from kodi_lab.managed import reviewed_image_file
from kodi_lab.runtime.preview import (
    PreviewAcknowledgement,
    admission_evidence,
    reviewed_preview_recipe,
)
from kodi_lab.runtime.recipes import (
    load_inactive_ostree_recipe,
    load_installation_recipe,
    load_recorded_reviewed_recipe,
)
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.storage.models import RuntimeRecipeInstallationRow
from kodi_lab.storage.repositories import RuntimeRecipeInstallationRepository

WORKER_RESOURCE = "reviewed-runtime-installation"
WORKER_LABEL = "io.kodi-lab.resource"
OPERATION_LABEL = "io.kodi-lab.installation-operation-id"
GENERATION_LABEL = "io.kodi-lab.installation-generation"
IMAGE_LABEL = "io.kodi-lab.installation-worker-image-id"
LAUNCH_LABEL = "io.kodi-lab.installation-worker-launch"
STATE_DESTINATION = "/var/lib/kodi-lab"
LOCK_DESTINATION = "/run/kodi-lab"
DOCKER_SOCKET = "/var/run/docker.sock"
HOST_ROUTE_SOURCE = "/proc/1/net/route"
HOST_ROUTE_DESTINATION = "/tmp/kodi-lab-host-ipv4-route"
GENERATIONS = ("matrix", "nexus", "omega", "piers")
TERMINAL = {RuntimeRecipeInstallationState.READY, RuntimeRecipeInstallationState.FAILED}
_LOGGER = logging.getLogger(__name__)
_RETIREMENT_INTERVAL = 2
_RETIREMENT_BATCH = 1


class ReviewedWorkerError(RuntimeError):
    """The exact installation worker cannot be safely launched or observed."""


class ReviewedWorkerBusy(ReviewedWorkerError):
    """Another reviewed or CLI installation holds global acquisition authority."""


@dataclass(frozen=True)
class WorkerObservation:
    state: Literal["running", "exited", "missing", "mismatch"]
    lock_held: bool


def worker_name(operation_id: UUID) -> str:
    return f"kodi-lab-install-{operation_id.hex}"


def _volume(mounts: object, destination: str) -> str:
    if not isinstance(mounts, list):
        raise ReviewedWorkerError("broker mounts are unavailable")
    if not all(isinstance(item, dict) for item in mounts):
        raise ReviewedWorkerError("broker mounts are unavailable")
    matches = [item for item in mounts if item.get("Destination") == destination]
    if len(matches) != 1 or matches[0].get("Type") != "volume":
        raise ReviewedWorkerError("broker state mount is not an exact volume")
    name = matches[0].get("Name")
    if not isinstance(name, str) or not name:
        raise ReviewedWorkerError("broker state volume identity is invalid")
    return name


@dataclass(frozen=True)
class _DesktopBindSources:
    socket: str
    route: str


def _desktop_bind_sources(
    client: Any, own: dict[str, Any], state_volume: str, lock_volume: str
) -> _DesktopBindSources | None:
    """Admit only the two known aliases of this broker's WSL distro.

    Docker/root administrators remain trusted, as for literal daemon-host paths.
    A worker label or a Desktop-looking source is never provenance for an alias.
    """
    labels = own["Config"].get("Labels")
    distro = (
        labels.get("desktop.docker.io/wsl-distro") if isinstance(labels, dict) else None
    )
    if (
        not isinstance(distro, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", distro) is None
    ):
        return None
    try:
        info = client.info()
    except DockerException:
        return None
    if (
        not isinstance(info, dict)
        or not isinstance(info.get("OperatingSystem"), str)
        or info["OperatingSystem"]
        not in {"Docker Desktop", "Docker Desktop (containerized)"}
        or info.get("OSType") != "linux"
    ):
        return None
    root = f"/run/desktop/mnt/host/wsl/docker-desktop-bind-mounts/{distro}"
    desktop = _DesktopBindSources(
        socket=f"{root}/docker.sock",
        route=f"{root}/{hashlib.sha256(HOST_ROUTE_SOURCE.encode('utf-8')).hexdigest()}",
    )
    host = own.get("HostConfig")
    if not isinstance(host, dict) or not _installation_mounts_match(
        own.get("Mounts"), host, state_volume, lock_volume, desktop, broker=True
    ):
        return None
    return desktop


def _source_matches(
    actual: object, expected: str, desktop: _DesktopBindSources | None
) -> bool:
    if actual == expected:
        return True
    if desktop is None:
        return False
    if expected == DOCKER_SOCKET:
        return actual == desktop.socket
    if expected == HOST_ROUTE_SOURCE:
        return actual == desktop.route
    return False


def _installation_mounts_match(
    mounts: object,
    host: dict[str, Any],
    state_volume: str,
    lock_volume: str,
    desktop: _DesktopBindSources | None,
    *,
    broker: bool = False,
) -> bool:
    """Match installation roles in both requested and resolved Docker metadata."""
    expected = {
        STATE_DESTINATION: ("volume", state_volume, True),
        LOCK_DESTINATION: ("volume", lock_volume, True),
        DOCKER_SOCKET: ("bind", DOCKER_SOCKET, True),
        HOST_ROUTE_DESTINATION: ("bind", HOST_ROUTE_SOURCE, False),
    }
    # The base native-Linux Compose broker predates the route mount. It may
    # omit that role when no aliases are admitted; workers still require it.
    if (
        broker
        and desktop is None
        and isinstance(mounts, list)
        and not any(
            isinstance(m, dict) and m.get("Destination") == HOST_ROUTE_DESTINATION
            for m in mounts
        )
    ):
        del expected[HOST_ROUTE_DESTINATION]
    # The supported development overlay adds one read-only broker bind. Its
    # host-selected source is never passed to a worker or used as alias authority.
    development = "/mnt/kodi-lab-development"
    if broker and isinstance(mounts, list):
        extras = [
            m
            for m in mounts
            if isinstance(m, dict) and m.get("Destination") == development
        ]
        if extras:
            source = extras[0].get("Source")
            if (
                len(extras) != 1
                or not isinstance(source, str)
                or not source.startswith("/")
                or source == "/"
                or str(PurePosixPath(source)) != source
                or ".." in PurePosixPath(source).parts
                or "\0" in source
                or ":" in source
            ):
                return False
            expected[development] = ("bind", source, False)
    managed_destination = "/var/lib/kodi-lab-managed"
    if broker and isinstance(mounts, list):
        managed_mounts = [
            m
            for m in mounts
            if isinstance(m, dict) and m.get("Destination") == managed_destination
        ]
        if managed_mounts:
            name = managed_mounts[0].get("Name")
            if (
                len(managed_mounts) != 1
                or not isinstance(name, str)
                or re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", name) is None
                or name in {state_volume, lock_volume}
            ):
                return False
            expected[managed_destination] = ("volume", name, True)
    binds = host.get("Binds")
    structured = host.get("Mounts")
    if broker and structured:
        # Compose's long-form development bind is the only supported structured
        # descriptor. Installation roles keep the exact existing Binds contract.
        if (
            not isinstance(structured, list)
            or len(structured) != 1
            or not isinstance(structured[0], dict)
            or development not in expected
            or not isinstance(binds, list)
        ):
            return False
        item = structured[0]
        source = expected[development][1]
        if (
            set(item)
            - {"Type", "Source", "Target", "ReadOnly", "BindOptions", "Consistency"}
            or item.get("Type") != "bind"
            or item.get("Source") != source
            or item.get("Target") != development
            or item.get("ReadOnly") is not True
            or item.get("Consistency") not in (None, "")
            or item.get("BindOptions") not in (None, {}, {"CreateMountpoint": False})
        ):
            return False
        binds = [*binds, f"{source}:{development}:ro"]
        structured = None
    if (
        not isinstance(mounts, list)
        or len(mounts) != len(expected)
        or not isinstance(binds, list)
        or len(binds) != len(expected)
        or structured not in (None, [])
        or host.get("VolumesFrom") not in (None, [])
    ):
        return False
    seen: set[str] = set()
    for mount in mounts:
        if not isinstance(mount, dict):
            return False
        destination = mount.get("Destination")
        if (
            not isinstance(destination, str)
            or destination not in expected
            or destination in seen
        ):
            return False
        seen.add(destination)
        kind, source, writable = expected[destination]
        actual = mount.get("Name" if kind == "volume" else "Source")
        # Docker may leave resolved Mode empty for a read-only bind. Require
        # the exact RW boolean and the matching read-only request descriptor too.
        modes = (
            ("ro", "")
            if kind == "bind" and not writable
            else ("rw" if writable else "ro",)
        )
        if (
            mount.get("Type") != kind
            or mount.get("RW") is not writable
            or mount.get("Mode") not in modes
            or not _source_matches(actual, source, desktop if kind == "bind" else None)
        ):
            return False
    seen.clear()
    for bind in binds:
        if not isinstance(bind, str):
            return False
        parts = bind.split(":")
        if len(parts) != 3:
            return False
        actual, destination, mode = parts
        if destination not in expected or destination in seen:
            return False
        seen.add(destination)
        kind, source, writable = expected[destination]
        if mode != ("rw" if writable else "ro") or not _source_matches(
            actual, source, desktop if kind == "bind" else None
        ):
            return False
    return True


class DockerReviewedInstallationWorker:
    """Derive the worker image and mounts from this installed broker container."""

    def __init__(
        self,
        *,
        client_factory: Any = None,
        runtime_root: Path = Path(LOCK_DESTINATION),
        session_factory: sessionmaker[Session] | None = None,
    ) -> None:
        self._client_factory = client_factory or docker.from_env
        self._runtime_root = runtime_root
        self._session_factory = session_factory
        self._receipts = WorkerReceipts(runtime_root)
        self._lifecycle = Lock()
        self._retirement_cursor = ""

    def _client(self, *, timeout: int = 10) -> Any:
        return self._client_factory(timeout=timeout)

    @staticmethod
    def _self_contract(
        client: Any,
    ) -> tuple[str, str, str, _DesktopBindSources | None]:
        own = client.api.inspect_container(socket.gethostname())
        if not isinstance(own, dict):
            raise ReviewedWorkerError("broker inspection is unavailable")
        config = own.get("Config")
        image_id = own.get("Image")
        if (
            not isinstance(config, dict)
            or config.get("Entrypoint") != ["kodi-lab-broker"]
            or not isinstance(image_id, str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", image_id) is None
        ):
            raise ReviewedWorkerError("installed broker image identity is invalid")
        image = client.api.inspect_image(image_id)
        if (
            not isinstance(image, dict)
            or image.get("Id") != image_id
            or image.get("Os") != "linux"
            or image.get("Architecture") != "amd64"
        ):
            raise ReviewedWorkerError("installed worker image is unavailable")
        mounts = own.get("Mounts")
        state_volume = _volume(mounts, STATE_DESTINATION)
        lock_volume = _volume(mounts, LOCK_DESTINATION)
        if state_volume == lock_volume:
            raise ReviewedWorkerError("broker state and lock volumes are not distinct")
        desktop = _desktop_bind_sources(client, own, state_volume, lock_volume)
        host = own.get("HostConfig")
        if not isinstance(host, dict) or not _installation_mounts_match(
            mounts, host, state_volume, lock_volume, desktop, broker=True
        ):
            raise ReviewedWorkerError(
                "broker Docker socket or installation mounts are unavailable"
            )
        return image_id, state_volume, lock_volume, desktop

    def lock_held(self) -> bool:
        path = self._runtime_root / "recipe-install.lock"
        try:
            fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
        except FileNotFoundError:
            return False
        try:
            item = os.fstat(fd)
            if (
                not stat.S_ISREG(item.st_mode)
                or item.st_uid != os.geteuid()
                or item.st_nlink != 1
                or stat.S_IMODE(item.st_mode) != 0o600
            ):
                raise ReviewedWorkerError("runtime installation lock is unsafe")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            return False
        finally:
            os.close(fd)

    @staticmethod
    def _identity(
        container: Any,
        operation_id: UUID,
        image_id: str,
        state_volume: str,
        lock_volume: str,
        generation: str | None = None,
        *,
        desktop: _DesktopBindSources | None = None,
    ) -> bool:
        attrs = container.attrs
        config = attrs.get("Config", {})
        host = attrs.get("HostConfig", {})
        if not isinstance(config, dict) or not isinstance(host, dict):
            return False
        labels = config.get("Labels", {})
        if not isinstance(labels, dict):
            return False
        caps = host.get("CapAdd")
        actual_generation = labels.get(GENERATION_LABEL)
        mounts = attrs.get("Mounts")
        return (
            attrs.get("Name") == "/" + worker_name(operation_id)
            and attrs.get("Image") == image_id
            and labels.get(WORKER_LABEL) == WORKER_RESOURCE
            and labels.get(OPERATION_LABEL) == str(operation_id)
            and actual_generation in GENERATIONS
            and (generation is None or actual_generation == generation)
            and labels.get(IMAGE_LABEL) == image_id
            and config.get("Entrypoint") == ["kodi-lab-runtime"]
            and config.get("Cmd")
            == ["install", actual_generation, "--operation-id", str(operation_id)]
            and host.get("NetworkMode") == "none"
            and isinstance(caps, list)
            and all(isinstance(cap, str) for cap in caps)
            and set(caps) == {"SETUID", "SETGID"}
            and host.get("CapDrop") == ["ALL"]
            and host.get("ReadonlyRootfs") is True
            and host.get("Privileged") is False
            and host.get("AutoRemove") is False
            and host.get("SecurityOpt") == ["no-new-privileges:true"]
            and config.get("User") == "0:0"
            and _installation_mounts_match(
                mounts, host, state_volume, lock_volume, desktop
            )
        )

    def _active(self, client: Any) -> tuple[UUID, str] | None:
        active: tuple[UUID, str] | None = None
        image_id, state_volume, lock_volume, desktop = self._self_contract(client)
        containers = client.containers.list(
            all=True,
            sparse=True,
            limit=129,
            filters={
                "label": f"{WORKER_LABEL}={WORKER_RESOURCE}",
                "status": ["running", "paused", "restarting", "removing"],
            },
        )
        if len(containers) > 128:
            raise ReviewedWorkerError("worker inventory exceeds its bound")
        for container in containers:
            container.reload()
            if container.attrs.get("State", {}).get("Running") is not True:
                continue
            labels = container.attrs.get("Config", {}).get("Labels", {})
            try:
                operation_id = UUID(labels.get(OPERATION_LABEL, ""))
            except (ValueError, TypeError) as error:
                raise ReviewedWorkerError(
                    "active worker identity is invalid"
                ) from error
            generation = labels.get(GENERATION_LABEL)
            if (
                not self._identity(
                    container,
                    operation_id,
                    image_id,
                    state_volume,
                    lock_volume,
                    desktop=desktop,
                )
                or active is not None
            ):
                raise ReviewedWorkerError("active worker ownership is ambiguous")
            active = operation_id, generation
        return active

    def active(self) -> tuple[UUID, str] | None:
        try:
            client = self._client()
            try:
                return self._active(client)
            finally:
                client.close()
        except DockerException as error:
            raise ReviewedWorkerError("worker inventory is unavailable") from error

    def _admit_preview(self, operation_id: UUID, ack: PreviewAcknowledgement) -> None:
        reviewed = reviewed_preview_recipe(ack)
        expected = admission_evidence(ack, operation_id)
        factory = self._session_factory
        engine = None
        if factory is None:
            url = make_url(Settings().database_url)
            if (
                url.drivername != "sqlite"
                or not url.database
                or not Path(url.database).is_absolute()
            ):
                raise ReviewedWorkerError("Preview admission database identity differs")
            engine = create_database_engine(
                "sqlite:///file:" + quote(url.database, safe="/") + "?mode=ro&uri=true"
            )
            factory = create_session_factory(engine)
        try:
            with factory() as session:
                row = RuntimeRecipeInstallationRepository(session).get(operation_id)
                if (
                    row is None
                    or row.state.value != "pending"
                    or row.generation != "piers"
                    or row.recipe_id != reviewed.recipe.recipe_id
                    or row.recipe_revision != reviewed.recipe.recipe_revision
                    or row.recipe_digest != reviewed.digest
                    or row.attestation_json != expected
                ):
                    raise ReviewedWorkerError(
                        "exact application-owned Preview admission missing"
                    )
        finally:
            if engine is not None:
                engine.dispose()

    def launch(
        self,
        generation: str,
        operation_id: UUID,
        *,
        preview_acknowledgement: PreviewAcknowledgement | None = None,
    ) -> bool:
        with self._lifecycle:
            return self._launch(generation, operation_id, preview_acknowledgement)

    def _launch(
        self,
        generation: str,
        operation_id: UUID,
        preview_acknowledgement: PreviewAcknowledgement | None,
    ) -> bool:
        if generation not in GENERATIONS:
            raise ReviewedWorkerError("generation is unsupported")
        if generation == "piers" and preview_acknowledgement is None:
            raise ReviewedWorkerError("exact Preview acknowledgement required")
        if generation != "piers" and preview_acknowledgement is not None:
            raise ReviewedWorkerError("Preview acknowledgement/provider mismatch")
        try:
            client = self._client()
            try:
                active = self._active(client)
                if active is not None:
                    if active == (operation_id, generation):
                        return False
                    raise ReviewedWorkerBusy("another runtime installation is active")
                if self.lock_held():
                    raise ReviewedWorkerBusy("another runtime installation is active")
                image_id, state_volume, lock_volume, desktop = self._self_contract(
                    client
                )
                pending = self._receipts.pending()
                filename = f"{operation_id.hex}.json"
                if any(path.name == filename for path in pending) or (
                    self._receipts.contains_retired(operation_id)
                ):
                    raise ReviewedWorkerError("worker launch already recorded")
                row = self._recorded_operation(operation_id)
                if row is not None and (
                    row.state in TERMINAL
                    or generation != "piers"
                    or row.generation != generation
                ):
                    raise ReviewedWorkerError("worker installation already recorded")
                if len(pending) >= RECEIPT_LIMIT:
                    raise ReviewedWorkerError(
                        "worker retirement backlog requires review"
                    )
                if generation == "piers":
                    assert preview_acknowledgement is not None
                    image = client.api.inspect_image(image_id)
                    if (
                        image.get("Config", {})
                        .get("Labels", {})
                        .get("io.kodi-lab.reviewed-ostree-worker")
                        != "v1"
                    ):
                        raise ReviewedWorkerError(
                            "installed worker lacks reviewed OSTree contract"
                        )
                    self._admit_preview(operation_id, preview_acknowledgement)
                    reviewed = reviewed_preview_recipe(preview_acknowledgement)
                else:
                    reviewed = load_installation_recipe(generation)
                receipt = WorkerReceipt.model_validate(
                    {
                        "operation": operation_id,
                        "nonce": uuid4(),
                        "generation": generation,
                        "engine": self._engine_id(client),
                        "image": image_id,
                        "state_volume": state_volume,
                        "lock_volume": lock_volume,
                        "recipe_digest": reviewed.digest,
                    }
                )
                # The nonce binds a worker created just before a lost Docker reply
                # to this exact pre-create intent. Persist the full ID before start.
                self._receipts.save(receipt)
                settings = Settings()
                worker_environment = {
                    "KODI_LAB_TEMPORARY_NETWORK_CHILD_PREFIX": str(
                        settings.temporary_network_child_prefix
                    )
                }
                if settings.temporary_network_pool is not None:
                    worker_environment["KODI_LAB_TEMPORARY_NETWORK_POOL"] = (
                        settings.temporary_network_pool
                    )
                container = client.containers.create(
                    image=image_id,
                    name=worker_name(operation_id),
                    entrypoint=["kodi-lab-runtime"],
                    command=[
                        "install",
                        generation,
                        "--operation-id",
                        str(operation_id),
                    ],
                    user="0:0",
                    detach=True,
                    auto_remove=False,
                    network_mode="none",
                    log_config={
                        "type": "json-file",
                        "config": {"max-size": "256k", "max-file": "1"},
                    },
                    read_only=True,
                    tmpfs={"/tmp": "rw,noexec,nosuid,size=64m"},
                    cap_drop=["ALL"],
                    cap_add=["SETGID", "SETUID"],
                    security_opt=["no-new-privileges:true"],
                    volumes={
                        state_volume: {"bind": STATE_DESTINATION, "mode": "rw"},
                        lock_volume: {"bind": LOCK_DESTINATION, "mode": "rw"},
                        DOCKER_SOCKET: {"bind": DOCKER_SOCKET, "mode": "rw"},
                        HOST_ROUTE_SOURCE: {
                            "bind": HOST_ROUTE_DESTINATION,
                            "mode": "ro",
                        },
                    },
                    environment=worker_environment,
                    labels={
                        WORKER_LABEL: WORKER_RESOURCE,
                        OPERATION_LABEL: str(operation_id),
                        GENERATION_LABEL: generation,
                        IMAGE_LABEL: image_id,
                        LAUNCH_LABEL: str(receipt.nonce),
                    },
                )
                container.reload()
                if not self._receipt_matches(container, receipt, desktop):
                    raise ReviewedWorkerError("created worker identity differs")
                self._receipts.save(
                    WorkerReceipt.model_validate(
                        {
                            **receipt.model_dump(),
                            "container": container.id,
                            "start_requested": True,
                        }
                    )
                )
                container.start()
                return True
            finally:
                client.close()
        except (DockerException, OSError, SQLAlchemyError, ValueError) as error:
            raise ReviewedWorkerError("worker launch is unavailable") from error

    @staticmethod
    def _engine_id(client: Any) -> str:
        info = client.info()
        value = info.get("ID") if isinstance(info, dict) else None
        if not isinstance(value, str) or not re.fullmatch(
            r"[A-Za-z0-9:-]{1,128}", value
        ):
            raise ReviewedWorkerError("Docker Engine identity is unavailable")
        return value

    def _recorded_operation(
        self, operation: UUID
    ) -> RuntimeRecipeInstallationRow | None:
        factory = self._session_factory
        if factory is not None:
            with factory() as session:
                return RuntimeRecipeInstallationRepository(session).get(operation)
        url = make_url(Settings().database_url)
        if (
            url.drivername != "sqlite"
            or not url.database
            or not Path(url.database).is_absolute()
        ):
            raise ReviewedWorkerError("installation database identity differs")
        engine = create_database_engine(
            "sqlite:///file:"
            + quote(url.database, safe="/")
            + "?mode=ro&timeout=1&uri=true"
        )
        try:
            with create_session_factory(engine)() as session:
                return RuntimeRecipeInstallationRepository(session).get(operation)
        finally:
            engine.dispose()

    def _receipt_matches(
        self,
        container: Any,
        receipt: WorkerReceipt,
        desktop: _DesktopBindSources | None,
    ) -> bool:
        attrs = container.attrs
        config = attrs.get("Config")
        labels = config.get("Labels") if isinstance(config, dict) else None
        container_id = attrs.get("Id")
        return (
            isinstance(container_id, str)
            and re.fullmatch(r"[0-9a-f]{64}", container_id) is not None
            and container_id == container.id
            and (receipt.container is None or receipt.container == container_id)
            and isinstance(labels, dict)
            and labels.get(LAUNCH_LABEL) == str(receipt.nonce)
            and self._identity(
                container,
                receipt.operation,
                receipt.image,
                receipt.state_volume,
                receipt.lock_volume,
                receipt.generation,
                desktop=desktop,
            )
        )

    @staticmethod
    def _stopped_state(container: Any, receipt: WorkerReceipt) -> dict[str, Any] | None:
        attrs = container.attrs
        state = attrs.get("State")
        network_settings = attrs.get("NetworkSettings")
        networks = (
            network_settings.get("Networks")
            if isinstance(network_settings, dict)
            else None
        )
        host = attrs.get("HostConfig")
        restart = host.get("RestartPolicy") if isinstance(host, dict) else None
        expected_status = "exited" if receipt.start_requested else "created"
        if (
            not isinstance(state, dict)
            or state.get("Running") is not False
            or any(
                state.get(flag) is not False
                for flag in ("Paused", "Restarting", "Dead")
            )
            or state.get("Status") != expected_status
            or type(state.get("ExitCode")) is not int
            or type(state.get("OOMKilled")) is not bool
            or not isinstance(state.get("StartedAt"), str)
            or not isinstance(state.get("FinishedAt"), str)
            or not isinstance(networks, dict)
            or set(networks) - {"none"}
            or not isinstance(restart, dict)
            or restart.get("Name") not in ("", "no")
            or restart.get("MaximumRetryCount") != 0
        ):
            return None
        return state

    def _document_outcome(
        self, receipt: WorkerReceipt, state: dict[str, Any], tail: str
    ) -> WorkerDiagnostics | None:
        row = self._recorded_operation(receipt.operation)
        outcome: Literal["ready", "failed", "reused", "not_started", "interrupted"]
        if row is not None:
            if (
                row.state not in TERMINAL
                or row.generation != receipt.generation
                or row.recipe_digest != receipt.recipe_digest
            ):
                return None
            outcome = (
                "ready"
                if row.state is RuntimeRecipeInstallationState.READY
                else "failed"
            )
        elif state["Status"] == "created":
            outcome = "not_started"
        elif state["ExitCode"] != 0:
            # The exact process exited before creating an installation row.
            # Preserve that observed failure without synthesizing an app record.
            outcome = "interrupted"
        else:
            # A warm ensure can return another operation's existing READY receipt.
            # The fixed worker CLI reports that receipt only on verified reuse.
            matching = re.findall(
                r"^kodi-lab-reviewed-reuse "
                + str(receipt.operation)
                + r" ([0-9a-f-]{36}) (sha256:[0-9a-f]{64})$",
                tail,
                re.MULTILINE,
            )
            if len(matching) != 1:
                return None
            previous, image = matching[0]
            row = self._recorded_operation(UUID(previous))
            if (
                row is None
                or row.state is not RuntimeRecipeInstallationState.READY
                or row.recipe_digest != receipt.recipe_digest
                or row.generation != receipt.generation
                or row.image_id != image
            ):
                return None
            outcome = "reused"
        return WorkerDiagnostics(
            outcome=outcome,
            recorded_operation=UUID(row.id) if row else None,
            completed_at=row.completed_at if row else None,
            failure_code=row.failure_code if row else None,
            exit_code=state["ExitCode"],
            oom_killed=state["OOMKilled"],
            started_at=state["StartedAt"],
            finished_at=state["FinishedAt"],
            log_tail=tail,
        )

    def _retire(self, path: Path, client: Any) -> None:
        receipt = self._receipts.read(path)
        _, state_volume, lock_volume, desktop = self._self_contract(client)
        if (
            self._engine_id(client) != receipt.engine
            or state_volume != receipt.state_volume
            or lock_volume != receipt.lock_volume
        ):
            return
        try:
            container = client.containers.get(
                receipt.container or worker_name(receipt.operation)
            )
        except NotFound:
            if receipt.container is not None and receipt.diagnostics is not None:
                self._receipts.finish(path)
            return
        if not self._receipt_matches(container, receipt, desktop):
            return
        state = self._stopped_state(container, receipt)
        if state is None:
            return
        if receipt.diagnostics is not None:
            tail = receipt.diagnostics.log_tail
        elif state["Status"] == "created":
            tail = ""
        else:
            log = container.logs(stdout=True, stderr=True, tail=48)
            if not isinstance(log, bytes):
                return
            tail = log[-LOG_TAIL_BYTES:].decode("utf-8", errors="replace")
        result = self._document_outcome(receipt, state, tail)
        if result is None:
            return
        if receipt.diagnostics is None:
            self._receipts.save(
                receipt.model_copy(
                    update={"container": container.id, "diagnostics": result}
                )
            )
        elif receipt.diagnostics != result:
            return
        container.reload()
        if (
            not self._receipt_matches(container, receipt, desktop)
            or self._stopped_state(container, receipt) != state
        ):
            return
        # Always address Docker by full ID and leave every attached volume in place.
        container.remove(force=False, v=False)
        self._receipts.finish(path)

    def retire_terminal_workers(self) -> None:
        """Retry a small set of privately owned receipts per broker cycle."""
        from kodi_lab.runtime.install import RecipeInstallationError, _installation_lock

        if not self._lifecycle.acquire(blocking=False):
            return
        try:
            self._receipts.trim()
            paths = self._receipts.pending()
            if not paths:
                return
            batch = (
                [path for path in paths if path.name > self._retirement_cursor]
                + [path for path in paths if path.name <= self._retirement_cursor]
            )[:_RETIREMENT_BATCH]
            with _installation_lock(self._runtime_root):
                client = self._client(timeout=2)
                try:
                    if self._active(client) is not None:
                        return
                    for path in batch:
                        self._retirement_cursor = path.name
                        try:
                            self._retire(path, client)
                        except (
                            DockerException,
                            OSError,
                            SQLAlchemyError,
                            ValueError,
                            ReviewedWorkerError,
                        ) as error:
                            _LOGGER.warning(
                                "Installer retirement pending for %s (%s)",
                                path.stem,
                                type(error).__name__,
                            )
                finally:
                    client.close()
        except (DockerException, OSError, RecipeInstallationError, ReviewedWorkerError):
            # A concurrent install or an unavailable Engine leaves work pending.
            return
        finally:
            self._lifecycle.release()

    def serve_retirement(self, stopped: Event) -> None:
        while not stopped.is_set():
            self.retire_terminal_workers()
            stopped.wait(_RETIREMENT_INTERVAL)

    def observe(self, operation_id: UUID) -> WorkerObservation:
        try:
            client = self._client()
            try:
                image_id, state_volume, lock_volume, desktop = self._self_contract(
                    client
                )
                try:
                    container = client.containers.get(worker_name(operation_id))
                except NotFound:
                    return WorkerObservation("missing", self.lock_held())
                container.reload()
                if not self._identity(
                    container,
                    operation_id,
                    image_id,
                    state_volume,
                    lock_volume,
                    desktop=desktop,
                ):
                    return WorkerObservation("mismatch", self.lock_held())
                state: Literal["running", "exited"] = (
                    "running"
                    if container.attrs.get("State", {}).get("Running") is True
                    else "exited"
                )
                return WorkerObservation(state, self.lock_held())
            finally:
                client.close()
        except DockerException as error:
            raise ReviewedWorkerError("worker observation is unavailable") from error

    def image_present(self, operation_id: UUID) -> bool:
        factory = self._session_factory
        if factory is None:
            engine = create_database_engine(Settings().database_url)
            factory = create_session_factory(engine)
        try:
            with factory() as session:
                row = RuntimeRecipeInstallationRepository(session).get(operation_id)
                if (
                    row is None
                    or row.state is not RuntimeRecipeInstallationState.READY
                    or row.image_id is None
                    or row.generation not in GENERATIONS
                ):
                    raise ReviewedWorkerError(
                        "reviewed installation image is unavailable"
                    )
                image_id = row.image_id
                digest = row.recipe_digest
                try:
                    reviewed = load_recorded_reviewed_recipe(
                        row.generation,
                        row.recipe_id,
                        row.recipe_revision,
                        digest,
                    )
                except ValueError:
                    return False
                if digest == load_installation_recipe(row.generation).digest:
                    try:
                        selected = (
                            self._runtime_root / reviewed_image_file(row.generation)
                        ).read_text()
                    except FileNotFoundError:
                        return False
                    if selected != image_id:
                        return False
            client = self._client()
            try:
                try:
                    image = client.api.inspect_image(image_id)
                except NotFound:
                    return False
                labels = image.get("Config", {}).get("Labels", {})
                return bool(
                    image.get("Id") == image_id
                    and image.get("Os") == "linux"
                    and image.get("Architecture") == "amd64"
                    and labels.get("io.kodi-lab.recipe-digest")
                    == (
                        load_inactive_ostree_recipe(reviewed.recipe.kodi_version).digest
                        if row.generation == "piers"
                        else digest
                    )
                )
            finally:
                client.close()
        except DockerException as error:
            raise ReviewedWorkerError(
                "exact image observation is unavailable"
            ) from error
        finally:
            if self._session_factory is None:
                engine.dispose()
