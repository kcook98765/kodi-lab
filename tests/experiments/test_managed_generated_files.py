import io
from pathlib import Path
from zipfile import ZipFile

import pytest

from kodi_lab.managed.controller import Controller
from kodi_lab.managed.packages import sha
from kodi_lab.managed.protocol import Refusal
from kodi_lab.managed.snapshots import Snapshots
from kodi_lab.managed.source_provenance import read_sources, write_sources
from tests.experiments.test_managed_v2 import Volumes

ADDON = "skin.fixture"
GENERATED = "1080i/generated.xml"
CONTENTS = b"<includes><include name='Generated' /></includes>"


@pytest.fixture
def prepared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Controller, Path, Path, str]:
    root = tmp_path / "lab"
    root.mkdir(mode=0o700)
    c = Controller(root)
    slot = "a" * 32
    c.runtime.pointer.write_text(slot)
    installed = root / "volumes" / slot / "addons" / ADDON
    installed.mkdir(parents=True)
    monkeypatch.setattr(c.runtime, "require_stopped", lambda: None)
    descriptor = (
        b"<addon id='skin.fixture' name='Fixture' version='1.0.0'>"
        b"<extension point='xbmc.gui.skin'/></addon>"
    )
    archive = io.BytesIO()
    original = {
        "addon.xml": descriptor,
        "1080i/Home.xml": b"<window/>",
        "obsolete.txt": b"old",
    }
    with ZipFile(archive, "w") as file:
        for name, data in original.items():
            file.writestr(f"{ADDON}/{name}", data)
            path = installed / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
    pkg = c.packages.approve(archive.getvalue())
    c.packages.open_workspace(pkg.digest)
    src = c.packages.workspaces / ADDON
    (src / "obsolete.txt").unlink()
    (src / "1080i/Home.xml").write_bytes(b"<window><controls/></window>")
    (installed / GENERATED).write_bytes(CONTENTS)
    (installed / GENERATED).chmod(0o600)
    c.record_source(ADDON, pkg.members)
    return c, src, installed, pkg.digest


def test_exact_review_preserves_generated_and_removes_old_source(
    prepared: tuple[Controller, Path, Path, str],
) -> None:
    c, src, installed, _ = prepared
    review = c.deploy(ADDON)
    assert review["state"] == "review_required"
    assert review["generated"] == {
        GENERATED: {"digest": sha(CONTENTS), "bytes": len(CONTENTS), "mode": 0o600}
    }
    assert (installed / "obsolete.txt").exists()
    result = c.deploy(ADDON, review=review["review"])
    assert result["generated_preserved"] == [GENERATED]
    assert not (installed / "obsolete.txt").exists()
    assert not (src / GENERATED).exists()
    assert (installed / GENERATED).read_bytes() == CONTENTS
    assert (installed / GENERATED).stat().st_mode & 0o777 == 0o600
    assert (installed / "1080i/Home.xml").read_bytes() == (
        src / "1080i/Home.xml"
    ).read_bytes()
    assert set(read_sources(c.runtime.root, c.runtime.read_pointer())[ADDON]) == {
        "addon.xml",
        "1080i/Home.xml",
    }
    with pytest.raises(Refusal, match="generated_review_changed"):
        c.deploy(ADDON, review=review["review"])


@pytest.mark.parametrize(
    "unsafe",
    [
        "executable",
        "symlink",
        "same_path",
        "source_ancestor",
        "runtime_ancestor",
        "stale",
    ],
)
def test_review_cannot_authorize_unsafe_or_changed_content(
    prepared: tuple[Controller, Path, Path, str],
    unsafe: str,
) -> None:
    c, src, installed, _ = prepared
    review = c.deploy(ADDON)
    failure = "unexpected_runtime_addition"
    if unsafe == "executable":
        (installed / "extra.py").write_text("pass")
    elif unsafe == "symlink":
        (installed / "extra.xml").symlink_to(installed / GENERATED)
        failure = "nonregular_tree_entry"
    elif unsafe == "same_path":
        (src / GENERATED).write_bytes(CONTENTS)
        failure = "generated_source_conflict"
    elif unsafe == "source_ancestor":
        (src / "1080i/Home.xml").unlink()
        (src / "1080i").rmdir()
        (src / "1080i").write_bytes(CONTENTS)
        failure = "generated_source_conflict"
    elif unsafe == "runtime_ancestor":
        (src / GENERATED).mkdir()
        (src / GENERATED / "x.xml").write_bytes(CONTENTS)
        failure = "generated_source_conflict"
    else:
        (installed / GENERATED).write_bytes(b"<includes/>")
        failure = "generated_review_changed"
    with pytest.raises(Refusal, match=failure):
        c.deploy(ADDON, review=review["review"])
    assert (installed / "obsolete.txt").read_bytes() == b"old"


def test_old_profile_needs_exact_reviewed_archive(
    prepared: tuple[Controller, Path, Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c, _, installed, digest = prepared
    provenance = (
        c.runtime.root / "source-provenance" / (c.runtime.read_pointer() + ".json")
    )
    provenance.unlink()
    with pytest.raises(Refusal, match="source_provenance_required"):
        c.deploy(ADDON)
    monkeypatch.setattr(c, "verify_origin", lambda _addon: None)
    assert c.deploy(ADDON, baseline=digest)["state"] == "review_required"
    (installed / "obsolete.txt").write_bytes(b"drift")
    with pytest.raises(Refusal, match="installed_source_changed"):
        c.deploy(ADDON, baseline=digest)


def test_snapshot_restores_broker_provenance(tmp_path: Path) -> None:
    native = Volumes(tmp_path)
    userdata = native.source / "userdata"
    userdata.mkdir()
    (userdata / "guisettings.xml").write_text("<settings/>")
    sources = {ADDON: {"addon.xml": sha(b"fixture")}}
    write_sources(native.root, native.read_pointer(), sources)
    snapshot = Snapshots(native)
    snapshot.save("Known source")
    snapshot.load("Known source")
    assert read_sources(native.root, native.read_pointer()) == sources
