from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.broker.docker_inventory import (
    ContainerHealthState,
    ContainerRuntimeState,
)
from kodi_lab.broker.operations import ManagedContainerInventoryEntry
from kodi_lab.kodi.addons import (
    KodiAddonInventoryRecord,
    KodiAddonInventoryResult,
)
from kodi_lab.kodi.readiness import KodiApplicationIdentity
from kodi_lab.storage.models import (
    DevelopmentSourceDeploymentOperationRow,
    DevelopmentSourceDeploymentRow,
    DevelopmentSourceRow,
)

INSTANCE_ID = UUID("00000000-0000-4000-8000-000000000101")
REVISION_ID = UUID("3a000000-0000-4000-8000-000000000006")
CONTAINER_ID = "a" * 64
PASSWORD = "correct horse battery staple"
SOURCE_ID = UUID("00000000-0000-4000-8000-000000000120")
OPERATION_ID = UUID("00000000-0000-4000-8000-000000000121")
NOW = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)


class WebBroker(Protocol):
    container: ManagedContainerInventoryEntry | None
    addon_inventory_result: KodiAddonInventoryResult
    calls: list[tuple[str, dict[str, object]]]


def inventory_entry(state: ContainerRuntimeState) -> ManagedContainerInventoryEntry:
    return ManagedContainerInventoryEntry(
        managed_instance_id=str(INSTANCE_ID),
        container_id=CONTAINER_ID,
        image_id="sha256:" + "b" * 64,
        runtime_revision_id=str(REVISION_ID),
        runtime_state=state,
        health_state=ContainerHealthState.HEALTHY,
    )


def login(client: TestClient) -> None:
    response = client.post(
        "/login",
        data={"password": PASSWORD},
        headers={"Origin": "http://127.0.0.1"},
        follow_redirects=False,
    )
    assert response.status_code == 303


def configure_live_inventory(broker: WebBroker) -> None:
    broker.container = inventory_entry(ContainerRuntimeState.RUNNING)
    broker.addon_inventory_result = KodiAddonInventoryResult(
        identity=KodiApplicationIdentity("Kodi", 21, 3, "stable"),
        items=(
            KodiAddonInventoryRecord(
                addon_id="a.enabled",
                name="Enabled <not markup>",
                version="01.02",
                enabled=True,
                kodi_type="xbmc.service",
            ),
            KodiAddonInventoryRecord(
                addon_id="plugin.video.example",
                name="Example",
                version="1.2.3",
                enabled=False,
                kodi_type="xbmc.python.pluginsource",
            ),
        ),
    )


def add_verified_evidence(
    database: sessionmaker[Session], *, confidence: str = "verified_at_last_operation"
) -> None:
    with database.begin() as session:
        session.add(
            DevelopmentSourceRow(
                id=str(SOURCE_ID),
                logical_name="safe source",
                source_kind="local_directory",
                relative_locator="safe-source",
                addon_id="plugin.video.example",
                observed_name="Example",
                observed_version="1.2.3",
                extension_types_json='["xbmc.python.pluginsource"]',
                registered_at=NOW.isoformat(),
                validated_at=NOW.isoformat(),
            )
        )
        session.add(
            DevelopmentSourceDeploymentOperationRow(
                id=str(OPERATION_ID),
                original_managed_instance_id=str(INSTANCE_ID),
                managed_instance_id=str(INSTANCE_ID),
                development_source_id=str(SOURCE_ID),
                source_logical_name="safe source",
                addon_id="plugin.video.example",
                runtime_revision_id=str(REVISION_ID),
                request_kind="copy_sync",
                requested_at=NOW.isoformat(),
                state="succeeded",
                phase="finalize",
                result_classification="initial_deployment",
                new_count=1,
                changed_count=0,
                unchanged_count=0,
                destination_only_count=0,
                transferred_bytes=1,
                expected_container_id=CONTAINER_ID,
                started_at=NOW.isoformat(),
                heartbeat_at=NOW.isoformat(),
                completed_at=NOW.isoformat(),
                reconciliation_required=False,
            )
        )
        session.add(
            DevelopmentSourceDeploymentRow(
                managed_instance_id=str(INSTANCE_ID),
                development_source_id=str(SOURCE_ID),
                addon_id="plugin.video.example",
                mode="copy",
                last_successful_operation_id=str(OPERATION_ID),
                source_manifest_digest="sha256:" + "1" * 64,
                verified_destination_digest="sha256:" + "2" * 64,
                runtime_revision_id=str(REVISION_ID),
                trusted_container_id=CONTAINER_ID,
                observed_source_name="Example",
                observed_source_version="1.2.3",
                verified_at=NOW.isoformat(),
                confidence=confidence,
            )
        )


def test_inventory_api_requires_authentication_and_is_no_store(
    client: TestClient,
) -> None:
    unauthenticated = client.get(f"/api/instances/{INSTANCE_ID}/addons")
    assert unauthenticated.status_code == 401
    login(client)
    response = client.get(f"/api/instances/{INSTANCE_ID}/addons")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["unavailable_reason"] == "instance_not_provisioned"


def test_inventory_api_uses_existing_typed_instance_not_found_error(
    client: TestClient,
) -> None:
    login(client)
    response = client.get(f"/api/instances/{UUID(int=999)}/addons")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "managed_instance_not_found"


def test_api_returns_normalized_complete_inventory_including_disabled(
    client: TestClient, broker: WebBroker
) -> None:
    configure_live_inventory(broker)
    login(client)
    activity_before = client.get(f"/api/instances/{INSTANCE_ID}/activity").json()
    response = client.get(f"/api/instances/{INSTANCE_ID}/addons")
    assert response.status_code == 200
    document = response.json()
    assert document["query_status"] == "available"
    assert document["complete"] is True
    assert [item["addon_id"] for item in document["items"]] == [
        "a.enabled",
        "plugin.video.example",
    ]
    assert document["items"][1]["enabled"] is False
    serialized = response.text
    for forbidden in (
        "10.250.",
        "/jsonrpc",
        CONTAINER_ID,
        "sha256:",
        "source_manifest_digest",
        "verified_destination_digest",
    ):
        assert forbidden not in serialized
    assert (
        client.get(f"/api/instances/{INSTANCE_ID}/activity").json() == activity_before
    )


def test_caller_network_query_fields_cannot_override_trusted_endpoint(
    client: TestClient, broker: WebBroker
) -> None:
    configure_live_inventory(broker)
    login(client)
    response = client.get(
        f"/api/instances/{INSTANCE_ID}/addons",
        params={
            "host": "attacker.invalid",
            "ip": "203.0.113.9",
            "port": "1",
            "url": "http://attacker.invalid/",
            "container_id": "c" * 64,
        },
    )
    assert response.status_code == 200
    assert response.json()["query_status"] == "available"
    assert "attacker.invalid" not in response.text


def test_instance_page_survives_unavailable_inventory_without_lifecycle_action(
    client: TestClient, broker: WebBroker
) -> None:
    login(client)
    response = client.get(f"/instances/{INSTANCE_ID}")
    assert response.status_code == 200
    assert "Add-on inventory unavailable: instance not provisioned" in response.text
    assert broker.calls == []


def test_instance_page_escapes_metadata_and_has_no_inventory_mutation_controls(
    client: TestClient, broker: WebBroker
) -> None:
    configure_live_inventory(broker)
    login(client)
    response = client.get(f"/instances/{INSTANCE_ID}")
    assert response.status_code == 200
    assert "Enabled &lt;not markup&gt;" in response.text
    assert "Disabled" in response.text
    assert "Provenance unknown" in response.text
    assert "enable add-on" not in response.text.lower()
    assert "disable add-on" not in response.text.lower()
    assert "repository install" not in response.text.lower()


def test_page_uses_only_historical_verified_copy_wording(
    client: TestClient,
    broker: WebBroker,
    database: sessionmaker[Session],
) -> None:
    configure_live_inventory(broker)
    add_verified_evidence(database)
    login(client)
    response = client.get(f"/instances/{INSTANCE_ID}")
    assert "Prior verified copy: safe source" in response.text
    assert "Installed from safe source" not in response.text
    assert "Current source" not in response.text
    assert "Source verified" not in response.text


def test_page_exposes_stale_evidence_without_source_claim(
    client: TestClient,
    broker: WebBroker,
    database: sessionmaker[Session],
) -> None:
    configure_live_inventory(broker)
    add_verified_evidence(database, confidence="stale")
    login(client)
    response = client.get(f"/instances/{INSTANCE_ID}")
    assert "Prior verified copy evidence stale: safe source" in response.text
    assert "Prior verified copy: safe source" not in response.text
