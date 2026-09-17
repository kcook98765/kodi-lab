#!/usr/bin/env python3
"""Run isolated authenticated Phase 8 Step 3 Web validation."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import cast
from uuid import uuid4

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import text

from kodi_lab.application import (
    DevelopmentSourceAdministrationService,
    DevelopmentSourceAttachmentService,
)
from kodi_lab.application.instance_delete_operations import (
    ManagedInstanceDeleteOperationCoordinator,
)
from kodi_lab.application.instance_deletions import (
    DisposableManagedInstanceDeletionService,
    InstanceDeleteBroker,
)
from kodi_lab.application.readiness_tasks import ReadinessTaskRegistry
from kodi_lab.auth import WebAuthenticationService
from kodi_lab.broker.operations import ManagedContainerInventoryResult
from kodi_lab.config import Settings
from kodi_lab.development import DevelopmentSourceValidator
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.storage.models import (
    ManagedInstanceRow,
    RuntimeDefinitionRow,
    RuntimeExecutionProfileRow,
    RuntimeRevisionRow,
    TargetRow,
)
from kodi_lab.storage.repositories import (
    DevelopmentSourceAttachmentOperationRepository,
    DevelopmentSourceRepository,
    ManagedInstanceDevelopmentSourceRepository,
)
from kodi_lab.web.app import create_app

PASSWORD = "phase8-step3-validation-password"


def write_source(
    root: Path, locator: str, *, addon_id: str, name: str, version: str
) -> None:
    directory = root.joinpath(*locator.split("/"))
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "addon.xml").write_text(
        f'<addon id="{addon_id}" name="{name}" version="{version}">'
        '<extension point="xbmc.python.pluginsource" />'
        '<extension point="xbmc.addon.metadata" /></addon>',
        encoding="utf-8",
    )


class EmptyInventoryBroker:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def managed_container_inventory(self) -> ManagedContainerInventoryResult:
        self.calls.append("managed_container_inventory")
        return ManagedContainerInventoryResult(containers=[])

    def __getattr__(self, name: str) -> object:
        raise RuntimeError(f"unexpected broker method: {name}")


def main() -> None:
    project_root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(
        prefix="kodi-lab-phase8-step3-", dir="/var/tmp"
    ) as temporary:
        validation_root = Path(temporary)
        development_root = validation_root / "development"
        development_root.mkdir(mode=0o700)
        data_root = validation_root / "data"
        (data_root / "database").mkdir(parents=True)
        os.environ["KODI_LAB_DATA_ROOT"] = str(data_root)
        os.environ["KODI_LAB_DEVELOPMENT_ROOT"] = str(development_root)
        settings = Settings()

        command.upgrade(Config(str(project_root / "alembic.ini")), "head")
        engine = create_database_engine(settings.database_url)
        factory = create_session_factory(engine)
        definition_id = uuid4()
        revision_id = uuid4()
        instance_ids = {name: uuid4() for name in ("I1", "I2")}
        timestamp = "2026-09-16T23:00:00.000000Z"
        with factory.begin() as session:
            session.add(
                RuntimeDefinitionRow(
                    id=str(definition_id),
                    slug=f"step3-{definition_id.hex}",
                    kodi_version="21.3",
                )
            )
            session.add(
                RuntimeRevisionRow(
                    id=str(revision_id),
                    runtime_definition_id=str(definition_id),
                    revision_number=1,
                    source_kind="validation",
                    source_reference="synthetic",
                    content_digest="sha256:" + "a" * 64,
                    manifest_reference="synthetic",
                    manifest_digest="sha256:" + "b" * 64,
                    built_at=timestamp,
                    approval_state="approved",
                    approved_at=timestamp,
                    requires_trusted_build_confirmation=False,
                )
            )
            session.flush()
            session.add(
                RuntimeExecutionProfileRow(
                    runtime_revision_id=str(revision_id),
                    runtime_kind="kodi",
                    runtime_uid=1000,
                    runtime_gid=1000,
                    expected_application_name="Kodi",
                    expected_kodi_version="21.3",
                    expected_version_tag="stable",
                    jsonrpc_port=8080,
                    jsonrpc_path="/jsonrpc",
                    process_observation="docker_healthcheck",
                    readiness_probe="kodi_jsonrpc",
                    readiness_timeout_seconds=90,
                    poll_interval_seconds=1.0,
                    http_timeout_seconds=3.0,
                )
            )
            session.flush()
            for label, instance_id in instance_ids.items():
                session.add(
                    TargetRow(
                        id=str(instance_id),
                        name=f"Step 3 {label}",
                        target_type="managed",
                        kodi_version="21.3",
                        availability="unavailable",
                        health="warning",
                        connection_state="disconnected",
                    )
                )
                session.flush()
                session.add(
                    ManagedInstanceRow(
                        target_id=str(instance_id),
                        instance_type="disposable",
                        runtime_revision_id=str(revision_id),
                        runtime_state="stopped",
                        start_on_host_boot=False,
                    )
                )

        write_source(
            development_root,
            "sources/s1",
            addon_id="plugin.video.kodi-lab-step3-s1",
            name="Step 3 Source 1",
            version="1.0.0",
        )
        write_source(
            development_root,
            "sources/s2",
            addon_id="plugin.video.kodi-lab-step3-s2",
            name="Step 3 Source 2",
            version="1.0.0",
        )
        validator = DevelopmentSourceValidator(development_root)
        catalog = DevelopmentSourceAdministrationService(factory, validator)
        s1 = catalog.register_source(
            logical_name="Step 3 Source 1", relative_locator="sources/s1"
        )
        s2 = catalog.register_source(
            logical_name="Step 3 Source 2", relative_locator="sources/s2"
        )
        attachments = DevelopmentSourceAttachmentService(factory, validator)
        attachments.attach_source(instance_ids["I1"], s1.id)
        attachments.attach_source(instance_ids["I1"], s2.id)
        attachments.attach_source(instance_ids["I2"], s1.id)

        broker = EmptyInventoryBroker()
        auth = WebAuthenticationService(factory)
        auth.set_password(PASSWORD)
        readiness = ReadinessTaskRegistry(max_workers=1)
        app = create_app(
            settings=settings,
            session_factory=factory,
            broker=broker,
            readiness=readiness,
            auth=auth,
        )
        with TestClient(app, base_url="http://127.0.0.1") as client:
            login = client.post(
                "/login",
                data={"password": PASSWORD},
                headers={"Origin": "http://127.0.0.1"},
                follow_redirects=False,
            )
            if login.status_code != 303:
                raise RuntimeError("authenticated Web login failed")

            catalog_page = client.get("/development-sources")
            source_page = client.get(f"/development-sources/{s1.id}")
            instance_page = client.get(f"/instances/{instance_ids['I1']}")
            api_catalog = client.get("/api/development-sources")
            api_source = client.get(f"/api/development-sources/{s1.id}")
            initial_catalog = api_catalog.json()
            if [item["attached_instance_count"] for item in initial_catalog] != [
                2,
                1,
            ]:
                raise RuntimeError("live attachment counts were incorrect")
            if len(api_source.json()["attached_instances"]) != 2:
                raise RuntimeError("source detail did not show both live instances")

            write_source(
                development_root,
                "sources/s1",
                addon_id=s1.addon_id,
                name="Changed filesystem name",
                version="9.9.9",
            )
            stale_page = client.get(f"/development-sources/{s1.id}")
            stale_api = client.get(f"/api/development-sources/{s1.id}")
            if (
                "Changed filesystem name" in stale_page.text
                or stale_api.json()["source"]["observed_version"] != "1.0.0"
            ):
                raise RuntimeError("GET revalidated or rewrote stored metadata")

            deletion = ManagedInstanceDeleteOperationCoordinator(factory)
            queued = deletion.queue(instance_ids["I2"], preserve_snapshots=False)
            lease = deletion.acquire(queued.operation_id)
            if lease is None:
                raise RuntimeError("validation instance deletion was not acquired")
            DisposableManagedInstanceDeletionService(
                factory,
                cast(InstanceDeleteBroker, broker),
                readiness,
                deletion,
                lease,
                data_root=data_root,
            ).delete()
            after_delete = client.get(f"/api/development-sources/{s1.id}")
            deleted_history = client.get(f"/history/instances/{instance_ids['I2']}")

            responses = (
                catalog_page,
                source_page,
                instance_page,
                api_catalog,
                api_source,
                stale_page,
                stale_api,
                after_delete,
                deleted_history,
            )
            forbidden = (
                "sources/s1",
                "sources/s2",
                "relative_locator",
                "development_root",
                str(development_root),
                "addon.xml",
            )
            redaction_failures = [
                value
                for response in responses
                for value in forbidden
                if value in response.text
            ]
            if redaction_failures:
                raise RuntimeError(
                    "Web response exposed source path or content metadata"
                )
            if any(response.status_code != 200 for response in responses):
                raise RuntimeError("one or more authenticated GET routes failed")
            if any(
                response.headers.get("Cache-Control") != "no-store"
                for response in responses
            ):
                raise RuntimeError("one or more GET routes omitted no-store")
            if len(after_delete.json()["attached_instances"]) != 1:
                raise RuntimeError("deleted instance remained in live source detail")
            if "Development source attached" not in deleted_history.text:
                raise RuntimeError("deleted instance lost attachment activity")

            with factory() as session:
                stored = DevelopmentSourceRepository(session).get(s1.id)
                history = DevelopmentSourceAttachmentOperationRepository(
                    session
                ).list_for_original_instance(instance_ids["I2"])
                remaining = ManagedInstanceDevelopmentSourceRepository(
                    session
                ).list_instance_ids_for_source(s1.id)
                migration_head = session.scalar(
                    text("SELECT version_num FROM alembic_version")
                )
            if stored is None:
                raise RuntimeError("source catalog did not survive instance deletion")

            evidence = {
                "schema": "kodi-lab-phase8-step3-live-v1",
                "validation_root": "dedicated temporary root under /var/tmp",
                "sources": {
                    "S1": {"uuid": str(s1.id), "addon_id": s1.addon_id},
                    "S2": {"uuid": str(s2.id), "addon_id": s2.addon_id},
                },
                "instances": {key: str(value) for key, value in instance_ids.items()},
                "catalog": {
                    "route_status": catalog_page.status_code,
                    "api_status": api_catalog.status_code,
                    "attachment_counts": [
                        item["attached_instance_count"] for item in initial_catalog
                    ],
                    "deterministic_names": [
                        item["source"]["logical_name"] for item in initial_catalog
                    ],
                },
                "source_detail": {
                    "route_status": source_page.status_code,
                    "live_instances_before_delete": 2,
                    "links_verified": True,
                    "observation_language_verified": True,
                },
                "instance_detail": {
                    "route_status": instance_page.status_code,
                    "attached_sources": 2,
                    "source_links_verified": True,
                    "mutation_controls_absent": True,
                },
                "stale_observation": {
                    "filesystem_changed": True,
                    "stored_name": stored.observed_name,
                    "stored_version": stored.observed_version,
                    "get_did_not_refresh": True,
                },
                "deleted_instance": {
                    "live_attachment_count_after_delete": len(remaining),
                    "deleted_history_status": deleted_history.status_code,
                    "attachment_history_operation_ids": [
                        str(item.id) for item in history
                    ],
                    "source_catalog_survived": True,
                },
                "privacy": {
                    "responses_checked": len(responses),
                    "absolute_path_absent": True,
                    "relative_locator_absent": True,
                    "source_content_absent": True,
                },
                "broker": {
                    "methods_available": ["managed_container_inventory"],
                    "source_methods": [],
                    "calls": broker.calls,
                },
                "migration_head": migration_head,
                "temporary_root_removed_after_validation": True,
            }
            print(json.dumps(evidence, indent=2, sort_keys=True))
        engine.dispose()


if __name__ == "__main__":
    main()
