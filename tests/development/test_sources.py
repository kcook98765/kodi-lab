import os
from pathlib import Path

import pytest

from kodi_lab.development import (
    MAX_ADDON_XML_BYTES,
    DevelopmentSourceValidationCode,
    DevelopmentSourceValidationError,
    DevelopmentSourceValidator,
    copied_video_plugin_library,
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


def smoke_descriptor(
    extension_xml: str, *, addon_id: str = "plugin.video.example"
) -> str:
    return (
        f'<addon id="{addon_id}" name="Example" version="1.0.0">{extension_xml}</addon>'
    )


def test_copied_smoke_accepts_only_one_safe_python_video_plugin(tmp_path: Path) -> None:
    source = make_source(tmp_path, "smoke")
    (source / "addon.xml").write_text(
        smoke_descriptor(
            '<extension point="xbmc.python.pluginsource" library="resources/main.py">'
            "<provides>video</provides></extension>"
            '<extension point="xbmc.addon.metadata"/>'
        ),
        encoding="utf-8",
    )
    observation = DevelopmentSourceValidator(tmp_path).validate("smoke")
    assert copied_video_plugin_library(observation) == "resources/main.py"


@pytest.mark.parametrize(
    "extension_xml",
    [
        (
            '<extension point="xbmc.python.pluginsource">'
            "<provides>video</provides></extension>"
        ),
        (
            '<extension point="xbmc.python.pluginsource" library="../main.py">'
            "<provides>video</provides></extension>"
        ),
        (
            '<extension point="xbmc.python.pluginsource" library="main.py">'
            "<provides>audio</provides></extension>"
        ),
        (
            '<extension point="xbmc.python.pluginsource" library="main.py">'
            "<provides>video audio</provides></extension>"
        ),
        (
            '<extension point="xbmc.python.pluginsource" library="main.py">'
            "<provides>video</provides></extension>"
            '<extension point="xbmc.python.pluginsource" library="other.py">'
            "<provides>video</provides></extension>"
        ),
        '<extension point="xbmc.service" library="service.py"/>',
        '<extension point="xbmc.python.script" library="script.py"/>',
        '<extension point="kodi.binary.global.main" library="addon.so"/>',
        '<extension point="xbmc.python.module" library="lib"/>',
        (
            '<extension point="xbmc.python.pluginsource" library="main.py">'
            "<provides>video</provides></extension>"
            '<extension point="xbmc.service" library="service.py"/>'
        ),
    ],
)
def test_copied_smoke_rejects_unsupported_or_mixed_descriptors(
    tmp_path: Path, extension_xml: str
) -> None:
    source = make_source(tmp_path, "smoke")
    (source / "addon.xml").write_text(smoke_descriptor(extension_xml), encoding="utf-8")
    observation = DevelopmentSourceValidator(tmp_path).validate("smoke")
    assert copied_video_plugin_library(observation) is None


def test_copied_smoke_never_infers_authority_from_addon_id(tmp_path: Path) -> None:
    source = make_source(tmp_path, "smoke")
    (source / "addon.xml").write_text(
        smoke_descriptor('<extension point="xbmc.python.module" library="lib"/>'),
        encoding="utf-8",
    )
    observation = DevelopmentSourceValidator(tmp_path).validate("smoke")
    assert observation.addon_id.startswith("plugin.video.")
    assert copied_video_plugin_library(observation) is None


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


@pytest.mark.parametrize(
    ("requires", "expected"),
    (
        ("", ()),
        (
            '<requires><import addon="script.module.required" /></requires>',
            (("script.module.required", None, None, False),),
        ),
        (
            '<requires><import addon="script.module.optional" '
            'optional="true" /></requires>',
            (("script.module.optional", None, None, True),),
        ),
        (
            '<requires><import addon="script.module.versioned" '
            'version="1.2.3" /></requires>',
            (("script.module.versioned", "1.2.3", None, False),),
        ),
        (
            '<requires><import addon="xbmc.python" minversion="3.0.0" /></requires>',
            (("xbmc.python", None, "3.0.0", False),),
        ),
    ),
)
def test_dependency_declarations_are_normalized(
    tmp_path: Path,
    requires: str,
    expected: tuple[tuple[str, str | None, str | None, bool], ...],
) -> None:
    source = make_source(tmp_path, "source")
    document = addon_xml().replace(">", f">{requires}", 1)
    (source / "addon.xml").write_text(document, encoding="utf-8")

    result = DevelopmentSourceValidator(tmp_path).validate("source")

    assert (
        tuple(
            (item.dependency_id, item.version, item.minversion, item.optional)
            for item in result.dependencies
        )
        == expected
    )
    assert result.is_python_addon is True
    assert result.has_auto_start_behavior is False


@pytest.mark.parametrize(
    "requires",
    (
        '<requires><import addon="script.module.bad" optional="1" /></requires>',
        (
            '<requires><import addon="script.module.dup" />'
            '<import addon="script.module.dup" /></requires>'
        ),
        '<import addon="script.module.outside" />',
        '<requires><import addon="script.module.nested"><child /></import></requires>',
        '<requires><other addon="script.module.wrong" /></requires>',
    ),
)
def test_malformed_dependency_relationships_are_rejected(
    tmp_path: Path, requires: str
) -> None:
    source = make_source(tmp_path, "source")
    document = addon_xml().replace(">", f">{requires}", 1)
    (source / "addon.xml").write_text(document, encoding="utf-8")

    assert_code(
        DevelopmentSourceValidator(tmp_path),
        "source",
        DevelopmentSourceValidationCode.ADDON_METADATA_INVALID,
    )


def test_service_extension_is_auto_start_without_blanket_python_rule(
    tmp_path: Path,
) -> None:
    source = make_source(tmp_path, "source")
    (source / "addon.xml").write_text(
        addon_xml(extensions=("xbmc.service",)), encoding="utf-8"
    )

    result = DevelopmentSourceValidator(tmp_path).validate("source")

    assert result.has_auto_start_behavior is True
    assert result.is_python_addon is False


def test_oversized_addon_xml_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "addon.xml").write_bytes(b"x" * (MAX_ADDON_XML_BYTES + 1))
    assert_code(
        DevelopmentSourceValidator(tmp_path),
        "source",
        DevelopmentSourceValidationCode.ADDON_XML_TOO_LARGE,
    )


def test_service_platform_library_metadata_is_retained() -> None:
    from kodi_lab.development.sources import parse_captured_addon_xml

    result = parse_captured_addon_xml(
        "source",
        b'<addon id="service.example" name="Example" version="1.0.0">'
        b'<extension point="xbmc.service" library="main.py" '
        b'library_linux="native.so"/></addon>',
    )
    assert result.extensions[0].has_platform_library is True
    assert result.has_auto_start_behavior is True
