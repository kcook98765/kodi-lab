from __future__ import annotations

import pytest

from kodi_lab.application.addon_dependencies import (
    DependencyStatus,
    declaration_mismatch,
    dependency_failure_code,
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
    dependency_id: str, *, enabled: bool = True, version: str = "1.0.0"
) -> KodiAddonInventoryRecord:
    return KodiAddonInventoryRecord(
        dependency_id, dependency_id, version, enabled, "xbmc.python.module"
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
        ("22.0-rc.1", "3.0.0"),
        ("22.0-rc.1", "3.0.1"),
        ("22.0-rc.1", "3.1.0"),
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
    ("release", "required", "minversion", "inventory", "expected"),
    [
        ("19.4", "3.0.1", None, {}, DependencyStatus.INCOMPATIBLE),
        ("21.3", "3.0.2", None, {}, DependencyStatus.INCOMPATIBLE),
        ("21.3", "2.26.0", None, {}, DependencyStatus.INCOMPATIBLE),
        ("21.3", "3.bad", None, {}, DependencyStatus.VERSION_UNVERIFIED),
        ("21.3", "3.0.0", "3.0.0", {}, DependencyStatus.VERSION_UNVERIFIED),
        ("22.0", "3.0.0", None, {}, DependencyStatus.VERSION_UNVERIFIED),
        ("22.0-rc.2", "3.0.0", None, {}, DependencyStatus.VERSION_UNVERIFIED),
        ("22.0-beta.2", "3.0.0", None, {}, DependencyStatus.VERSION_UNVERIFIED),
        ("22.0-rc.1", "3.1.1", None, {}, DependencyStatus.INCOMPATIBLE),
        ("22.0-rc.1", "2.26.0", None, {}, DependencyStatus.INCOMPATIBLE),
        ("22.0-rc.1", "3.0.0", "3.0.0", {}, DependencyStatus.VERSION_UNVERIFIED),
        (
            "22.0-rc.1",
            "3.0.0",
            None,
            {"xbmc.python": inventory_record("xbmc.python")},
            DependencyStatus.UNKNOWN,
        ),
        (
            "21.3",
            "3.0.0",
            None,
            {"xbmc.python": inventory_record("xbmc.python")},
            DependencyStatus.UNKNOWN,
        ),
    ],
)
def test_unproven_system_python_dependency_stays_blocked(
    release: str,
    required: str,
    minversion: str | None,
    inventory: dict[str, KodiAddonInventoryRecord],
    expected: DependencyStatus,
) -> None:
    states = evaluate_dependencies(
        (declaration(version=required, minversion=minversion),),
        inventory,
        kodi_release=release,
    )
    assert states[0].status is expected
    if release == "21.3" and not inventory:
        assert states[0].recognized is True
    assert first_blocking_dependency(states) is states[0]
    assert dependency_failure_code(states[0].status).startswith("dependency_")


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
    assert constrained[0].status is DependencyStatus.PRESENT_ENABLED


@pytest.mark.parametrize(
    ("required", "installed", "expected"),
    [
        ("5.4.16", "5.4.16", DependencyStatus.PRESENT_ENABLED),
        ("9.0.5", "9.0.7", DependencyStatus.PRESENT_ENABLED),
        ("0.0.5", "3.0.2", DependencyStatus.PRESENT_ENABLED),
        ("1.9.0", "1.10.0", DependencyStatus.PRESENT_ENABLED),
        ("1.10.0", "1.9.0", DependencyStatus.INCOMPATIBLE),
        ("2.0.0", "1.9.0", DependencyStatus.INCOMPATIBLE),
        ("1.0.1", "1.0.0", DependencyStatus.INCOMPATIBLE),
        ("1.0.0", "1.0.0~beta1", DependencyStatus.VERSION_UNVERIFIED),
        ("1.0.0", "1.0.0+omega.1", DependencyStatus.VERSION_UNVERIFIED),
        ("1.0.0", "1.0.0-1", DependencyStatus.VERSION_UNVERIFIED),
        ("1.0.0", "1:1.0.0", DependencyStatus.VERSION_UNVERIFIED),
        ("1.0.0", "1.0", DependencyStatus.VERSION_UNVERIFIED),
        ("1.0.0", "01.0.0", DependencyStatus.VERSION_UNVERIFIED),
        ("1.0.0", "1.0.0.0", DependencyStatus.VERSION_UNVERIFIED),
        ("1.0.0", "1.0.0\n", DependencyStatus.VERSION_UNVERIFIED),
        ("1.0.0", "?.0.0", DependencyStatus.VERSION_UNVERIFIED),
        ("1.0.0", "1000000000.0.0", DependencyStatus.VERSION_UNVERIFIED),
        ("1.0.0~beta1", "1.0.0", DependencyStatus.VERSION_UNVERIFIED),
        ("1.0", "1.0.0", DependencyStatus.VERSION_UNVERIFIED),
        ("1.0.0", "", DependencyStatus.VERSION_UNVERIFIED),
    ],
)
def test_installed_dependency_numeric_minimum(
    required: str, installed: str, expected: DependencyStatus
) -> None:
    dependency_id = "script.module.example"
    states = evaluate_dependencies(
        (declaration(dependency_id, version=required),),
        {dependency_id: inventory_record(dependency_id, version=installed)},
    )
    assert states[0].status is expected
    assert states[0].declared_version == required
    assert states[0].reported_version == installed
    assert (first_blocking_dependency(states) is None) == (
        expected is DependencyStatus.PRESENT_ENABLED
    )


@pytest.mark.parametrize("version", [None, "1.0.0"])
def test_explicit_dependency_range_remains_unverified(version: str | None) -> None:
    dependency_id = "script.module.example"
    states = evaluate_dependencies(
        (declaration(dependency_id, version=version, minversion="1.0.0"),),
        {dependency_id: inventory_record(dependency_id)},
    )
    assert states[0].status is DependencyStatus.VERSION_UNVERIFIED
    assert first_blocking_dependency(states) is states[0]


@pytest.mark.parametrize(
    ("release", "required", "expected"),
    [
        ("21.3", "5.17.0", DependencyStatus.PRESENT_ENABLED),
        ("21.3", "5.16.0", DependencyStatus.INCOMPATIBLE),
        ("21.3", "5.15.0", DependencyStatus.INCOMPATIBLE),
        ("21.3", "5.18.0", DependencyStatus.INCOMPATIBLE),
        ("21.3", "5.17.0~beta1", DependencyStatus.VERSION_UNVERIFIED),
        ("20.5", "5.16.0", DependencyStatus.VERSION_UNVERIFIED),
        ("22.0", "5.17.0", DependencyStatus.VERSION_UNVERIFIED),
    ],
)
def test_reviewed_system_gui_api_respects_abi_floor(
    release: str, required: str, expected: DependencyStatus
) -> None:
    states = evaluate_dependencies(
        (declaration("xbmc.gui", version=required),), {}, kodi_release=release
    )
    assert states[0].status is expected
    assert states[0].reported_version == ("5.17.0" if release == "21.3" else None)


def test_system_gui_contradictory_inventory_and_explicit_range_stay_blocked() -> None:
    states = evaluate_dependencies(
        (declaration("xbmc.gui", version="5.17.0"),),
        {"xbmc.gui": inventory_record("xbmc.gui", version="5.17.0")},
        kodi_release="21.3",
    )
    assert states[0].status is DependencyStatus.UNKNOWN
    states = evaluate_dependencies(
        (declaration("xbmc.gui", version="5.17.0", minversion="5.17.0"),),
        {},
        kodi_release="21.3",
    )
    assert states[0].status is DependencyStatus.VERSION_UNVERIFIED


def test_other_system_api_does_not_use_ordinary_numeric_minimum() -> None:
    states = evaluate_dependencies(
        (declaration("xbmc.json", version="1.0.0"),),
        {"xbmc.json": inventory_record("xbmc.json", version="1.0.0")},
        kodi_release="21.3",
    )
    assert states[0].status is DependencyStatus.VERSION_UNVERIFIED
