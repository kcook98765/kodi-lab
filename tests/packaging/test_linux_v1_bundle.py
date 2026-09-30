"""The release inventory accepts recipes and rejects Kodi binary artifacts."""

from __future__ import annotations

import hashlib
import io
import json
import runpy
import tarfile
import zipfile
from collections.abc import Mapping
from pathlib import Path

import pytest

from scripts.build_narrow_bridge import (
    BUILDER_ID,
    RELEASE_INVENTORY,
    SEMANTIC_FINGERPRINT,
    build_archive,
)

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


def _bundle_hash(bundle: Path) -> str:
    return str(digest(bundle / "BUNDLE.json")).removeprefix("sha256:")


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
        if name in {"repository/STABLE.json", "repository/DEVELOPMENT.json"}:
            path.write_bytes((root / name).read_bytes())
        elif name.startswith("runtime-recipes/"):
            path.write_bytes(
                (root / "src/kodi_lab/runtime/recipes" / path.name).read_bytes()
            )
        elif name == "linux-v1-bundle":
            path.write_bytes((root / "scripts/linux-v1-bundle").read_bytes())
        elif name in {"compose.release.yaml", "compose.development.yaml"}:
            path.write_bytes((root / name).read_bytes())
        elif name in {"INSTALL.md", "RELEASE.md"}:
            path.write_bytes((root / "docs" / f"LINUX-V1-{name}").read_bytes())
        elif name == "PRODUCTION-OPERATIONS.md":
            path.write_bytes((root / "docs/PRODUCTION-OPERATIONS.md").read_bytes())
        else:
            path.write_bytes(b"release artifact\n")
    addon_root = root / "bridge/service.kodi_lab_bridge"
    bridge_zip = tmp_path / bundle_tool["BRIDGE_ZIP"]
    build_archive(addon_root, bridge_zip)
    contents = {name: (addon_root / name).read_bytes() for name in RELEASE_INVENTORY}
    bridge_provenance = {
        "schema": "kodi-lab-bridge-artifact-v1",
        "addon_id": "service.kodi_lab_bridge",
        "version": "1.1.13",
        "protocol": "1",
        "bootstrap": "otp-1",
        "license": "MIT",
        "semantic_fingerprint": SEMANTIC_FINGERPRINT,
        "capabilities": ["screenshot", "workspace_deployment"],
        "source_commit": "c" * 40,
        "source_tree": "a" * 40,
        "bridge_tree": bundle_tool["_bridge_tree"](contents),
        "builder": BUILDER_ID,
        "builder_sha256": hashlib.sha256(
            (root / "scripts/build_narrow_bridge.py").read_bytes()
        ).hexdigest(),
        "filename": bridge_zip.name,
        "sha256": hashlib.sha256(bridge_zip.read_bytes()).hexdigest(),
        "inventory": [
            {
                "path": name,
                "sha256": hashlib.sha256(content).hexdigest(),
                "size": len(content),
            }
            for name, content in contents.items()
        ],
    }
    bridge_provenance["inventory_sha256"] = hashlib.sha256(
        canonical(bridge_provenance["inventory"])
    ).hexdigest()
    (tmp_path / bundle_tool["BRIDGE_PROVENANCE"]).write_bytes(
        canonical(bridge_provenance)
    )
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
        "schema": "kodi-lab-linux-v1-bundle-v3",
        "platform": "linux/amd64",
        "version": "1.0.0rc1",
        "source_tree": "a" * 40,
        "source_commit": "c" * 40,
        "bridge": {
            "zip": bundle_tool["BRIDGE_ZIP"],
            "provenance": bundle_tool["BRIDGE_PROVENANCE"],
        },
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
        "repository": {
            "stable_manifest": "repository/STABLE.json",
            "generator": "kodi-lab-stable-repository-v1",
            "per_install_generation": True,
            "development_manifest": "repository/DEVELOPMENT.json",
            "development_generator": "kodi-lab-development-repository-v1",
            "publication_migration": "0055_development_repository_publications",
            "workspace_deployment_migration": "0056_repository_workspace_deployment",
            "candidate_transition": "explicit-reviewed-a-b-v1",
        },
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
    assert (
        "Install and pair a remote Kodi bridge" in (bundle / "INSTALL.md").read_text()
    )
    assert (
        "Manual production bridge installation"
        in (bundle / "PRODUCTION-OPERATIONS.md").read_text()
    )
    document = json.loads((bundle / "BUNDLE.json").read_text())
    document["application"]["images"].pop("broker")
    (bundle / "BUNDLE.json").write_bytes(canonical(document))
    _checksums(bundle, document["files"])
    with pytest.raises(ValueError, match="application archive image identities"):
        verify(bundle)
    (bundle / "INSTALL.md").write_bytes(b"altered")
    with pytest.raises(ValueError, match="artifact differs"):
        verify(bundle)


def test_verifier_rejects_repository_write_mount_even_when_rehashed(
    bundle: Path,
) -> None:
    compose = bundle / "compose.release.yaml"
    body = compose.read_text()
    repository_start = body.index("\n  repository:\n")
    initializer_start = body.index("\n  repository-init:\n")
    repository = body[repository_start:initializer_start].replace(
        "repository_data:/srv/kodi-lab-repository:ro",
        "repository_data:/srv/kodi-lab-repository",
    )
    compose.write_text(body[:repository_start] + repository + body[initializer_start:])
    document = json.loads((bundle / "BUNDLE.json").read_bytes())
    document["files"]["compose.release.yaml"] = {
        "sha256": digest(compose),
        "size": compose.stat().st_size,
    }
    (bundle / "BUNDLE.json").write_bytes(canonical(document))
    _checksums(bundle, document["files"])
    with pytest.raises(ValueError, match="service isolation"):
        verify(bundle)


def test_verifier_requires_initializer_chown_capability(bundle: Path) -> None:
    compose = bundle / "compose.release.yaml"
    body = compose.read_text()
    initializer_start = body.index("\n  repository-init:\n")
    broker_start = body.index("\n  broker:\n")
    initializer = body[initializer_start:broker_start].replace(
        "cap_drop: [ALL]\n    cap_add: [CHOWN]", "cap_drop: [ALL]"
    )
    compose.write_text(body[:initializer_start] + initializer + body[broker_start:])
    document = json.loads((bundle / "BUNDLE.json").read_bytes())
    document["files"]["compose.release.yaml"] = {
        "sha256": digest(compose),
        "size": compose.stat().st_size,
    }
    (bundle / "BUNDLE.json").write_bytes(canonical(document))
    _checksums(bundle, document["files"])
    with pytest.raises(ValueError, match="service isolation"):
        verify(bundle)


@pytest.mark.parametrize(
    "name", ("kodi-22.0-beta2.tar", "kodi_21.3_amd64.deb", "kodi-runtime-image.tar")
)
def test_bundle_rejects_kodi_archive_even_with_checksum(
    bundle: Path, name: str
) -> None:
    (bundle / name).write_bytes(b"private")
    with pytest.raises(ValueError, match="bundle file inventory differs"):
        verify(bundle)


def test_bridge_artifact_is_exact_and_tamper_is_rejected(bundle: Path) -> None:
    document = verify(bundle)
    assert document["bridge"]["zip"] == bundle_tool["BRIDGE_ZIP"]
    archive = bundle / bundle_tool["BRIDGE_ZIP"]
    with zipfile.ZipFile(archive) as package:
        assert package.read("service.kodi_lab_bridge/addon.xml").count(b"1.1.13") == 1
    original = archive.read_bytes()
    archive.write_bytes(original + b"tampered")
    with pytest.raises(ValueError, match="artifact differs"):
        verify(bundle)
    archive.write_bytes(original)
    archive.unlink()
    with pytest.raises(ValueError, match="bundle file inventory differs"):
        verify(bundle)


def test_stable_manifest_is_reviewed_and_cannot_change_id(bundle: Path) -> None:
    assert verify(bundle)["repository"]["stable_manifest"] == "repository/STABLE.json"
    path = bundle / "repository/STABLE.json"
    stable = json.loads(path.read_bytes())
    stable["bridge_id"] = "plugin.video.unreviewed"
    path.write_bytes(canonical(stable))
    document = json.loads((bundle / "BUNDLE.json").read_bytes())
    document["files"]["repository/STABLE.json"] = {
        "sha256": digest(path),
        "size": path.stat().st_size,
    }
    (bundle / "BUNDLE.json").write_bytes(canonical(document))
    _checksums(bundle, document["files"])
    with pytest.raises(ValueError, match="stable repository input manifest differs"):
        verify(bundle)


@pytest.mark.parametrize(
    "bind,base",
    (
        ("0.0.0.0", "http://192.168.50.10:18120"),
        ("192.168.50.10", "http://admin:secret@192.168.50.10:18120"),
        ("192.168.50.10", "http://192.168.50.10:18120/path"),
    ),
)
def test_repository_configuration_rejects_unsafe_values(bind: str, base: str) -> None:
    with pytest.raises(ValueError, match="repository"):
        bundle_tool["validate_repository_config"](bind, 18120, base)


def test_bridge_identity_and_provenance_mismatch_are_rejected(bundle: Path) -> None:
    document = json.loads((bundle / "BUNDLE.json").read_bytes())
    document["bridge"]["zip"] = "bridge/wrong.zip"
    (bundle / "BUNDLE.json").write_bytes(canonical(document))
    _checksums(bundle, document["files"])
    with pytest.raises(ValueError, match="bridge bundle identity"):
        verify(bundle)
    document["bridge"]["zip"] = bundle_tool["BRIDGE_ZIP"]
    (bundle / "BUNDLE.json").write_bytes(canonical(document))
    _checksums(bundle, document["files"])
    provenance_path = bundle / bundle_tool["BRIDGE_PROVENANCE"]
    provenance = json.loads(provenance_path.read_bytes())
    provenance["source_commit"] = "d" * 40
    provenance_path.write_bytes(canonical(provenance))
    document["files"][bundle_tool["BRIDGE_PROVENANCE"]] = {
        "sha256": digest(provenance_path),
        "size": provenance_path.stat().st_size,
    }
    (bundle / "BUNDLE.json").write_bytes(canonical(document))
    _checksums(bundle, document["files"])
    with pytest.raises(ValueError, match="bridge provenance identity"):
        verify(bundle)


@pytest.mark.parametrize(
    "field,value", (("version", "1.1.12"), ("inventory_sha256", "0" * 64))
)
def test_bridge_wrong_version_or_inventory_rejected_after_rehash(
    bundle: Path, field: str, value: str
) -> None:
    manifest_path = bundle / "BUNDLE.json"
    document = json.loads(manifest_path.read_bytes())
    provenance_path = bundle / bundle_tool["BRIDGE_PROVENANCE"]
    provenance = json.loads(provenance_path.read_bytes())
    provenance[field] = value
    provenance_path.write_bytes(canonical(provenance))
    document["files"][bundle_tool["BRIDGE_PROVENANCE"]] = {
        "sha256": digest(provenance_path),
        "size": provenance_path.stat().st_size,
    }
    manifest_path.write_bytes(canonical(document))
    _checksums(bundle, document["files"])
    with pytest.raises(
        ValueError, match=r"bridge provenance identity|bridge release inventory hash"
    ):
        verify(bundle)


def test_bridge_zip_noncanonical_tamper_rejected_after_rehash(bundle: Path) -> None:
    archive = bundle / bundle_tool["BRIDGE_ZIP"]
    archive.write_bytes(archive.read_bytes() + b"trailing bytes")
    provenance_path = bundle / bundle_tool["BRIDGE_PROVENANCE"]
    provenance = json.loads(provenance_path.read_bytes())
    provenance["sha256"] = hashlib.sha256(archive.read_bytes()).hexdigest()
    provenance_path.write_bytes(canonical(provenance))
    manifest_path = bundle / "BUNDLE.json"
    document = json.loads(manifest_path.read_bytes())
    for path in (archive, provenance_path):
        name = path.relative_to(bundle).as_posix()
        document["files"][name] = {"sha256": digest(path), "size": path.stat().st_size}
    manifest_path.write_bytes(canonical(document))
    _checksums(bundle, document["files"])
    with pytest.raises(ValueError, match="deterministic builder form"):
        verify(bundle)


def test_bundle_rejects_local_image_selection_outside_manifest(bundle: Path) -> None:
    (bundle / ".env").write_text(
        "KODI_LAB_REMOTE_IMAGE=sha256:" + "0" * 64 + "\n"
        "KODI_LAB_BROKER_IMAGE=sha256:" + "0" * 64 + "\n"
        "KODI_LAB_PUBLISHED_PORT=18100\n"
        "KODI_LAB_REPOSITORY_BIND=192.168.50.10\n"
        "KODI_LAB_REPOSITORY_PORT=18120\n"
        "KODI_LAB_REPOSITORY_BASE_URL=http://192.168.50.10:18120\n"
        f"KODI_LAB_REPOSITORY_BUNDLE_SHA256={_bundle_hash(bundle)}\n"
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
        "KODI_LAB_REPOSITORY_BIND=192.168.50.10\n"
        "KODI_LAB_REPOSITORY_PORT=18120\n"
        "KODI_LAB_REPOSITORY_BASE_URL=http://192.168.50.10:18120\n"
        f"KODI_LAB_REPOSITORY_BUNDLE_SHA256={_bundle_hash(bundle)}\n"
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
        "KODI_LAB_REPOSITORY_BIND=192.168.50.10\n"
        "KODI_LAB_REPOSITORY_PORT=18120\n"
        "KODI_LAB_REPOSITORY_BASE_URL=http://192.168.50.10:18120\n"
        f"KODI_LAB_REPOSITORY_BUNDLE_SHA256={_bundle_hash(bundle)}\n"
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
        "KODI_LAB_REPOSITORY_BIND=192.168.50.10\n"
        "KODI_LAB_REPOSITORY_PORT=18120\n"
        "KODI_LAB_REPOSITORY_BASE_URL=http://192.168.50.10:18120\n"
        f"KODI_LAB_REPOSITORY_BUNDLE_SHA256={_bundle_hash(bundle)}\n"
        f"KODI_LAB_DEVELOPMENT_SOURCE='{source}'\n"
    )
    with pytest.raises(ValueError, match="development source"):
        verify(bundle)
