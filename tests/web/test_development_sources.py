from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.development_sources import (
    DevelopmentSourceAdministrationService,
)
from kodi_lab.development import DevelopmentSourceValidator
from kodi_lab.domain import (
    DevelopmentSource,
    DevelopmentSourceAttachmentOperation,
    DevelopmentSourceAttachmentOperationKind,
    DevelopmentSourceAttachmentOperationState,
    DevelopmentSourceKind,
    ManagedInstanceDevelopmentSource,
)
from kodi_lab.storage.models import (
    ManagedInstanceDeleteOperationRow,
    ManagedInstanceRow,
    TargetRow,
)
from kodi_lab.storage.repositories import (
    DevelopmentSourceAttachmentOperationRepository,
    DevelopmentSourceRepository,
    ManagedInstanceDevelopmentSourceRepository,
)

from .conftest import INSTANCE_ID, REVISION_ID, login

SOURCE_A = UUID("88000000-0000-4000-8000-000000000001")
SOURCE_B = UUID("88000000-0000-4000-8000-000000000002")
SECOND_INSTANCE = UUID("88000000-0000-4000-8000-000000000003")
AT = datetime(2026, 9, 16, 18, 0, tzinfo=UTC)
SECRET_LOCATOR_A = "private-source-tree/video"
SECRET_LOCATOR_B = "internal/other-addon"


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
    assert 'href="/development-sources"' in client.get("/").text
    assert client.get(f"/development-sources/{SOURCE_A}").status_code == 404
    missing = client.get(f"/api/development-sources/{SOURCE_A}")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "development_source_not_found"


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
    assert "<form" not in detail.text
    assert "<script>not markup</script>" not in detail.text
    assert "Kodi &lt;script&gt;not markup&lt;/script&gt;" in detail.text
    assert "<form" not in catalog.text

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
            "relative_locator",
            "development_root",
            "/home/",
            "addon.xml",
        ):
            assert forbidden not in response.text


def test_instance_detail_lists_attachments_without_mutation_controls(
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
    assert ">Detach<" not in section
    assert ">Deploy<" not in section
    assert "<form" not in section
    assert SECRET_LOCATOR_A not in page.text


def test_instance_detail_has_source_empty_state(client: TestClient) -> None:
    login(client)
    page = client.get(f"/instances/{INSTANCE_ID}")
    assert "No development sources are attached." in page.text


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
        database, DevelopmentSourceValidator(development_root)
    ).register_source(logical_name="Mutable source", relative_locator="mutable")
    addon_xml.write_text(
        '<addon id="plugin.video.mutable" name="Changed name" version="9.9">'
        '<extension point="xbmc.python.pluginsource"/></addon>',
        encoding="utf-8",
    )

    login(client)
    page = client.get(f"/development-sources/{registered.id}")
    api = client.get(f"/api/development-sources/{registered.id}")
    assert "Original name" in page.text
    assert "1.0" in page.text
    assert "Changed name" not in page.text
    assert "9.9" not in page.text
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
