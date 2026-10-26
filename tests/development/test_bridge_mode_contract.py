"""Real source, archive, destination and resident producers share execute masks."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from kodi_lab.development.deployment import (
    DevelopmentTreeError,
    build_delta_archive,
    capture_destination_archive,
    capture_development_tree,
    classify_delta,
    verify_destination,
)
from tests.kodi.test_narrow_bridge_addon import _load


@pytest.mark.parametrize(
    "execute", [0, 0o100, 0o010, 0o001, 0o110, 0o101, 0o011, 0o111]
)
def test_all_execute_masks_match_through_staging_archive_and_resident(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, execute: int
) -> None:
    root = tmp_path / "source"
    addon = root / "addon"
    addon.mkdir(parents=True)
    (addon / "addon.xml").write_text('<addon id="plugin.video.modes" version="1.0.0"/>')
    (addon / "main.py").write_text("pass\n")
    (addon / "main.py").chmod(0o644 | execute)
    (addon / "resources").mkdir()
    (addon / "resources/probe.sh").write_text("#!/bin/sh\n")
    (addon / "resources/probe.sh").chmod(0o644 | execute)
    stage = tmp_path / "stage"
    stage.mkdir()
    source = capture_development_tree(root, "addon", stage_root=stage)
    assert source.by_path()["main.py"].mode == 0o644 | execute
    assert (stage / "main.py").stat().st_mode & 0o777 == 0o644 | execute
    archive = tmp_path / "deployment.tar"
    build_delta_archive(
        stage,
        archive,
        source,
        classify_delta(source, None),
        runtime_uid=os.getuid(),
        runtime_gid=os.getgid(),
        root_name="addon",
    )
    destination = capture_destination_archive([archive.read_bytes()], root_name="addon")
    verify_destination(
        source,
        None,
        destination,
        runtime_uid=os.getuid(),
        runtime_gid=os.getgid(),
    )
    bridge = _load(monkeypatch)
    entries, digest, files, size = bridge["_tree_manifest"](str(stage))
    assert entries == json.loads(source.canonical_json)["entries"]
    assert digest == source.digest
    accepted = bridge["_validate_manifest"](
        {
            "manifest_version": 1,
            "entries": entries,
            "manifest_digest": source.digest,
            "file_count": files,
            "total_bytes": size,
        }
    )
    assert accepted[0] == entries
    # Resident direct deployment uses this exact writer with authenticated modes.
    resident = tmp_path / "resident"
    resident.mkdir()
    for entry in source.entries:
        if entry.entry_type == "directory":
            (resident / entry.path).mkdir(mode=entry.mode)
            continue
        with bridge["_deployment_file"](
            str(resident / entry.path), "xb", entry.mode
        ) as stream:
            stream.write((stage / entry.path).read_bytes())
        assert (resident / entry.path).stat().st_mode & 0o777 == entry.mode
    assert bridge["_tree_manifest"](str(resident))[1] == source.digest
    # Recovery must preserve the same partial modes without a metadata bypass.
    from kodi_lab.remote_lifecycle import _rollback_entry

    for entry in source.entries:
        metadata = _rollback_entry(
            stage / entry.path, directory=entry.entry_type == "directory"
        )
        assert metadata.st_mode & 0o777 == entry.mode


@pytest.mark.parametrize("failure", ["uid", "gid", "content", "mode", "special_bits"])
def test_ownership_content_and_actual_destination_mode_stay_exact(
    tmp_path: Path, failure: str
) -> None:
    addon = tmp_path / "addon"
    addon.mkdir()
    (addon / "main.py").write_text("pass\n")
    (addon / "main.py").chmod(0o744)
    stage = tmp_path / "stage"
    stage.mkdir()
    source = capture_development_tree(tmp_path, "addon", stage_root=stage)
    archive = tmp_path / "deployment.tar"
    build_delta_archive(
        stage,
        archive,
        source,
        classify_delta(source, None),
        runtime_uid=1000,
        runtime_gid=1000,
        root_name="addon",
    )
    destination = capture_destination_archive([archive.read_bytes()], root_name="addon")
    entry = destination.entries[0]
    if failure == "uid":
        changed = replace(entry, uid=1001)
    elif failure == "gid":
        changed = replace(entry, gid=1001)
    elif failure == "content":
        changed = replace(entry, sha256="0" * 64)
    else:
        changed = replace(entry, mode=0o755 if failure == "mode" else 0o4744)
    destination = replace(destination, entries=(changed,))
    with pytest.raises(DevelopmentTreeError):
        verify_destination(
            source, None, destination, runtime_uid=1000, runtime_gid=1000
        )


@pytest.mark.parametrize(
    "mode", [0o600, 0o666, 0o777, 0o4744, 0o2744, 0o1744, True, 420.0]
)
def test_resident_refuses_noncanonical_or_special_permission_modes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: object
) -> None:
    addon = tmp_path / "addon"
    addon.mkdir()
    (addon / "main.py").write_text("pass\n")
    source = capture_development_tree(tmp_path, "addon")
    payload = {
        "manifest_version": 1,
        "entries": [{**source.entries[0].canonical(), "mode": mode}],
        "manifest_digest": source.digest,
        "file_count": 1,
        "total_bytes": source.included_bytes,
    }
    bridge = _load(monkeypatch)
    payload["manifest_digest"] = (
        "sha256:"
        + hashlib.sha256(bridge["_manifest_bytes"](payload["entries"])).hexdigest()
    )
    with pytest.raises(ValueError, match="file entry"):
        bridge["_validate_manifest"](payload)


@pytest.mark.parametrize("unsafe", ["symlink", "hardlink", "fifo"])
def test_real_source_and_resident_both_refuse_unsafe_file_types(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unsafe: str
) -> None:
    addon = tmp_path / "addon"
    addon.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("preserve\n")
    path = addon / "main.py"
    if unsafe == "symlink":
        path.symlink_to(outside)
    elif unsafe == "hardlink":
        os.link(outside, path)
    else:
        os.mkfifo(path)
    with pytest.raises(DevelopmentTreeError):
        capture_development_tree(tmp_path, "addon")
    # The refusal itself is bounded: a FIFO must never wait for a writer.
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import sys, pytest
from tests.kodi.test_narrow_bridge_addon import _load
with pytest.MonkeyPatch.context() as patch:
    bridge = _load(patch)
    try:
        bridge['_tree_manifest'](sys.argv[1])
    except (ValueError, OSError):
        print('refused')
    else:
        raise AssertionError('unsafe tree accepted')
""",
            str(addon),
        ],
        capture_output=True,
        text=True,
        timeout=3,
        check=True,
    )
    assert result.stdout.strip() == "refused"
    assert outside.read_text() == "preserve\n"
