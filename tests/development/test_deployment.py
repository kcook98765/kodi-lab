from __future__ import annotations

import io
import os
import socket
import stat
import tarfile
from pathlib import Path

import pytest

import kodi_lab.development.deployment as deployment
from kodi_lab.development.deployment import (
    DevelopmentTreeEntry,
    DevelopmentTreeError,
    DevelopmentTreeManifest,
    build_delta_archive,
    capture_destination_archive,
    capture_development_tree,
    classify_delta,
    compare_captures,
    verify_destination,
)


def _source(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "development"
    source = root / "addon"
    (source / "resources").mkdir(parents=True)
    (source / "addon.xml").write_text(
        '<addon id="plugin.video.example" name="Example" version="1.0.0"/>'
    )
    executable = source / "resources" / "run.sh"
    executable.write_text("#!/bin/sh\n")
    executable.chmod(0o775)
    return root, source


def _manifest(*entries: DevelopmentTreeEntry) -> DevelopmentTreeManifest:
    ordered = tuple(sorted(entries, key=lambda item: item.path.encode()))
    canonical = deployment._canonical_manifest(
        ordered, include_ownership=any(item.uid is not None for item in ordered)
    )
    return DevelopmentTreeManifest(ordered, "sha256:test", canonical, 0)


def _destination_archive(
    root: str, members: list[tuple[str, bytes | None, int, int, int]]
) -> bytes:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        root_info = tarfile.TarInfo(root)
        root_info.type = tarfile.DIRTYPE
        root_info.mode = 0o755
        archive.addfile(root_info)
        for relative, content, mode, uid, gid in members:
            info = tarfile.TarInfo(f"{root}/{relative}")
            info.mode = mode
            info.uid = uid
            info.gid = gid
            if content is None:
                info.type = tarfile.DIRTYPE
                archive.addfile(info)
            else:
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
    return stream.getvalue()


def test_recursive_capture_is_deterministic_normalized_and_excludes_only_vcs(
    tmp_path: Path,
) -> None:
    root, source = _source(tmp_path)
    (source / ".git" / "objects").mkdir(parents=True)
    (source / ".git" / "objects" / "secret").write_text("excluded")
    (source / ".github").mkdir()
    (source / ".github" / "workflow.yml").write_text("included")
    (source / ".gitignore").write_text("resources\n")
    first = capture_development_tree(root, "addon")
    second = capture_development_tree(root, "addon")
    assert first.canonical_json == second.canonical_json
    assert first.digest == second.digest
    paths = {entry.path for entry in first.entries}
    assert not any(path == ".git" or path.startswith(".git/") for path in paths)
    assert {".github", ".github/workflow.yml", ".gitignore"} <= paths
    entries = first.by_path()
    assert entries["resources"].mode == 0o755
    assert entries["addon.xml"].mode == 0o644
    assert entries["resources/run.sh"].mode == 0o755


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "socket"])
def test_recursive_capture_rejects_links_and_special_files(
    tmp_path: Path, kind: str
) -> None:
    root, source = _source(tmp_path)
    unsafe = source / "unsafe"
    if kind == "symlink":
        unsafe.symlink_to(source / "addon.xml")
    elif kind == "hardlink":
        os.link(source / "addon.xml", unsafe)
    elif kind == "fifo":
        os.mkfifo(unsafe)
    else:
        unix_socket = socket.socket(socket.AF_UNIX)
        unix_socket.bind(str(unsafe))
    with pytest.raises(DevelopmentTreeError) as captured:
        capture_development_tree(root, "addon")
    if kind == "socket":
        unix_socket.close()
    assert captured.value.code == "development_source_unsafe_entry"


def test_vcs_name_is_not_silently_excluded_when_it_is_unsafe(tmp_path: Path) -> None:
    root, source = _source(tmp_path)
    (source / ".git").symlink_to(source / "resources", target_is_directory=True)
    with pytest.raises(DevelopmentTreeError) as captured:
        capture_development_tree(root, "addon")
    assert captured.value.code == "development_source_unsafe_entry"


@pytest.mark.parametrize("file_type", [stat.S_IFBLK, stat.S_IFCHR])
def test_recursive_capture_rejects_block_and_character_devices(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    file_type: int,
) -> None:
    root, source = _source(tmp_path)
    (source / "unsafe-device").write_bytes(b"")
    real_stat = os.stat

    def device_stat(
        path: str | bytes | int,
        *,
        dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> os.stat_result:
        result = real_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)
        if path == "unsafe-device":
            values = list(result)
            values[0] = file_type | 0o600
            return os.stat_result(values)
        return result

    monkeypatch.setattr(os, "stat", device_stat)
    with pytest.raises(DevelopmentTreeError) as captured:
        capture_development_tree(root, "addon")
    assert captured.value.code == "development_source_unsafe_entry"


@pytest.mark.parametrize("name", ["bad\\name", "bad\x01name"])
def test_recursive_capture_rejects_prohibited_names(tmp_path: Path, name: str) -> None:
    root, source = _source(tmp_path)
    (source / name).write_text("unsafe")
    with pytest.raises(DevelopmentTreeError) as captured:
        capture_development_tree(root, "addon")
    assert captured.value.code == "development_source_unsafe_name"


def test_recursive_capture_rejects_invalid_utf8_name(tmp_path: Path) -> None:
    root, source = _source(tmp_path)
    fd = os.open(source, os.O_RDONLY | os.O_DIRECTORY)
    try:
        invalid_fd = os.open(b"bad-\xff", os.O_WRONLY | os.O_CREAT, 0o600, dir_fd=fd)
        os.close(invalid_fd)
    finally:
        os.close(fd)
    with pytest.raises(DevelopmentTreeError) as captured:
        capture_development_tree(root, "addon")
    assert captured.value.code == "development_source_unsafe_name"


def test_recursive_capture_enforces_fixed_entry_and_file_bounds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _source_path = _source(tmp_path)
    monkeypatch.setattr(deployment, "MAX_REGULAR_FILES", 1)
    with pytest.raises(DevelopmentTreeError) as captured:
        capture_development_tree(root, "addon")
    assert captured.value.code == "development_source_tree_bounds"
    monkeypatch.setattr(deployment, "MAX_REGULAR_FILES", 25_000)
    monkeypatch.setattr(deployment, "MAX_FILE_BYTES", 4)
    with pytest.raises(DevelopmentTreeError) as captured:
        capture_development_tree(root, "addon")
    assert captured.value.code == "development_source_tree_bounds"


@pytest.mark.parametrize(
    ("constant", "limit", "expected_code"),
    [
        ("MAX_ENTRIES", 1, "development_source_tree_bounds"),
        ("MAX_INCLUDED_BYTES", 1, "development_source_tree_bounds"),
        ("MAX_RELATIVE_PATH_BYTES", 8, "development_source_tree_bounds"),
        ("MAX_COMPONENT_BYTES", 3, "development_source_unsafe_name"),
        ("MAX_DEPTH", 1, "development_source_tree_bounds"),
        ("MAX_MANIFEST_BYTES", 1, "development_source_tree_bounds"),
    ],
)
def test_recursive_capture_enforces_every_fixed_tree_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    constant: str,
    limit: int,
    expected_code: str,
) -> None:
    root, _source_path = _source(tmp_path)
    monkeypatch.setattr(deployment, constant, limit)
    with pytest.raises(DevelopmentTreeError) as captured:
        capture_development_tree(root, "addon")
    assert captured.value.code == expected_code


def test_two_capture_comparison_detects_content_or_mode_change(tmp_path: Path) -> None:
    root, source = _source(tmp_path)
    first = capture_development_tree(root, "addon")
    (source / "addon.xml").write_text("changed")
    second = capture_development_tree(root, "addon")
    with pytest.raises(DevelopmentTreeError) as captured:
        compare_captures(first, second)
    assert captured.value.code == "development_source_changed_during_capture"


def test_delta_classifies_new_changed_executable_and_destination_only() -> None:
    source = _manifest(
        DevelopmentTreeEntry("same", "file", 0o644, 1, "a"),
        DevelopmentTreeEntry("bytes", "file", 0o644, 2, "b"),
        DevelopmentTreeEntry("mode", "file", 0o755, 1, "c"),
        DevelopmentTreeEntry("new", "file", 0o644, 3, "d"),
    )
    destination = _manifest(
        DevelopmentTreeEntry("same", "file", 0o644, 1, "a", 42, 43),
        DevelopmentTreeEntry("bytes", "file", 0o644, 2, "x", 42, 43),
        DevelopmentTreeEntry("mode", "file", 0o644, 1, "c", 42, 43),
        DevelopmentTreeEntry("only", "file", 0o644, 4, "e", 42, 43),
    )
    delta = classify_delta(source, destination)
    assert delta.new_paths == ("new",)
    assert delta.changed_paths == ("bytes", "mode")
    assert delta.unchanged_count == 1
    assert delta.destination_only_count == 1
    assert delta.transferred_bytes == 6


def test_delta_rejects_file_directory_replacement() -> None:
    source = _manifest(DevelopmentTreeEntry("entry", "file", 0o644, 1, "a"))
    destination = _manifest(
        DevelopmentTreeEntry("entry", "directory", 0o755, 0, None, 1, 1)
    )
    with pytest.raises(DevelopmentTreeError) as captured:
        classify_delta(source, destination)
    assert captured.value.code == "development_source_type_conflict"


def test_delta_archive_contains_only_new_and_changed_paths(tmp_path: Path) -> None:
    stage = tmp_path / "stage"
    stage.mkdir()
    for name, value in (("same", b"a"), ("changed", b"bb"), ("new", b"ccc")):
        (stage / name).write_bytes(value)
    source = _manifest(
        DevelopmentTreeEntry("same", "file", 0o644, 1, "ca"),
        DevelopmentTreeEntry("changed", "file", 0o644, 2, "cb"),
        DevelopmentTreeEntry("new", "file", 0o644, 3, "cc"),
    )
    destination = _manifest(
        DevelopmentTreeEntry("same", "file", 0o644, 1, "ca", 42, 43),
        DevelopmentTreeEntry("changed", "file", 0o644, 1, "old", 42, 43),
    )
    delta = classify_delta(source, destination)
    archive_path = tmp_path / "delta.tar"
    build_delta_archive(
        stage,
        archive_path,
        source,
        delta,
        runtime_uid=42,
        runtime_gid=43,
        root_name=None,
    )
    with tarfile.open(archive_path) as archive:
        assert archive.getnames() == ["changed", "new"]
        assert {(item.uid, item.gid) for item in archive} == {(42, 43)}


def test_destination_readback_rejects_symlink_and_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        root = tarfile.TarInfo("plugin.video.example")
        root.type = tarfile.DIRTYPE
        archive.addfile(root)
        link = tarfile.TarInfo("plugin.video.example/link")
        link.type = tarfile.SYMTYPE
        link.linkname = "target"
        archive.addfile(link)
    with pytest.raises(DevelopmentTreeError) as captured:
        capture_destination_archive(
            [stream.getvalue()], root_name="plugin.video.example"
        )
    assert captured.value.code == "development_destination_unsafe"

    monkeypatch.setattr(deployment, "MAX_FILE_BYTES", 1)
    payload = _destination_archive(
        "plugin.video.example", [("large", b"xx", 0o644, 42, 43)]
    )
    with pytest.raises(DevelopmentTreeError) as captured:
        capture_destination_archive([payload], root_name="plugin.video.example")
    assert captured.value.code == "development_destination_bounds"


@pytest.mark.parametrize(
    "member_type",
    [tarfile.LNKTYPE, tarfile.FIFOTYPE, tarfile.BLKTYPE, tarfile.CHRTYPE],
)
def test_destination_readback_rejects_hardlinks_and_special_files(
    member_type: bytes,
) -> None:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        root = tarfile.TarInfo("plugin.video.example")
        root.type = tarfile.DIRTYPE
        archive.addfile(root)
        unsafe = tarfile.TarInfo("plugin.video.example/unsafe")
        unsafe.type = member_type
        unsafe.linkname = "plugin.video.example/addon.xml"
        archive.addfile(unsafe)
    with pytest.raises(DevelopmentTreeError) as captured:
        capture_destination_archive(
            [stream.getvalue()], root_name="plugin.video.example"
        )
    assert captured.value.code == "development_destination_unsafe"


def test_destination_verification_preserves_destination_only_and_ownership() -> None:
    source = _manifest(DevelopmentTreeEntry("source", "file", 0o644, 1, "a"))
    before = _manifest(DevelopmentTreeEntry("only", "file", 0o644, 1, "b", 42, 43))
    after = _manifest(
        DevelopmentTreeEntry("only", "file", 0o644, 1, "b", 42, 43),
        DevelopmentTreeEntry("source", "file", 0o644, 1, "a", 42, 43),
    )
    verify_destination(source, before, after, runtime_uid=42, runtime_gid=43)
    bad = _manifest(
        DevelopmentTreeEntry("only", "file", 0o644, 1, "changed", 42, 43),
        DevelopmentTreeEntry("source", "file", 0o644, 1, "a", 42, 43),
    )
    with pytest.raises(DevelopmentTreeError):
        verify_destination(source, before, bad, runtime_uid=42, runtime_gid=43)


def test_selective_staging_does_not_filter_manifest_or_descriptor(
    tmp_path: Path,
) -> None:
    root, source = _source(tmp_path)
    full = capture_development_tree(root, "addon")
    stage = tmp_path / "stage"
    stage.mkdir()
    selected = capture_development_tree(
        root,
        "addon",
        stage_root=stage,
        stage_paths=frozenset({"resources/run.sh"}),
        capture_addon_xml=True,
    )
    assert selected.canonical_json == full.canonical_json
    assert selected.digest == full.digest
    assert selected.included_bytes == full.included_bytes
    assert selected.captured_file("addon.xml") == (source / "addon.xml").read_bytes()
    assert not (stage / "addon.xml").exists()
    assert (stage / "resources/run.sh").read_bytes() == (
        source / "resources/run.sh"
    ).read_bytes()
    assert stat.S_IMODE((stage / "resources/run.sh").stat().st_mode) == 0o755


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo"])
def test_unstaged_members_still_enforce_complete_tree_safety(
    tmp_path: Path, kind: str
) -> None:
    root, source = _source(tmp_path)
    if kind == "symlink":
        (source / "unsafe").symlink_to(source / "addon.xml")
    elif kind == "hardlink":
        os.link(source / "addon.xml", source / "unsafe")
    else:
        os.mkfifo(source / "unsafe")
    stage = tmp_path / "stage"
    stage.mkdir()
    with pytest.raises(DevelopmentTreeError, match="development_source_unsafe_entry"):
        capture_development_tree(
            root, "addon", stage_root=stage, stage_paths=frozenset()
        )


def test_captured_source_descriptor_is_bounded_even_when_not_staged(
    tmp_path: Path,
) -> None:
    root, source = _source(tmp_path)
    (source / "addon.xml").write_bytes(
        b"x" * (deployment.MAX_CAPTURED_DESCRIPTOR_BYTES + 1)
    )
    with pytest.raises(
        DevelopmentTreeError, match="development_source_identity_invalid"
    ):
        capture_development_tree(root, "addon", capture_addon_xml=True)
