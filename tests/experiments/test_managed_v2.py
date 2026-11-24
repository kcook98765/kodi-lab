import io
import json
import tarfile
from pathlib import Path
from zipfile import ZipFile

import pytest

from experiments.managed_v2.controller import Controller
from experiments.managed_v2.packages import read_zip
from experiments.managed_v2.runtime import IMAGE, Refusal
from experiments.managed_v2.snapshots import Snapshots, archive_digest


def package(member="script.v2fixture/default.py"):
    root = (
        "<addon id='script.v2fixture' name='V2 Fixture' version='1.0.0'>"
        "<extension point='xbmc.python.script' library='default.py'/></addon>"
    )
    stream = io.BytesIO()
    with ZipFile(stream, "w") as archive:
        archive.writestr("script.v2fixture/addon.xml", root)
        archive.writestr(member, "print('inert fixture')")
    return stream.getvalue()


def test_exact_approval(tmp_path):
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


def test_zip_rejects_traversal():
    with pytest.raises(Refusal, match="unsafe_package_path"):
        read_zip(package("script.v2fixture/../default.py"))


def test_strict_writer_and_active_gate(tmp_path):
    root = tmp_path / "trial"
    root.mkdir(mode=0o700)
    controller = Controller(root)
    with pytest.raises(Refusal, match="managed_write_denied"):
        controller.perform("addon_install", {}, write=False)
    with controller.runtime.exclusive(), pytest.raises(Refusal, match="managed_busy"):
        controller.perform("managed_reset", {}, write=True)


def test_workspace_package_preserves_files(tmp_path):
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
    def __init__(self, root: Path):
        self.root = root
        self.source = root / "current"
        self.source.mkdir()
        (root / "snapshots").mkdir()
        self.retired = []
        self.current = "current"

    def require_stopped(self):
        pass

    def capacity(self):
        pass

    def stage(self, *, clean):
        assert not clean
        (self.root / "staging").mkdir()
        return "staging"

    def profile(self, slot=None):
        return self.root / (slot or self.current)

    def exchange(self, slot):
        self.current = slot

    def read_pointer(self):
        return self.current

    def retire(self, slot):
        self.retired.append(slot)


def test_snapshot_excludes_authority_and_round_trips(tmp_path):
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


def test_invalid_snapshot_never_replaces_active_profile(tmp_path):
    native = Volumes(tmp_path)
    snapshots = Snapshots(native)
    with tarfile.open(snapshots.path("Unsafe"), "w:gz") as tar:
        info = tarfile.TarInfo("addons/service.kodi_lab_bridge/service.py")
        info.size = 1
        tar.addfile(info, io.BytesIO(b"x"))
    path = snapshots.path("Unsafe")
    path.with_suffix(".json").write_text(
        json.dumps({"image": IMAGE, "sha256": archive_digest(path)})
    )
    with pytest.raises(Refusal, match="unsafe_snapshot_archive"):
        snapshots.load("Unsafe")
    assert native.current == "current" and native.retired == ["staging"]


def test_snapshot_digest_mismatch_keeps_active_profile(tmp_path):
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


def test_controller_refuses_nonempty_initial_state(tmp_path):
    root = tmp_path / "trial"
    root.mkdir(mode=0o700)
    (root / "preexisting").write_text("leave in place")
    with pytest.raises(Refusal, match="fresh_state_directory_required"):
        Controller(root)
    assert (root / "preexisting").read_text() == "leave in place"


def test_stopped_skin_rewrite_preserves_other_settings(tmp_path, monkeypatch):
    from xml.etree import ElementTree as ET

    from experiments.managed_v2.runtime import Runtime

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
    assert active.find("setting[@id='lookandfeel.skin']").text == "skin.estuary"
    active.find("setting[@id='lookandfeel.skinzoom']").text = "0"
    ET.ElementTree(active).write(file)
    native.write_skin("skin.fixture", saved=entries)
    assert {e.attrib["id"]: e.text for e in ET.parse(file).getroot()} == {
        "general.addonupdates": "2",
        "lookandfeel.skin": "skin.fixture",
        "lookandfeel.skinzoom": "7",
    }


def test_broker_rechecks_scope_and_closed_arguments(tmp_path):
    import os

    from experiments.managed_v2.broker import Broker

    root = tmp_path / "trial"
    root.mkdir(mode=0o700)
    token_request = {"token": "reader", "name": "managed_reset", "args": {}}
    with Broker(
        tmp_path / "broker.sock", Controller(root), os.getuid(), ("owner", "reader")
    ) as broker:
        with pytest.raises(Refusal, match="managed_write_denied"):
            broker.dispatch(token_request)
        with pytest.raises(Refusal, match="invalid_broker_request"):
            broker.dispatch({**token_request, "write": True})
        with pytest.raises(Refusal, match="broker_access_denied"):
            broker.dispatch({**token_request, "token": "wrong"})
        with pytest.raises(Refusal, match="invalid_arguments"):
            broker.dispatch({"token": "owner", "name": "docker", "args": {"cmd": "ps"}})


def test_verified_result_survives_interrupted_reply(tmp_path):
    root = tmp_path / "trial"
    root.mkdir(mode=0o700)

    def interrupt(event):
        if event == "reply":
            raise InterruptedError

    controller = Controller(root, interrupt=interrupt)
    controller.actions["managed_start"] = lambda: {"state": "ready"}
    with pytest.raises(InterruptedError):
        controller.perform("managed_start", {}, write=True)
    recorded = Controller(root).last_operation()
    assert recorded["state"] == "complete"
    assert recorded["result"] == {"state": "ready"}


def test_failed_skin_recovery_stays_pending(tmp_path, monkeypatch):
    root = tmp_path / "trial"
    root.mkdir(mode=0o700)
    controller = Controller(root)
    native = controller.runtime
    native.pointer.write_text("a" * 32)
    monkeypatch.setattr(native, "stop", lambda: None)
    monkeypatch.setattr(native, "write_skin", lambda *_a, **_kw: None)

    def cannot_load(_addon):
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
        controller.perform("managed_recover", {}, write=True)
    assert controller.last_operation()["state"] == "pending"
    assert (
        controller.last_operation()["recovery_error"]
        == "skin_boot_readback_unconfirmed"
    )

    monkeypatch.setattr(native, "stop", lambda: {"state": "stopped"})
    assert controller.perform("managed_stop", {}, write=True)["result"] == {
        "state": "stopped"
    }
    assert controller.last_operation()["skin"]["addon"] == "skin.fixture"


def test_snapshot_strips_managed_rpc_password(tmp_path):
    native = Volumes(tmp_path)
    userdata = native.source / "userdata"
    userdata.mkdir()
    (userdata / "guisettings.xml").write_text(
        "<settings><setting id='services.webserverpassword'>do-not-restore</setting>"
        "<setting id='lookandfeel.skin'>skin.fixture</setting></settings>"
    )
    snapshots = Snapshots(native)
    snapshots.save("No authority")
    snapshots.load("No authority")
    content = (native.profile() / "userdata/guisettings.xml").read_text()
    assert "webserverpassword" not in content and "skin.fixture" in content
