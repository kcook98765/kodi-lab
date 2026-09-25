"""Exact curated image distribution and catalog bootstrap checks."""

import json
import shutil
from dataclasses import replace
from importlib import resources
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.runtime.distribution import (
    CuratedRuntimeError,
    bootstrap_catalog,
    load_curated_runtimes,
    verify_archive,
    verify_local_images,
)
from kodi_lab.storage import metadata
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.storage.models import RuntimeContainerArtifactRow, RuntimeDefinitionRow

RUNTIME_ROOT = Path(__file__).parents[2] / "runtime"
PACKAGE_ROOT = Path(__file__).parents[2] / "src/kodi_lab/runtime"


def _inspect(image_id: str) -> dict[str, object]:
    return {"Id": image_id, "Os": "linux", "Architecture": "amd64"}


def _factory(tmp_path: Path) -> sessionmaker[Session]:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'catalog.db'}")
    metadata.create_all(engine)
    return create_session_factory(engine)


def test_four_wheel_manifests_match_reviewed_source_and_evidence() -> None:
    runtimes = load_curated_runtimes()
    assert [item.manifest.kodi_version for item in runtimes] == [
        "19.5",
        "20.5",
        "21.3",
        "22.0-beta.2",
    ]
    for item in runtimes:
        assert (RUNTIME_ROOT / item.name / "manifest.json").read_bytes() == (
            PACKAGE_ROOT / "catalog" / f"{item.name}.json"
        ).read_bytes()
        assert item.manifest.build.platform == "linux/amd64"
        assert item.manifest.distribution is not None
        assert item.manifest.distribution.archive_digest.startswith("sha256:")
        assert item.evidence.result.value == "passed"


def test_bootstrap_is_atomic_and_repeatable(tmp_path: Path) -> None:
    factory = _factory(tmp_path)
    assert bootstrap_catalog(factory, _inspect) == 20
    assert bootstrap_catalog(factory, _inspect) == 0
    with factory() as session:
        assert session.query(RuntimeDefinitionRow).count() == 4
        assert session.query(RuntimeContainerArtifactRow).count() == 4


@pytest.mark.parametrize("case", ["missing", "tag", "wrong_id", "architecture"])
def test_bootstrap_rejects_unreviewed_or_missing_images(
    tmp_path: Path, case: str
) -> None:
    factory = _factory(tmp_path)
    first = load_curated_runtimes()[0]
    distribution = first.manifest.distribution
    assert distribution is not None
    expected = {first.artifact.image_id, distribution.loaded_image_id}

    def inspect(image_id: str) -> dict[str, object]:
        if image_id not in expected:
            return _inspect(image_id)
        if case == "missing":
            raise LookupError(image_id)
        if case == "architecture":
            return {"Id": image_id, "Os": "linux", "Architecture": "arm64"}
        if case == "tag":
            return {
                "Id": "sha256:" + "f" * 64,
                "Os": "linux",
                "Architecture": "amd64",
                "RepoTags": ["kodi-lab:19.5-phase5-r1"],
            }
        return {"Id": "sha256:" + "f" * 64, "Os": "linux", "Architecture": "amd64"}

    with pytest.raises(CuratedRuntimeError):
        bootstrap_catalog(factory, inspect)
    with factory() as session:
        assert session.query(RuntimeDefinitionRow).count() == 0


@pytest.mark.parametrize("case", ["definition", "artifact"])
def test_bootstrap_rejects_catalog_conflicts(tmp_path: Path, case: str) -> None:
    factory = _factory(tmp_path)
    bootstrap_catalog(factory, _inspect)
    first = load_curated_runtimes()[0]
    with factory.begin() as session:
        if case == "definition":
            definition_row = session.get(RuntimeDefinitionRow, str(first.definition.id))
            assert definition_row is not None
            definition_row.slug = "wrong-curated-definition"
        else:
            artifact_row = session.get(
                RuntimeContainerArtifactRow, str(first.revision.id)
            )
            assert artifact_row is not None
            artifact_row.image_id = "sha256:" + "f" * 64
    with pytest.raises(CuratedRuntimeError):
        bootstrap_catalog(factory, _inspect)


def test_packaged_manifest_rejects_wrong_platform_and_lineage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shutil.copytree(PACKAGE_ROOT / "catalog", tmp_path / "catalog")
    shutil.copytree(PACKAGE_ROOT / "evidence", tmp_path / "evidence")
    manifest_path = tmp_path / "catalog/kodi-19.5.json"
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data["build"]["platform"] = "linux/arm64"
    manifest_path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(resources, "files", lambda _name: tmp_path)
    with pytest.raises(CuratedRuntimeError, match="lineage"):
        load_curated_runtimes()


def test_verify_local_images_uses_exact_ids() -> None:
    runtimes = load_curated_runtimes()
    seen: list[str] = []

    def inspect(image_id: str) -> dict[str, object]:
        seen.append(image_id)
        return _inspect(image_id)

    verify_local_images(runtimes, inspect)
    assert seen == [item.artifact.image_id for item in runtimes]


def test_archive_loaded_id_is_accepted_as_reviewed_translation(
    tmp_path: Path,
) -> None:
    factory = _factory(tmp_path)
    runtimes = load_curated_runtimes()
    original_ids = {item.artifact.image_id for item in runtimes}

    def inspect(image_id: str) -> dict[str, object]:
        if image_id in original_ids:
            raise LookupError(image_id)
        return _inspect(image_id)

    assert bootstrap_catalog(factory, inspect) == 20
    assert bootstrap_catalog(factory, inspect) == 0
    with factory() as session:
        actual = {
            row.image_id for row in session.query(RuntimeContainerArtifactRow).all()
        }
    assert actual == {
        item.manifest.distribution.loaded_image_id
        for item in runtimes
        if item.manifest.distribution is not None
    }


def test_altered_archive_is_rejected_before_docker_load(tmp_path: Path) -> None:
    runtime = load_curated_runtimes()[0]
    distribution = runtime.manifest.distribution
    assert distribution is not None
    archive = tmp_path / "wrong.tar"
    archive.write_bytes(b"wrong reviewed artifact")
    modified = replace(
        runtime,
        manifest=runtime.manifest.model_copy(
            update={
                "distribution": distribution.model_copy(
                    update={"archive_size": archive.stat().st_size}
                )
            }
        ),
    )
    with pytest.raises(CuratedRuntimeError, match="digest"):
        verify_archive(modified, archive)
