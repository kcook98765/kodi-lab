"""Exact temporary resources owned by one managed Prepare journal entry."""

from __future__ import annotations

import os
import re
import shutil
import stat
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
OPERATION_LABEL = "io.kodi-lab.prepare-operation"


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
        from .runtime import sync_dir

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
        sync_dir(self.scratch.parent)
        self.receipt["scratch"] = self.scratch.lstat().st_ino
        self.checkpoint("prepare_staging_allocated")
        build = self.scratch / "material"
        build.mkdir(mode=0o700)
        return build

    def mark_baseline_ready(self, build: Path) -> None:
        self.receipt["material"] = build.lstat().st_ino
        self.checkpoint("prepare_baseline_ready")

    def publish_baseline(self) -> None:
        from .runtime import sync_dir

        self._require_directory(self.scratch, self.receipt["scratch"])
        self._require_unmounted_tree(self.scratch)
        build = self.scratch / "material"
        try:
            self._require_directory(build, self.receipt["material"])
        except FileNotFoundError:
            self._require_directory(self.runtime.material, self.receipt["material"])
        else:
            previous = self.scratch / "prior-material"
            if self.runtime.material.exists() or self.runtime.material.is_symlink():
                self._require_directory(self.runtime.material, None)
                self._require_unmounted_tree(self.runtime.material)
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

    @staticmethod
    def _require_unmounted_tree(path: Path) -> None:
        # Bind mounts within the same filesystem can share its device number.
        # Refuse recursive deletion across any mount below this exact path.
        mount = os.fsencode(path)
        for char, escape in (
            (b"\\", rb"\134"),
            (b" ", rb"\040"),
            (b"\t", rb"\011"),
            (b"\n", rb"\012"),
        ):
            mount = mount.replace(char, escape)
        for line in Path("/proc/self/mountinfo").read_bytes().splitlines():
            point = line.split()[4]
            if point == mount or point.startswith(mount + b"/"):
                raise Refusal("prepare_staging_mount")

    def retire_scratch(self) -> None:
        from .runtime import sync_dir

        inode = self.receipt["scratch"]
        try:
            self._require_directory(self.scratch, inode)
        except FileNotFoundError:
            pass
        else:
            if inode is None:
                # Only mkdir can have happened before the inode checkpoint.
                # An unexpected nonempty directory must survive this window.
                self.scratch.rmdir()
            else:
                self._require_unmounted_tree(self.scratch)
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
    def _client() -> Any:
        from .runtime import DOCKER

        return APIClient(base_url=DOCKER[2], version="auto", timeout=35)

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

    def retire_inspection(self, persist: Callable[[], None]) -> None:
        try:
            with self._client() as client:
                if self._engine(client) != self.receipt["engine"]:
                    raise Refusal("prepare_engine_identity")
                identity = self.receipt.get("container", self.name)
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
                with suppress(NotFound):
                    client.remove_container(container, force=False, v=False)
        except DockerException as error:
            raise Refusal("prepare_inspection_cleanup_pending") from error
