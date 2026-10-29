from __future__ import annotations

import json

import pytest

from kodi_lab.development.activation import assess_declared_extensions


@pytest.mark.parametrize(
    ("extensions", "expected"),
    [
        (["xbmc.python.pluginsource"], []),
        (["xbmc.python.script"], []),
        (["xbmc.service"], ["service_enablement_unsupported"]),
        (["xbmc.python.module"], ["dependent_interpreters_require_restart"]),
        (["xbmc.gui.skin"], ["skin_selection_and_recovery_unqualified"]),
        (["kodi.resource.images"], ["resource_consumer_refresh_unqualified"]),
        (["xbmc.addon.repository"], ["repository_refresh_unqualified"]),
        (["kodi.inputstream"], ["native_or_unknown_extension_unqualified"]),
        (["new.future.extension"], ["native_or_unknown_extension_unqualified"]),
        (
            ["xbmc.python.script", "xbmc.service"],
            [
                "service_enablement_unsupported",
                "mixed_extensions_require_all_lifecycles",
            ],
        ),
    ],
)
def test_every_extension_and_unknowns_remain_unqualified(
    extensions: list[str],
    expected: list[str],
) -> None:
    result = assess_declared_extensions(
        json.dumps([*extensions, "xbmc.addon.metadata"])
    )
    assert result is not None
    assert list(result.constraints) == expected
    assert result.admission == "not_assessed" and result.live_qualified is False
    assert result.owned_removal_qualified is False
    assert result.tree_policy == "retain_destination_only"
    assert result.descriptor_metadata == result.runtime_qualification == "not_assessed"


@pytest.mark.parametrize(
    "encoded", ["{}", "[]", "[1]", "null", '["/tmp/private"]', '["x"', "x" * 4097]
)
def test_invalid_or_unbounded_descriptor_metadata_has_no_plan(encoded: str) -> None:
    assert assess_declared_extensions(encoded) is None


def test_nested_invalid_json_cannot_abort_preflight_assessment() -> None:
    assert assess_declared_extensions("[" * 1500 + "]" * 1500) is None
