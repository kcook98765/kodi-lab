import os
from pathlib import Path

import pytest

from kodi_lab.development import (
    MAX_ADDON_XML_BYTES,
    DevelopmentSourceValidationCode,
    DevelopmentSourceValidationError,
    DevelopmentSourceValidator,
)


def addon_xml(
    *,
    addon_id: str = "plugin.video.example-test",
    name: str = "Example",
    version: str = "1.2.3",
    extensions: tuple[str, ...] = ("xbmc.python.pluginsource",),
) -> str:
    extension_xml = "".join(
        f'<extension point="{extension}" />' for extension in extensions
    )
    return (
        f'<addon id="{addon_id}" name="{name}" version="{version}" '
        f'provider-name="Kodi Lab">{extension_xml}</addon>'
    )


def make_source(root: Path, locator: str = "sources/example") -> Path:
    source = root.joinpath(*locator.split("/"))
    source.mkdir(parents=True)
    (source / "addon.xml").write_text(addon_xml(), encoding="utf-8")
    return source


def assert_code(
    validator: DevelopmentSourceValidator,
    locator: str,
    code: DevelopmentSourceValidationCode,
) -> None:
    with pytest.raises(DevelopmentSourceValidationError) as captured:
        validator.validate(locator)
    assert captured.value.code is code


def test_valid_nested_source_parses_deterministic_metadata(tmp_path: Path) -> None:
    source = make_source(tmp_path)
    (source / "addon.xml").write_text(
        addon_xml(
            name="Mý Kodi Add-on",
            extensions=(
                "xbmc.service",
                "xbmc.python.pluginsource",
                "xbmc.service",
            ),
        ),
        encoding="utf-8",
    )

    result = DevelopmentSourceValidator(tmp_path).validate("sources/example")

    assert result.relative_locator == "sources/example"
    assert result.addon_id == "plugin.video.example-test"
    assert result.observed_name == "Mý Kodi Add-on"
    assert result.observed_version == "1.2.3"
    assert result.extension_types == (
        "xbmc.python.pluginsource",
        "xbmc.service",
    )


@pytest.mark.parametrize(
    "locator",
    (
        "/absolute/source",
        "../escape",
        "source/../escape",
        "source/./child",
        "source//child",
        "source/",
        ".",
        "",
        "windows\\escape",
        "nul\x00escape",
    ),
)
def test_invalid_locator_forms_are_rejected(tmp_path: Path, locator: str) -> None:
    assert_code(
        DevelopmentSourceValidator(tmp_path),
        locator,
        DevelopmentSourceValidationCode.LOCATOR_INVALID,
    )


def test_symlink_component_and_final_source_are_rejected(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    make_source(outside, "source")
    (tmp_path / "component").symlink_to(outside, target_is_directory=True)
    (tmp_path / "final").symlink_to(outside / "source", target_is_directory=True)
    validator = DevelopmentSourceValidator(tmp_path)

    assert_code(
        validator,
        "component/source",
        DevelopmentSourceValidationCode.SOURCE_UNAVAILABLE,
    )
    assert_code(validator, "final", DevelopmentSourceValidationCode.SOURCE_UNAVAILABLE)


def test_unconfigured_nonexistent_and_symlink_roots_are_rejected(
    tmp_path: Path,
) -> None:
    assert_code(
        DevelopmentSourceValidator(None),
        "source",
        DevelopmentSourceValidationCode.ROOT_NOT_CONFIGURED,
    )
    assert_code(
        DevelopmentSourceValidator(tmp_path / "missing"),
        "source",
        DevelopmentSourceValidationCode.ROOT_UNAVAILABLE,
    )
    actual = tmp_path / "actual"
    actual.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(actual, target_is_directory=True)
    assert_code(
        DevelopmentSourceValidator(linked),
        "source",
        DevelopmentSourceValidationCode.ROOT_UNAVAILABLE,
    )


def test_non_directory_and_unreadable_source_are_rejected(tmp_path: Path) -> None:
    (tmp_path / "plain-file").write_text("not a directory", encoding="utf-8")
    assert_code(
        DevelopmentSourceValidator(tmp_path),
        "plain-file",
        DevelopmentSourceValidationCode.SOURCE_UNAVAILABLE,
    )

    source = make_source(tmp_path, "unreadable")
    source.chmod(0)
    try:
        if os.geteuid() != 0:
            assert_code(
                DevelopmentSourceValidator(tmp_path),
                "unreadable",
                DevelopmentSourceValidationCode.SOURCE_UNAVAILABLE,
            )
    finally:
        source.chmod(0o700)


def test_missing_directory_symlink_and_hardlinked_addon_xml_are_rejected(
    tmp_path: Path,
) -> None:
    validator = DevelopmentSourceValidator(tmp_path)
    missing = tmp_path / "missing-addon"
    missing.mkdir()
    assert_code(
        validator,
        "missing-addon",
        DevelopmentSourceValidationCode.ADDON_XML_MISSING,
    )

    directory = tmp_path / "directory-addon"
    (directory / "addon.xml").mkdir(parents=True)
    assert_code(
        validator,
        "directory-addon",
        DevelopmentSourceValidationCode.ADDON_XML_UNSAFE,
    )

    symlink = tmp_path / "symlink-addon"
    symlink.mkdir()
    target = tmp_path / "target.xml"
    target.write_text(addon_xml(), encoding="utf-8")
    (symlink / "addon.xml").symlink_to(target)
    assert_code(
        validator,
        "symlink-addon",
        DevelopmentSourceValidationCode.ADDON_XML_UNSAFE,
    )

    hardlink = tmp_path / "hardlink-addon"
    hardlink.mkdir()
    os.link(target, hardlink / "addon.xml")
    assert_code(
        validator,
        "hardlink-addon",
        DevelopmentSourceValidationCode.ADDON_XML_UNSAFE,
    )


@pytest.mark.parametrize(
    ("document", "code"),
    (
        ("<addon", DevelopmentSourceValidationCode.ADDON_XML_MALFORMED),
        (
            '<!DOCTYPE addon [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
            '<addon id="plugin.video.safe" name="&xxe;" version="1">'
            '<extension point="xbmc.python.pluginsource" /></addon>',
            DevelopmentSourceValidationCode.ADDON_XML_UNSAFE,
        ),
        (
            addon_xml(addon_id=""),
            DevelopmentSourceValidationCode.ADDON_METADATA_INVALID,
        ),
        (
            addon_xml(addon_id="Plugin.Video.Upper"),
            DevelopmentSourceValidationCode.ADDON_METADATA_INVALID,
        ),
        (
            addon_xml(addon_id="plugin/video/escape"),
            DevelopmentSourceValidationCode.ADDON_METADATA_INVALID,
        ),
        (
            addon_xml(addon_id="plugin..video.escape"),
            DevelopmentSourceValidationCode.ADDON_METADATA_INVALID,
        ),
        (
            addon_xml(addon_id="a" * 129),
            DevelopmentSourceValidationCode.ADDON_METADATA_INVALID,
        ),
        (
            addon_xml(name=""),
            DevelopmentSourceValidationCode.ADDON_METADATA_INVALID,
        ),
        (
            addon_xml(name="n" * 257),
            DevelopmentSourceValidationCode.ADDON_METADATA_INVALID,
        ),
        (
            addon_xml(version=""),
            DevelopmentSourceValidationCode.ADDON_METADATA_INVALID,
        ),
        (
            addon_xml(version="v" * 129),
            DevelopmentSourceValidationCode.ADDON_METADATA_INVALID,
        ),
        (
            addon_xml(extensions=()),
            DevelopmentSourceValidationCode.ADDON_METADATA_INVALID,
        ),
        (
            addon_xml(extensions=("XBMC.Service",)),
            DevelopmentSourceValidationCode.ADDON_METADATA_INVALID,
        ),
    ),
)
def test_malformed_unsafe_and_invalid_metadata_are_rejected(
    tmp_path: Path, document: str, code: DevelopmentSourceValidationCode
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "addon.xml").write_text(document, encoding="utf-8")
    assert_code(DevelopmentSourceValidator(tmp_path), "source", code)


def test_oversized_addon_xml_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "addon.xml").write_bytes(b"x" * (MAX_ADDON_XML_BYTES + 1))
    assert_code(
        DevelopmentSourceValidator(tmp_path),
        "source",
        DevelopmentSourceValidationCode.ADDON_XML_TOO_LARGE,
    )
