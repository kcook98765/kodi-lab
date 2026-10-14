from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.development_source_attachments import (
    AttachedDevelopmentSourceRead,
    DevelopmentSourceAttachmentService,
)
from kodi_lab.application.development_sources import (
    DevelopmentSourceAdministrationService,
    DevelopmentSourceRead,
)
from kodi_lab.application.errors import DevelopmentSourceAddonConflictError
from kodi_lab.config import Settings
from kodi_lab.development import DevelopmentSourceValidator
from kodi_lab.domain import (
    DevelopmentSource,
    DevelopmentSourceAttachmentOperation,
    DevelopmentSourceAttachmentOperationKind,
    DevelopmentSourceAttachmentOperationState,
    DevelopmentSourceKind,
    ManagedInstanceDevelopmentSource,
    ManagedInstanceMutationKind,
)
from kodi_lab.storage.models import (
    ManagedInstanceDeleteOperationRow,
    ManagedInstanceMutationLeaseRow,
    ManagedInstanceRow,
    TargetRow,
)
from kodi_lab.storage.repositories import (
    DevelopmentSourceAttachmentOperationRepository,
    DevelopmentSourceRepository,
    ManagedInstanceDevelopmentSourceRepository,
)
from kodi_lab.web.app import create_app

from .conftest import INSTANCE_ID, ORIGIN, REVISION_ID, FakeBroker, login
from .test_addon_zips import _valid_zip, _wait_for_import

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
            "Origin": "http://127.0.0.1",
            "X-Kodi-Lab-CSRF": csrf,
        },
    )
    assert response.status_code == 201, response.text
    return cast(dict[str, object], response.json())


def _source(
    source_id: UUID,
    logical_name: str,
    locator: str,
    addon_id: str,
    observed_name: str,
    observed_version: str,
    extensions: tuple[str, ...],
) -> DevelopmentSource:
    return DevelopmentSource(
        id=source_id,
        logical_name=logical_name,
        source_kind=DevelopmentSourceKind.LOCAL_DIRECTORY,
        relative_locator=locator,
        addon_id=addon_id,
        observed_name=observed_name,
        observed_version=observed_version,
        extension_types=extensions,
        registered_at=AT,
        validated_at=AT,
    )


def _seed_sources(
    database: sessionmaker[Session], *, second_instance: bool = False
) -> None:
    with database.begin() as session:
        sources = DevelopmentSourceRepository(session)
        sources.create(
            _source(
                SOURCE_A,
                "Alpha source",
                SECRET_LOCATOR_A,
                "plugin.video.alpha",
                "Alpha Video",
                "1.2.3",
                ("xbmc.addon.metadata", "xbmc.python.pluginsource"),
            )
        )
        sources.create(
            _source(
                SOURCE_B,
                "Beta source",
                SECRET_LOCATOR_B,
                "service.beta",
                "Beta Service",
                "4.5.6",
                ("xbmc.service",),
            )
        )
        attachments = ManagedInstanceDevelopmentSourceRepository(session)
        attachments.create(
            ManagedInstanceDevelopmentSource(
                managed_instance_id=INSTANCE_ID,
                development_source_id=SOURCE_B,
                attached_at=AT,
            )
        )
        attachments.create(
            ManagedInstanceDevelopmentSource(
                managed_instance_id=INSTANCE_ID,
                development_source_id=SOURCE_A,
                attached_at=AT,
            )
        )
        if second_instance:
            session.add(
                TargetRow(
                    id=str(SECOND_INSTANCE),
                    name="Second Kodi",
                    target_type="managed",
                    kodi_version="21.3",
                    availability="available",
                    health="healthy",
                    connection_state="connected",
                )
            )
            session.add(
                ManagedInstanceRow(
                    target_id=str(SECOND_INSTANCE),
                    instance_type="disposable",
                    runtime_revision_id=str(REVISION_ID),
                    runtime_state="stopped",
                    start_on_host_boot=False,
                    notes=None,
                )
            )
            session.flush()
            attachments.create(
                ManagedInstanceDevelopmentSource(
                    managed_instance_id=SECOND_INSTANCE,
                    development_source_id=SOURCE_A,
                    attached_at=AT,
                )
            )


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


def test_unconfigured_source_root_keeps_web_and_zip_available(
    client: TestClient,
    database: sessionmaker[Session],
    broker: FakeBroker,
    data_root: Path,
) -> None:
    context = cast(FastAPI, client.app).state.kodi_lab
    settings = Settings(
        data_root=data_root,
        development_root=None,
        external_target_master_key=context.settings.external_target_master_key,
    )
    app = create_app(
        settings=settings,
        session_factory=database,
        broker=broker,
        auth=context.auth,
    )
    with TestClient(app, base_url="http://127.0.0.1") as unconfigured:
        csrf = login(unconfigured)
        page = unconfigured.get("/development-sources")
        assert page.status_code == 200
        assert "Local development sources are unavailable" in page.text
        assert "Uploaded ZIP testing remains available" in page.text
        assert 'id="development-source-registration"' not in page.text
        response = unconfigured.post(
            "/api/development-sources",
            json={"logical_name": "Unavailable", "relative_locator": "project"},
            headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "root_not_configured"
        data_root.mkdir(mode=0o700, exist_ok=True)
        uploaded = unconfigured.post(
            "/api/addon-zips",
            files={"zip": ("synthetic.zip", _valid_zip(), "application/zip")},
            data={"logical_label": "Without source root"},
            headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
        )
        assert uploaded.status_code == 202
        assert (
            _wait_for_import(unconfigured, uploaded.json()["status_url"])["state"]
            == "succeeded"
        )
        assert len(unconfigured.get("/api/addon-zips").json()) == 1


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


def test_catalog_detail_and_api_are_path_free_and_link_live_instances(
    client: TestClient, database: sessionmaker[Session]
) -> None:
    _seed_sources(database, second_instance=True)
    login(client)

    catalog = client.get("/development-sources")
    assert catalog.status_code == 200
    assert catalog.text.index("Alpha source") < catalog.text.index("Beta source")
    for text in (
        "plugin.video.alpha",
        "Alpha Video",
        "1.2.3",
        "xbmc.addon.metadata",
        "xbmc.python.pluginsource",
        str(AT),
        "Attached live instances</dt><dd>2",
    ):
        assert text in catalog.text

    detail = client.get(f"/development-sources/{SOURCE_A}")
    assert detail.status_code == 200
    assert detail.headers["Cache-Control"] == "no-store"
    assert f'href="/instances/{INSTANCE_ID}"' in detail.text
    assert f'href="/instances/{SECOND_INSTANCE}"' in detail.text
    assert f'href="/instances/{INSTANCE_ID}/history"' in detail.text
    assert "stored registration-time observations" in detail.text
    assert "does not revalidate source files" in detail.text
    # Source details stay read-only; account sign-out belongs to the shared shell.
    content = detail.text.partition("<main ")[2].partition("</main>")[0]
    assert content
    assert "<form" not in content
    assert "<script>not markup</script>" not in detail.text
    assert "Kodi &lt;script&gt;not markup&lt;/script&gt;" in detail.text
    assert 'id="development-source-registration"' in catalog.text

    api = client.get("/api/development-sources")
    assert api.status_code == 200
    assert [item["source"]["logical_name"] for item in api.json()] == [
        "Alpha source",
        "Beta source",
    ]
    assert [item["attached_instance_count"] for item in api.json()] == [2, 1]
    api_detail = client.get(f"/api/development-sources/{SOURCE_A}")
    attached_names = [
        item["instance_name"] for item in api_detail.json()["attached_instances"]
    ]
    assert attached_names == [
        "Kodi <script>not markup</script>",
        "Second Kodi",
    ]

    for response in (catalog, detail, api, api_detail):
        for forbidden in (
            SECRET_LOCATOR_A,
            SECRET_LOCATOR_B,
            "development_root",
            "/home/",
            "addon.xml",
        ):
            assert forbidden not in response.text
    assert "relative_locator" not in api.text
    assert "relative_locator" not in api_detail.text


def test_instance_detail_lists_attachments_with_attach_detach_controls(
    client: TestClient, database: sessionmaker[Session]
) -> None:
    _seed_sources(database)
    login(client)
    page = client.get(f"/instances/{INSTANCE_ID}")
    assert page.status_code == 200
    section = page.text.split('<section id="development-sources">', 1)[1].split(
        "</section>", 1
    )[0]
    assert section.index("Alpha source") < section.index("Beta source")
    assert f'href="/development-sources/{SOURCE_A}"' in section
    assert "Attached at" in section
    assert "metadata only" in section
    assert "data-action=" not in section
    assert ">Detach<" in section
    assert ">Deploy<" not in section
    assert 'id="development-source-attach"' not in section
    assert SECRET_LOCATOR_A not in page.text


def test_instance_detail_has_source_empty_state(client: TestClient) -> None:
    login(client)
    page = client.get(f"/instances/{INSTANCE_ID}")
    assert "No development sources are attached." in page.text


def test_attach_and_detach_api_reuse_core_authority_without_runtime_side_effects(
    client: TestClient,
    data_root: Path,
    broker: FakeBroker,
    database: sessionmaker[Session],
) -> None:
    locator = "workflow/source"
    _write_source(data_root.parent / "development-root", locator)
    csrf = login(client)
    registered = _register(client, csrf, relative_locator=locator)
    source_id = registered["id"]
    attach_path = f"/api/instances/{INSTANCE_ID}/development-sources/{source_id}/attach"
    choice_page = client.get(f"/instances/{INSTANCE_ID}")
    assert 'id="development-source-attach"' in choice_page.text
    assert f'<option value="{source_id}">Web source' in choice_page.text

    client.cookies.clear()
    assert client.post(attach_path, json={}).status_code == 401
    csrf = login(client)
    assert client.post(attach_path, json={}, headers=ORIGIN).status_code == 403
    assert (
        client.post(
            attach_path,
            json={},
            headers={
                "Origin": "http://attacker.invalid",
                "X-Kodi-Lab-CSRF": csrf,
            },
        ).status_code
        == 403
    )
    rejected_body = client.post(
        attach_path,
        json={
            "addon_id": "plugin.video.attacker",
            "mode": "bind",
            "source_path": "/tmp/private-attach-path",
        },
        headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
    )
    assert rejected_body.status_code == 422
    assert "private-attach-path" not in rejected_body.text

    destination = data_root / "simulated-existing-copy"
    destination.mkdir(parents=True)
    marker = destination / "must-remain"
    marker.write_text("untouched", encoding="utf-8")
    calls_before = list(broker.calls)
    attached = client.post(
        attach_path,
        json={},
        headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
    )
    assert attached.status_code == 200
    assert attached.json()["development_source_id"] == source_id
    assert "relative_locator" not in attached.text
    assert locator not in attached.text
    assert marker.read_text(encoding="utf-8") == "untouched"
    assert broker.calls == calls_before
    with database() as session:
        assert (
            ManagedInstanceDevelopmentSourceRepository(session).get(
                INSTANCE_ID, UUID(str(source_id))
            )
            is not None
        )

    duplicate = client.post(
        attach_path,
        json={},
        headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "development_source_already_attached"
    page = client.get(f"/instances/{INSTANCE_ID}")
    assert "Web source" in page.text
    assert ">Detach<" in page.text
    assert "Development source attached" in page.text

    detach_path = attach_path.removesuffix("/attach") + "/detach"
    assert (
        client.post(
            detach_path,
            json={"delete_destination": True},
            headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
        ).status_code
        == 422
    )
    detached = client.post(
        detach_path,
        json={},
        headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
    )
    assert detached.status_code == 200
    assert marker.read_text(encoding="utf-8") == "untouched"
    assert broker.calls == calls_before
    assert client.get(f"/api/development-sources/{source_id}").status_code == 200
    with database() as session:
        assert (
            DevelopmentSourceRepository(session).get(UUID(str(source_id))) is not None
        )
        assert (
            ManagedInstanceDevelopmentSourceRepository(session).get(
                INSTANCE_ID, UUID(str(source_id))
            )
            is None
        )
    activity = client.get(f"/api/instances/{INSTANCE_ID}/activity").text
    assert "Development source attached" in activity
    assert "Development source detached" in activity
    assert locator not in activity
    already_detached = client.post(
        detach_path,
        json={},
        headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
    )
    assert already_detached.status_code == 409
    assert already_detached.json()["error"]["code"] == "development_source_not_attached"


def test_attach_revalidates_source_and_unknown_entities_are_bounded(
    client: TestClient, data_root: Path
) -> None:
    locator = "revalidation/source"
    source = _write_source(data_root.parent / "development-root", locator)
    csrf = login(client)
    registered = _register(client, csrf, relative_locator=locator)
    source_id = registered["id"]
    (source / "addon.xml").write_text("<addon", encoding="utf-8")
    path = f"/api/instances/{INSTANCE_ID}/development-sources/{source_id}/attach"
    rejected = client.post(
        path,
        json={},
        headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
    )
    assert rejected.status_code == 409
    assert (
        rejected.json()["error"]["code"]
        == "development_source_attachment_validation_failed"
    )
    assert locator not in rejected.text

    missing_source = client.post(
        f"/api/instances/{INSTANCE_ID}/development-sources/{UUID(int=999)}/attach",
        json={},
        headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
    )
    assert missing_source.status_code == 404
    missing_instance = client.post(
        f"/api/instances/{UUID(int=998)}/development-sources/{source_id}/attach",
        json={},
        headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
    )
    assert missing_instance.status_code == 404
    missing_source_detach = client.post(
        f"/api/instances/{INSTANCE_ID}/development-sources/{UUID(int=999)}/detach",
        json={},
        headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
    )
    assert missing_source_detach.status_code == 404
    missing_instance_detach = client.post(
        f"/api/instances/{UUID(int=998)}/development-sources/{source_id}/detach",
        json={},
        headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
    )
    assert missing_instance_detach.status_code == 404


def test_active_mutation_blocks_detach_and_preserves_association(
    client: TestClient,
    data_root: Path,
    database: sessionmaker[Session],
) -> None:
    locator = "coordination/source"
    _write_source(data_root.parent / "development-root", locator)
    csrf = login(client)
    registered = _register(client, csrf, relative_locator=locator)
    source_id = UUID(str(registered["id"]))
    base = f"/api/instances/{INSTANCE_ID}/development-sources/{source_id}"
    assert (
        client.post(
            f"{base}/attach",
            json={},
            headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
        ).status_code
        == 200
    )
    with database.begin() as session:
        session.add(
            ManagedInstanceMutationLeaseRow(
                managed_instance_id=str(INSTANCE_ID),
                operation_kind=ManagedInstanceMutationKind.FIXTURE_ASSIGNMENT,
                operation_id=str(UUID(int=777)),
            )
        )
    blocked = client.post(
        f"{base}/detach",
        json={},
        headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
    )
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "managed_instance_mutation_conflict"
    with database() as session:
        assert (
            ManagedInstanceDevelopmentSourceRepository(session).get(
                INSTANCE_ID, source_id
            )
            is not None
        )


def test_routes_delegate_only_logical_authority_to_existing_services(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_id = UUID("88000000-0000-4000-8000-000000000099")
    source_read = DevelopmentSourceRead(
        id=source_id,
        logical_name="Delegated source",
        source_kind=DevelopmentSourceKind.LOCAL_DIRECTORY,
        addon_id="plugin.video.delegated",
        observed_name="Delegated",
        observed_version="1.0.0",
        extension_types=("xbmc.python.pluginsource",),
        registered_at=AT,
        validated_at=AT,
    )
    attached_read = AttachedDevelopmentSourceRead(
        development_source_id=source_id,
        logical_name="Delegated source",
        addon_id="plugin.video.delegated",
        observed_name="Delegated",
        observed_version="1.0.0",
        source_kind=DevelopmentSourceKind.LOCAL_DIRECTORY,
        attached_at=AT,
    )
    calls: list[tuple[object, ...]] = []

    def register(
        _service: DevelopmentSourceAdministrationService,
        *,
        logical_name: str,
        relative_locator: str,
    ) -> DevelopmentSourceRead:
        calls.append(("register", logical_name, relative_locator))
        return source_read

    def attach(
        _service: DevelopmentSourceAttachmentService,
        instance_id: UUID,
        requested_source_id: UUID,
    ) -> AttachedDevelopmentSourceRead:
        calls.append(("attach", instance_id, requested_source_id))
        return attached_read

    def detach(
        _service: DevelopmentSourceAttachmentService,
        instance_id: UUID,
        requested_source_id: UUID,
    ) -> AttachedDevelopmentSourceRead:
        calls.append(("detach", instance_id, requested_source_id))
        return attached_read

    monkeypatch.setattr(
        DevelopmentSourceAdministrationService, "register_source", register
    )
    monkeypatch.setattr(DevelopmentSourceAttachmentService, "attach_source", attach)
    monkeypatch.setattr(DevelopmentSourceAttachmentService, "detach_source", detach)
    csrf = login(client)
    headers = {**ORIGIN, "X-Kodi-Lab-CSRF": csrf}
    registration = client.post(
        "/api/development-sources",
        json={
            "logical_name": "Delegated source",
            "relative_locator": "does/not/exist",
        },
        headers=headers,
    )
    assert registration.status_code == 201
    base = f"/api/instances/{INSTANCE_ID}/development-sources/{source_id}"
    assert client.post(f"{base}/attach", json={}, headers=headers).status_code == 200
    assert client.post(f"{base}/detach", json={}, headers=headers).status_code == 200
    assert calls == [
        ("register", "Delegated source", "does/not/exist"),
        ("attach", INSTANCE_ID, source_id),
        ("detach", INSTANCE_ID, source_id),
    ]


def test_attach_addon_collision_is_a_safe_api_conflict(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_id = UUID("88000000-0000-4000-8000-000000000098")

    def collide(
        _service: DevelopmentSourceAttachmentService,
        instance_id: UUID,
        _source_id: UUID,
    ) -> AttachedDevelopmentSourceRead:
        raise DevelopmentSourceAddonConflictError(
            instance_id, "plugin.video.private-collision"
        )

    monkeypatch.setattr(DevelopmentSourceAttachmentService, "attach_source", collide)
    csrf = login(client)
    response = client.post(
        f"/api/instances/{INSTANCE_ID}/development-sources/{source_id}/attach",
        json={},
        headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "development_source_addon_conflict"
    assert "private-collision" not in response.text


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


def test_deleted_instance_leaves_source_live_view_and_retains_attachment_activity(
    client: TestClient, database: sessionmaker[Session]
) -> None:
    _seed_sources(database)
    history_id = UUID("88000000-0000-4000-8000-000000000010")
    with database.begin() as session:
        DevelopmentSourceAttachmentOperationRepository(session).create(
            DevelopmentSourceAttachmentOperation(
                id=history_id,
                original_managed_instance_id=INSTANCE_ID,
                managed_instance_id=INSTANCE_ID,
                development_source_id=SOURCE_A,
                source_logical_name="Alpha source",
                addon_id="plugin.video.alpha",
                kind=DevelopmentSourceAttachmentOperationKind.ATTACH,
                requested_at=AT,
                completed_at=AT,
                state=DevelopmentSourceAttachmentOperationState.SUCCEEDED,
            )
        )
        session.add(
            ManagedInstanceDeleteOperationRow(
                id="88000000-0000-4000-8000-000000000011",
                original_managed_instance_id=str(INSTANCE_ID),
                source_instance_name="Deleted Kodi",
                runtime_revision_id=str(REVISION_ID),
                fixture_definition_id=None,
                preserve_snapshots=False,
                snapshot_count=0,
                aggregate_snapshot_bytes=0,
                completed_snapshot_count=0,
                prior_state="stopped",
                notes_at_deletion=None,
                requested_at="2026-09-16T18:00:00.000000Z",
                started_at="2026-09-16T18:00:00.000000Z",
                heartbeat_at="2026-09-16T18:00:00.000000Z",
                completed_at="2026-09-16T18:00:00.000000Z",
                state="succeeded",
                phase="succeeded",
                reconciliation_required=False,
            )
        )
        attachments = ManagedInstanceDevelopmentSourceRepository(session)
        attachments.delete_for_instance(INSTANCE_ID)
        DevelopmentSourceAttachmentOperationRepository(session).clear_live_instance(
            INSTANCE_ID
        )
        instance = session.get(ManagedInstanceRow, str(INSTANCE_ID))
        target = session.get(TargetRow, str(INSTANCE_ID))
        assert instance is not None and target is not None
        session.delete(instance)
        session.flush()
        session.delete(target)

    login(client)
    detail = client.get(f"/development-sources/{SOURCE_A}")
    assert detail.status_code == 200
    assert "This source has no current live instance attachments." in detail.text
    catalog = client.get("/development-sources")
    assert "Attached live instances</dt><dd>0" in catalog.text
    history = client.get(f"/history/instances/{INSTANCE_ID}")
    assert history.status_code == 200
    assert "Development source attached" in history.text
    assert "Alpha source" not in history.text
    assert SECRET_LOCATOR_A not in history.text
    assert client.get(f"/api/development-sources/{SOURCE_A}").status_code == 200
