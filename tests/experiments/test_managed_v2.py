from __future__ import annotations

import io
import json
import tarfile
from hashlib import sha256
from pathlib import Path
from uuid import uuid4
from zipfile import ZipFile

import pytest

from kodi_lab.managed.controller import Controller
from kodi_lab.managed.packages import read_zip
from kodi_lab.managed.runtime import Refusal, Runtime
from kodi_lab.managed.snapshots import Snapshots, archive_digest


def package(
    member: str = "script.v2fixture/default.py", *, extensions: str = ""
) -> bytes:
    root = (
        "<addon id='script.v2fixture' name='V2 Fixture' version='1.0.0'>"
        "<extension point='xbmc.python.script' library='default.py'/>"
        f"{extensions}</addon>"
    )
    stream = io.BytesIO()
    with ZipFile(stream, "w") as archive:
        archive.writestr("script.v2fixture/addon.xml", root)
        archive.writestr(member, "print('inert fixture')")
    return stream.getvalue()


def test_exact_approval(tmp_path: Path) -> None:
    root = tmp_path / "trial"
    root.mkdir(mode=0o700)
    controller = Controller(root)
    approved = controller.packages.approve(package())
    with pytest.raises(Refusal, match="package_identity_mismatch"):
        controller.install(approved.id, "9.0.0", approved.digest)
    (controller.packages.approved / (approved.digest + ".zip")).write_bytes(
        package("script.v2fixture/replacement.py")
    )
    with pytest.raises(Refusal, match="package_digest_mismatch"):
        controller.packages.get(approved.digest)


def test_zip_rejects_traversal() -> None:
    with pytest.raises(Refusal, match="unsafe_package_path"):
        read_zip(package("script.v2fixture/../default.py"))


def test_binary_admission_and_explicit_atomic_workspace_replace(tmp_path: Path) -> None:
    import os

    from kodi_lab.managed.broker import Broker

    root = tmp_path / "trial"
    root.mkdir(mode=0o700)
    controller = Controller(root)
    actor = str(uuid4())
    data = package()
    digest = sha256(data).hexdigest()
    with Broker(tmp_path / "upload.sock", controller, os.getuid()) as broker:
        frame = {
            "actor": actor,
            "name": "package_upload",
            "args": {"size": len(data), "digest": digest},
        }
        with pytest.raises(Refusal, match="incomplete_package_upload"):
            broker.dispatch(frame, lambda _size: data[:-1])
        with pytest.raises(Refusal, match="invalid_broker_request"):
            broker.dispatch({**frame, "actor": "admin"}, lambda _size: data)
        with pytest.raises(Refusal, match="package_digest_mismatch"):
            broker.dispatch(frame, lambda _size: b"x" * len(data))
        assert broker.dispatch(frame, lambda _size: data)["result"] == {
            "addon": "script.v2fixture",
            "version": "1.0.0",
            "digest": digest,
        }
    with pytest.raises(Refusal, match="package_not_approved"):
        controller.perform("workspace_open", {"digest": digest}, actor=str(uuid4()))
    controller.perform("workspace_open", {"digest": digest}, actor=actor)
    current = (
        controller.actor_directory(actor) / "workspaces/script.v2fixture/default.py"
    )
    current.write_text("local change")
    with pytest.raises(Refusal, match="workspace_exists"):
        controller.perform("workspace_open", {"digest": digest}, actor=actor)
    assert current.read_text() == "local change"
    controller.perform(
        "workspace_open", {"digest": digest, "replace": "true"}, actor=actor
    )
    assert current.read_text() == "print('inert fixture')"


def test_malformed_zip_is_an_actionable_refusal() -> None:
    with pytest.raises(Refusal, match="invalid_package_zip"):
        read_zip(b"not a zip")


def test_managed_logging_does_not_inherit_daemon_local_compression(
    tmp_path: Path,
) -> None:
    root = tmp_path / "trial"
    root.mkdir(mode=0o700)
    args = Runtime(root).args()
    assert args[args.index("--log-driver") + 1] == "json-file"
    assert "max-file=1" in args
    assert "max-size=2m" in args


def test_strict_writer_and_active_gate(tmp_path: Path) -> None:
    root = tmp_path / "trial"
    root.mkdir(mode=0o700)
    controller = Controller(root)
    with pytest.raises(Refusal, match="managed_acquire_required"):
        controller.perform("managed_start", {}, actor=str(uuid4()))
    with controller.runtime.exclusive(), pytest.raises(Refusal, match="managed_busy"):
        controller.perform("managed_reset", {}, actor="admin")


def test_workspace_package_preserves_files(tmp_path: Path) -> None:
    root = tmp_path / "trial"
    root.mkdir(mode=0o700)
    controller = Controller(root)
    original = controller.packages.approve(package())
    workspace = root / "workspaces" / original.id
    workspace.mkdir()
    with ZipFile(io.BytesIO(original.data)) as archive:
        for file in original.members:
            (workspace / file).write_bytes(archive.read(original.id + "/" + file))
    packaged = controller.packages.workspace_package(original.id)
    assert packaged.members == original.members
    checksum, selected = controller.packages.publish(packaged)
    assert selected[packaged.id].digest == packaged.digest
    assert (root / "feed/addons.xml.sha256").read_text().strip() == checksum


class Volumes:
    image = "sha256:" + "a" * 64

    def __init__(self, root: Path) -> None:
        self.root = root
        self.source = root / "current"
        self.source.mkdir()
        (root / "snapshots").mkdir()
        self.retired: list[str] = []
        self.current = "current"

    def require_stopped(self) -> None:
        pass

    def capacity(self) -> str:
        return self.image

    def stage(self, clean: bool) -> str:
        assert not clean
        (self.root / "staging").mkdir()
        return "staging"

    def profile(self, slot: str | None = None) -> Path:
        return self.root / (slot or self.current)

    def exchange(self, slot: str) -> None:
        self.current = slot

    def read_pointer(self) -> str:
        return self.current

    def retire(self, slot: str) -> None:
        self.retired.append(slot)


def test_snapshot_excludes_authority_and_round_trips(tmp_path: Path) -> None:
    native = Volumes(tmp_path)
    userdata = native.source / "userdata"
    bridge = userdata / "addon_data/service.kodi_lab_bridge"
    bridge.mkdir(parents=True)
    (bridge / "state.json").write_text(json.dumps({"authority": "old"}))
    (userdata / "guisettings.xml").write_bytes(b"<settings/>")
    snapshots = Snapshots(native)
    snapshots.save("Known state")
    assert snapshots.list()[0]["name"] == "Known state"
    snapshots.load("Known state")
    assert (
        native.profile() / "userdata/guisettings.xml"
    ).read_bytes() == b"<settings/>"
    assert not (native.profile() / bridge.relative_to(native.source)).exists()
    snapshots.delete("Known state")
    assert snapshots.list() == []


def test_invalid_snapshot_never_replaces_active_profile(tmp_path: Path) -> None:
    native = Volumes(tmp_path)
    snapshots = Snapshots(native)
    with tarfile.open(snapshots.path("Unsafe"), "w:gz") as tar:
        info = tarfile.TarInfo("addons/service.kodi_lab_bridge/service.py")
        info.size = 1
        tar.addfile(info, io.BytesIO(b"x"))
    path = snapshots.path("Unsafe")
    path.with_suffix(".json").write_text(
        json.dumps({"image": native.image, "sha256": archive_digest(path)})
    )
    with pytest.raises(Refusal, match="unsafe_snapshot_archive"):
        snapshots.load("Unsafe")
    assert native.current == "current" and native.retired == ["staging"]


def test_snapshot_digest_mismatch_keeps_active_profile(tmp_path: Path) -> None:
    native = Volumes(tmp_path)
    (native.source / "userdata").mkdir()
    (native.source / "userdata/guisettings.xml").write_bytes(b"<settings/>")
    snapshots = Snapshots(native)
    snapshots.save("Validated")
    with snapshots.path("Validated").open("ab") as archive:
        archive.write(b"corruption")
    with pytest.raises(Refusal, match="snapshot_identity_mismatch"):
        snapshots.load("Validated")
    assert native.current == "current"
    assert native.retired == []


def test_controller_refuses_nonempty_initial_state(tmp_path: Path) -> None:
    root = tmp_path / "trial"
    root.mkdir(mode=0o700)
    (root / "preexisting").write_text("leave in place")
    with pytest.raises(Refusal, match="fresh_state_directory_required"):
        Controller(root)
    assert (root / "preexisting").read_text() == "leave in place"


def test_profile_keeps_reviewed_image_when_catalog_changes(tmp_path: Path) -> None:
    root = tmp_path / "trial"
    root.mkdir(mode=0o700)
    catalog = tmp_path / "reviewed-image"
    runtime = Runtime(root, reviewed_image=catalog)
    with pytest.raises(Refusal, match="reviewed_image_required"):
        _ = runtime.image
    first = "sha256:" + "a" * 64
    catalog.write_text(first)
    assert runtime.image == first
    runtime.image_pin.write_text(first)
    runtime.pointer.write_text("a" * 32)
    catalog.write_text("sha256:" + "b" * 64)
    assert runtime.image == first
    catalog.unlink()
    assert runtime.image == first


def test_stopped_skin_rewrite_preserves_other_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from xml.etree import ElementTree as ET

    from kodi_lab.managed.runtime import Runtime

    root = tmp_path / "trial"
    root.mkdir(mode=0o700)
    native = Runtime(root)
    profile = root / "volumes" / ("a" * 32)
    userdata = profile / "userdata"
    userdata.mkdir(parents=True)
    native.pointer.write_text("a" * 32)
    file = userdata / "guisettings.xml"
    file.write_text(
        "<settings><setting id='lookandfeel.skin'>skin.fixture</setting>"
        "<setting id='lookandfeel.skinzoom'>7</setting>"
        "<setting id='general.addonupdates'>2</setting></settings>"
    )
    monkeypatch.setattr(native, "require_stopped", lambda: None)
    _, saved = native.skin_settings()
    entries = [
        ET.tostring(e)
        for e in saved.getroot()
        if e.get("id", "").startswith("lookandfeel.")
    ]
    native.write_skin("skin.estuary")
    active = ET.parse(file).getroot()
    selection = active.find("setting[@id='lookandfeel.skin']")
    zoom = active.find("setting[@id='lookandfeel.skinzoom']")
    assert selection is not None and selection.text == "skin.estuary"
    assert zoom is not None
    zoom.text = "0"
    ET.ElementTree(active).write(file)
    native.write_skin("skin.fixture", saved=entries)
    assert {e.attrib["id"]: e.text for e in ET.parse(file).getroot()} == {
        "general.addonupdates": "2",
        "lookandfeel.skin": "skin.fixture",
        "lookandfeel.skinzoom": "7",
    }


def test_broker_rechecks_scope_and_closed_arguments(tmp_path: Path) -> None:
    import os

    from kodi_lab.managed.broker import Broker

    root = tmp_path / "trial"
    root.mkdir(mode=0o700)
    actor = str(uuid4())
    prepare = {"actor": actor, "name": "managed_prepare", "args": {}}
    with Broker(tmp_path / "broker.sock", Controller(root), os.getuid()) as broker:
        with pytest.raises(Refusal, match="managed_acquire_required"):
            broker.dispatch(prepare)
        with pytest.raises(Refusal, match="invalid_broker_request"):
            broker.dispatch({**prepare, "write": True})
        with pytest.raises(Refusal, match="invalid_managed_actor"):
            broker.dispatch({**prepare, "actor": "../outside"})
        with pytest.raises(Refusal, match="invalid_arguments"):
            broker.dispatch({"actor": actor, "name": "docker", "args": {"cmd": "ps"}})


def test_verified_result_survives_interrupted_reply(tmp_path: Path) -> None:
    root = tmp_path / "trial"
    root.mkdir(mode=0o700)

    def interrupt(event: str) -> None:
        if event == "reply":
            raise InterruptedError

    controller = Controller(root, interrupt=interrupt)
    controller.actions["managed_start"] = lambda: {"state": "ready"}
    with pytest.raises(InterruptedError):
        controller.perform("managed_start", {}, actor="admin")
    recorded = Controller(root).last_operation()
    assert recorded is not None and recorded["state"] == "complete"
    assert recorded["result"] == {"state": "ready"}


def test_failed_skin_recovery_stays_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "trial"
    root.mkdir(mode=0o700)
    controller = Controller(root)
    native = controller.runtime
    native.pointer.write_text("a" * 32)
    monkeypatch.setattr(native, "stop", lambda: None)
    monkeypatch.setattr(native, "write_skin", lambda *_a, **_kw: None)

    def cannot_load(_addon: str) -> str:
        raise Refusal("skin_boot_readback_unconfirmed")

    monkeypatch.setattr(native, "start_with_skin", cannot_load)
    controller.record.write_text(
        json.dumps(
            {
                "id": "test",
                "name": "addon_install",
                "subject": {},
                "state": "pending",
                "event": "estuary_active",
                "skin": {
                    "slot": "a" * 32,
                    "addon": "skin.fixture",
                    "settings": [],
                },
            }
        )
    )
    with pytest.raises(Refusal, match="skin_boot_readback_unconfirmed"):
        controller.perform("managed_recover", {}, actor="admin")
    operation = controller.last_operation()
    assert operation is not None and operation["state"] == "pending"
    assert operation["recovery_error"] == "skin_boot_readback_unconfirmed"

    monkeypatch.setattr(native, "stop", lambda: {"state": "stopped"})
    assert controller.perform("managed_stop", {}, actor="admin")["result"] == {
        "state": "stopped"
    }
    stopped = controller.last_operation()
    assert stopped is not None and stopped["skin"]["addon"] == "skin.fixture"


def test_snapshot_strips_managed_rpc_password(tmp_path: Path) -> None:
    native = Volumes(tmp_path)
    userdata = native.source / "userdata"
    userdata.mkdir()
    (userdata / "guisettings.xml").write_text(
        "<settings><setting id='services.webserverpassword'>do-not-restore</setting>"
        "<setting id='lookandfeel.skin'>skin.fixture</setting></settings>"
    )
    orphan = ".stage-" + ("a" * 32)
    (userdata / orphan).write_text("credential from an interrupted atomic write")
    snapshots = Snapshots(native)
    snapshots.save("No authority")
    snapshots.load("No authority")
    content = (native.profile() / "userdata/guisettings.xml").read_text()
    assert "webserverpassword" not in content and "skin.fixture" in content
    assert not (native.profile() / "userdata" / orphan).exists()


def test_private_namespaces_and_take_control(tmp_path: Path) -> None:
    root = tmp_path / "trial"
    root.mkdir(mode=0o700)
    controller = Controller(root)
    first, second = str(uuid4()), str(uuid4())
    controller.perform("managed_acquire", {}, actor=first)
    for actor in (second, "admin"):
        with pytest.raises(Refusal, match="managed_owned"):
            controller.perform("managed_stop", {}, actor=actor)
    controller.perform("managed_take_control", {}, actor="admin")
    with pytest.raises(Refusal, match="managed_owned"):
        controller.perform("managed_stop", {}, actor=first)

    import base64

    data = package()
    from kodi_lab.managed.packages import sha

    digest = sha(data)
    controller.perform(
        "package_import",
        {
            "encoded": base64.b64encode(data).decode(),
            "digest": digest,
        },
        actor=first,
    )
    with pytest.raises(Refusal, match="package_not_approved"):
        controller.perform("workspace_open", {"digest": digest}, actor=second)
    controller.perform("workspace_open", {"digest": digest}, actor=first)
    with pytest.raises(Refusal, match="workspace_missing"):
        controller.perform(
            "workspace_tree", {"addon": "script.v2fixture"}, actor=second
        )
    assert controller.perform(
        "workspace_tree", {"addon": "script.v2fixture"}, actor=first
    )["result"] == ["addon.xml", "default.py"]


def test_mixed_archive_admission_and_service_consent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "trial"
    root.mkdir(mode=0o700)
    controller = Controller(root)
    artifact = controller.packages.approve(
        package(
            extensions=(
                "<extension point='xbmc.service' library='service.py'/>"
                "<extension point='xbmc.python.module' library='resources/lib'/>"
            )
        )
    )
    assert artifact.category == "xbmc.python.script"
    monkeypatch.setattr(controller.runtime, "ready", lambda: {"state": "ready"})
    monkeypatch.setattr(controller, "installed", lambda _addon: None)
    with pytest.raises(Refusal, match="service_activation_consent_required"):
        controller.install(artifact.id, artifact.version, artifact.digest)


def test_takeover_recovers_interrupted_snapshot_in_original_namespace(
    tmp_path: Path,
) -> None:
    root = tmp_path / "trial"
    root.mkdir(mode=0o700)
    controller = Controller(root)
    actor = str(uuid4())
    controller.perform("managed_acquire", {}, actor=actor)
    archived = controller.actor_snapshots(actor).root
    stage = ".stage-" + ("a" * 32)
    (controller.snapshots.root / stage).write_bytes(b"unrelated admin staging")
    (archived / stage).write_bytes(b"interrupted agent snapshot")

    def interrupted_save(*, name: str) -> None:
        controller.checkpoint("snapshot_staging", snapshot=name, archive=stage)
        raise InterruptedError

    controller.actions["snapshot_save"] = interrupted_save
    with pytest.raises(InterruptedError):
        controller.perform("snapshot_save", {"name": "Partial"}, actor=actor)
    restarted = Controller(root)
    restarted.perform("managed_take_control", {}, actor="admin")
    restarted.perform("managed_recover", {}, actor="admin")
    assert not (archived / stage).exists()
    assert (restarted.snapshots.root / stage).read_bytes() == b"unrelated admin staging"


def test_human_release_waits_for_rfb_helper_exit(tmp_path: Path) -> None:
    root = tmp_path / "trial"
    root.mkdir(mode=0o700)
    controller = Controller(root)
    attempt = uuid4()
    agent = str(uuid4())

    controller.perform("managed_acquire", {}, actor=agent)
    with controller.runtime.exclusive():
        controller.begin_input(attempt)
    with pytest.raises(Refusal, match="managed_input_in_use"):
        controller.perform("managed_acquire", {}, actor=agent)

    release = controller.perform("managed_release", {}, actor="admin")
    assert release["result"] == {"owner": "admin", "release_pending": True}
    controller.finish_input(attempt, False)
    with pytest.raises(Refusal, match="managed_input_in_use"):
        controller.perform("managed_acquire", {}, actor=agent)

    controller.finish_input(attempt, True)
    assert controller.owner() is None
    assert controller.perform("managed_acquire", {}, actor=agent)["result"] == {
        "owner": agent,
        "idle_timeout_seconds": 900,
    }


def test_generation_scope_preserves_one_owner_and_separate_profiles(
    tmp_path: Path,
) -> None:
    root = tmp_path / "trial"
    root.mkdir(mode=0o700)
    handoff = tmp_path / "handoff"
    handoff.mkdir()
    controller = Controller(root, handoff_root=handoff, daemon_root=root)
    actor = str(uuid4())
    controller.perform("managed_acquire", {}, actor=actor)
    with pytest.raises(Refusal, match="managed_owned"):
        controller.perform("managed_start", {"generation": "nexus"}, actor=str(uuid4()))
    with pytest.raises(Refusal, match="invalid_arguments"):
        controller.perform("managed_status", {"generation": "../other"}, actor=actor)
    assert controller.runtimes["matrix"].pointer != controller.runtimes["nexus"].pointer
    assert len({runtime.target for runtime in controller.runtimes.values()}) == 4
    assert controller.runtime is controller.omega


def test_start_blocks_other_running_managed_version_before_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kodi_lab.managed import runtime as module

    root = tmp_path / "trial"
    root.mkdir(mode=0o700)
    runtime = Runtime(root, generation="nexus")
    monkeypatch.setattr(runtime, "capacity", lambda: "approved")
    monkeypatch.setattr(runtime, "inspect", lambda: None)

    def other_running(*args: str) -> str:
        assert args[:3] == ("ps", "-q", "--filter")
        assert "label=io.kodi-lab.managed-v2" in args
        return "running-matrix"

    monkeypatch.setattr(module, "docker", other_running)
    with pytest.raises(Refusal, match="another_managed_runtime_running"):
        runtime.start()
    assert not list(runtime.credentials.iterdir())


def test_interrupted_operation_can_only_recover_its_original_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "trial"
    root.mkdir(mode=0o700)
    controller = Controller(root, daemon_root=root, handoff_root=tmp_path)
    pending = {
        "id": uuid4().hex,
        "name": "managed_start",
        "generation": "piers",
        "subject": {},
        "state": "pending",
    }
    controller.record.write_text(json.dumps(pending))
    with pytest.raises(Refusal, match="managed_recovery_generation_mismatch"):
        controller.perform("managed_recover", {"generation": "nexus"}, actor="admin")
    assert controller.last_operation() == pending

    observed = []

    def reconcile(_record: dict[str, object]) -> None:
        observed.append(controller.runtime.generation)

    monkeypatch.setattr(controller, "reconcile", reconcile)
    result = controller.perform(
        "managed_recover", {"generation": "piers"}, actor="admin"
    )
    assert result["result"] == {
        "state": "recovered",
        "interrupted": "managed_start",
        "completion": "unconfirmed",
        "native_verification_required": False,
    }
    assert observed == ["piers"]
    assert controller.runtime is controller.omega
