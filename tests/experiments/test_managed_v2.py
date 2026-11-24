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
