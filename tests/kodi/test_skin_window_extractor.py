"""A skin source tree is data: extraction cannot execute it or verify semantics."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from scripts.extract_skin_windows import (
    ExtractionError,
    ExtractionLimits,
    extract_skin_windows,
    main,
)


def test_live_fixture_uses_explicit_ids_and_remains_review_only() -> None:
    source = Path(__file__).parents[1] / "fixtures" / "window-mapping-skin"
    result = extract_skin_windows(source)
    assert result["skin_id"] == "skin.kodilab.windowfixture"
    assert result["skin_version"] == "1.0.0"
    assert [(entry["xml_id"], entry["window_id"]) for entry in result["entries"]] == [
        (19001, 29001),
        (19002, 29002),
        (19003, 29003),
        (19004, 29004),
    ]
    assert all(entry["verification"] == "unverified" for entry in result["entries"])
    assert not result["warnings"]


def _skin(tmp_path: Path, *, folders: tuple[str, ...] = ("1080i",)) -> Path:
    res = "".join(f'<res folder="{folder}"/>' for folder in folders)
    (tmp_path / "addon.xml").write_text(
        '<addon id="skin.example" version="1.2.0" name="Example">'
        '<requires><import addon="xbmc.gui" version="5.17.0"/></requires>'
        f'<extension point="xbmc.gui.skin">{res}</extension></addon>'
    )
    for folder in folders:
        (tmp_path / folder).mkdir()
    return tmp_path


def _xml(source: Path, filename: str, xml: str) -> None:
    (source / "1080i" / filename).write_text(xml)


def test_extracts_only_loaded_custom_roots_and_explicit_ids(tmp_path: Path) -> None:
    source = _skin(tmp_path)
    _xml(source, "Custom_9001_PowerMenu.xml", '<window id="9001" type="dialog"/>')
    _xml(
        source,
        "cUsToM_Details.XML",
        "<WINDOW><id>1234</id><type>dialog</type></WINDOW>",
    )
    _xml(source, "DialogIgnored.xml", '<window id="9991"/>')
    _xml(
        source,
        "Custom_9011_NoId.xml",
        "<window><controls><control id='4'/></controls></window>",
    )
    nested = source / "1080i" / "not-loaded"
    nested.mkdir()
    (nested / "Custom_Nested.xml").write_text('<window id="9002"/>')
    result = extract_skin_windows(
        source, source_url="https://example.org/skin", source_revision="abc123"
    )
    assert result["skin_id"] == "skin.example"
    assert result["skin_version"] == "1.2.0"
    assert result["kind"] == "skin_mapping_candidates"
    assert result["kodi_versions"] == []
    assert result["dependencies"] == [{"addon": "xbmc.gui", "version": "5.17.0"}]
    assert result["source"]["revision"] == "abc123"
    entries = result["entries"]
    assert [(item["xml_id"], item["window_id"]) for item in entries] == [
        (1234, 11234),
        (9001, 19001),
    ]
    assert entries[1]["role"] == "dialog"
    assert entries[1]["candidate_name"] == "Power Menu"
    assert all(item["verification"] == "unverified" for item in entries)
    assert result["status"] == "partial"
    assert len(result["warnings"]) == 1


def test_always_adds_home_offset_even_for_high_xml_ids(tmp_path: Path) -> None:
    source = _skin(tmp_path)
    _xml(source, "Custom_High.xml", '<window id="11901"/>')
    _xml(
        source,
        "Home.xml",
        "<window><onload>ActivateWindow(21901)</onload><onload>ActivateWindow(11901)</onload></window>",
    )
    entry = extract_skin_windows(source)["entries"][0]
    assert entry["xml_id"] == 11901
    assert entry["window_id"] == 21901
    assert [ref["target"] for ref in entry["activation_references"]] == ["21901"]
    assert "outside_documented_custom_XML_ID_range" in entry["issues"]


def test_references_include_low_ids_filenames_and_conditions(tmp_path: Path) -> None:
    source = _skin(tmp_path)
    _xml(
        source,
        "Custom_Search.xml",
        '<window id="9003"><heading>$LOCALIZE[32001]</heading></window>',
    )
    _xml(
        source,
        "Home.xml",
        "<window><onclick>ActivateWindow(9003,return)</onclick><onclick>ReplaceWindow(Custom_Search.xml)</onclick><visible>Window.IsActive(19003)</visible></window>",
    )
    entry = extract_skin_windows(source)["entries"][0]
    assert len(entry["activation_references"]) == 3
    assert "dynamic_name_candidate" in entry["issues"]
    assert entry["verification"] == "unverified"


def test_attribute_id_and_type_take_precedence_like_kodi(tmp_path: Path) -> None:
    source = _skin(tmp_path)
    _xml(
        source,
        "Custom_Attribute.xml",
        '<window id="9001" type="dialog"><id>9002</id><type>window</type></window>',
    )
    entry = extract_skin_windows(source)["entries"][0]
    assert entry["window_id"] == 19001
    assert entry["role"] == "dialog"


def test_duplicate_and_conflicting_definitions_are_never_silently_selected(
    tmp_path: Path,
) -> None:
    source = _skin(tmp_path, folders=("1080i", "720p"))
    _xml(source, "Custom_Search.xml", '<window id="9001" type="dialog"/>')
    (source / "720p" / "Custom_Settings.xml").write_text(
        "<window><id>9001</id></window>"
    )
    entry = extract_skin_windows(source)["entries"][0]
    assert len(entry["definitions"]) == 2
    assert len(entry["source_files"]) == 2
    assert "duplicate_window_id" in entry["issues"]
    assert "conflicting_definitions" in entry["issues"]
    assert entry["name_candidates"] == ["Search", "Settings"]
    assert entry["role"] == "unknown"
    assert entry["verification"] == "unverified"


def test_same_filename_in_two_resolutions_has_duplicate_without_conflict(
    tmp_path: Path,
) -> None:
    source = _skin(tmp_path, folders=("1080i", "720p"))
    for folder in ("1080i", "720p"):
        (source / folder / "Custom_Search.xml").write_text('<window id="9001"/>')
    entry = extract_skin_windows(source)["entries"][0]
    assert "duplicate_window_id" in entry["issues"]
    assert "conflicting_definitions" not in entry["issues"]


def test_same_filename_with_different_headings_still_flags_conflict(
    tmp_path: Path,
) -> None:
    source = _skin(tmp_path, folders=("1080i", "720p"))
    for folder, heading in (("1080i", "Search"), ("720p", "Settings")):
        (source / folder / "Custom_Shared.xml").write_text(
            f'<window id="9001"><heading>{heading}</heading></window>'
        )
    entry = extract_skin_windows(source)["entries"][0]
    assert "conflicting_definitions" in entry["issues"]


@pytest.mark.parametrize(
    "xml",
    [
        "<window>",
        '<other id="9001"/>',
        '<window id="-1"/>',
        '<window id="9999"/>',
        '<window id="2147483647"/>',
        '<window id="$PARAM[id]"/>',
    ],
)
def test_bad_custom_documents_are_partial_reports(tmp_path: Path, xml: str) -> None:
    source = _skin(tmp_path)
    _xml(source, "Custom_Broken.xml", xml)
    result = extract_skin_windows(source)
    assert result["entries"] == []
    assert result["status"] == "partial"
    assert result["warnings"][0]["file"] == "1080i/Custom_Broken.xml"


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16"])
def test_dtd_and_entity_expansion_rejected_in_any_encoding(
    tmp_path: Path, encoding: str
) -> None:
    source = _skin(tmp_path)
    payload = (
        f'<?xml version="1.0" encoding="{encoding}"?>'
        '<!DOCTYPE window [<!ENTITY x "expanded">]>'
        '<window id="9001"><label>&x;</label></window>'
    )
    (source / "1080i" / "Custom_Entities.xml").write_bytes(payload.encode(encoding))
    result = extract_skin_windows(source)
    assert result["entries"] == []
    assert "DTD" in result["warnings"][0]["issue"]


def test_does_not_execute_skin_scripts(tmp_path: Path) -> None:
    source = _skin(tmp_path)
    marker = tmp_path / "executed"
    (source / "default.py").write_text(
        f"from pathlib import Path; Path({str(marker)!r}).touch()"
    )
    _xml(
        source,
        "Custom_Search.xml",
        '<window id="9001"><onload>RunScript(default.py)</onload></window>',
    )
    result = extract_skin_windows(source)
    assert len(result["entries"]) == 1
    assert not marker.exists()


def test_symlink_xml_is_skipped_and_resolution_symlinks_rejected(
    tmp_path: Path,
) -> None:
    source = _skin(tmp_path)
    outside = tmp_path / "outside.xml"
    outside.write_text('<window id="9001"/>')
    (source / "1080i" / "Custom_Symlink.xml").symlink_to(outside)
    result = extract_skin_windows(source)
    assert result["entries"] == []
    assert result["warnings"][0]["issue"] == "non_regular_XML_skipped"
    (source / "1080i" / "Custom_Symlink.xml").unlink()
    (source / "1080i").rmdir()
    (source / "1080i").symlink_to(source, target_is_directory=True)
    with pytest.raises(ExtractionError, match="symlink resolution"):
        extract_skin_windows(source)


def test_addon_symlink_and_unsafe_resolution_paths_rejected(tmp_path: Path) -> None:
    source = _skin(tmp_path)
    addon = source / "addon.xml"
    data = addon.read_text()
    addon.write_text(data.replace('folder="1080i"', 'folder="../outside"'))
    with pytest.raises(ExtractionError, match="unsafe resolution"):
        extract_skin_windows(source)
    addon.rename(source / "actual-addon.xml")
    addon.symlink_to(source / "actual-addon.xml")
    with pytest.raises(ExtractionError, match="symlink"):
        extract_skin_windows(source)


def test_fifo_is_skipped_without_opening_or_blocking(tmp_path: Path) -> None:
    source = _skin(tmp_path)
    os.mkfifo(source / "1080i" / "Custom_Pipe.xml")
    report = extract_skin_windows(source)
    assert report["entries"] == []
    assert report["warnings"][0]["issue"] == "non_regular_XML_skipped"


@pytest.mark.parametrize(
    "addon",
    [
        '<addon id="plugin.example" version="1"/>',
        '<addon id="skin.example"/>',
        '<addon id="skin.example" version="1"/>',
        "<addon",
    ],
)
def test_invalid_skin_metadata_fails_closed(tmp_path: Path, addon: str) -> None:
    (tmp_path / "addon.xml").write_text(addon)
    with pytest.raises(ExtractionError):
        extract_skin_windows(tmp_path)


def test_legacy_resolution_fallback_and_empty_maps(tmp_path: Path) -> None:
    source = _skin(tmp_path)
    addon = source / "addon.xml"
    addon.write_text(addon.read_text().replace('<res folder="1080i"/>', ""))
    result = extract_skin_windows(source)
    assert result["resolution_folders"] == ["1080i"]
    assert result["entries"] == []
    assert result["status"] == "candidates_require_review"


@pytest.mark.parametrize(
    ("limit", "value", "match"),
    [
        ("max_files", 1, "file count"),
        ("max_total_bytes", 400, "total XML size"),
        ("max_directory_entries", 1, "directory entry"),
        ("max_references", 1, "reference limit"),
    ],
)
def test_global_processing_limits(
    tmp_path: Path, limit: str, value: int, match: str
) -> None:
    source = _skin(tmp_path)
    payload = (
        '<window id="9001"><onclick>ActivateWindow(9001)</onclick>'
        "<onclick>ActivateWindow(9001)</onclick></window>"
    )
    _xml(source, "Custom_One.xml", payload)
    _xml(source, "Custom_Two.xml", payload)
    limits = ExtractionLimits(**{limit: value})
    with pytest.raises(ExtractionError, match=match):
        extract_skin_windows(source, limits=limits)


@pytest.mark.parametrize(
    ("limits", "payload", "match"),
    [
        (
            ExtractionLimits(max_file_bytes=350),
            '<window id="9001">' + " " * 400 + "</window>",
            "file size",
        ),
        (
            ExtractionLimits(max_xml_depth=4),
            '<window id="9001"><a><b><c><d/></c></b></a></window>',
            "depth limit",
        ),
        (
            ExtractionLimits(max_xml_nodes=8),
            '<window id="9001">' + "<a/>" * 10 + "</window>",
            "node limit",
        ),
    ],
)
def test_per_document_processing_limits(
    tmp_path: Path, limits: ExtractionLimits, payload: str, match: str
) -> None:
    source = _skin(tmp_path)
    _xml(source, "Custom_Bound.xml", payload)
    result = extract_skin_windows(source, limits=limits)
    assert result["entries"] == []
    assert match in result["warnings"][0]["issue"]


def test_directory_budget_counts_non_xml_files_too(tmp_path: Path) -> None:
    source = _skin(tmp_path)
    for index in range(3):
        (source / "1080i" / f"unused-{index}.txt").write_text("not XML")
    with pytest.raises(ExtractionError, match="directory entry limit"):
        extract_skin_windows(source, limits=ExtractionLimits(max_directory_entries=2))


def test_malformed_xml_consumes_cumulative_byte_budget(tmp_path: Path) -> None:
    source = _skin(tmp_path)
    _xml(source, "Custom_One.xml", "<window>" + " " * 300)
    _xml(source, "Custom_Two.xml", "<window>" + " " * 300)
    with pytest.raises(ExtractionError, match="total XML size"):
        extract_skin_windows(source, limits=ExtractionLimits(max_total_bytes=700))


def test_deterministic_reports_and_cli(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = _skin(tmp_path)
    _xml(source, "Custom_Search.xml", '<window id="9001"/>')
    first = extract_skin_windows(source)
    assert extract_skin_windows(source) == first
    assert main([str(source)]) == 0
    output: dict[str, Any] = json.loads(capsys.readouterr().out)
    assert output == first
    assert main([str(source / "missing")]) == 2
    assert "extraction failed" in capsys.readouterr().err
