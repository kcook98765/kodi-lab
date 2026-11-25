"""Matrix 19.5 retains sealed acquisition and exact Matrix recovery identity."""

from pathlib import Path

import pytest

from kodi_lab.managed.runtime import Refusal, Runtime
from kodi_lab.runtime.assembly import RecipeAssemblyError
from kodi_lab.runtime.assembly import build_context as ordinary_context
from kodi_lab.runtime.display_capabilities import reviewed_display_capability
from kodi_lab.runtime.matrix_candidate import (
    MatrixAcquisitionError,
    _load,
    _match_packages,
    build_context,
)
from kodi_lab.runtime.recipes import (
    MATRIX_CANDIDATE_DIGEST,
    load_installation_recipe,
    load_recorded_reviewed_recipe,
    recipe_target_id,
)


def test_standard_selection_reuses_exact_assets_and_preserves_19_4() -> None:
    candidate, recipe, closure, _ = _load()
    stable = load_recorded_reviewed_recipe(
        "matrix",
        "ubuntu-jammy-matrix-19.4",
        4,
        "sha256:0e16005b14c643dc999309bb7ef116fc8c76bd525d0487338b810c15cf7b75ac",
    )
    assert stable.recipe.kodi_version == "19.4"
    assert candidate == load_installation_recipe("matrix")
    assert recipe_target_id(candidate) != recipe_target_id(stable)
    assert reviewed_display_capability(candidate) is not None
    assert candidate.assets == stable.assets
    assert recipe.base_image_reference == stable.recipe.base_image_reference
    assert candidate.digest == MATRIX_CANDIDATE_DIGEST
    assert {p["name"] for p in closure["packages"] if p["index"] == "ppa/main"} == {
        "kodi",
        "kodi-bin",
    }
    assert (
        load_recorded_reviewed_recipe(
            "matrix", recipe.recipe_id, recipe.recipe_revision, candidate.digest
        )
        == candidate
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
        return f"{observed} {digest}"

    monkeypatch.setattr("kodi_lab.managed.runtime.docker", inspect)
    observed = "sha256:" + "b" * 64
    digest = MATRIX_CANDIDATE_DIGEST
    with pytest.raises(Refusal, match="reviewed_image_required"):
        runtime.expected_release()
    observed = expected
    assert runtime.expected_release() == (19, 5)
    digest = "sha256:0e16005b14c643dc999309bb7ef116fc8c76bd525d0487338b810c15cf7b75ac"
    assert runtime.expected_release() == (19, 4)
    digest = "sha256:" + "c" * 64
    with pytest.raises(Refusal, match="reviewed_image_required"):
        runtime.expected_release()
