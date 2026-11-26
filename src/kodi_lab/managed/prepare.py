"""Exact temporary resources owned by one managed Prepare journal entry."""

from __future__ import annotations

import os
import re
import shutil
import stat
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any

from docker import APIClient  # type: ignore[import-untyped]
from docker.errors import (  # type: ignore[import-untyped]
    APIError,
    DockerException,
    NotFound,
)

from .protocol import Document, Refusal

if TYPE_CHECKING:
    from .runtime import Runtime

ABANDONED_SCRATCH_GRACE = 24 * 60 * 60
PREPARE_CLEANUP_CAPACITY = 6
PREPARE_CLEANUP_BATCH = 2
PREPARE_CLEANUP_SECONDS = 1.0
PREPARE_CLEANUP_INTERVAL = 5.0
OPERATION_LABEL = "io.kodi-lab.prepare-operation"


def valid_prepare_receipt(
    operation: object, receipt: object, *, terminal: bool
) -> bool:
    if (
        not isinstance(operation, str)
        or not re.fullmatch(r"[0-9a-f]{32}", operation)
        or not isinstance(receipt, dict)
    ):
        return False
    fields = {
        "version",
        "target",
        "image",
        "scratch",
        "scratch_allocation",
        "material",
        "material_allocation",
        "engine",
        "container",
    }
    if terminal:
        fields.update(("generation", "scratch_after"))
        after = receipt.get("scratch_after")
        # Reject nonfinite or out-of-range POSIX timestamps.
        if (
            not isinstance(receipt.get("generation"), str)
            or not isinstance(after, (int, float))
            or isinstance(after, bool)
            or not 0 <= after <= 253402300799
            or not ({"scratch", "engine"} & receipt.keys())
        ):
            return False
    # Missing version is the original additive receipt format.
    version = receipt.get("version", 1)
    if (
        not receipt.keys() <= fields
        or type(version) is not int
        or version != 1
        or not _matches(
            receipt.get("target"), r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"
        )
        or not _matches(receipt.get("image"), r"sha256:[0-9a-f]{64}")
        or (
            "engine" in receipt
            and not _matches(receipt["engine"], r"[A-Za-z0-9:-]{1,128}")
        )
        or (
            "container" in receipt
            and (
                "engine" not in receipt
                or not _matches(receipt["container"], r"[0-9a-f]{64}")
            )
        )
    ):
        return False
    inode = receipt.get("scratch")
    if inode is not None and (type(inode) is not int or not 0 < inode < 2**64):
        return False
    if "engine" in receipt and inode is None and (not terminal or "scratch" in receipt):
        return False
    if "material" in receipt:
        material = receipt["material"]
        if type(material) is not int or not 0 < material < 2**64:
            return False
        if inode is None and (not terminal or "scratch" in receipt):
            return False
    for key in ("scratch", "material"):
        allocation = key + "_allocation"
        if allocation in receipt and (
            receipt.get(key) is None
            or not _matches(receipt[allocation], r"[0-9a-f]{32}")
        ):
            return False
    return True


def _matches(value: object, pattern: str) -> bool:
    return isinstance(value, str) and re.fullmatch(pattern, value) is not None


def maintenance_timeout(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise Refusal("prepare_cleanup_budget")
    return remaining


class PrepareResources:
    def __init__(self, runtime: Runtime, operation: str, receipt: Document) -> None:
        if not re.fullmatch(r"[0-9a-f]{32}", operation) or receipt.get("target") != str(
            runtime.target
        ):
            raise Refusal("prepare_resource_identity")
        self.runtime = runtime
        self.operation = operation
        self.receipt = receipt
        self.scratch = runtime.root / ("baseline-" + operation)
        self.name = "kodi-lab-prepare-" + operation

    def checkpoint(self, event: str) -> None:
        self.runtime.checkpoint(event, prepare=self.receipt)

    def allocate_scratch(self) -> Path:
        from .runtime import mark_allocation

        if self.scratch.exists() or self.scratch.is_symlink():
            raise Refusal("prepare_staging_collision")
        self.receipt["scratch"] = None
        self.checkpoint("prepare_staging")
        try:
            self.scratch.mkdir(mode=0o700)
        except FileExistsError as error:
            del self.receipt["scratch"]
            self.checkpoint("prepare_staging_collision")
            raise Refusal("prepare_staging_collision") from error
        inode, allocation = mark_allocation(self.scratch)
        self.receipt.update(scratch=inode, scratch_allocation=allocation)
        self.checkpoint("prepare_staging_allocated")
        build = self.scratch / "material"
        build.mkdir(mode=0o700)
        return build

    def mark_baseline_ready(self, build: Path) -> None:
        from .runtime import mark_allocation

        inode, allocation = mark_allocation(build)
        self.receipt.update(material=inode, material_allocation=allocation)
        self.checkpoint("prepare_baseline_ready")

    def publish_baseline(self) -> None:
        from .runtime import require_unmounted_tree, sync_dir

        if self.receipt.get("scratch") is None or "material" not in self.receipt:
            raise Refusal("prepare_baseline_identity")
        self._require_allocation(self.scratch, "scratch")
        require_unmounted_tree(self.scratch)
        build = self.scratch / "material"
        try:
            self._require_allocation(build, "material")
        except FileNotFoundError:
            self._require_allocation(self.runtime.material, "material")
        else:
            previous = self.scratch / "prior-material"
            if self.runtime.material.exists() or self.runtime.material.is_symlink():
                self._require_directory(self.runtime.material, None)
                require_unmounted_tree(self.runtime.material)
                if previous.exists() or previous.is_symlink():
                    raise Refusal("prepare_baseline_identity")
                self.runtime.material.rename(previous)
            build.rename(self.runtime.material)
        sync_dir(self.scratch)
        sync_dir(self.runtime.root)

    def _require_directory(self, path: Path, inode: int | None) -> None:
        observed = path.lstat()
        if (
            not stat.S_ISDIR(observed.st_mode)
            or stat.S_IMODE(observed.st_mode) != 0o700
            or observed.st_uid != os.getuid()
            or observed.st_dev != self.runtime.root.stat().st_dev
            or (inode is not None and observed.st_ino != inode)
        ):
            raise Refusal("prepare_staging_identity")

    def _require_allocation(self, path: Path, key: str) -> None:
        from .runtime import has_allocation

        self._require_directory(path, self.receipt[key])
        if self.receipt[key] is None or not has_allocation(
            path, self.receipt.get(key + "_allocation")
        ):
            raise Refusal("prepare_staging_allocation_unconfirmed")

    def retire_scratch(self) -> None:
        from .runtime import require_unmounted_tree, sync_dir

        try:
            self._require_allocation(self.scratch, "scratch")
        except FileNotFoundError:
            pass
        else:
            require_unmounted_tree(self.scratch)
            shutil.rmtree(self.scratch)
        sync_dir(self.scratch.parent)

    @staticmethod
    def _engine(client: Any) -> str:
        observed = client.info().get("ID")
        if not isinstance(observed, str) or not re.fullmatch(
            r"[A-Za-z0-9:-]{1,128}", observed
        ):
            raise Refusal("prepare_engine_identity")
        return observed

    @staticmethod
    def _client(*, timeout: float = 35) -> Any:
        from .runtime import DOCKER

        return APIClient(base_url=DOCKER[2], version="auto", timeout=timeout)

    def _labels(self) -> dict[str, str]:
        from .runtime import LABEL

        return {LABEL: str(self.runtime.target), OPERATION_LABEL: self.operation}

    def create_inspection(self) -> str:
        image = self.receipt["image"]
        with self._client() as client:
            # A declared volume would allocate storage even for a never-started
            # container. This helper needs only the image's files.
            if client.inspect_image(image)["Config"].get("Volumes"):
                raise Refusal("prepare_inspection_image_volumes")
            host = client.create_host_config(
                network_mode="none",
                cap_drop=["ALL"],
                security_opt=["no-new-privileges"],
            )
            self.receipt["engine"] = self._engine(client)
            self.checkpoint("prepare_inspection_dispatch")
            try:
                reply = client.create_container(
                    image, name=self.name, labels=self._labels(), host_config=host
                )
            except APIError as error:
                # The low-level call issues only the Create POST. These answers
                # definitively rejected creation. Transport and server failures
                # leave the dispatch journaled.
                if error.status_code in (400, 404, 409):
                    del self.receipt["engine"]
                    self.checkpoint("prepare_inspection_rejected")
                    raise Refusal("prepare_inspection_create_rejected") from error
                raise
            container = reply.get("Id")
            if not isinstance(container, str) or not re.fullmatch(
                r"[0-9a-f]{64}", container
            ):
                raise Refusal("prepare_inspection_identity")
            self.receipt["container"] = container
            self.checkpoint("prepare_inspection_created")
            self._require_inspection(client.inspect_container(container), container)
            return container

    def _require_inspection(self, observed: Document, identity: str) -> str:
        container = observed.get("Id")
        config = observed.get("Config")
        state = observed.get("State")
        host = observed.get("HostConfig")
        if not (
            isinstance(config, dict)
            and isinstance(state, dict)
            and isinstance(host, dict)
        ):
            raise Refusal("prepare_inspection_identity")
        labels = config.get("Labels")
        if (
            not isinstance(container, str)
            or not re.fullmatch(r"[0-9a-f]{64}", container)
            or (identity != self.name and container != identity)
            or observed.get("Name") != "/" + self.name
            or observed.get("Image") != self.receipt["image"]
            or not isinstance(labels, dict)
            or any(labels.get(key) != value for key, value in self._labels().items())
            or config.get("Volumes")
            or state.get("Status") != "created"
            or state.get("Running") is not False
            or host.get("NetworkMode") != "none"
            or host.get("PortBindings")
            or observed.get("Mounts") != []
        ):
            raise Refusal("prepare_inspection_identity")
        return container

    def retire_inspection(
        self, persist: Callable[[], None], *, deadline: float
    ) -> None:
        try:
            with self._client(timeout=maintenance_timeout(deadline)) as client:
                client.timeout = maintenance_timeout(deadline)
                if self._engine(client) != self.receipt["engine"]:
                    raise Refusal("prepare_engine_identity")
                identity = self.receipt.get("container", self.name)
                client.timeout = maintenance_timeout(deadline)
                try:
                    observed = client.inspect_container(identity)
                except NotFound:
                    if "container" not in self.receipt:
                        # A lost Create reply and present absence do not rule out
                        # a request that has yet to finish on the Engine.
                        raise Refusal(
                            "prepare_inspection_dispatch_unconfirmed"
                        ) from None
                    return
                container = self._require_inspection(observed, identity)
                if "container" not in self.receipt:
                    self.receipt["container"] = container
                    persist()
                client.timeout = maintenance_timeout(deadline)
                with suppress(NotFound):
                    client.remove_container(container, force=False, v=False)
        except DockerException as error:
            raise Refusal("prepare_inspection_cleanup_pending") from error
