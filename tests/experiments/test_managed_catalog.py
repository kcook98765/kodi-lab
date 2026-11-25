from pathlib import Path
from zipfile import ZipFile

import pytest

from kodi_lab.managed.catalog import approve_batch
from kodi_lab.managed.packages import Packages
from kodi_lab.managed.runtime import Refusal, Runtime


def archive(path: Path, addon: str, version: str = "1.0.0", code: str = "") -> Path:
    with ZipFile(path, "w") as bundle:
        bundle.writestr(
            f"{addon}/addon.xml",
            f'<addon id="{addon}" version="{version}" name="Catalog fixture">'
            '<extension point="xbmc.python.module" library="lib"/></addon>',
        )
        bundle.writestr(f"{addon}/lib/fixture.py", code)
    return path


def test_catalog_reuses_exact_archives_and_retains_other_entries(
    tmp_path: Path,
) -> None:
    root = tmp_path / "lab"
    root.mkdir(mode=0o700)
    original = archive(tmp_path / "a.zip", "script.module.fixture.a")
    other = archive(tmp_path / "b.zip", "script.module.fixture.b")
    receipt = approve_batch(root, [original, other], replace_versions=False)
    store = Packages(root, root / "feed")
    saved = store.catalog_file.stat()
    assert approve_batch(root, [original, other], replace_versions=False) == receipt
    assert store.catalog_file.stat().st_ino == saved.st_ino
    assert set(store.catalog()) == {row["addon"] for row in receipt}

    replacement = archive(tmp_path / "new.zip", "script.module.fixture.a", "2.0.0")
    with pytest.raises(Refusal, match="catalog_version_conflict"):
        approve_batch(root, [replacement], replace_versions=False)
    assert store.catalog_file.stat().st_ino == saved.st_ino
    approve_batch(root, [replacement], replace_versions=True)
    assert {key: package.version for key, package in store.catalog().items()} == {
        "script.module.fixture.a": "2.0.0",
        "script.module.fixture.b": "1.0.0",
    }
    changed_bytes = archive(
        tmp_path / "changed.zip", "script.module.fixture.a", "2.0.0", "# modified"
    )
    with pytest.raises(Refusal, match="catalog_version_conflict"):
        approve_batch(root, [changed_bytes], replace_versions=True)


def test_catalog_refuses_batch_with_unapproved_repository(tmp_path: Path) -> None:
    root = tmp_path / "lab"
    root.mkdir(mode=0o700)
    runtime = Runtime(root)
    valid = archive(tmp_path / "a.zip", "script.module.fixture.a")
    outside = archive(tmp_path / "outside.zip", "repository.unapproved")
    with pytest.raises(Refusal, match="invalid_addon_id"):
        approve_batch(root, [valid, outside], replace_versions=False)
    with pytest.raises(Refusal, match="duplicate_catalog_addon"):
        approve_batch(root, [valid, valid], replace_versions=False)
    with runtime.exclusive(), pytest.raises(Refusal, match="managed_busy"):
        approve_batch(root, [valid], replace_versions=False)
    assert not (root / "catalog.json").exists()
    assert list((root / "approved").iterdir()) == []
