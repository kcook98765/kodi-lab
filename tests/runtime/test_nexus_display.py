"""Noble packaging is exact and remains separate from standard activation."""

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


def test_nexus_r3_preserves_kodi_pins_and_signed_distro_identity() -> None:
    old = load_recorded_reviewed_recipe(
        "nexus",
        "ubuntu-noble-nexus-20.5",
        2,
        "sha256:9845a930212f91c20e1313f03cc289882b8555f6067a5bf22e11e724b9821631",
    )
    candidate = load_display_candidate_recipe("nexus")
    assert old.recipe.recipe_revision == 2
    assert candidate.recipe.recipe_revision == 4
    assert candidate.recipe.kodi_version == "20.5"
    assert candidate.recipe.provider == old.recipe.provider
    assert candidate.recipe.base_image_reference == old.recipe.base_image_reference
    assert candidate.recipe.critical_packages[:-1] == old.recipe.critical_packages
    tiger = candidate.recipe.critical_packages[-1]
    assert (tiger.name, tiger.version, tiger.sha256) == (
        "tigervnc-scraping-server",
        "1.13.1+dfsg-2build2",
        "sha256:f3844df856a95e4b306a61c03fa75433599d247ed2f9551df15003b191a8d054",
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
            / "nexus-20.5-display-r3/novnc_view_helper.py"
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
