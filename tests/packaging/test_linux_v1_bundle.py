"""Release inventory rejects changed bytes and mixed runtime metadata."""

from __future__ import annotations

import hashlib
import io
import json
import runpy
import tarfile
from pathlib import Path

import pytest

bundle_tool = runpy.run_path(str(Path(__file__).parents[2] / "scripts/linux-v1-bundle"))
verify = bundle_tool["verify"]
canonical = bundle_tool["canonical"]
digest = bundle_tool["digest"]


def _archive(path: Path, tags: tuple[str | None, ...]) -> dict[str | None, str]:
    descriptors = []
    configs = {}
    for tag in tags:
        config = json.dumps(
            {"architecture": "amd64", "os": "linux", "tag": tag}
        ).encode()
        config_hash = hashlib.sha256(config).hexdigest()
        configs[tag] = (config_hash, config)
        descriptors.append(
            {
                "Config": f"blobs/sha256/{config_hash}",
                "RepoTags": [tag] if tag else None,
            }
        )
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


@pytest.fixture
def bundle(tmp_path: Path) -> Path:
    (tmp_path / "kodi-lab-source.tar").write_bytes(b"source tree")
    app_archive = tmp_path / "kodi-lab-application-images.tar"
    app_configs = _archive(
        app_archive, ("kodi-lab-v1-web:1.0.0rc1", "kodi-lab-v1-broker:1.0.0rc1")
    )
    runtimes = []
    for name in bundle_tool["RUNTIMES"]:
        archive = tmp_path / f"{name}.tar"
        runtime_id = _archive(archive, (None,))[None]
        evidence = tmp_path / f"{name}.evidence.json"
        evidence.write_text('{"result":"passed"}\n')
        metadata = {
            "kodi_version": name.removeprefix("kodi-"),
            "result": {"image_id": runtime_id},
            "distribution": {
                "archive_digest": digest(archive),
                "archive_size": archive.stat().st_size,
                "loaded_image_id": runtime_id,
            },
            "catalog": {
                "support_tier": "current",
                "revision_id": name,
                "validation_report_digest": digest(evidence),
            },
        }
        manifest = tmp_path / f"{name}.json"
        manifest.write_bytes(canonical(metadata))
        runtimes.append(
            {
                "name": name,
                "release": metadata["kodi_version"],
                "tier": "current",
                "revision_id": name,
                "manifest": manifest.name,
                "evidence": evidence.name,
                "archive": archive.name,
                "reviewed_image_id": runtime_id,
                "archive_config_image_id": runtime_id,
            }
        )
    files = {
        p.name: {"sha256": digest(p), "size": p.stat().st_size}
        for p in tmp_path.iterdir()
    }
    document = {
        "schema": "kodi-lab-linux-v1-bundle-v1",
        "platform": "linux/amd64",
        "version": "1.0.0rc1",
        "source_archive": "kodi-lab-source.tar",
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
        "runtimes": runtimes,
        "files": files,
    }
    (tmp_path / "BUNDLE.json").write_bytes(canonical(document))
    (tmp_path / "SHA256SUMS").write_text(
        "".join(
            f"{digest(tmp_path / name).removeprefix('sha256:')}  {name}\n"
            for name in sorted((*files, "BUNDLE.json"))
        )
    )
    return tmp_path


def test_bundle_detects_missing_and_changed_artifacts(bundle: Path) -> None:
    assert verify(bundle)["version"] == "1.0.0rc1"
    document = json.loads((bundle / "BUNDLE.json").read_text())
    document["application"]["images"].pop("broker")
    (bundle / "BUNDLE.json").write_bytes(canonical(document))
    (bundle / "SHA256SUMS").write_text(
        "".join(
            f"{digest(bundle / name).removeprefix('sha256:')}  {name}\n"
            for name in sorted((*document["files"], "BUNDLE.json"))
        )
    )
    with pytest.raises(ValueError, match="application archive image identities"):
        verify(bundle)
    (bundle / "kodi-19.5.tar").write_bytes(b"altered")
    with pytest.raises(ValueError, match="artifact differs"):
        verify(bundle)


def test_bundle_rejects_local_image_selection_outside_manifest(bundle: Path) -> None:
    assert verify(bundle)["version"] == "1.0.0rc1"
    (bundle / ".env").write_text(
        "KODI_LAB_REMOTE_IMAGE=sha256:" + "0" * 64 + "\n"
        "KODI_LAB_BROKER_IMAGE=sha256:" + "0" * 64 + "\n"
        "KODI_LAB_PUBLISHED_PORT=18100\n"
    )
    with pytest.raises(ValueError, match="local image selection"):
        verify(bundle)
