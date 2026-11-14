"""Old immutable images remain a prerequisite before successor interruption."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import install
from tests.packaging.test_https_ingress_installer import installed_bundle
from tests.packaging.test_linux_v1_bundle import bundle as bundle_fixture

bundle = bundle_fixture


class LocalImages:
    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.commands: list[list[str]] = []
        manifest = json.loads((root / "bundle/BUNDLE.json").read_text())
        self.images = {
            item["saved_image_id"]: (
                f"{item['saved_image_id']} linux/amd64 {manifest['source_tree']} "
                f"{manifest['repository']['migration_head']} {manifest['version']}"
            )
            for item in manifest["application"]["images"].values()
        }
        self.tags: dict[str, str] = {}
        base_run = install.run

        def run(command: list[str], cwd: Path, *, capture: bool = False) -> str:
            if command[:2] != ["docker", "image"]:
                return base_run(command, cwd, capture=capture)
            self.commands.append(command)
            if command[2] == "inspect":
                selected = self.tags.get(command[-1], command[-1])
                if selected not in self.images:
                    raise subprocess.CalledProcessError(1, command)
                return selected if command[-2] == "{{.Id}}" else self.images[selected]
            assert command[2] == "tag"
            assert command[-2] in self.images
            self.tags[command[-1]] = command[-2]
            return ""

        monkeypatch.setattr(install, "run", run)


@pytest.fixture
def installation(
    bundle: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, LocalImages]:
    root = tmp_path / "old"
    data = installed_bundle(bundle, root)
    (root / "compose.windows.yaml").write_text(install.windows_overlay(18121))
    install.save(root, data, "ready")
    monkeypatch.setattr(install, "host_kind", lambda: "windows-wsl2")
    monkeypatch.setattr(install, "engine", lambda *_: "engine-one")
    monkeypatch.setattr(shutil, "disk_usage", lambda _: SimpleNamespace(free=20 << 30))
    return root, LocalImages(root, monkeypatch)


def inventory(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()
    }


def test_retains_exact_ids_idempotently_without_changing_site_or_running_services(
    installation: tuple[Path, LocalImages], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, images = installation
    before = inventory(root)
    monkeypatch.setattr(
        sys, "argv", ["install.py", "retain-images", "--directory", str(root)]
    )
    assert install.main() == 0
    assert install.main() == 0
    assert sorted(images.tags.values()) == sorted(images.images)
    assert sum(command[2] == "tag" for command in images.commands) == 2
    assert all("kodi-lab-012345abcdef" in tag for tag in images.tags)
    assert inventory(root) == before


@pytest.mark.parametrize("role", ["web", "broker"])
def test_missing_image_stops_successor_before_site_writes_or_service_commands(
    installation: tuple[Path, LocalImages], tmp_path: Path, role: str
) -> None:
    old, images = installation
    manifest = json.loads((old / "bundle/BUNDLE.json").read_text())
    del images.images[manifest["application"]["images"][role]["saved_image_id"]]
    root = tmp_path / "successor"
    root.mkdir()
    shutil.copytree(old / "bundle", root / "bundle")
    before = inventory(old), inventory(root)
    with pytest.raises(ValueError, match="Verify the original retained bundle"):
        install.configure_successor(
            argparse.Namespace(directory=str(root), previous_installation=str(old))
        )
    assert (inventory(old), inventory(root)) == before
    assert not images.tags
    assert all(command[2] == "inspect" for command in images.commands)


@pytest.mark.parametrize(
    "tamper", ["archive", "receipt", "engine", "pin", "headroom", "partial"]
)
def test_retention_refuses_failed_existing_prerequisites_before_any_docker_mutation(
    installation: tuple[Path, LocalImages],
    monkeypatch: pytest.MonkeyPatch,
    tamper: str,
) -> None:
    root, images = installation
    if tamper == "archive":
        (root / "bundle/kodi-lab-application-images.tar").unlink()
    elif tamper == "receipt":
        path = root / "INSTALLATION.json"
        data = json.loads(path.read_text())
        data["bundle_sha256"] = "f" * 64
        path.write_text(json.dumps(data))
    elif tamper == "engine":
        monkeypatch.setattr(install, "engine", lambda *_: "another-engine")
    elif tamper == "pin":
        path = root / "bundle/.env"
        path.write_text(
            path.read_text().replace(next(iter(images.images)), "sha256:" + "f" * 64)
        )
    elif tamper == "headroom":
        monkeypatch.setattr(
            shutil, "disk_usage", lambda _: SimpleNamespace(free=7 << 30)
        )
    else:
        path = root / "INSTALLATION.json"
        data = json.loads(path.read_text())
        data["completed_stage"] = "created"
        path.write_text(json.dumps(data))
    before = inventory(root)
    monkeypatch.setattr(
        sys, "argv", ["install.py", "retain-images", "--directory", str(root)]
    )
    assert install.main() == 1
    assert inventory(root) == before
    assert not images.commands


def test_successor_configuration_retains_old_ids_and_preserves_original_evidence(
    installation: tuple[Path, LocalImages], tmp_path: Path
) -> None:
    old, images = installation
    root = tmp_path / "successor"
    root.mkdir()
    shutil.copytree(old / "bundle", root / "bundle")
    before = inventory(old), inventory(root / "bundle")
    install.configure_successor(
        argparse.Namespace(directory=str(root), previous_installation=str(old))
    )
    assert sorted(images.tags.values()) == sorted(images.images)
    assert install.load(root)["previous_receipt"] == str(old / "INSTALLATION.json")
    assert (inventory(old), inventory(root / "bundle")) == before
    assert all(command[2] in {"inspect", "tag"} for command in images.commands)


@pytest.mark.parametrize(
    "old,new",
    [
        ("linux/amd64", "linux/arm64"),
        ("a" * 40, "b" * 40),
        ("0007_production_navigation", "0014_human_reset_bridge"),
        ("1.0.0rc1", "1.0.0"),
    ],
)
def test_image_identity_drift_is_not_hidden_by_retention_tags(
    installation: tuple[Path, LocalImages], old: str, new: str
) -> None:
    root, images = installation
    first = next(iter(images.images))
    assert old in images.images[first]
    images.images[first] = images.images[first].replace(old, new)
    with pytest.raises(ValueError, match="image identity differs"):
        install.retain_images(root, install.load(root))
    assert not images.tags


def test_foreign_retention_reference_is_never_overwritten(
    installation: tuple[Path, LocalImages],
) -> None:
    root, images = installation
    manifest = json.loads((root / "bundle/BUNDLE.json").read_text())
    first = manifest["application"]["images"]["web"]["saved_image_id"]
    second = manifest["application"]["images"]["broker"]["saved_image_id"]
    reference = f"kodi-lab-retained/kodi-lab-012345abcdef/web:{first[7:]}"
    images.tags[reference] = second
    with pytest.raises(ValueError, match="already differs"):
        install.retain_images(root, install.load(root))
    assert images.tags == {reference: second}
    assert all(command[2] == "inspect" for command in images.commands)
