from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Self

import pytest
from docker.errors import APIError, NotFound  # type: ignore[import-untyped]
from requests import Response

from kodi_lab.managed import controller as managed
from kodi_lab.managed import runtime
from kodi_lab.managed.controller import Controller
from kodi_lab.managed.prepare import (
    ABANDONED_SCRATCH_GRACE,
    OPERATION_LABEL,
    PrepareResources,
)
from kodi_lab.managed.protocol import Document, Refusal

IMAGE = "sha256:" + "a" * 64


class Interrupted(BaseException):
    """Leave the on-disk journal at the interrupted checkpoint."""


class Engine:
    def __init__(self) -> None:
        self.identity = "test-engine"
        self.containers: dict[str, Document] = {}
        self.volumes: dict[str, Document] = {}
        self.removed: list[str] = []
        self.creates = 0
        self.fail_copy = False
        self.create_status: int | None = None
        self.lose_reply = False
        self.fail_inspect_once = False
        self.fail_removal = False
        self.lose_removal_reply = False

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        pass

    def info(self) -> Document:
        return {"ID": self.identity}

    def inspect_image(self, image: str) -> Document:
        assert image == IMAGE
        return {"Config": {}}

    def create_host_config(self, **options: object) -> Document:
        assert options["network_mode"] == "none"
        return {"NetworkMode": "none"}

    def create_container(
        self, image: str, *, name: str, labels: Document, host_config: Document
    ) -> Document:
        self.creates += 1
        if self.create_status:
            response = Response()
            response.status_code = self.create_status
            raise APIError("create failed", response=response)
        container = f"{self.creates:064x}"
        self.containers[container] = {
            "Id": container,
            "Name": "/" + name,
            "Image": image,
            "Config": {"Labels": labels},
            "State": {"Status": "created", "Running": False},
            "HostConfig": host_config,
            "Mounts": [],
        }
        if self.lose_reply:
            raise OSError("response lost")
        return {"Id": container}

    def inspect_container(self, identity: str) -> Document:
        if self.fail_inspect_once:
            self.fail_inspect_once = False
            raise OSError("inspect failed")
        for key, container in self.containers.items():
            if identity in (key, container["Name"].lstrip("/")):
                return container
        raise NotFound("absent")

    def remove_container(self, identity: str, *, force: bool, v: bool) -> None:
        assert force is v is False
        assert len(identity) == 64
        if self.fail_removal:
            raise APIError("remove failed")
        self.removed.append(identity)
        del self.containers[identity]
        if self.lose_removal_reply:
            raise OSError("remove reply lost")

    def cli(self, *args: str) -> str:
        match args:
            case ("cp", source, dest):
                assert source.partition(":")[0] in self.containers
                if self.fail_copy:
                    raise OSError("copy failed")
                Path(dest).write_text("<addons/>")
            case ("ps", "-aq", "--filter", _):
                return ""
            case ("volume", "create", "--label", label, *_, device, name):
                assert device.startswith("device=")
                self.volumes[name] = {
                    "Labels": dict([label.split("=", 1)]),
                    "Options": {"device": device.removeprefix("device=")},
                }
            case ("volume", "inspect", name):
                return json.dumps([self.volumes[name]])
            case ("volume", "ls", "-q", "--filter", expression):
                name = expression.removeprefix("name=^").removesuffix("$")
                return name if name in self.volumes else ""
            case ("volume", "rm", name):
                del self.volumes[name]
            case _:
                raise AssertionError(args)
        return ""


class Site:
    def __init__(self, path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.root = path / "site"
        self.root.mkdir(mode=0o700)
        self.image = path / "reviewed-image"
        self.image.write_text(IMAGE)
        self.engine = Engine()
        self.now = time.time()
        monkeypatch.setattr(runtime, "docker", self.engine.cli)
        monkeypatch.setattr(runtime.Runtime, "capacity", lambda native: native.image)
        monkeypatch.setattr(PrepareResources, "_client", lambda _resources: self.engine)
        monkeypatch.setattr(
            managed,
            "time",
            SimpleNamespace(time=lambda: self.now, monotonic=time.monotonic),
        )
        self.controller = Controller(self.root, reviewed_image=self.image)

    def perform(self, name: str = "managed_prepare") -> object:
        return self.controller.perform(name, {}, actor="admin")["result"]

    def restart(self) -> None:
        self.controller = Controller(self.root, reviewed_image=self.image)
        self.controller.resume_prepare_cleanup()

    def interrupt(self, event: str) -> None:
        def stop(checkpoint: str) -> None:
            if checkpoint == event:
                raise Interrupted

        self.controller.interrupt = stop
        with pytest.raises(Interrupted):
            self.perform()

    @property
    def record(self) -> Document:
        operation = self.controller.last_operation()
        assert operation is not None
        return operation

    @property
    def scratch(self) -> Path:
        return self.root / ("baseline-" + str(self.record["id"]))


@pytest.fixture
def site(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Site:
    return Site(tmp_path, monkeypatch)


def test_success_retains_baseline_profile_and_image_without_accumulation(
    site: Site,
) -> None:
    for _ in range(5):
        assert site.perform() == {"state": "prepared", "profile": "omega"}
        native = site.controller.runtime
        assert (native.profile() / "userdata/guisettings.xml").is_file()
        assert (native.material / "manifest.xml").is_file()
        assert native.image == IMAGE
        assert site.engine.containers == {}
        assert list(site.root.glob("baseline-*")) == []
        assert "prepare_cleanup" not in site.record
    assert (
        site.engine.creates == len(site.engine.removed) == len(site.engine.volumes) == 1
    )


def test_failure_before_allocation(site: Site, monkeypatch: pytest.MonkeyPatch) -> None:
    def reject() -> str:
        raise Refusal("disk_headroom")

    monkeypatch.setattr(site.controller.runtime, "capacity", reject)
    with pytest.raises(Refusal, match="disk_headroom"):
        site.perform()
    site.restart()
    assert site.record["state"] == "complete"
    assert "prepare_cleanup" not in site.record
    assert site.engine.creates == 0
    assert list(site.root.glob("baseline-*")) == []


def test_failure_after_scratch_allocation(
    site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    def reject(_image: str) -> Document:
        raise OSError("engine unavailable")

    monkeypatch.setattr(site.engine, "inspect_image", reject)
    with pytest.raises(OSError, match="engine unavailable"):
        site.perform()
    assert not site.scratch.exists()
    assert site.engine.creates == 0
    assert site.record["state"] == "complete"


@pytest.mark.parametrize("failure", ["inspect", "copy"])
def test_failure_after_container_creation(site: Site, failure: str) -> None:
    site.engine.fail_inspect_once = failure == "inspect"
    site.engine.fail_copy = failure == "copy"
    with pytest.raises(OSError):
        site.perform()
    assert len(site.engine.removed) == 1
    assert not site.scratch.exists()
    assert not site.controller.runtime.pointer.exists()
    assert "prepare_cleanup" not in site.record


@pytest.mark.parametrize("status", [400, 404, 409])
def test_definitive_create_rejection_clears_only_its_intent(
    site: Site, status: int
) -> None:
    foreign = site.engine.create_container(
        IMAGE, name="foreign", labels={}, host_config={"NetworkMode": "none"}
    )["Id"]
    site.engine.create_status = status
    with pytest.raises(Refusal, match="prepare_inspection_create_rejected"):
        site.perform()
    assert list(site.engine.containers) == [foreign]
    assert "prepare_cleanup" not in site.record
    assert not site.scratch.exists()


@pytest.mark.parametrize(
    "event",
    [
        "prepare_staging",
        "prepare_staging_allocated",
        "prepare_inspection_created",
        "profile_before_commit",
    ],
)
def test_restart_before_commit_with_grace(site: Site, event: str) -> None:
    native = site.controller.runtime
    # A valid seed from an earlier attempt and other recovery assets survive.
    native.material.mkdir(mode=0o700)
    baseline = native.material / "legacy-seed"
    baseline.write_text("seed")
    protected = [
        native.root / "snapshots/kept.tar.gz",
        native.root / "volumes" / ("f" * 32),
        native.root / "manual-backup",
        native.root / ("baseline-" + "b" * 32),
    ]
    for item in protected:
        item.write_text("keep")
    site.interrupt(event)
    scratch = site.scratch
    site.restart()
    assert site.record["state"] == "complete"
    assert not native.pointer.exists()
    assert not site.engine.containers
    assert not site.engine.volumes
    if event != "prepare_staging":
        assert scratch.is_dir()
    site.now += ABANDONED_SCRATCH_GRACE - 1
    site.restart()
    assert scratch.exists() == (event != "prepare_staging")
    site.now += 1
    site.restart()
    assert "prepare_cleanup" not in site.record
    assert not scratch.exists()
    assert all(item.read_text() == "keep" for item in protected)
    assert baseline.read_text() == "seed"


@pytest.mark.parametrize(
    "event", ["pointer_replaced", "profile_committed", "baseline_replaced"]
)
def test_restart_during_and_after_commit(
    site: Site, monkeypatch: pytest.MonkeyPatch, event: str
) -> None:
    native = site.controller.runtime
    if event == "profile_committed":
        site.interrupt(event)
    elif event == "pointer_replaced":
        original = runtime.atomic

        def interrupted(path: Path, data: bytes, *, mode: int = 0o600) -> None:
            if path == native.pointer:
                stage = path.with_name("simulated-pointer-rename")
                stage.write_bytes(data)
                stage.replace(path)
                raise Interrupted
            original(path, data, mode=mode)

        with monkeypatch.context() as patch:
            patch.setattr(runtime, "atomic", interrupted)
            with pytest.raises(Interrupted):
                site.perform()
    else:
        original_rename = Path.rename

        def interrupted_rename(path: Path, target: Path) -> Path:
            result = original_rename(path, target)
            if target == native.material:
                raise Interrupted
            return result

        with monkeypatch.context() as patch:
            patch.setattr(Path, "rename", interrupted_rename)
            with pytest.raises(Interrupted):
                site.perform()

    slot = native.read_pointer()
    profile = native.profile()
    (profile / "keep").write_text("working")
    site.image.write_text("sha256:" + "b" * 64)
    scratch = site.scratch
    site.restart()
    assert site.record["state"] == "complete"
    assert native.read_pointer() == slot and native.image == IMAGE
    assert (profile / "keep").read_text() == "working"
    assert (native.material / "manifest.xml").is_file()
    assert native.volume(slot) in site.engine.volumes
    assert not site.engine.containers and not scratch.exists()
    assert "prepare_cleanup" not in site.record


def test_docker_removal_failure_and_lost_reply_are_retried(site: Site) -> None:
    site.engine.fail_removal = True
    site.perform()
    obligation = site.record["prepare_cleanup"][site.record["id"]]
    identity = obligation["container"]
    assert identity in site.engine.containers
    assert "scratch" not in obligation
    assert site.perform() == {"state": "prepared", "profile": "omega"}
    assert site.engine.creates == 1
    site.engine.fail_removal = False
    site.engine.lose_removal_reply = True
    site.restart()
    assert site.engine.removed == [identity]
    assert "prepare_cleanup" in site.record
    site.restart()
    assert "prepare_cleanup" not in site.record
    site.restart()
    assert site.engine.removed == [identity]


def test_filesystem_cleanup_failure_survives_another_operation(
    site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = shutil.rmtree

    def fail(path: Path) -> None:
        if path.name.startswith("baseline-"):
            raise OSError("filesystem failure")
        original(path)

    with monkeypatch.context() as patch:
        patch.setattr(shutil, "rmtree", fail)
        site.perform()
        orphan = site.scratch
        assert orphan.exists()
        site.perform("managed_reset")
        assert site.record["name"] == "managed_reset"
        assert "prepare_cleanup" in site.record
        assert orphan.exists()
    site.restart()
    assert not orphan.exists()
    assert "prepare_cleanup" not in site.record
    assert len(site.engine.volumes) == 1
    site.restart()


def test_replaced_scratch_is_preserved(site: Site) -> None:
    site.interrupt("prepare_inspection_created")
    scratch = site.scratch
    moved = scratch.rename(scratch.with_name("evidence"))
    scratch.mkdir(mode=0o700)
    (scratch / "foreign").write_text("keep")
    site.restart()
    site.now += ABANDONED_SCRATCH_GRACE
    site.restart()
    assert (scratch / "foreign").read_text() == "keep"
    assert "prepare_cleanup" in site.record
    assert moved.is_dir()


def test_unknown_dispatch_persists_until_exact_container_observed(site: Site) -> None:
    site.interrupt("prepare_inspection_dispatch")
    operation = site.record["id"]
    site.restart()
    assert "container" not in site.record["prepare_cleanup"][operation]
    site.perform()
    assert operation in site.record["prepare_cleanup"]
    identity = site.engine.create_container(
        IMAGE,
        name="kodi-lab-prepare-" + operation,
        labels={
            runtime.LABEL: str(site.controller.runtime.target),
            OPERATION_LABEL: operation,
        },
        host_config={"NetworkMode": "none"},
    )["Id"]
    site.restart()
    assert identity in site.engine.removed
    assert "engine" not in site.record["prepare_cleanup"][operation]


def test_lost_create_reply_with_exact_match_retires_container(site: Site) -> None:
    site.engine.lose_reply = True
    with pytest.raises(OSError, match="response lost"):
        site.perform()
    assert site.engine.containers == {}
    assert len(site.engine.removed) == 1
    assert "prepare_cleanup" not in site.record


@pytest.mark.parametrize(
    "mismatch", ["target", "image", "name", "mount", "network", "running", "engine"]
)
def test_foreign_or_active_inspection_is_preserved(site: Site, mismatch: str) -> None:
    site.interrupt("prepare_inspection_created")
    identity, container = next(iter(site.engine.containers.items()))
    if mismatch == "target":
        container["Config"]["Labels"][runtime.LABEL] = "foreign"
    elif mismatch == "image":
        container["Image"] = "sha256:" + "b" * 64
    elif mismatch == "name":
        container["Name"] = "/foreign"
    elif mismatch == "mount":
        container["Mounts"] = [{"Name": "shared"}]
    elif mismatch == "network":
        container["HostConfig"]["NetworkMode"] = "bridge"
    elif mismatch == "running":
        container["State"].update(Status="running", Running=True)
    else:
        site.engine.identity = "other-engine"
    site.restart()
    assert list(site.engine.containers) == [identity]
    assert site.engine.removed == []
    assert site.record["prepare_cleanup"][site.record["id"]]["container"] == identity


def test_interruption_between_mkdir_and_inode_checkpoint(
    site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_sync = runtime.sync_dir

    def interrupt_after_mkdir(path: Path) -> None:
        if path == site.root and any(path.glob("baseline-*")):
            raise Interrupted
        original_sync(path)

    with monkeypatch.context() as patch:
        patch.setattr(runtime, "sync_dir", interrupt_after_mkdir)
        with pytest.raises(Interrupted):
            site.perform()
    scratch = site.scratch
    assert scratch.is_dir()
    assert site.record["prepare"]["scratch"] is None
    site.restart()
    site.now += ABANDONED_SCRATCH_GRACE
    site.restart()
    assert not scratch.exists()
    assert "prepare_cleanup" not in site.record


def test_durable_retirement_required_after_terminal_journal_sync_error(
    site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_sync = runtime.sync_dir

    def fail_terminal_sync(path: Path) -> None:
        if path == site.root and (site.controller.last_operation() or {}).get(
            "prepare_cleanup"
        ):
            raise OSError("journal directory sync failed")
        original_sync(path)

    with monkeypatch.context() as patch:
        patch.setattr(runtime, "sync_dir", fail_terminal_sync)
        patch.setattr(managed, "sync_dir", fail_terminal_sync)
        with pytest.raises(OSError, match="journal directory sync failed"):
            site.perform()
        assert site.scratch.is_dir()
        site.restart()
        assert site.scratch.is_dir()
        assert site.engine.removed == []
    site.restart()
    assert len(site.engine.removed) == 1
    assert not site.scratch.exists()


def test_scratch_with_nested_mount_is_preserved(
    site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    site.interrupt("prepare_inspection_created")
    scratch = site.scratch
    site.restart()
    site.now += ABANDONED_SCRATCH_GRACE
    read_bytes = Path.read_bytes

    def mountinfo(path: Path) -> bytes:
        if path == Path("/proc/self/mountinfo"):
            mount = str(scratch / "material").encode()
            return b"1 2 8:2 / " + mount + b" rw - ext4 /dev/sda2 rw\n"
        return read_bytes(path)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_bytes", mountinfo)
        site.restart()
        assert scratch.exists()
        assert "prepare_cleanup" in site.record
    site.restart()
    assert not scratch.exists()
