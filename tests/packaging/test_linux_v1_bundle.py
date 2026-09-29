"""The release inventory accepts recipes and rejects Kodi binary artifacts."""

from __future__ import annotations

import hashlib
import io
import json
import runpy
import tarfile
from collections.abc import Mapping
from pathlib import Path

import pytest

bundle_tool = runpy.run_path(str(Path(__file__).parents[2] / "scripts/linux-v1-bundle"))
verify = bundle_tool["verify"]
canonical = bundle_tool["canonical"]
digest = bundle_tool["digest"]


def _archive(path: Path, tags: tuple[str, ...]) -> dict[str, str]:
    descriptors = []
    configs = {}
    for tag in tags:
        config = json.dumps(
            {"architecture": "amd64", "os": "linux", "tag": tag}
        ).encode()
        config_hash = hashlib.sha256(config).hexdigest()
        configs[tag] = (config_hash, config)
        descriptors.append({"Config": f"blobs/sha256/{config_hash}", "RepoTags": [tag]})
    with tarfile.open(path, "w") as archive:
        contents = [("manifest.json", json.dumps(descriptors).encode())]
        contents.extend(
            (f"blobs/sha256/{value[0]}", value[1]) for value in configs.values()
        )
        for name, data in contents:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return {tag: "sha256:" + value[0] for tag, value in configs.items()}


def _checksums(bundle: Path, files: Mapping[str, object]) -> None:
    (bundle / "SHA256SUMS").write_text(
        "".join(
            f"{digest(bundle / name).removeprefix('sha256:')}  {name}\n"
            for name in sorted((*files, "BUNDLE.json"))
        )
    )


@pytest.fixture
def bundle(tmp_path: Path) -> Path:
    root = Path(__file__).parents[2]
    for name in bundle_tool["RELEASE_FILES"]:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if name.startswith("runtime-recipes/"):
            path.write_bytes(
                (root / "src/kodi_lab/runtime/recipes" / path.name).read_bytes()
            )
        elif name == "linux-v1-bundle":
            path.write_bytes((root / "scripts/linux-v1-bundle").read_bytes())
        elif name in {"compose.release.yaml", "compose.development.yaml"}:
            path.write_bytes((root / name).read_bytes())
        elif name in {"INSTALL.md", "RELEASE.md"}:
            path.write_bytes((root / "docs" / f"LINUX-V1-{name}").read_bytes())
        else:
            path.write_bytes(b"release artifact\n")
    app_archive = tmp_path / bundle_tool["APP_ARCHIVE"]
    app_configs = _archive(
        app_archive, ("kodi-lab-v1-web:1.0.0rc1", "kodi-lab-v1-broker:1.0.0rc1")
    )
    files = {
        name: {
            "sha256": digest(tmp_path / name),
            "size": (tmp_path / name).stat().st_size,
        }
        for name in bundle_tool["RELEASE_FILES"]
    }
    document = {
        "schema": "kodi-lab-linux-v1-bundle-v2",
        "platform": "linux/amd64",
        "version": "1.0.0rc1",
        "source_tree": "a" * 40,
        "application": {
            "archive": app_archive.name,
            "images": {
                role: {
                    "tag_hint": f"kodi-lab-v1-{role}:1.0.0rc1",
                    "config_image_id": app_configs[f"kodi-lab-v1-{role}:1.0.0rc1"],
                    "saved_image_id": app_configs[f"kodi-lab-v1-{role}:1.0.0rc1"],
                }
                for role in ("web", "broker")
            },
        },
        "runtimes": bundle_tool["_recipes"](tmp_path),
        "files": files,
    }
    (tmp_path / "BUNDLE.json").write_bytes(canonical(document))
    _checksums(tmp_path, files)
    return tmp_path


def test_bundle_detects_missing_and_changed_artifacts(bundle: Path) -> None:
    assert [item["generation"] for item in verify(bundle)["runtimes"]] == [
        "matrix",
        "nexus",
        "omega",
    ]
    document = json.loads((bundle / "BUNDLE.json").read_text())
    document["application"]["images"].pop("broker")
    (bundle / "BUNDLE.json").write_bytes(canonical(document))
    _checksums(bundle, document["files"])
    with pytest.raises(ValueError, match="application archive image identities"):
        verify(bundle)
    (bundle / "INSTALL.md").write_bytes(b"altered")
    with pytest.raises(ValueError, match="artifact differs"):
        verify(bundle)


def test_bundle_rejects_kodi_archive_even_with_checksum(bundle: Path) -> None:
    (bundle / "kodi-22.0-beta2.tar").write_bytes(b"private")
    with pytest.raises(ValueError, match="bundle file inventory differs"):
        verify(bundle)


def test_bundle_rejects_local_image_selection_outside_manifest(bundle: Path) -> None:
    (bundle / ".env").write_text(
        "KODI_LAB_REMOTE_IMAGE=sha256:" + "0" * 64 + "\n"
        "KODI_LAB_BROKER_IMAGE=sha256:" + "0" * 64 + "\n"
        "KODI_LAB_PUBLISHED_PORT=18100\n"
    )
    with pytest.raises(ValueError, match="local image selection"):
        verify(bundle)


def test_bundle_accepts_local_development_configuration_without_capturing_source(
    bundle: Path, tmp_path: Path
) -> None:
    document = json.loads((bundle / "BUNDLE.json").read_text())
    source = tmp_path.parent / f"{tmp_path.name} Source Trees"
    source.mkdir()
    (bundle / ".env").write_text(
        f"KODI_LAB_REMOTE_IMAGE={document['application']['images']['web']['saved_image_id']}\n"
        f"KODI_LAB_BROKER_IMAGE={document['application']['images']['broker']['saved_image_id']}\n"
        "KODI_LAB_PUBLISHED_PORT=18100\n"
    )
    installed_tool = runpy.run_path(str(bundle / "linux-v1-bundle"))
    installed_tool["configure_development_source"](bundle, str(source))
    assert verify(bundle)["files"].keys() == document["files"].keys()
    assert str(source) in (bundle / ".env").read_text()
    assert str(source) not in (bundle / "BUNDLE.json").read_text()
    assert str(source) not in (bundle / "SHA256SUMS").read_text()
    for name in document["files"]:
        assert str(source) not in (bundle / name).read_bytes().decode(
            "utf-8", errors="ignore"
        )


def test_bundle_rejects_removed_or_relinked_development_directory(
    bundle: Path, tmp_path: Path
) -> None:
    document = json.loads((bundle / "BUNDLE.json").read_text())
    source = tmp_path.parent / f"{tmp_path.name} Source Trees"
    source.mkdir()
    (bundle / ".env").write_text(
        f"KODI_LAB_REMOTE_IMAGE={document['application']['images']['web']['saved_image_id']}\n"
        f"KODI_LAB_BROKER_IMAGE={document['application']['images']['broker']['saved_image_id']}\n"
        "KODI_LAB_PUBLISHED_PORT=18100\n"
        f"KODI_LAB_DEVELOPMENT_SOURCE='{source}'\n"
    )
    assert verify(bundle)
    source.rmdir()
    with pytest.raises(OSError):
        verify(bundle)
    source.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(OSError):
        verify(bundle)
    source.unlink()


@pytest.mark.parametrize("source", ("", ".", "relative", "/", "/tmp/../tmp"))
def test_bundle_rejects_malformed_local_development_source(
    bundle: Path, source: str
) -> None:
    document = json.loads((bundle / "BUNDLE.json").read_text())
    (bundle / ".env").write_text(
        f"KODI_LAB_REMOTE_IMAGE={document['application']['images']['web']['saved_image_id']}\n"
        f"KODI_LAB_BROKER_IMAGE={document['application']['images']['broker']['saved_image_id']}\n"
        "KODI_LAB_PUBLISHED_PORT=18100\n"
        f"KODI_LAB_DEVELOPMENT_SOURCE='{source}'\n"
    )
    with pytest.raises(ValueError, match="development source"):
        verify(bundle)
