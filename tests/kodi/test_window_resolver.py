from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema  # type: ignore[import-untyped]
import pytest
from pydantic import ValidationError

from kodi_lab.kodi.ui_state import DialogState, UiObservation, WindowState
from kodi_lab.kodi.ui_window_resolution import (
    SkinContext,
    collect_skin_context,
    needs_skin_context,
    resolve_ui_observation,
)
from kodi_lab.kodi.window_resolver import (
    BUILTIN_ROOT,
    MappingDocument,
    WindowResolver,
    builtin_resolver,
    read_document,
)

MAPPINGS = sorted((BUILTIN_ROOT / "kodi").glob("*.json")) + sorted(
    (BUILTIN_ROOT / "skins").glob("*.json")
)


@pytest.mark.parametrize("path", MAPPINGS, ids=lambda p: p.name)
def test_all_bundled_mappings_are_valid_and_resolvable(path: Path) -> None:
    schema = json.loads((BUILTIN_ROOT / "schema.json").read_text())
    jsonschema.Draft202012Validator(schema).validate(json.loads(path.read_text()))
    document = read_document(path)
    assert len(document.source.revision) == 40
    assert document.source.urls
    resolver = builtin_resolver()
    for entry in document.entries:
        for major in document.kodi_versions:
            resolved = resolver.resolve(
                entry.window_id,
                None,
                kodi_version=f"{major}.0",
                skin_id=document.skin_id,
                skin_version=document.skin_versions[0]
                if document.skin_versions
                else None,
            )
            if entry.kodi_min_version:
                resolved = resolver.resolve(
                    entry.window_id, None, kodi_version=entry.kodi_min_version
                )
            assert resolved.status == entry.verification
            assert resolved.name == (
                entry.name if entry.verification == "verified" else None
            )


def test_schema_is_generated_from_runtime_validator() -> None:
    assert (
        json.loads((BUILTIN_ROOT / "schema.json").read_text())
        == MappingDocument.model_json_schema()
    )


@pytest.mark.parametrize("version", ["19.4", "20.5", "21.3", "22.0-rc.1"])
def test_core_windows_and_dialogs_across_versions(version: str) -> None:
    resolver = builtin_resolver()
    assert resolver.resolve(10000, None, kodi_version=version).name == "Home"
    assert resolver.resolve(10100, None, kodi_version=version).role == "dialog"
    assert resolver.resolve(12005, None, kodi_version=version).role == "playback"
    assert resolver.resolve(12998, None, kodi_version=version).status == "ambiguous"


@pytest.mark.parametrize(
    "major,window,present",
    [
        (19, 12008, False),
        (20, 12008, True),
        (20, 12004, False),
        (21, 12004, True),
        (21, 12301, False),
        (22, 12301, True),
        (21, 12015, True),
        (22, 12015, False),
    ],
)
def test_version_specific_additions_and_removals(
    major: int, window: int, present: bool
) -> None:
    assert (
        builtin_resolver().resolve(window, None, kodi_version=f"{major}.0").status
        == "verified"
    ) is present


@pytest.mark.parametrize(
    "version,expected",
    [
        ("22", False),
        ("22.0-alpha.2", False),
        ("22.0-beta.1", False),
        ("22.0-beta.2", False),
        ("22.0-rc.1", True),
        ("22.0", True),
    ],
)
def test_22_preview_floor(version: str, expected: bool) -> None:
    assert (
        builtin_resolver().resolve(10839, None, kodi_version=version).status
        == "verified"
    ) is expected


def test_active_skin_controls_shared_id_and_runtime_offset() -> None:
    resolver = builtin_resolver()
    confluence = resolver.resolve(
        11112,
        None,
        kodi_version="20.5",
        skin_id="skin.confluence",
        skin_version="4.8.4",
    )
    estouchy = resolver.resolve(
        11112, None, kodi_version="20.5", skin_id="skin.estouchy", skin_version="3.0.8"
    )
    assert confluence.name == "Side menu help"
    assert estouchy.name == "Volume control"
    assert (
        resolver.resolve(
            1112,
            None,
            kodi_version="20.5",
            skin_id="skin.estouchy",
            skin_version="3.0.8",
        ).status
        == "unknown"
    )


@pytest.mark.parametrize(
    "context",
    [
        dict(),
        dict(skin_id="skin.estouchy"),
        dict(skin_id="skin.unrelated", skin_version="3.0.8"),
        dict(skin_id="skin.estouchy", skin_version="3.0.9"),
    ],
)
def test_no_guess_without_exact_skin_version(context: dict[str, str]) -> None:
    value = builtin_resolver().resolve(11112, None, kodi_version="20.5", **context)
    assert value.source == "classification" and value.status == "classified"


def test_skin_kodi_mismatch_and_unknown_version() -> None:
    resolver = builtin_resolver()
    assert (
        resolver.resolve(
            11112,
            None,
            kodi_version="19.4",
            skin_id="skin.estouchy",
            skin_version="3.0.8",
        ).source
        == "classification"
    )
    for version in (None, "23.0", "invalid"):
        assert resolver.resolve(10000, None, kodi_version=version).status == "unknown"


@pytest.mark.parametrize(
    "window,name",
    [
        (11177, "Custom skin window"),
        (13001, "Python window"),
        (14001, "Add-on window"),
        (98765, None),
    ],
)
def test_conservative_generic_classification(window: int, name: str | None) -> None:
    result = builtin_resolver().resolve(window, None, kodi_version="21.3")
    assert result.name == name and result.status != "verified"


def test_reported_name_preserves_localized_name_and_original_fields() -> None:
    value = UiObservation(
        window=WindowState(id=10000, label="Accueil"),
        dialog=DialogState(id=10100, label="Oui / Non", active=True),
    )
    result = resolve_ui_observation(value, kodi_version="21.3")
    assert result.window.id == value.window.id and result.window.label == "Accueil"
    assert result.dialog.id == value.dialog.id and result.dialog.label == "Oui / Non"
    assert result.window.resolution and result.window.resolution.source == "reported"
    assert result.dialog.resolution and result.dialog.resolution.name == "Oui / Non"
    assert result.summary == "Oui / Non dialog open."
    assert UiObservation.model_validate_json(result.model_dump_json()) == result


@pytest.mark.parametrize("label", [None, "", "11999", "Window 11999", "unknown"])
def test_invalid_reported_name_falls_back_without_changing_it(
    label: str | None,
) -> None:
    result = resolve_ui_observation(
        UiObservation(window=WindowState(id=10000, label=label)), kodi_version="21.3"
    )
    assert result.window.label == label and result.window.id == 10000
    assert result.window.resolution and result.window.resolution.name == "Home"


def test_dialog_resolution_does_not_replace_base_window() -> None:
    value = UiObservation(
        window=WindowState(id=10025), dialog=DialogState(id=12002, active=True)
    )
    result = resolve_ui_observation(value, kodi_version="21.3")
    assert result.window.resolution and result.window.resolution.role == "library"
    assert result.dialog.resolution and result.dialog.resolution.role == "dialog"
    assert "dialog open" in result.summary
    assert resolve_ui_observation(value, kodi_version="21.3").summary == result.summary
    assert result.focus == value.focus and result.container == value.container


def extension(
    *,
    window: int = 11112,
    name: str = "Local volume control",
    verification: str = "verified",
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "kind": "skin",
        "skin_id": "skin.estouchy",
        "skin_versions": ["3.0.8"],
        "kodi_versions": [20],
        "source": {
            "repository": "local review",
            "revision": "review-1",
            "version": "3.0.8",
            "urls": ["https://example.test/review"],
        },
        "entries": [
            {
                "window_id": window,
                "name": name,
                "role": "settings",
                "verification": verification,
            }
        ],
    }


def test_local_correction_precedence_and_immutable_builtin(tmp_path: Path) -> None:
    original = builtin_resolver().resolve(
        11112, None, kodi_version="20.5", skin_id="skin.estouchy", skin_version="3.0.8"
    )
    (tmp_path / "10-correction.json").write_text(json.dumps(extension()))
    (tmp_path / "20-correction.json").write_text(
        json.dumps(extension(name="Reviewed volume"))
    )
    resolver = WindowResolver(local_root=tmp_path)
    result = resolver.resolve(
        11112, None, kodi_version="20.5", skin_id="skin.estouchy", skin_version="3.0.8"
    )
    assert result.name == "Reviewed volume"
    assert (
        builtin_resolver().resolve(
            11112,
            None,
            kodi_version="20.5",
            skin_id="skin.estouchy",
            skin_version="3.0.8",
        )
        == original
    )
    # Reload is explicit construction/restart, no hidden request-time IO.
    (tmp_path / "20-correction.json").write_text(
        json.dumps(extension(name="Next restart"))
    )
    assert (
        resolver.resolve(
            11112,
            None,
            kodi_version="20.5",
            skin_id="skin.estouchy",
            skin_version="3.0.8",
        ).name
        == "Reviewed volume"
    )


@pytest.mark.parametrize(
    "fault",
    [
        "duplicate_id",
        "duplicate_key",
        "invalid",
        "core_override",
        "core_document",
        "unknown_field",
        "symlink",
        "oversize",
        "future_schema",
    ],
)
def test_invalid_extensions_never_break_inspection(tmp_path: Path, fault: str) -> None:
    document = extension()
    path = tmp_path / "bad.json"
    if fault == "duplicate_id":
        document["entries"] *= 2
    elif fault == "core_override":
        document["entries"][0]["window_id"] = 10000
    elif fault == "core_document":
        document.update(kind="kodi", skin_id=None, skin_versions=[])
    elif fault == "unknown_field":
        document["execute"] = "no"
    elif fault == "future_schema":
        document["schema_version"] = 2
    path.write_text(json.dumps(document))
    if fault == "invalid":
        path.write_text("{")
    elif fault == "duplicate_key":
        path.write_text('{"schema_version":1,"schema_version":1}')
    elif fault == "oversize":
        path.write_bytes(b" " * (2 * 1024 * 1024 + 1))
    elif fault == "symlink":
        path.unlink()
        path.symlink_to(BUILTIN_ROOT / "kodi" / "20.json")
    resolver = WindowResolver(local_root=tmp_path)
    assert len(resolver.errors) == 1
    assert resolver.resolve(10000, None, kodi_version="20.5").name == "Home"


@pytest.mark.parametrize("verification", ["ambiguous", "unverified"])
def test_uncertain_local_entry_never_claims_a_name(
    tmp_path: Path, verification: str
) -> None:
    (tmp_path / "candidate.json").write_text(
        json.dumps(extension(verification=verification))
    )
    value = WindowResolver(local_root=tmp_path).resolve(
        11112, None, kodi_version="20.5", skin_id="skin.estouchy", skin_version="3.0.8"
    )
    assert value.name is None and value.status == verification


def custom_observation() -> UiObservation:
    return UiObservation(window=WindowState(id=11112), dialog=DialogState(active=False))


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "skin_changed",
        "foreground_changed",
        "version_changed",
        "missing_skin",
        "version_missing",
        "disabled",
        "error",
    ],
)
def test_skin_context_is_fresh_and_failures_are_optional(fault: str | None) -> None:
    from kodi_lab.kodi.ui_state_native import UiObservationError

    calls = []

    def call(method: str, params: dict[str, Any] | None) -> object:
        calls.append(method)
        if fault == "error":
            raise UiObservationError("target_unreachable")
        if method == "GUI.GetProperties":
            if fault == "missing_skin":
                return {}
            return {
                "skin": {
                    "id": "skin.other"
                    if len(calls) == 4 and fault == "skin_changed"
                    else "skin.estouchy"
                },
                "currentwindow": {
                    "id": 11113
                    if len(calls) == 4 and fault == "foreground_changed"
                    else 11112
                },
            }
        return {
            "addon": {
                "addonid": "skin.estouchy",
                "version": None
                if fault == "version_missing"
                else "3.0.9"
                if fault == "version_changed" and len(calls) == 3
                else "3.0.8",
                "enabled": fault != "disabled",
            }
        }

    initial = custom_observation()
    value, context = collect_skin_context(call, initial, recollect=custom_observation)
    assert context == (SkinContext("skin.estouchy", "3.0.8") if fault is None else None)
    assert value == initial
    assert len(calls) <= 4


def test_skin_context_skipped_when_not_needed_and_discarded_after_instability() -> None:
    resolver = builtin_resolver()
    assert needs_skin_context(custom_observation(), "20.5", resolver)
    named = UiObservation(window=WindowState(id=11112, label="Volume"))
    assert not needs_skin_context(named, "20.5", resolver)
    core = UiObservation(window=WindowState(id=10000))
    assert not needs_skin_context(core, "20.5", resolver)
    changed = custom_observation().model_copy(
        update={"changed_during_collection": True}
    )
    assert not needs_skin_context(changed, "20.5", resolver)
    result = resolve_ui_observation(
        changed, kodi_version="20.5", skin=SkinContext("skin.estouchy", "3.0.8")
    )
    assert (
        result.window.resolution and result.window.resolution.source == "classification"
    )


def test_response_schema_two_preserves_original_ids_and_labels() -> None:
    observed = {
        "schema_version": 2,
        "window": {"id": 10000, "label": None},
        "dialog": {"id": None, "label": None, "active": False},
    }
    parsed = UiObservation.model_validate(observed)
    enriched = resolve_ui_observation(parsed, kodi_version="21.3")
    jsonschema.validate(
        enriched.model_dump(mode="json"), UiObservation.model_json_schema()
    )
    assert enriched.schema_version == 2
    for key in ("window", "dialog"):
        for field in ("id", "label"):
            assert enriched.model_dump()[key][field] == observed[key][field]  # type: ignore[index]


def test_duplicate_ids_and_malformed_bundled_files_are_development_errors(
    tmp_path: Path,
) -> None:
    (tmp_path / "skins").mkdir()
    document = extension()
    document["entries"] *= 2
    (tmp_path / "skins" / "bad.json").write_text(json.dumps(document))
    with pytest.raises(ValidationError):
        WindowResolver(builtin_root=tmp_path)


@pytest.mark.parametrize(
    "window,label,name",
    [
        (13000, "System", "Python window"),
        (13001, "Immediate HDD spindown", "Python window"),
        (14000, "Stack", "Add-on window"),
    ],
)
def test_unrelated_localization_strings_are_not_dynamic_window_names(
    window: int, label: str, name: str
) -> None:
    result = resolve_ui_observation(
        UiObservation(window=WindowState(id=window, label=label)),
        kodi_version="22.0-rc.1",
    )
    assert result.window.label == label
    assert result.window.resolution and result.window.resolution.name == name
    assert result.window.resolution.status == "classified"


def test_recollection_is_required_for_skin_association_and_failures_propagate() -> None:
    from kodi_lab.kodi.ui_state_native import UiObservationError

    initial = custom_observation()
    refreshed = initial.model_copy(update={"summary": "Fresh collection"})

    def rpc(method: str, params: dict[str, Any] | None) -> object:
        if method == "GUI.GetProperties":
            return {"skin": {"id": "skin.estouchy"}, "currentwindow": {"id": 11112}}
        return {
            "addon": {"addonid": "skin.estouchy", "version": "3.0.8", "enabled": True}
        }

    result, skin = collect_skin_context(rpc, initial, recollect=lambda: refreshed)
    assert result is refreshed and skin == SkinContext("skin.estouchy", "3.0.8")

    def revoked() -> UiObservation:
        raise UiObservationError("identity_mismatch")

    with pytest.raises(UiObservationError, match="identity_mismatch"):
        collect_skin_context(rpc, initial, recollect=revoked)

    def unavailable() -> UiObservation:
        raise UiObservationError("target_unreachable")

    retained, context = collect_skin_context(rpc, initial, recollect=unavailable)
    assert retained is initial and context is None


def test_extractor_candidate_report_cannot_become_a_local_mapping(
    tmp_path: Path,
) -> None:
    report = extension()
    report["kind"] = "skin_mapping_candidates"
    (tmp_path / "candidates.json").write_text(json.dumps(report))
    resolver = WindowResolver(local_root=tmp_path)
    assert len(resolver.errors) == 1
    assert (
        resolver.resolve(
            11112,
            None,
            kodi_version="20.5",
            skin_id="skin.estouchy",
            skin_version="3.0.8",
        ).name
        == "Volume control"
    )
