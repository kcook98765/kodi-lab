"""Mixed declaration policy is separate from bounded launch and smoke policy."""

from itertools import permutations

import pytest

from kodi_lab.development.launch import (
    manifest_launch_entries,
    python_service_descriptor_eligible,
)
from kodi_lab.development.sources import (
    DevelopmentSourceValidationResult,
    copied_video_plugin_unsupported_reason,
    parse_captured_addon_xml,
)

SCRIPT = '<extension point="xbmc.python.script" library="script.py"/>'
PLUGIN = (
    '<extension point="xbmc.python.pluginsource" library="plugin.py">'
    "<provides>video</provides></extension>"
)
SERVICE = '<extension point="xbmc.service" library="service.py"/>'
META = (
    '<extension point="xbmc.addon.metadata">'
    '<summary lang="en_GB">Fixture</summary></extension>'
)


def descriptor(*entries: str) -> DevelopmentSourceValidationResult:
    return parse_captured_addon_xml(
        "fixture",
        (
            '<addon id="script.mixed.fixture" name="Fixture" version="1.0.0">'
            + "".join(entries)
            + "</addon>"
        ).encode(),
    )


@pytest.mark.parametrize(
    "entries",
    [
        (SERVICE,),
        (SCRIPT, SERVICE),
        (PLUGIN, SERVICE),
        *permutations((SCRIPT, PLUGIN, SERVICE)),
    ],
)
def test_service_admission_preserves_ordinal_and_distinct_launch_semantics(
    entries: tuple[str, ...],
) -> None:
    observed = descriptor(META, *entries)
    assert python_service_descriptor_eligible(observed)
    launch = manifest_launch_entries(observed)
    assert [entry.ordinal for entry in launch] == list(range(len(entries) + 1))
    assert [entry.eligible for entry in launch] == [
        item.point in {"xbmc.python.script", "xbmc.python.pluginsource"}
        for item in observed.extensions
    ]
    assert launch[0].reason == "unsupported_extension"
    if len(entries) > 1:
        assert (
            copied_video_plugin_unsupported_reason(observed)
            == "multiple_executable_extensions"
        )


@pytest.mark.parametrize(
    "entries",
    [
        (SCRIPT,),
        (SCRIPT, PLUGIN),
        (SERVICE, SERVICE),
        (SCRIPT, SCRIPT, SERVICE),
        (PLUGIN, PLUGIN, SERVICE),
        (SERVICE, '<extension point="xbmc.gui.skin"/>'),
        (SERVICE.replace("service.py", "../escape.py"),),
        (
            SERVICE.replace(
                'library="service.py"', 'library="service.py" library_linux="other.py"'
            ),
        ),
        (SCRIPT.replace('library="script.py"', ""), SERVICE),
        (PLUGIN.replace("video", "unknown"), SERVICE),
    ],
)
def test_service_admission_refuses_unqualified_or_unsafe_shapes(
    entries: tuple[str, ...],
) -> None:
    assert not python_service_descriptor_eligible(descriptor(*entries))


def test_multi_content_descriptor_and_bounded_launch_are_distinct() -> None:
    observed = descriptor(
        PLUGIN.replace("video", "audio video image executable"), SERVICE
    )
    assert python_service_descriptor_eligible(observed)
    assert manifest_launch_entries(observed)[0].reason == "unsupported_plugin_content"
