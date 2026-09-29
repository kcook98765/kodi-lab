from __future__ import annotations

import pytest

from kodi_lab.application.addon_dependencies import (
    DependencyStatus,
    declaration_mismatch,
    evaluate_dependencies,
    first_blocking_dependency,
)
from kodi_lab.domain import AddonZipDependency
from kodi_lab.kodi.addons import KodiAddonInventoryRecord, KodiReportedAddonDependency


def declaration(
    dependency_id: str = "xbmc.python",
    *,
    version: str | None = "3.0.0",
    minversion: str | None = None,
    optional: bool = False,
) -> AddonZipDependency:
    return AddonZipDependency(dependency_id, version, minversion, optional)


def inventory_record(
    dependency_id: str, *, enabled: bool = True
) -> KodiAddonInventoryRecord:
    return KodiAddonInventoryRecord(
        dependency_id, dependency_id, "1.0.0", enabled, "xbmc.python.module"
    )


def test_kodi_implicit_minversion_matches_source_declaration() -> None:
    source = (declaration(),)
    observed = (KodiReportedAddonDependency("xbmc.python", "3.0.0", False, "3.0.0"),)
    assert declaration_mismatch(source, observed) is None
    ordinary = (declaration("script.module.example", version="1.0.0"),)
    ordinary_observed = (
        KodiReportedAddonDependency("script.module.example", "1.0.0", False, "1.0.0"),
    )
    assert declaration_mismatch(ordinary, ordinary_observed) is None


@pytest.mark.parametrize(
    "observed",
    [
        KodiReportedAddonDependency("xbmc.python", "3.0.0", False, "3.0.1"),
        KodiReportedAddonDependency("xbmc.python", "3.0.0", True, "3.0.0"),
        KodiReportedAddonDependency("xbmc.gui", "3.0.0", False, "3.0.0"),
    ],
)
def test_contradictory_declaration_remains_unknown(
    observed: KodiReportedAddonDependency,
) -> None:
    mismatch = declaration_mismatch((declaration(),), (observed,))
    assert mismatch is not None
    assert mismatch.status is DependencyStatus.UNKNOWN


@pytest.mark.parametrize(
    ("release", "required"),
    [
        ("19.4", "3.0.0"),
        ("20.5", "3.0.0"),
        ("20.5", "3.0.1"),
        ("21.3", "3.0.0"),
        ("21.3", "3.0.1"),
    ],
)
def test_reviewed_system_python_api_satisfies_compatible_minimum(
    release: str, required: str
) -> None:
    states = evaluate_dependencies(
        (declaration(version=required),), {}, kodi_release=release
    )
    assert states[0].status is DependencyStatus.PRESENT_ENABLED
    assert states[0].recognized is True
    assert states[0].enabled is True
    assert first_blocking_dependency(states) is None


@pytest.mark.parametrize(
    ("release", "required", "minversion", "inventory"),
    [
        ("19.4", "3.0.1", None, {}),
        ("21.3", "3.0.2", None, {}),
        ("21.3", "2.26.0", None, {}),
        ("21.3", "3.bad", None, {}),
        ("21.3", "3.0.0", "3.0.0", {}),
        ("22.0", "3.0.0", None, {}),
        ("21.3", "3.0.0", None, {"xbmc.python": inventory_record("xbmc.python")}),
    ],
)
def test_unproven_system_python_dependency_stays_blocked(
    release: str,
    required: str,
    minversion: str | None,
    inventory: dict[str, KodiAddonInventoryRecord],
) -> None:
    states = evaluate_dependencies(
        (declaration(version=required, minversion=minversion),),
        inventory,
        kodi_release=release,
    )
    assert states[0].status in {
        DependencyStatus.VERSION_UNVERIFIED,
        DependencyStatus.UNKNOWN,
    }
    if release == "21.3" and not inventory:
        assert states[0].recognized is True
    assert first_blocking_dependency(states) is states[0]


def test_ordinary_dependency_and_optional_missing_semantics_remain_closed() -> None:
    required = declaration("script.module.example", version=None)
    optional = declaration("script.module.optional", version=None, optional=True)
    missing = evaluate_dependencies((required, optional), {}, kodi_release="21.3")
    assert [item.status for item in missing] == [
        DependencyStatus.MISSING,
        DependencyStatus.OPTIONAL_MISSING,
    ]
    assert first_blocking_dependency(missing) is missing[0]

    disabled = evaluate_dependencies(
        (required,),
        {
            required.dependency_id: inventory_record(
                required.dependency_id, enabled=False
            )
        },
    )
    assert disabled[0].status is DependencyStatus.DISABLED
    constrained = evaluate_dependencies(
        (declaration("script.module.example", version="1.0.0"),),
        {required.dependency_id: inventory_record(required.dependency_id)},
    )
    assert constrained[0].status is DependencyStatus.VERSION_UNVERIFIED
