"""Docker-save transport metadata is canonical without rewriting image blobs."""

from __future__ import annotations

import hashlib
import io
import json
import runpy
import tarfile
from pathlib import Path

import pytest

from scripts.canonicalize_docker_archive import canonicalize

bundle_tool = runpy.run_path(str(Path(__file__).parents[2] / "scripts/linux-v1-bundle"))


def _json(value: object, *, alternate: bool) -> bytes:
    return json.dumps(
        value,
        sort_keys=alternate,
        separators=(",", ":") if alternate else None,
    ).encode()


def _blob(data: bytes) -> str:
    return "blobs/sha256/" + hashlib.sha256(data).hexdigest()


def _saved_archive(
    path: Path,
    *,
    reverse: bool = False,
    alternate_metadata: bool = False,
    config_change: bool = False,
    layer_change: bool = False,
    one_image: bool = False,
    repositories: bool = False,
    oci: bool = True,
) -> tuple[dict[str, str], set[str]]:
    tags = ("example-web:v1",) if one_image else ("example-web:v1", "example-broker:v1")
    files: dict[str, bytes] = {}
    manifest: list[dict[str, object]] = []
    index: list[dict[str, object]] = []
    configs: dict[str, str] = {}
    image_ids: set[str] = set()
    for position, tag in enumerate(tags):
        config = _json(
            {
                "architecture": "amd64",
                "os": "linux",
                "name": tag,
                "changed": config_change and position == 0,
            },
            alternate=False,
        )
        layer = (
            "layer-" + tag + ("-changed" if layer_change and position == 0 else "")
        ).encode()
        config_path, layer_path = _blob(config), _blob(layer)
        files[config_path] = config
        files[layer_path] = layer
        image_manifest = _json(
            {
                "schemaVersion": 2,
                "config": {"digest": "sha256:" + config_path.rsplit("/", 1)[1]},
                "layers": [{"digest": "sha256:" + layer_path.rsplit("/", 1)[1]}],
            },
            alternate=False,
        )
        manifest_path = _blob(image_manifest)
        files[manifest_path] = image_manifest
        manifest.append(
            {"Config": config_path, "RepoTags": [tag], "Layers": [layer_path]}
        )
        index.append(
            {
                "digest": "sha256:" + manifest_path.rsplit("/", 1)[1],
                "size": len(image_manifest),
                "annotations": {"name": tag},
            }
        )
        configs[tag] = "sha256:" + config_path.rsplit("/", 1)[1]
        image_ids.add("sha256:" + manifest_path.rsplit("/", 1)[1])
    if reverse:
        manifest.reverse()
        index.reverse()
    files["manifest.json"] = _json(manifest, alternate=reverse)
    if oci:
        files["index.json"] = _json(
            {"schemaVersion": 2, "manifests": index}, alternate=reverse
        )
        files["oci-layout"] = _json({"imageLayoutVersion": "1.0.0"}, alternate=reverse)
    if repositories:
        files["repositories"] = _json(
            {"example": {tag: identity for tag, identity in configs.items()}},
            alternate=reverse,
        )
    names = sorted(files, reverse=reverse)
    with tarfile.open(path, "w:") as archive:
        if alternate_metadata:
            for name in ("blobs", "blobs/sha256"):
                entry = tarfile.TarInfo(name)
                entry.type = tarfile.DIRTYPE
                entry.mtime = 123456
                entry.uid = 501
                entry.gid = 20
                entry.uname = "host-user"
                entry.gname = "host-group"
                entry.mode = 0o700
                archive.addfile(entry)
        for name in names:
            data = files[name]
            entry = tarfile.TarInfo(name)
            entry.size = len(data)
            if alternate_metadata:
                entry.mtime = 123456
                entry.uid = 501
                entry.gid = 20
                entry.uname = "host-user"
                entry.gname = "host-group"
                entry.mode = 0o600
            archive.addfile(entry, io.BytesIO(data))
    return configs, image_ids


@pytest.mark.parametrize("one_image", [False, True])
@pytest.mark.parametrize("repositories", [False, True])
@pytest.mark.parametrize("oci", [False, True])
def test_semantically_equal_save_orders_and_metadata_canonicalize_identically(
    tmp_path: Path, one_image: bool, repositories: bool, oci: bool
) -> None:
    first, second = tmp_path / "first.tar", tmp_path / "second.tar"
    expected, image_ids = _saved_archive(
        first, one_image=one_image, repositories=repositories, oci=oci
    )
    _saved_archive(
        second,
        reverse=True,
        alternate_metadata=True,
        one_image=one_image,
        repositories=repositories,
        oci=oci,
    )
    output_first, output_second = (
        tmp_path / "out-first.tar",
        tmp_path / "out-second.tar",
    )
    canonicalize(first, output_first)
    canonicalize(second, output_second)
    assert output_first.read_bytes() == output_second.read_bytes()
    tags, configs, _ = bundle_tool["archive_images"](output_first)
    assert tags == expected
    assert configs == set(expected.values())
    assert all(
        bundle_tool["archive_contains_digest"](output_first, item) for item in image_ids
    )
    repeated = tmp_path / "repeated.tar"
    canonicalize(output_first, repeated)
    assert output_first.read_bytes() == repeated.read_bytes()
    with tarfile.open(output_first, "r:") as archive:
        members = archive.getmembers()
        assert [item.name for item in members] == [
            "blobs",
            "blobs/sha256",
            *sorted(item.name for item in members if item.isfile()),
        ]
        assert all(item.mtime == item.uid == item.gid == 0 for item in members)
        assert all(item.uname == item.gname == "" for item in members)
        assert all(item.mode == (0o755 if item.isdir() else 0o644) for item in members)


@pytest.mark.parametrize("change", ["tag", "config", "layer"])
def test_meaningful_archive_changes_retain_distinct_identity(
    tmp_path: Path, change: str
) -> None:
    first, second = tmp_path / "first.tar", tmp_path / "second.tar"
    _saved_archive(first)
    _saved_archive(
        second, config_change=change == "config", layer_change=change == "layer"
    )
    if change == "tag":
        with tarfile.open(second, "r:") as archive:
            files = {
                item.name: archive.extractfile(item).read()
                for item in archive
                if item.isfile()
            }
        entries = json.loads(files["manifest.json"])
        entries[0]["RepoTags"] = ["different-web:v1"]
        files["manifest.json"] = _json(entries, alternate=False)
        with tarfile.open(second, "w:") as archive:
            for name, data in files.items():
                entry = tarfile.TarInfo(name)
                entry.size = len(data)
                archive.addfile(entry, io.BytesIO(data))
    output_first, output_second = (
        tmp_path / "out-first.tar",
        tmp_path / "out-second.tar",
    )
    canonicalize(first, output_first)
    canonicalize(second, output_second)
    assert (
        hashlib.sha256(output_first.read_bytes()).digest()
        != hashlib.sha256(output_second.read_bytes()).digest()
    )


def test_changed_blob_bytes_cannot_be_normalized_away(tmp_path: Path) -> None:
    source = tmp_path / "source.tar"
    _saved_archive(source)
    with tarfile.open(source, "r:") as archive:
        files = {
            item.name: archive.extractfile(item).read()
            for item in archive
            if item.isfile()
        }
    layer = next(
        name
        for name in files
        if name.startswith("blobs/") and files[name].startswith(b"layer-")
    )
    files[layer] += b"tampered"
    with tarfile.open(source, "w:") as archive:
        for name, data in files.items():
            entry = tarfile.TarInfo(name)
            entry.size = len(data)
            archive.addfile(entry, io.BytesIO(data))
    with pytest.raises(ValueError, match="blob digest differs"):
        canonicalize(source, tmp_path / "output.tar")
