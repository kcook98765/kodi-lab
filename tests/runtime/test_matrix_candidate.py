"""The disposable release accepts only its signed provenance and configured image."""

from pathlib import Path

import pytest

from kodi_lab.managed.runtime import Runtime
from kodi_lab.runtime.assembly import RecipeAssemblyError
from kodi_lab.runtime.assembly import build_context as ordinary_context
from kodi_lab.runtime.matrix_candidate import (
    MatrixAcquisitionError,
    _load,
    _match_packages,
    build_context,
)
from kodi_lab.runtime.recipes import (
    MATRIX_CANDIDATE_DIGEST,
    load_recorded_reviewed_recipe,
    load_reviewed_recipe,
)


def test_disposable_recipe_reuses_exact_assets_and_has_no_install_selection() -> None:
    candidate, recipe, closure, _ = _load()
    stable = load_reviewed_recipe("matrix")
    assert stable.recipe.kodi_version == "19.4"
    assert candidate.assets == stable.assets
    assert recipe.base_image_reference == stable.recipe.base_image_reference
    assert candidate.digest == MATRIX_CANDIDATE_DIGEST
    assert {p["name"] for p in closure["packages"] if p["index"] == "ppa/main"} == {
        "kodi",
        "kodi-bin",
    }
    with pytest.raises(ValueError):
        load_recorded_reviewed_recipe(
            "matrix", recipe.recipe_id, recipe.recipe_revision, candidate.digest
        )
    with pytest.raises(RecipeAssemblyError, match="sealed offline"):
        ordinary_context(candidate)


def test_build_context_refuses_substituted_package(tmp_path: Path) -> None:
    _, _, closure, _ = _load()
    first = Path(closure["packages"][0]["path"]).name
    (tmp_path / first).write_bytes(b"substituted package")
    with pytest.raises(MatrixAcquisitionError, match="reviewed artifact differs"):
        build_context(tmp_path)


def test_signed_package_record_requires_exact_architecture() -> None:
    import gzip

    _, _, closure, _ = _load()
    package = next(p for p in closure["packages"] if p["name"] == "kodi")
    index = closure["indexes"][package["index"]]
    stanza = (
        f"Package: {package['name']}\nVersion: {package['version']}\n"
        f"Architecture: {package['architecture']}\nFilename: {package['path']}\n"
        f"SHA256: {package['sha256']}\nSize: {package['size']}\n"
    )
    _match_packages(index, gzip.compress(stanza.encode()), [package])
    wrong = stanza.replace("Architecture: all", "Architecture: amd64")
    with pytest.raises(MatrixAcquisitionError, match="signed package record differs"):
        _match_packages(index, gzip.compress(wrong.encode()), [package])


def test_candidate_release_requires_exact_admitted_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handoff = tmp_path / "handoff"
    expected = "sha256:" + "a" * 64
    handoff.write_text(expected)
    root = tmp_path / "runtime"
    root.mkdir(mode=0o700)
    runtime = Runtime(root, generation="matrix", reviewed_image=handoff)

    def inspect(*args: str) -> str:
        assert args[:2] == ("image", "inspect") and args[-1] == expected
        return f"{observed} {MATRIX_CANDIDATE_DIGEST}"

    monkeypatch.setattr("kodi_lab.managed.runtime.docker", inspect)
    observed = "sha256:" + "b" * 64
    assert runtime.expected_release() == (19, 4)
    observed = expected
    assert runtime.expected_release() == (19, 5)
