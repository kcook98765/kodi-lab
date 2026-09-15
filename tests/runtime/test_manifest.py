import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from kodi_lab.runtime import load_runtime_build_manifest

RUNTIME_ROOT = Path(__file__).parents[2] / "runtime"


def manifest_for(
    tmp_path: Path,
    *,
    kodi_version: str = "21.3",
    kodi_package_version: str = "2:21.3+dfsg-1ubuntu1",
) -> Path:
    asset = tmp_path / "Dockerfile"
    asset.write_text("FROM scratch\n", encoding="utf-8")
    digest = "sha256:" + hashlib.sha256(asset.read_bytes()).hexdigest()
    base_digest = "sha256:" + ("a" * 64)
    document = {
        "schema_version": 1,
        "runtime_kind": "kodi",
        "kodi_version": kodi_version,
        "base_image_reference": f"ubuntu:26.04@{base_digest}",
        "base_image_digest": base_digest,
        "package_sources": ["http://archive.ubuntu.com/ubuntu resolute universe"],
        "kodi_package_version": kodi_package_version,
        "compatibility_assets": [],
        "repository_file_hashes": {"Dockerfile": digest},
        "build": {
            "context": ".",
            "dockerfile": "Dockerfile",
            "platform": "linux/amd64",
            "pull": False,
            "no_cache": False,
        },
        "result": {
            "built_at": "2026-09-14T12:00:00Z",
            "image_id": "sha256:" + ("b" * 64),
            "build_log_reference": "phase3-validation://build.log",
            "build_log_digest": "sha256:" + ("c" * 64),
        },
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def test_manifest_validates_declared_immutable_inputs(tmp_path: Path) -> None:
    manifest = load_runtime_build_manifest(manifest_for(tmp_path))
    assert manifest.kodi_version == "21.3"
    assert manifest.build.platform == "linux/amd64"


def test_manifest_accepts_exact_kodi_20_package_identity(tmp_path: Path) -> None:
    manifest = load_runtime_build_manifest(
        manifest_for(
            tmp_path,
            kodi_version="20.5",
            kodi_package_version="2:20.5+dfsg-1ubuntu1",
        )
    )
    assert manifest.kodi_version == "20.5"


def test_manifest_accepts_exact_team_kodi_old_stable_package(tmp_path: Path) -> None:
    path = manifest_for(
        tmp_path,
        kodi_version="19.5",
        kodi_package_version="6:19.5+git20221224.1006-f8fdeb6b1b-0~jammy",
    )
    data = json.loads(path.read_text(encoding="utf-8"))
    data["package_sources"] = [
        "https://ppa.launchpadcontent.net/team-xbmc/kodi-old/ubuntu/ jammy main"
    ]
    data["package_artifact_hashes"] = {"kodi.deb": "sha256:" + ("d" * 64)}
    path.write_text(json.dumps(data), encoding="utf-8")
    manifest = load_runtime_build_manifest(path)
    assert manifest.kodi_version == "19.5"
    assert manifest.package_artifact_hashes["kodi.deb"].startswith("sha256:")


def test_manifest_accepts_exact_trusted_source_build(tmp_path: Path) -> None:
    path = manifest_for(tmp_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["kodi_version"] = "22.0-beta.2"
    data["kodi_package_version"] = None
    data["source_build"] = {
        "repository_url": "https://github.com/xbmc/xbmc",
        "release_tag": "22.0b2-Piers",
        "commit_sha": "e513e0ff4331fc25fd2454659a9dd3e6b7670146",
        "source_archive_url": (
            "https://codeload.github.com/xbmc/xbmc/tar.gz/"
            "e513e0ff4331fc25fd2454659a9dd3e6b7670146"
        ),
        "source_archive_digest": "sha256:" + ("d" * 64),
        "kodi_version": "22.0-beta.2",
        "toolchain_packages": ["cmake", "ninja-build"],
        "cmake_options": ["-DCMAKE_BUILD_TYPE=Release"],
    }
    path.write_text(json.dumps(data), encoding="utf-8")
    manifest = load_runtime_build_manifest(path)
    assert manifest.source_build is not None
    assert manifest.source_build.release_tag == "22.0b2-Piers"
    assert manifest.source_build.commit_sha.startswith("e513e0ff")


@pytest.mark.parametrize(
    ("directory", "version"),
    (
        ("kodi-19.5", "19.5"),
        ("kodi-20.5", "20.5"),
        ("kodi-21.3", "21.3"),
        ("kodi-22.0-beta2", "22.0-beta.2"),
    ),
)
def test_repository_runtime_manifest_matches_immutable_inputs(
    directory: str, version: str
) -> None:
    manifest = load_runtime_build_manifest(RUNTIME_ROOT / directory / "manifest.json")
    assert manifest.kodi_version == version


def test_manifest_detects_repository_file_drift(tmp_path: Path) -> None:
    path = manifest_for(tmp_path)
    (tmp_path / "Dockerfile").write_text("CHANGED\n", encoding="utf-8")
    with pytest.raises(ValueError, match="digest mismatch"):
        load_runtime_build_manifest(path)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("runtime_kind", "shell"),
        ("kodi_version", "v21.3"),
        ("kodi_package_version", "latest"),
        ("base_image_reference", "ubuntu:26.04"),
    ],
)
def test_manifest_rejects_unapproved_inputs(
    tmp_path: Path, field: str, value: object
) -> None:
    path = manifest_for(tmp_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    data[field] = value
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValidationError):
        load_runtime_build_manifest(path)


def test_manifest_rejects_path_escape(tmp_path: Path) -> None:
    path = manifest_for(tmp_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["repository_file_hashes"] = {"../secret": "sha256:" + ("d" * 64)}
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValidationError, match="relative and bounded"):
        load_runtime_build_manifest(path)


def test_manifest_rejects_package_release_mismatch(tmp_path: Path) -> None:
    path = manifest_for(tmp_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["kodi_version"] = "20.5"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValidationError, match="exact release identity"):
        load_runtime_build_manifest(path)


def test_manifest_rejects_unbounded_compatibility_asset(tmp_path: Path) -> None:
    path = manifest_for(tmp_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["compatibility_assets"] = ["x" * 257]
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValidationError, match="bounded and nonblank"):
        load_runtime_build_manifest(path)


def test_manifest_rejects_unbounded_package_artifact_name(tmp_path: Path) -> None:
    path = manifest_for(tmp_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["package_artifact_hashes"] = {"../kodi.deb": "sha256:" + ("d" * 64)}
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValidationError, match="relative and bounded"):
        load_runtime_build_manifest(path)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("commit_sha", "e513e0f", "full lowercase Git SHA"),
        ("kodi_version", "22.0-beta.1", "exact release identity"),
    ],
)
def test_source_build_rejects_ambiguous_or_mismatched_identity(
    tmp_path: Path, field: str, value: str, message: str
) -> None:
    path = manifest_for(tmp_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["kodi_version"] = "22.0-beta.2"
    data["kodi_package_version"] = None
    data["source_build"] = {
        "repository_url": "https://github.com/xbmc/xbmc",
        "release_tag": "22.0b2-Piers",
        "commit_sha": "e513e0ff4331fc25fd2454659a9dd3e6b7670146",
        "source_archive_url": "https://codeload.github.com/xbmc/xbmc/tar.gz/source",
        "source_archive_digest": "sha256:" + ("d" * 64),
        "kodi_version": "22.0-beta.2",
        "toolchain_packages": ["cmake"],
        "cmake_options": ["-DCMAKE_BUILD_TYPE=Release"],
    }
    data["source_build"][field] = value
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValidationError, match=message):
        load_runtime_build_manifest(path)
