from __future__ import annotations

from pathlib import Path
from runpy import run_path

import pytest

from kodi_lab.broker import passive_view_helper
from kodi_lab.runtime.assembly import build_context
from kodi_lab.runtime.recipes import (
    load_installation_recipe,
    load_novnc_phase1_recipe,
    load_reviewed_recipe,
)


@pytest.mark.parametrize("generation", ["matrix", "nexus", "omega", "piers"])
def test_passive_server_preserves_each_sealed_policy(
    generation: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sealed = tmp_path / "sealed-helper.py"
    sealed.write_bytes(
        load_installation_recipe(generation).assets["novnc_view_helper.py"]
    )
    monkeypatch.setattr(passive_view_helper, "SEALED_HELPER", str(sealed))

    socket = "/run/kodi/private/rfb.sock"
    reference = run_path(str(sealed))["command"](socket, 2)
    args = passive_view_helper.command(socket, 2)
    assert args == [
        "-MaxConnectionTime=0" if arg == "-MaxConnectionTime=600" else arg
        for arg in reference
    ]
    assert args[0] == "/usr/bin/X0tigervnc"
    assert "-display=:99" in args and "-rfbport=-1" in args
    assert "-FrameRate=2" in args and "-rfbunixmode=0600" in args
    for capability in (
        "AcceptKeyEvents",
        "AcceptPointerEvents",
        "AcceptSetDesktopSize",
    ):
        assert f"-{capability}=0" in args
    for capability in ("AcceptCutText", "SendCutText", "SetPrimary", "SendPrimary"):
        if generation in {"matrix", "nexus"}:
            assert all(not arg.startswith(f"-{capability}") for arg in args)
        else:
            assert f"-{capability}=0" in args
    with pytest.raises(ValueError):
        passive_view_helper.command(socket, 30)


def test_historical_experimental_recipe_is_pinned_and_separate_from_standard() -> None:
    import io
    import tarfile

    assert load_reviewed_recipe("omega").recipe.recipe_revision == 6
    candidate = load_novnc_phase1_recipe()
    assert candidate.recipe.recipe_revision == 4
    assert "novnc_view_helper.py" in candidate.assets
    assert candidate.recipe.critical_packages[-1].name == "tigervnc-scraping-server"
    with tarfile.open(fileobj=io.BytesIO(build_context(candidate))) as archive:
        entry = archive.extractfile("Dockerfile")
        assert entry is not None
        content = entry.read()
    assert b"--chown=root:root novnc_view_helper.py" in content
    assert b"chmod 0444 /usr/local/lib/kodi-lab-novnc-view.py" in content


@pytest.mark.parametrize("generation", ["matrix", "nexus", "omega", "piers"])
def test_sealed_interactive_helper_and_limits_are_retained(generation: str) -> None:
    helper = load_installation_recipe(generation).assets["novnc_view_helper.py"]
    assert b"-MaxConnectionTime=600" in helper
    assert b"LIFETIME_SECONDS = 600.0" in helper
    assert b"check_input(release=True)" in helper
