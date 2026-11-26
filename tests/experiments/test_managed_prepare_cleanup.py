from __future__ import annotations

import json
import os
import shutil
import signal
import time
from collections.abc import Callable
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace
from typing import Self
from uuid import UUID

import pytest
from docker.errors import APIError, NotFound  # type: ignore[import-untyped]
from requests import Response
from requests.exceptions import ReadTimeout

from kodi_lab.managed import broker, prepare, runtime
from kodi_lab.managed import controller as managed
from kodi_lab.managed.controller import Controller
from kodi_lab.managed.prepare import (
    ABANDONED_SCRATCH_GRACE,
    OPERATION_LABEL,
    PREPARE_CLEANUP_BATCH,
    PREPARE_CLEANUP_CAPACITY,
    PREPARE_CLEANUP_SECONDS,
    PrepareResources,
)
from kodi_lab.managed.protocol import Document, Refusal

IMAGE = "sha256:" + "a" * 64


class Interrupted(BaseException):
    """Leave the on-disk journal at the interrupted checkpoint."""


class Engine:
    def __init__(self) -> None:
        self.identity = "test-engine"
        self.timeout = 35.0
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
            case ("ps", "-aq", "--filter", _) | ("network", "ls", "-q", *_):
                return ""
            case (
                "volume",
                "create",
                "--label",
                label,
                "--label",
                allocation,
                *_,
                device,
                name,
            ):
                assert device.startswith("device=")
                self.volumes.setdefault(
                    name,
                    {
                        "Labels": dict(
                            item.split("=", 1) for item in (label, allocation)
                        ),
                        "Options": {"device": device.removeprefix("device=")},
                    },
                )
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
        monkeypatch.setattr(PrepareResources, "_client", self.client)
        monkeypatch.setattr(
            managed,
            "time",
            SimpleNamespace(time=lambda: self.now, monotonic=time.monotonic),
        )
        self.controller = Controller(self.root, reviewed_image=self.image)

    def client(self, *, timeout: float = 35) -> Engine:
        self.engine.timeout = timeout
        return self.engine

    def perform(self, action: str = "managed_prepare", **args: str) -> Document:
        try:
            result = self.controller.perform(action, args, actor="admin")["result"]
        finally:
            if action in {"managed_prepare", "managed_recover"}:
                self.controller.resume_prepare_cleanup()
        assert isinstance(result, dict)
        return result

    def restart(self) -> None:
        self.controller = Controller(self.root, reviewed_image=self.image)
        self.controller.resume_interrupted_prepare()
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
    assert scratch.is_dir()
    assert site.record["prepare_cleanup"][site.record["id"]]["scratch"] is None
    scratch.rmdir()  # Simulate administrator resolution of the ambiguous allocation.
    site.restart()
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


def test_intent_only_preserves_unrelated_empty_directory(site: Site) -> None:
    site.interrupt("prepare_staging")
    scratch = site.scratch
    assert site.record["prepare"]["scratch"] is None
    assert not scratch.exists()
    scratch.mkdir(mode=0o700)
    site.restart()
    site.now += ABANDONED_SCRATCH_GRACE
    site.restart()
    assert scratch.is_dir() and not list(scratch.iterdir())
    assert site.record["prepare_cleanup"][site.record["id"]]["scratch"] is None


@pytest.mark.parametrize(
    "action", ["managed_prepare", "managed_reset", "snapshot_load"]
)
def test_profile_collision_preserves_directory(
    site: Site, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    args: dict[str, str] = {}
    native = site.controller.runtime
    if action != "managed_prepare":
        site.perform()
        if action == "snapshot_load":
            args = {"name": "Preserved"}
            site.perform("snapshot_save", **args)
    active = native.read_pointer() if native.pointer.exists() else None
    slot = UUID("f" * 32)
    profile = site.root / "volumes" / slot.hex
    profile.mkdir(mode=0o700)
    sentinel = profile / "keep"
    sentinel.write_text("foreign")
    monkeypatch.setattr(runtime, "uuid4", lambda: slot)
    with pytest.raises(Refusal, match="profile_staging_collision"):
        site.perform(action, **args)
    site.perform("managed_recover")
    assert sentinel.read_text() == "foreign"
    assert native.volume(slot.hex) not in site.engine.volumes
    assert (native.read_pointer() if native.pointer.exists() else None) == active
    assert site.record["state"] == "complete"


def test_profile_mkdir_interrupted_before_receipt(
    site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = runtime.sync_dir

    def interrupt(path: Path) -> None:
        if path == site.root / "volumes":
            raise Interrupted
        original(path)

    with monkeypatch.context() as patch:
        patch.setattr(runtime, "sync_dir", interrupt)
        with pytest.raises(Interrupted):
            site.perform()
    assert site.record["slot_inode"] is None
    profile = site.root / "volumes" / site.record["slot"]
    assert profile.is_dir()
    site.restart()
    assert profile.is_dir()
    assert not site.engine.volumes
    assert site.record["state"] == "complete"


@pytest.mark.parametrize("kind", ["scratch", "profile"])
def test_failure_to_record_allocation_preserves_directory(
    site: Site, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    original = site.controller.write_operation
    fail = {"scratch": "prepare_staging_allocated", "profile": "profile_allocated"}[
        kind
    ]

    def reject(record: Document) -> None:
        if record["event"] == fail:
            raise OSError("allocation receipt not persisted")
        original(record)

    with monkeypatch.context() as patch:
        patch.setattr(site.controller, "write_operation", reject)
        with pytest.raises(OSError, match="allocation receipt not persisted"):
            site.perform()
    if kind == "scratch":
        assert site.scratch.is_dir()
        assert site.record["prepare_cleanup"][site.record["id"]]["scratch"] is None
    else:
        assert (site.root / "volumes" / site.record["slot"]).is_dir()
        assert site.record["slot_inode"] is None
    assert site.record["state"] == "complete"
    assert not site.engine.volumes


def test_legacy_profile_intent_preserves_unverified_directory(site: Site) -> None:
    slot = "a" * 32
    detached = site.root / "volumes" / slot
    detached.mkdir(mode=0o700)
    (detached / "keep").write_text("manual")
    site.controller.write_operation(
        {
            "id": "b" * 32,
            "name": "managed_reset",
            "slot": slot,
            "subject": {},
            "event": "profile_staging",
            "state": "pending",
        }
    )
    assert site.perform("managed_recover")["state"] == "recovered"
    assert (detached / "keep").read_text() == "manual"


def test_failed_prepare_before_pointer_preserves_prior_baseline(
    site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    native = site.controller.runtime
    native.material.mkdir(mode=0o700)
    prior = native.material / "prior"
    prior.write_text("valid seed")

    def refuse(event: str) -> None:
        if event == "profile_before_commit":
            raise Refusal("cannot_commit")

    site.controller.interrupt = refuse
    with pytest.raises(Refusal, match="cannot_commit"):
        site.perform()
    assert prior.read_text() == "valid seed"
    assert not native.pointer.exists()
    assert site.record["event"] == "prepare_failed"
    assert not site.engine.volumes
    assert not site.scratch.exists()
    site.controller.interrupt = lambda _event: None
    assert site.perform()["state"] == "prepared"
    assert (native.material / "manifest.xml").is_file()


@pytest.mark.parametrize("persistent", [False, True])
def test_publish_error_after_pointer_records_committed_outcome(
    site: Site, monkeypatch: pytest.MonkeyPatch, persistent: bool
) -> None:
    native = site.controller.runtime
    native.material.mkdir(mode=0o700)
    old = native.material / "prior"
    old.write_text("valid seed")
    rename = Path.rename
    failures = 0

    def fail_publication(path: Path, target: Path) -> Path:
        nonlocal failures
        if target == native.material and (persistent or failures == 0):
            failures += 1
            raise OSError("baseline publication failure")
        return rename(path, target)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "rename", fail_publication)
        if persistent:
            with pytest.raises(OSError, match="baseline publication failure"):
                site.perform()
        else:
            assert site.perform() == {"state": "prepared", "profile": "omega"}
    assert failures == (2 if persistent else 1)
    pointer = native.read_pointer()
    assert native.require_volume(pointer) == native.profile()
    if persistent:
        assert site.record["state"] == "pending"
        assert (site.scratch / "prior-material" / old.name).read_text() == "valid seed"
        assert site.perform("managed_recover")["completion"] == "committed"
    else:
        assert site.record["event"] == "reply"
        assert site.record["result"]["state"] == "prepared"
    assert site.record["state"] == "complete"
    assert not old.exists()
    assert (native.material / "manifest.xml").is_file()
    assert native.read_pointer() == pointer
    assert not site.scratch.exists()
    marker = native.profile() / "keep"
    marker.write_text("working")
    snapshot = site.perform("snapshot_save", name="Recovered")
    archive_bytes = site.controller.snapshots.path("Recovered").read_bytes()
    assert site.perform("managed_reset")["state"] == "stopped"
    assert not (native.profile() / marker.name).exists()
    assert site.perform("snapshot_load", name="Recovered")["state"] == "restored"
    assert (native.profile() / marker.name).read_text() == "working"
    assert (
        site.controller.snapshots.path(snapshot["name"]).read_bytes() == archive_bytes
    )


def test_retirement_write_failure_keeps_successful_reply(
    site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = site.controller.write_operation

    def reject_removal_write(record: Document) -> None:
        previous = site.controller.last_operation()
        if previous and previous.get("prepare_cleanup"):
            raise OSError("failed to persist retirement")
        original(record)

    with monkeypatch.context() as patch:
        patch.setattr(site.controller, "write_operation", reject_removal_write)
        assert site.perform()["state"] == "prepared"
        assert site.record["state"] == "complete"
        assert site.record["prepare_cleanup"]
    site.restart()
    assert not site.engine.containers
    assert "prepare_cleanup" not in site.record


def test_six_ambiguous_dispatches_bound_admission_and_maintenance(
    site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts = 0

    def unknown_create(*_args: object, **_kwargs: object) -> Document:
        nonlocal attempts
        attempts += 1
        raise ReadTimeout("simulated lost Create reply")

    monkeypatch.setattr(site.engine, "create_container", unknown_create)
    for _ in range(6):
        with pytest.raises(ReadTimeout):
            site.perform()
    assert (
        attempts == len(site.record["prepare_cleanup"]) == PREPARE_CLEANUP_CAPACITY == 6
    )
    with pytest.raises(Refusal, match="prepare_cleanup_capacity"):
        site.perform()
    assert attempts == 6
    assert site.perform("managed_status")["state"] == "unprepared"
    assert site.perform("managed_stop")["state"] == "stopped"
    assert site.perform("managed_recover")["state"] == "no_recovery_required"

    clock = SimpleNamespace(seconds=100.0)
    time_source = SimpleNamespace(
        time=lambda: site.now, monotonic=lambda: clock.seconds
    )
    monkeypatch.setattr(managed, "time", time_source)
    monkeypatch.setattr(prepare, "time", time_source)
    calls: list[float] = []

    def slow_client(*, timeout: float) -> Engine:
        assert 0 < timeout <= PREPARE_CLEANUP_SECONDS
        calls.append(timeout)
        clock.seconds += timeout
        raise ReadTimeout("simulated Engine timeout")

    monkeypatch.setattr(PrepareResources, "_client", staticmethod(slow_client))
    obligations = site.record["prepare_cleanup"]
    first = sorted(obligations)[0]
    second = sorted(obligations)[1]
    site.controller = Controller(site.root, reviewed_image=site.image)
    site.controller.resume_prepare_cleanup()
    assert calls == [PREPARE_CLEANUP_SECONDS]
    assert site.controller._prepare_cleanup_cursor == first
    site.controller.resume_prepare_cleanup()
    assert calls == [PREPARE_CLEANUP_SECONDS] * 2
    assert site.controller._prepare_cleanup_cursor == second
    assert site.record["prepare_cleanup"] == obligations
    calls.clear()

    def unavailable_client(*, timeout: float) -> Engine:
        calls.append(timeout)
        raise OSError("simulated unavailable Engine")

    monkeypatch.setattr(PrepareResources, "_client", staticmethod(unavailable_client))
    site.controller.resume_prepare_cleanup()
    assert len(calls) == PREPARE_CLEANUP_BATCH


def test_maintenance_shares_budget_across_docker_requests(
    site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    site.engine.fail_removal = True
    site.perform()
    site.engine.fail_removal = False
    identity = next(iter(site.engine.containers))
    clock = SimpleNamespace(seconds=100.0)
    time_source = SimpleNamespace(
        time=lambda: site.now, monotonic=lambda: clock.seconds
    )
    monkeypatch.setattr(managed, "time", time_source)
    monkeypatch.setattr(prepare, "time", time_source)
    info = site.engine.info
    inspect = site.engine.inspect_container

    def client(*, timeout: float) -> Engine:
        assert timeout == PREPARE_CLEANUP_SECONDS
        clock.seconds += 0.25
        return site.client(timeout=timeout)

    def slow_info() -> Document:
        assert site.engine.timeout == 0.75
        clock.seconds += 0.25
        return info()

    def slow_inspect(identity: str) -> Document:
        assert site.engine.timeout == 0.5
        clock.seconds += 0.5
        return inspect(identity)

    with monkeypatch.context() as patch:
        patch.setattr(PrepareResources, "_client", staticmethod(client))
        patch.setattr(site.engine, "info", slow_info)
        patch.setattr(site.engine, "inspect_container", slow_inspect)
        site.controller.resume_prepare_cleanup()
    assert identity in site.engine.containers
    assert site.record["prepare_cleanup"]
    site.controller.resume_prepare_cleanup()
    assert not site.engine.containers
    assert "prepare_cleanup" not in site.record


@pytest.mark.parametrize("unsupported", [None, ["private-value"], "private-value", 0])
def test_unsupported_collection_survives_startup_and_operations(
    site: Site, caplog: pytest.LogCaptureFixture, unsupported: object
) -> None:
    site.controller.write_operation(
        {"state": "complete", "prepare_cleanup": unsupported}
    )
    site.restart()
    assert site.perform("managed_status")["state"] == "unprepared"
    assert site.perform("managed_stop")["state"] == "stopped"
    assert site.perform("managed_recover")["state"] == "no_recovery_required"
    with pytest.raises(Refusal, match="prepare_cleanup_collection_invalid"):
        site.perform()
    assert site.record["prepare_cleanup"] == unsupported
    assert "prepare_cleanup_collection_invalid" in caplog.text
    assert "private-value" not in caplog.text


def test_mixed_cleanup_receipts_preserve_invalid_and_unsupported_entries(
    site: Site, caplog: pytest.LogCaptureFixture
) -> None:
    def scratch(key: str) -> tuple[Path, Document]:
        path = site.root / ("baseline-" + key)
        path.mkdir(mode=0o700)
        inode, allocation = runtime.mark_allocation(path)
        return path, {
            "generation": "omega",
            "scratch_after": 0,
            "scratch": inode,
            "scratch_allocation": allocation,
            "target": str(site.controller.runtime.target),
            "image": IMAGE,
        }

    other, unsupported = scratch("a" * 32)
    unsupported["generation"] = "private-future-runtime"
    unknown, newer = scratch("b" * 32)
    newer["version"] = 2
    own, valid = scratch("c" * 32)
    malformed, invalid = scratch("d" * 32)
    invalid["scratch"] = str(invalid["scratch"])
    no_engine, dangling = scratch("e" * 32)
    dangling["container"] = "0" * 64
    preserved = {
        "a" * 32: unsupported,
        "b" * 32: newer,
        "d" * 32: invalid,
        "e" * 32: dangling,
        "f" * 32: ["invalid"],
    }
    site.controller.write_operation(
        {"state": "complete", "prepare_cleanup": {**preserved, "c" * 32: valid}}
    )
    site.restart()
    assert own.exists()
    site.controller.resume_prepare_cleanup()
    assert not own.exists()
    site.controller.resume_prepare_cleanup()
    assert all(path.exists() for path in (other, unknown, malformed, no_engine))
    assert site.record["prepare_cleanup"] == preserved
    site.perform("managed_stop")
    assert site.record["prepare_cleanup"] == preserved
    assert "prepare_cleanup_generation_unsupported" in caplog.text
    assert "prepare_cleanup_receipt_invalid" in caplog.text
    assert "private-future-runtime" not in caplog.text


@pytest.mark.parametrize("recover_on_startup", [True, False])
def test_interrupted_public_prepare_revokes_session(
    site: Site, recover_on_startup: bool
) -> None:
    native = site.controller.runtime
    native.public.session.write_text(str(time.time() + 900))
    site.interrupt("profile_committed")
    assert native.public.session.is_file()
    if recover_on_startup:
        site.restart()
    else:
        assert site.perform("managed_recover")["completion"] == "committed"
    assert not native.public.session.exists()
    assert native.public_slot.read_text() == native.read_pointer()
    assert (native.material / "manifest.xml").is_file()
    assert site.record["state"] == "complete"


def test_broker_binds_before_maintenance(
    site: Site, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "broker.sock"
    stop = Event()
    recovery_started = Event()
    recovery_finished = Event()

    def recover() -> None:
        if path.is_socket():
            recovery_started.set()
        stop.wait(3)
        recovery_finished.set()

    def maintenance() -> None:
        assert path.is_socket()
        assert recovery_started.wait(2)
        assert not recovery_finished.is_set()
        stop.set()

    monkeypatch.setattr(site.controller, "resume_interrupted_prepare", recover)
    monkeypatch.setattr(site.controller, "resume_prepare_cleanup", maintenance)
    monkeypatch.setattr(broker, "Controller", lambda *_args, **_kwargs: site.controller)
    monkeypatch.setattr(broker, "Event", lambda: stop)
    monkeypatch.setattr(signal, "signal", lambda *_args: None)
    monkeypatch.setattr(
        broker,
        "display_lane",
        lambda *_args: SimpleNamespace(start=lambda _: None, close=lambda: None),
    )
    frontend_uid = os.getuid() + 1
    broker.serve(site.root, path, frontend_uid, os.getgid(), site.root)
    assert stop.is_set()
    assert recovery_finished.wait(2)
    assert not path.exists()


def test_replaced_staged_profile_is_preserved(site: Site) -> None:
    site.interrupt("profile_before_commit")
    slot = site.record["slot"]
    path = site.root / "volumes" / slot
    moved = path.rename(path.with_name("parked-profile"))
    path.mkdir(mode=0o700)
    (path / "keep").write_text("replacement")
    site.restart()
    assert site.record["state"] == "pending"
    assert site.record["recovery_error"] == "profile_staging_identity"
    assert (path / "keep").read_text() == "replacement"
    assert moved.is_dir()
    assert site.controller.runtime.volume(slot) in site.engine.volumes


def test_cleanup_capacity_does_not_block_prepared_profile(site: Site) -> None:
    site.perform()
    native = site.controller.runtime
    prior = native.read_pointer()
    record = site.record
    record["prepare_cleanup"] = {
        f"{i:032x}": {} for i in range(PREPARE_CLEANUP_CAPACITY)
    }
    site.controller.write_operation(record)
    assert site.perform()["state"] == "prepared"
    assert native.read_pointer() == prior
    assert site.engine.creates == 1
    assert len(site.record["prepare_cleanup"]) == PREPARE_CLEANUP_CAPACITY


@pytest.mark.parametrize("resource", ["scratch", "material", "profile"])
def test_recycled_directory_inode_cannot_authorize_prepare(
    site: Site, resource: str
) -> None:
    checkpoint = {
        "scratch": "prepare_inspection_created",
        "material": "profile_committed",
        "profile": "profile_before_commit",
    }[resource]
    site.interrupt(checkpoint)
    record = site.record
    if resource == "profile":
        directory = site.root / "volumes" / record["slot"]
    elif resource == "material":
        directory = site.scratch / "material"
    else:
        directory = site.scratch
    shutil.rmtree(directory)
    directory.mkdir(mode=0o700)
    (directory / "keep").write_text("replacement")
    # Reproduce the inode equality even on filesystems which do not reuse it.
    if resource == "profile":
        record["slot_inode"] = directory.lstat().st_ino
    else:
        record["prepare"][resource] = directory.lstat().st_ino
    site.controller.write_operation(record)
    site.now += ABANDONED_SCRATCH_GRACE
    site.restart()
    site.now += ABANDONED_SCRATCH_GRACE
    site.controller.resume_prepare_cleanup()
    assert (directory / "keep").read_text() == "replacement"
    if resource != "scratch":
        assert site.record["state"] == "pending"


def test_colliding_volume_without_allocation_label_survives(site: Site) -> None:
    native = site.controller.runtime

    def collide(event: str) -> None:
        if event == "profile_allocated":
            slot = site.record["slot"]
            site.engine.volumes[native.volume(slot)] = {
                "Labels": {runtime.LABEL: str(native.target)},
                "Options": {"device": str(native.daemon_path(native.profile(slot)))},
            }

    site.controller.interrupt = collide
    with pytest.raises(Refusal, match="profile_staging_identity"):
        site.perform()
    site.restart()
    assert not native.pointer.exists()
    assert site.record["state"] == "pending"
    assert native.volume(site.record["slot"]) in site.engine.volumes


@pytest.mark.parametrize("resource", ["engine", "scratch"])
def test_slow_terminal_cleanup_leaves_lifecycle_available(
    site: Site, monkeypatch: pytest.MonkeyPatch, resource: str
) -> None:
    # Exercise the controller directly so its reply is independent of the
    # simulated broker maintenance pass in the helper.
    assert site.controller.perform("managed_prepare", {}, actor="admin")["result"] == {
        "state": "prepared",
        "profile": "omega",
    }
    assert site.record["prepare_cleanup"]
    entered, release = Event(), Event()
    removed = PrepareResources.retire_scratch
    if resource == "engine":
        remove_engine = PrepareResources.retire_inspection

        def retire_engine(
            resources: PrepareResources, persist: Callable[[], None], *, deadline: float
        ) -> None:
            entered.set()
            assert release.wait(5)
            remove_engine(resources, persist, deadline=deadline)

        monkeypatch.setattr(PrepareResources, "retire_inspection", retire_engine)
    else:

        def retire_scratch(resources: PrepareResources) -> None:
            entered.set()
            assert release.wait(5)
            removed(resources)

        monkeypatch.setattr(PrepareResources, "retire_scratch", retire_scratch)
    maintenance = Thread(target=site.controller.resume_prepare_cleanup, daemon=True)
    maintenance.start()
    try:
        assert entered.wait(2)
        assert site.perform("managed_status")["state"] == "stopped"
        assert site.perform("managed_stop")["state"] == "stopped"
        stop = site.record["id"]
    finally:
        release.set()
        maintenance.join(5)
    assert not maintenance.is_alive()
    assert site.record["id"] == stop
    assert site.record["name"] == "managed_stop"
    assert "prepare_cleanup" not in site.record


@pytest.mark.parametrize(
    "action", ["managed_prepare", "managed_reset", "snapshot_load"]
)
def test_recovery_does_not_accept_replaced_committed_slot(
    site: Site, action: str
) -> None:
    native = site.controller.runtime
    args: dict[str, str] = {}
    if action != "managed_prepare":
        site.perform()
        if action == "snapshot_load":
            args = {"name": "Safe"}
            site.perform("snapshot_save", **args)
    else:
        native.public.session.write_text(str(time.time() + 900))
    before = native.profile() if native.pointer.exists() else None

    def interrupt(checkpoint: str) -> None:
        if checkpoint == "profile_committed":
            raise Interrupted

    site.controller.interrupt = interrupt
    with pytest.raises(Interrupted):
        site.perform(action, **args)
    selected = native.profile()
    selected.rename(selected.with_name("parked-after-commit"))
    selected.mkdir()
    kept = selected / "keep"
    kept.write_text("foreign")
    site.restart()
    if action != "managed_prepare":
        with pytest.raises(Refusal, match="profile_staging_identity"):
            site.perform("managed_recover")
    assert site.record["state"] == "pending"
    assert site.record["recovery_error"] == "profile_staging_identity"
    assert list(selected.iterdir()) == [kept]
    assert kept.read_text() == "foreign"
    assert before is None or before.exists()
    assert not native.public.session.exists()
