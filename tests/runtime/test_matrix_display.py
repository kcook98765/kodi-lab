"""Jammy packaging is exact and remains separate from standard activation."""

from dataclasses import replace

from kodi_lab.runtime.display_capabilities import (
    reviewed_display_capability,
)
from kodi_lab.runtime.recipes import (
    load_display_candidate_recipe,
    load_recorded_reviewed_recipe,
    recipe_target_id,
)
from tests.runtime import test_recipe_installation as fixture

factory = fixture.factory


def test_matrix_r3_preserves_kodi_pins_and_signed_distro_identity() -> None:
    old = load_recorded_reviewed_recipe(
        "matrix",
        "ubuntu-jammy-matrix-19.4",
        2,
        "sha256:2f962f50580c53e9aa98447975617b2bc18552f6d8a7e645f07d90fa19641e70",
    )
    candidate = load_display_candidate_recipe("matrix")
    assert old.recipe.recipe_revision == 2
    assert candidate.recipe.recipe_revision == 4
    assert candidate.recipe.kodi_version == "19.4"
    assert candidate.recipe.provider == old.recipe.provider
    assert candidate.recipe.base_image_reference == old.recipe.base_image_reference
    assert candidate.recipe.critical_packages[:-1] == old.recipe.critical_packages
    tiger = candidate.recipe.critical_packages[-1]
    assert (tiger.name, tiger.version, tiger.sha256) == (
        "tigervnc-scraping-server",
        "1.12.0+dfsg-4ubuntu0.22.04.1",
        "sha256:eb8928727ce07aae043211e9f35390e2aafabf89b16168d43d3c608322e5a852",
    )
    for name, payload in old.assets.items():
        if name != "entrypoint.sh":
            assert candidate.assets[name] == payload
    assert b"640x360x24" in candidate.assets["supervisor.py"]
    assert b'"LP_NUM_THREADS": "1"' in candidate.assets["supervisor.py"]
    import runpy
    from pathlib import Path

    helper = runpy.run_path(
        str(
            Path(__file__).parents[2]
            / "src/kodi_lab/runtime/assembly_assets"
            / "matrix-19.4-display-r3/novnc_view_helper.py"
        )
    )
    for mode in ("view-only", "interactive"):
        options = helper["command"]("/run/kodi/test.sock", 2, mode)
        for flag in ("AcceptCutText", "SendCutText", "SetPrimary", "SendPrimary"):
            assert not any(flag in option for option in options)
        assert "-rfbport=-1" in options and "-FrameRate=2" in options
        assert "-AcceptSetDesktopSize=0" in options
        assert f"-AcceptKeyEvents={int(mode == 'interactive')}" in options
        assert f"-AcceptPointerEvents={int(mode == 'interactive')}" in options
    assert helper["CLIPBOARD_POLICY"] == "unimplemented"
    assert recipe_target_id(candidate) == recipe_target_id(old)
    capability = reviewed_display_capability(candidate)
    assert capability is not None and capability.qualification == "standard"
    assert reviewed_display_capability(replace(candidate, recipe=old.recipe)) is None
