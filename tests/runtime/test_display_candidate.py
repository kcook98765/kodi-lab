"""A new sealed runtime packages viewing without promoting standard admission."""

import json

import pytest

from kodi_lab.config import Settings
from kodi_lab.runtime.display_capabilities import (
    reviewed_display_capability,
)
from kodi_lab.runtime.display_validation import parse_display_validation
from kodi_lab.runtime.recipes import (
    load_display_candidate_recipe,
    load_novnc_phase1_recipe,
    load_recorded_reviewed_recipe,
    load_reviewed_recipe,
    recipe_target_id,
)
from tests.runtime import test_recipe_installation as fixture

factory = fixture.factory


def proof() -> dict[str, object]:
    capability = reviewed_display_capability(load_display_candidate_recipe("omega"))
    assert capability is not None
    return {
        "schema": "reviewed-display-packaging-v2",
        "helper_integrity_before_execution": True,
        "geometry_probe": "xlib",
        "helper_digest": capability.helper_digest,
        "package": list(capability.package),
        "width": 640,
        "height": 360,
        "lp_num_threads": 1,
        "helper_probe": True,
        "neutral_input_probe": True,
        "options_supported": True,
        "libraries": True,
    }


def test_omega_candidate_preserves_payload_defaults_and_historical_target() -> None:
    candidate = load_display_candidate_recipe("omega")
    historical = load_novnc_phase1_recipe()
    assert candidate.recipe.recipe_revision == 6
    assert {
        name: value
        for name, value in candidate.assets.items()
        if name not in {"entrypoint.sh", "supervisor.py"}
    } == {
        name: value
        for name, value in historical.assets.items()
        if name != "entrypoint.sh"
    }
    assert candidate.recipe.critical_packages == historical.recipe.critical_packages
    assert candidate.recipe.execution_profile == historical.recipe.execution_profile
    assert candidate.digest != historical.digest
    capability = reviewed_display_capability(candidate)
    assert capability is not None and capability.qualification == "standard"
    assert load_reviewed_recipe("omega").recipe.recipe_revision == 6
    assert recipe_target_id(candidate) == recipe_target_id(
        load_reviewed_recipe("omega")
    )
    assert recipe_target_id(candidate) != recipe_target_id(historical)
    assert (
        load_recorded_reviewed_recipe(
            "omega", candidate.recipe.recipe_id, 6, candidate.digest
        )
        == candidate
    )


@pytest.mark.parametrize("field", list(proof()))
def test_every_native_packaging_requirement_is_mandatory(field: str) -> None:
    capability = reviewed_display_capability(load_display_candidate_recipe("omega"))
    assert capability is not None
    report = proof()
    assert parse_display_validation(json.dumps(report).encode(), capability) == report
    del report[field]
    with pytest.raises(ValueError):
        parse_display_validation(json.dumps(report).encode(), capability)


@pytest.mark.parametrize(
    "field,value",
    [
        ("width", 1280),
        ("height", 720),
        ("lp_num_threads", 2),
        ("helper_probe", False),
        ("helper_probe", 1),
        ("lp_num_threads", True),
        ("neutral_input_probe", False),
        ("package", []),
        ("helper_digest", "sha256:" + "a" * 64),
    ],
)
def test_native_packaging_substitutions_are_rejected(field: str, value: object) -> None:
    capability = reviewed_display_capability(load_display_candidate_recipe("omega"))
    assert capability is not None
    with pytest.raises(ValueError):
        parse_display_validation(
            json.dumps({**proof(), field: value}).encode(), capability
        )


def test_conflicting_candidate_install_selectors_fail_before_state_access() -> None:
    from kodi_lab.runtime.install import (
        RecipeInstallationError,
        install_reviewed_runtime,
    )

    with pytest.raises(RecipeInstallationError, match="selectors conflict"):
        install_reviewed_runtime(
            "omega", settings=Settings(), novnc_phase1=True, display_candidate=True
        )


def test_unknown_candidate_generation_is_not_an_arbitrary_recipe_selector() -> None:
    with pytest.raises(ValueError, match="no reviewed display candidate"):
        load_display_candidate_recipe("unreviewed")


def test_substituted_helper_cannot_execute_to_forge_its_own_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import runpy
    import stat
    from pathlib import Path
    from types import SimpleNamespace

    from kodi_lab.runtime.display_validation import display_validation_command

    capability = reviewed_display_capability(load_display_candidate_recipe("omega"))
    assert capability is not None
    calls: list[str] = []
    original = Path.read_bytes

    def substituted(path: Path) -> bytes:
        if str(path) == "/usr/local/lib/kodi-lab-novnc-view.py":
            return b"print('forged expected receipt'); raise SystemExit(0)"
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", substituted)
    monkeypatch.setattr(
        Path,
        "lstat",
        lambda path: SimpleNamespace(
            st_mode=stat.S_IFREG | 0o444, st_uid=0, st_size=60
        ),
    )
    monkeypatch.setattr(
        runpy, "run_path", lambda *args, **kwargs: calls.append("executed")
    )
    with pytest.raises(AssertionError):
        exec(display_validation_command(capability)[-1], {})
    assert calls == []


def test_geometry_probe_does_not_dump_unbounded_visual_inventory() -> None:
    from kodi_lab.runtime.display_validation import display_validation_command

    capability = reviewed_display_capability(load_display_candidate_recipe("omega"))
    assert capability is not None
    program = display_validation_command(capability)[-1]
    assert "xdpyinfo" not in program
    assert "XDisplayWidth" in program and "XDisplayHeight" in program
    assert "XCloseDisplay(display)" in program
