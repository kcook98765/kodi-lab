from __future__ import annotations

import pytest

from kodi_lab.runtime.assembly import build_context
from kodi_lab.runtime.assembly_assets.novnc_view_helper import command
from kodi_lab.runtime.recipes import load_novnc_phase1_recipe, load_reviewed_recipe


def test_server_itself_refuses_input_clipboard_resize_and_tcp() -> None:
    args = command("/run/kodi/private/rfb.sock", 10)
    assert args[0] == "/usr/bin/X0tigervnc"
    assert "-display=:99" in args and "-rfbport=-1" in args
    for capability in (
        "AcceptKeyEvents",
        "AcceptPointerEvents",
        "AcceptCutText",
        "SendCutText",
        "AcceptSetDesktopSize",
        "SetPrimary",
        "SendPrimary",
    ):
        assert f"-{capability}=0" in args
    assert "-rfbunixmode=0600" in args
    assert "-FrameRate=10" in args
    assert "-FrameRate=15" in command("/run/kodi/private/rfb.sock", 15)
    with pytest.raises(ValueError):
        command("/run/kodi/private/rfb.sock", 30)


def test_candidate_recipe_is_pinned_and_does_not_replace_default() -> None:
    import io
    import tarfile

    assert load_reviewed_recipe("omega").recipe.recipe_revision == 2
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


def test_interactive_flags_are_closed_and_other_capabilities_stay_denied() -> None:
    args = command("/run/kodi/private/rfb.sock", 2, "interactive")
    assert "-AcceptKeyEvents=1" in args and "-AcceptPointerEvents=1" in args
    for capability in (
        "AcceptCutText",
        "SendCutText",
        "AcceptSetDesktopSize",
        "SetPrimary",
        "SendPrimary",
    ):
        assert f"-{capability}=0" in args
    assert "-rfbport=-1" in args and "-MaxConnectionTime=600" in args
    with pytest.raises(ValueError):
        command("/run/kodi/private/rfb.sock", 2, "arbitrary")
    assert "-AcceptKeyEvents=0" in command("/run/kodi/private/rfb.sock", 2)
