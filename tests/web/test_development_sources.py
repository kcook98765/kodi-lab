from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.development_sources import (
    DevelopmentSourceAdministrationService,
)
from kodi_lab.development import DevelopmentSourceValidator
from kodi_lab.storage.repositories import (
    DevelopmentSourceRepository,
)

from .conftest import ORIGIN, login

SOURCE_A = UUID("88000000-0000-4000-8000-000000000001")
SOURCE_B = UUID("88000000-0000-4000-8000-000000000002")
SECOND_INSTANCE = UUID("88000000-0000-4000-8000-000000000003")
AT = datetime(2026, 9, 16, 18, 0, tzinfo=UTC)
SECRET_LOCATOR_A = "private-source-tree/video"
SECRET_LOCATOR_B = "internal/other-addon"


def _write_source(
    development_root: Path,
    locator: str,
    *,
    addon_id: str = "plugin.video.web-source",
    name: str = "Web Source",
    version: str = "1.0.0",
) -> Path:
    source = development_root / locator
    source.mkdir(parents=True)
    (source / "addon.xml").write_text(
        f'<addon id="{addon_id}" name="{name}" version="{version}">'
        '<extension point="xbmc.python.pluginsource" library="default.py">'
        "<provides>video</provides></extension>"
        '<extension point="xbmc.addon.metadata" /></addon>',
        encoding="utf-8",
    )
    (source / "default.py").write_text("# passive test source\n", encoding="utf-8")
    return source


def _register(
    client: TestClient,
    csrf: str,
    *,
    logical_name: str = "Web source",
    relative_locator: str = "web/source",
) -> dict[str, object]:
    response = client.post(
        "/api/development-sources",
        json={
            "logical_name": logical_name,
            "relative_locator": relative_locator,
        },
        headers={
            "Origin": "https://127.0.0.1",
            "X-Kodi-Lab-CSRF": csrf,
        },
    )
    assert response.status_code == 201, response.text
    return cast(dict[str, object], response.json())


def test_development_source_routes_require_authentication(client: TestClient) -> None:
    assert client.get("/development-sources", follow_redirects=False).status_code == 303
    assert (
        client.get(
            f"/development-sources/{SOURCE_A}", follow_redirects=False
        ).status_code
        == 303
    )
    assert client.get("/api/development-sources").status_code == 401
    assert client.get(f"/api/development-sources/{SOURCE_A}").status_code == 401


def test_empty_catalog_and_missing_source(
    client: TestClient,
) -> None:
    login(client)
    page = client.get("/development-sources")
    assert page.status_code == 200
    assert page.headers["Cache-Control"] == "no-store"
    assert "No development sources are registered." in page.text
    assert 'href="/development-sources"' in client.get("/development").text
    assert client.get(f"/development-sources/{SOURCE_A}").status_code == 404
    missing = client.get(f"/api/development-sources/{SOURCE_A}")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "development_source_not_found"


def test_registration_requires_auth_origin_csrf_strict_schema_and_is_path_free(
    client: TestClient, data_root: Path
) -> None:
    development_root = data_root.parent / "development-root"
    locator = "team/video-addon"
    _write_source(development_root, locator)
    body = {"logical_name": "Team video", "relative_locator": locator}

    assert client.post("/api/development-sources", json=body).status_code == 401
    csrf = login(client)
    assert (
        client.post("/api/development-sources", json=body, headers=ORIGIN).status_code
        == 403
    )
    assert (
        client.post(
            "/api/development-sources",
            json=body,
            headers={
                "Origin": "http://attacker.invalid",
                "X-Kodi-Lab-CSRF": csrf,
            },
        ).status_code
        == 403
    )
    unknown = client.post(
        "/api/development-sources",
        json={**body, "absolute_path": "/tmp/private-attacker-path"},
        headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
    )
    assert unknown.status_code == 422
    assert unknown.json()["error"]["code"] == "request_validation_failed"
    assert "private-attacker-path" not in unknown.text

    response = client.post(
        "/api/development-sources",
        json=body,
        headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
    )
    assert response.status_code == 201
    assert set(response.json()) == {
        "id",
        "logical_name",
        "source_kind",
        "addon_id",
        "observed_name",
        "observed_version",
        "extension_types",
        "registered_at",
        "validated_at",
    }
    for forbidden in (locator, str(development_root), "relative_locator", "addon.xml"):
        assert forbidden not in response.text
    page = client.get("/development-sources")
    assert "Team video" in page.text
    assert "Path is relative to the configured development-source root" in page.text
    assert locator not in page.text
    assert str(development_root) not in page.text


@pytest.mark.parametrize("locator", ("/absolute/source", "../escape", "a/../escape"))
def test_registration_rejects_absolute_and_traversal_locators(
    client: TestClient, locator: str
) -> None:
    csrf = login(client)
    response = client.post(
        "/api/development-sources",
        json={"logical_name": "Unsafe", "relative_locator": locator},
        headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "locator_invalid"
    assert "/home/" not in response.text


@pytest.mark.parametrize(
    ("case", "expected_code"),
    (
        ("missing", "source_unavailable"),
        ("symlink", "source_unavailable"),
        ("unreadable", "source_unavailable"),
        ("missing_descriptor", "addon_xml_missing"),
        ("malformed_descriptor", "addon_xml_malformed"),
        ("invalid_metadata", "addon_metadata_invalid"),
    ),
)
def test_registration_returns_bounded_source_validation_errors(
    client: TestClient,
    data_root: Path,
    case: str,
    expected_code: str,
) -> None:
    development_root = data_root.parent / "development-root"
    locator = f"invalid/{case}"
    source = development_root / locator
    if case == "symlink":
        outside = data_root / "outside-source"
        _write_source(data_root, "outside-source")
        source.parent.mkdir(parents=True)
        source.symlink_to(outside, target_is_directory=True)
    elif case == "missing_descriptor":
        source.mkdir(parents=True)
    elif case == "malformed_descriptor":
        source.mkdir(parents=True)
        (source / "addon.xml").write_text("<addon", encoding="utf-8")
    elif case == "invalid_metadata":
        source.mkdir(parents=True)
        (source / "addon.xml").write_text(
            '<addon id="Invalid ID" name="Invalid" version="1.0" />',
            encoding="utf-8",
        )
    elif case == "unreadable":
        _write_source(development_root, locator)
        source.chmod(0)

    csrf = login(client)
    response = client.post(
        "/api/development-sources",
        json={"logical_name": "Invalid source", "relative_locator": locator},
        headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
    )
    if case == "unreadable":
        source.chmod(0o700)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == expected_code
    for forbidden in (str(development_root), locator, "Traceback", "OSError"):
        assert forbidden not in response.text


def test_registration_preserves_duplicate_rejection_without_locator_disclosure(
    client: TestClient, data_root: Path
) -> None:
    development_root = data_root.parent / "development-root"
    locator = "duplicates/source"
    _write_source(development_root, locator)
    csrf = login(client)
    _register(
        client,
        csrf,
        logical_name="Original source",
        relative_locator=locator,
    )
    duplicate = client.post(
        "/api/development-sources",
        json={"logical_name": "Second label", "relative_locator": locator},
        headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "development_source_already_registered"
    assert locator not in duplicate.text

    same_name_locator = "duplicates/same-name"
    _write_source(
        development_root,
        same_name_locator,
        addon_id="plugin.video.same-name",
    )
    same_name = client.post(
        "/api/development-sources",
        json={
            "logical_name": "Original source",
            "relative_locator": same_name_locator,
        },
        headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
    )
    assert same_name.status_code == 409
    assert same_name.json()["error"]["code"] == (
        "development_source_logical_name_exists"
    )

    same_addon_locator = "duplicates/same-addon"
    _write_source(development_root, same_addon_locator)
    same_addon = client.post(
        "/api/development-sources",
        json={
            "logical_name": "Same add-on",
            "relative_locator": same_addon_locator,
        },
        headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
    )
    assert same_addon.status_code == 409
    assert same_addon.json()["error"]["code"] == "development_source_addon_id_exists"


def test_get_uses_stored_observation_without_filesystem_revalidation(
    client: TestClient,
    database: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    development_root = tmp_path / "development"
    source_root = development_root / "mutable"
    source_root.mkdir(parents=True)
    addon_xml = source_root / "addon.xml"
    addon_xml.write_text(
        '<addon id="plugin.video.mutable" name="Original name" version="1.0">'
        '<extension point="xbmc.python.pluginsource"/></addon>',
        encoding="utf-8",
    )
    registered = DevelopmentSourceAdministrationService(
        database,
        DevelopmentSourceValidator(development_root),
        clock=lambda: AT.replace(second=9, microsecond=900000),
    ).register_source(logical_name="Mutable source", relative_locator="mutable")
    addon_xml.write_text(
        '<addon id="plugin.video.mutable" name="Changed name" version="9.9">'
        '<extension point="xbmc.python.pluginsource"/></addon>',
        encoding="utf-8",
    )

    login(client)
    page = client.get(f"/development-sources/{registered.id}")
    api = client.get(f"/api/development-sources/{registered.id}")
    assert "<dt>Observed name</dt><dd>Original name</dd>" in page.text
    assert "<dt>Observed version</dt><dd>1.0</dd>" in page.text
    assert "Changed name" not in page.text
    # A timestamp may legitimately contain the changed version's digits.
    assert "09.9" in page.text
    assert api.json()["source"]["observed_name"] == "Original name"
    assert api.json()["source"]["observed_version"] == "1.0"
    with database() as session:
        stored = DevelopmentSourceRepository(session).get(registered.id)
        assert stored is not None
        assert stored.observed_name == "Original name"
        assert stored.observed_version == "1.0"
