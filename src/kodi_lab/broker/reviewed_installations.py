"""Closed Docker authority for one reviewed runtime installation worker."""

from __future__ import annotations

import fcntl
import os
import socket
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote
from uuid import UUID

import docker  # type: ignore[import-untyped]
from docker.errors import DockerException, NotFound  # type: ignore[import-untyped]
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.config import Settings
from kodi_lab.domain import RuntimeRecipeInstallationState
from kodi_lab.runtime.preview import (
    PreviewAcknowledgement,
    admission_evidence,
    reviewed_preview_recipe,
)
from kodi_lab.runtime.recipes import (
    load_inactive_ostree_recipe,
    load_recorded_reviewed_recipe,
)
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.storage.repositories import RuntimeRecipeInstallationRepository

WORKER_RESOURCE = "reviewed-runtime-installation"
WORKER_LABEL = "io.kodi-lab.resource"
OPERATION_LABEL = "io.kodi-lab.installation-operation-id"
GENERATION_LABEL = "io.kodi-lab.installation-generation"
IMAGE_LABEL = "io.kodi-lab.installation-worker-image-id"
STATE_DESTINATION = "/var/lib/kodi-lab"
LOCK_DESTINATION = "/run/kodi-lab"
DOCKER_SOCKET = "/var/run/docker.sock"
HOST_ROUTE_SOURCE = "/proc/1/net/route"
HOST_ROUTE_DESTINATION = "/tmp/kodi-lab-host-ipv4-route"
GENERATIONS = ("matrix", "nexus", "omega", "piers")


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
    matches = [item for item in mounts if item.get("Destination") == destination]
    if len(matches) != 1 or matches[0].get("Type") != "volume":
        raise ReviewedWorkerError("broker state mount is not an exact volume")
    name = matches[0].get("Name")
    if not isinstance(name, str) or not name:
        raise ReviewedWorkerError("broker state volume identity is invalid")
    return name


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

    def _client(self) -> Any:
        return self._client_factory(timeout=10)

    @staticmethod
    def _self_contract(client: Any) -> tuple[str, str, str]:
        own = client.api.inspect_container(socket.gethostname())
        config = own.get("Config")
        image_id = own.get("Image")
        if (
            not isinstance(config, dict)
            or config.get("Entrypoint") != ["kodi-lab-broker"]
            or not isinstance(image_id, str)
            or len(image_id) != 71
            or not image_id.startswith("sha256:")
        ):
            raise ReviewedWorkerError("installed broker image identity is invalid")
        image = client.api.inspect_image(image_id)
        if (
            image.get("Id") != image_id
            or image.get("Os") != "linux"
            or image.get("Architecture") != "amd64"
        ):
            raise ReviewedWorkerError("installed worker image is unavailable")
        mounts = own.get("Mounts")
        if (
            not isinstance(mounts, list)
            or len(
                [
                    item
                    for item in mounts
                    if item.get("Destination") == DOCKER_SOCKET
                    and item.get("Type") == "bind"
                    and item.get("Source") == DOCKER_SOCKET
                ]
            )
            != 1
        ):
            raise ReviewedWorkerError("broker Docker socket mount is unavailable")
        return (
            image_id,
            _volume(mounts, STATE_DESTINATION),
            _volume(mounts, LOCK_DESTINATION),
        )

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
                or item.st_uid != 0
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
    ) -> bool:
        attrs = container.attrs
        config = attrs.get("Config", {})
        host = attrs.get("HostConfig", {})
        labels = config.get("Labels", {})
        actual_generation = labels.get(GENERATION_LABEL)
        mounts = attrs.get("Mounts")
        expected_mounts = {
            ("volume", state_volume, STATE_DESTINATION, True),
            ("volume", lock_volume, LOCK_DESTINATION, True),
            ("bind", DOCKER_SOCKET, DOCKER_SOCKET, True),
            ("bind", HOST_ROUTE_SOURCE, HOST_ROUTE_DESTINATION, False),
        }
        actual_mounts = (
            {
                (
                    item.get("Type"),
                    item.get("Name")
                    if item.get("Type") == "volume"
                    else item.get("Source"),
                    item.get("Destination"),
                    item.get("RW"),
                )
                for item in mounts
            }
            if isinstance(mounts, list)
            else set()
        )
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
            and set(host.get("CapAdd") or []) == {"SETUID", "SETGID"}
            and host.get("CapDrop") == ["ALL"]
            and host.get("ReadonlyRootfs") is True
            and host.get("Privileged") is False
            and host.get("AutoRemove") is False
            and host.get("SecurityOpt") == ["no-new-privileges:true"]
            and config.get("User") == "0:0"
            and actual_mounts == expected_mounts
        )

    def _active(self, client: Any) -> tuple[UUID, str] | None:
        active: tuple[UUID, str] | None = None
        image_id, state_volume, lock_volume = self._self_contract(client)
        containers = client.containers.list(
            all=True, filters={"label": f"{WORKER_LABEL}={WORKER_RESOURCE}"}
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
                    container, operation_id, image_id, state_volume, lock_volume
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
                image_id, state_volume, lock_volume = self._self_contract(client)
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
                    },
                )
                container.reload()
                if not self._identity(
                    container,
                    operation_id,
                    image_id,
                    state_volume,
                    lock_volume,
                    generation,
                ):
                    raise ReviewedWorkerError("created worker identity differs")
                container.start()
                return True
            finally:
                client.close()
        except DockerException as error:
            raise ReviewedWorkerError("worker launch is unavailable") from error

    def observe(self, operation_id: UUID) -> WorkerObservation:
        try:
            client = self._client()
            try:
                image_id, state_volume, lock_volume = self._self_contract(client)
                try:
                    container = client.containers.get(worker_name(operation_id))
                except NotFound:
                    return WorkerObservation("missing", self.lock_held())
                container.reload()
                if not self._identity(
                    container, operation_id, image_id, state_volume, lock_volume
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
                    load_recorded_reviewed_recipe(
                        row.generation,
                        row.recipe_id,
                        row.recipe_revision,
                        digest,
                    )
                except ValueError:
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
                        load_inactive_ostree_recipe().digest
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
