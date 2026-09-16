from datetime import UTC, datetime
from uuid import UUID

import pytest

from kodi_lab.domain import (
    DevelopmentSource,
    DevelopmentSourceKind,
    normalize_development_source_logical_name,
    validate_addon_id,
)


@pytest.mark.parametrize(
    "addon_id",
    (
        "plugin.video.example",
        "script_module.example-test",
        "service.example_2",
        "metadata.local",
    ),
)
def test_kodi_addon_id_forms_are_accepted(addon_id: str) -> None:
    assert validate_addon_id(addon_id) == addon_id


@pytest.mark.parametrize(
    "addon_id",
    (
        "../escape",
        "plugin/video",
        "white space",
        "plugin.video.bad\nid",
        "UPPER",
        "a" * 129,
    ),
)
def test_unsafe_addon_ids_are_rejected(addon_id: str) -> None:
    with pytest.raises(ValueError):
        validate_addon_id(addon_id)


def test_logical_name_is_trimmed_normalized_and_not_a_path() -> None:
    assert normalize_development_source_logical_name("  Example  ") == "Example"
    with pytest.raises(ValueError):
        normalize_development_source_logical_name("path/name")
    with pytest.raises(ValueError):
        normalize_development_source_logical_name("bad\x00name")


def test_development_source_normalizes_extension_order_and_utc() -> None:
    source = DevelopmentSource(
        id=UUID("91000000-0000-4000-8000-000000000001"),
        logical_name=" Source ",
        source_kind=DevelopmentSourceKind.LOCAL_DIRECTORY,
        relative_locator="nested/source",
        addon_id="plugin.video.example",
        observed_name="Example",
        observed_version="1.0.0",
        extension_types=("xbmc.service", "xbmc.python.pluginsource", "xbmc.service"),
        registered_at=datetime(2026, 9, 16, tzinfo=UTC),
        validated_at=datetime(2026, 9, 16, tzinfo=UTC),
    )

    assert source.logical_name == "Source"
    assert source.extension_types == ("xbmc.python.pluginsource", "xbmc.service")
