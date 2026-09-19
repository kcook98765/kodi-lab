#!/usr/bin/env python3
"""Isolated live proof for Phase 10 Slice 1 managed Kodi screenshots."""

# ruff: noqa: E402 -- bytecode suppression must precede project imports.

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import UUID, uuid4

sys.dont_write_bytecode = True

import docker  # type: ignore[import-untyped]
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import text

from kodi_lab.auth import WebAuthenticationService
from kodi_lab.broker.docker_inventory import DockerManagedContainerAdapter
from kodi_lab.broker.operations import build_foundation_registry
from kodi_lab.broker.provisioning import (
    MANAGED_NETWORK_NAME,
    SqliteManagedInstanceRevisionResolver,
    SqliteProvisioningResolver,
    SqliteRuntimeObservationResolver,
)
from kodi_lab.broker.screenshots import (
    DockerManagedScreenshotCaptureProvider,
    ManagedScreenshotCaptureSpec,
    ScreenshotCaptureError,
    SqliteScreenshotCaptureResolver,
)
from kodi_lab.config import Settings
from kodi_lab.kodi.screenshots import validate_png
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.storage.models import ManagedScreenshotRow, TargetRow
from kodi_lab.web.app import create_app

PASSWORD = "phase10-screenshot-live-password"
ORIGIN = "http://127.0.0.1"


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _phase8_helpers(project_root: Path) -> ModuleType:
    path = project_root / "scripts" / "validate-phase8-step5.py"
    spec = importlib.util.spec_from_file_location("phase8_step5_helpers", path)
    _require(spec is not None and spec.loader is not None, "helper import failed")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _managed_network_count() -> int:
    api = docker.APIClient()
    try:
        return len(api.inspect_network(MANAGED_NETWORK_NAME).get("Containers") or {})
    finally:
        api.close()


def _cleanup_created(instance_id: UUID) -> None:
    api = docker.APIClient()
    try:
        for summary in api.containers(
            all=True,
            filters={
                "label": [
                    "io.kodi-lab.managed=true",
                    f"io.kodi-lab.instance-id={instance_id}",
                ]
            },
        ):
            if summary.get("State") == "running":
                api.stop(summary["Id"], timeout=10)
            api.remove_container(summary["Id"])
    finally:
        api.close()


class InjectingScreenshotProvider:
    def __init__(self) -> None:
        self._delegate = DockerManagedScreenshotCaptureProvider()
        self.fail_next = False
        self.last_failure_class: str | None = None

    def capture_managed_kodi_screenshot(
        self, *, spec: ManagedScreenshotCaptureSpec, expected_container_id: str
    ) -> Any:
        if self.fail_next:
            self.fail_next = False
            raise ScreenshotCaptureError("injected fixed-backend failure")
        try:
            return self._delegate.capture_managed_kodi_screenshot(
                spec=spec, expected_container_id=expected_container_id
            )
        except Exception as error:
            cause = error.__cause__
            self.last_failure_class = f"{type(error).__name__}:{error}"
            if cause is not None:
                self.last_failure_class += f"/{type(cause).__name__}"
            raise


def _wait_visual_ready(client: TestClient, instance_id: UUID) -> dict[str, Any]:
    last: dict[str, Any] | None = None
    for _ in range(900):
        response = client.get(f"/api/instances/{instance_id}/visual-status")
        _require(response.status_code == 200, "visual status request failed")
        last = response.json()
        if last["screenshot_available_now"]:
            return last
        time.sleep(0.1)
    raise RuntimeError(f"visual status did not become ready: {last!r}")


def _wait_delete(client: TestClient, operation_id: str) -> dict[str, Any]:
    last: dict[str, Any] | None = None
    for _ in range(900):
        response = client.get(f"/api/instance-delete-operations/{operation_id}")
        _require(response.status_code == 200, "delete status request failed")
        last = response.json()
        if last["state"] not in {"queued", "running"}:
            return last
        time.sleep(0.1)
    raise RuntimeError(f"instance deletion did not terminalize: {last!r}")


def _capture(
    client: TestClient,
    headers: dict[str, str],
    instance_id: UUID,
    provider: InjectingScreenshotProvider,
) -> tuple[dict[str, Any], bytes]:
    response = client.post(
        f"/api/instances/{instance_id}/screenshots", json={}, headers=headers
    )
    _require(
        response.status_code == 200,
        f"capture failed ({provider.last_failure_class}): {response.text[:256]}",
    )
    document = response.json()
    image = client.get(f"/api/instances/{instance_id}/screenshots/latest")
    _require(image.status_code == 200, "authenticated latest image failed")
    _require(image.headers.get("cache-control") == "no-store", "no-store missing")
    _require(
        image.headers.get("x-content-type-options") == "nosniff", "nosniff missing"
    )
    validated = validate_png(image.content)
    _require(validated.width == 1280 and validated.height == 720, "geometry differs")
    return document, image.content


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--evidence-path",
        type=Path,
        default=Path("<project-root>-phase10-slice1-evidence.json"),
    )
    arguments = parser.parse_args()
    _require(os.geteuid() == 0, "live validation must run as root")
    project_root = Path(__file__).resolve().parents[1]
    helper = _phase8_helpers(project_root)
    helper.__dict__["PASSWORD"] = PASSWORD
    helper.__dict__["ORIGIN"] = ORIGIN
    protected_before = helper._protected_fingerprint()
    _require(_managed_network_count() == 0, "managed network was not empty")
    _require(
        protected_before["production_data_root_absent"]
        and protected_before["production_runtime_root_absent"],
        "production roots were present",
    )
    instances = {str(item["release"]): uuid4() for item in helper.RUNTIMES}
    instance_id = instances["21.3"]
    evidence: dict[str, Any] = {}
    engine = None
    try:
        with tempfile.TemporaryDirectory(
            prefix="kodi-lab-phase10-slice1-", dir="/var/tmp"
        ) as temporary:
            root = Path(temporary)
            data_root = root / "data"
            (data_root / "database").mkdir(parents=True)
            runtime_root = root / "runtime"
            runtime_root.mkdir(mode=0o700)
            os.environ["KODI_LAB_DATA_ROOT"] = str(data_root)
            os.environ["KODI_LAB_RUNTIME_ROOT"] = str(runtime_root)
            settings = Settings()
            command.upgrade(Config(str(project_root / "alembic.ini")), "head")
            engine = create_database_engine(settings.database_url)
            factory = create_session_factory(engine)
            helper._seed(factory, instances)
            with factory.begin() as session:
                session.query(TargetRow).filter(
                    TargetRow.id == str(instance_id)
                ).update(
                    {
                        "availability": "available",
                        "health": "healthy",
                        "connection_state": "connected",
                    }
                )
            adapter = DockerManagedContainerAdapter()
            screenshot_provider = InjectingScreenshotProvider()
            registry = build_foundation_registry(
                adapter,
                provisioning_resolver=SqliteProvisioningResolver(factory),
                provisioning_provider=adapter,
                deprovisioning_provider=adapter,
                revision_resolver=SqliteManagedInstanceRevisionResolver(factory),
                runtime_observation_resolver=SqliteRuntimeObservationResolver(factory),
                runtime_observation_provider=adapter,
                screenshot_capture_resolver=SqliteScreenshotCaptureResolver(
                    factory, data_root=data_root
                ),
                screenshot_capture_provider=screenshot_provider,
            )
            with helper._broker_server(root, registry) as broker:
                auth = WebAuthenticationService(factory)
                auth.set_password(PASSWORD)
                app = create_app(
                    settings=settings,
                    session_factory=factory,
                    broker=broker,
                    auth=auth,
                )
                with TestClient(app, base_url=ORIGIN) as client:
                    headers = helper._login(client)
                    provisioned = client.post(
                        f"/api/instances/{instance_id}/provision",
                        json={},
                        headers=headers,
                    )
                    _require(provisioned.status_code == 200, "provision failed")
                    started = client.post(
                        f"/api/instances/{instance_id}/start",
                        json={},
                        headers=headers,
                    )
                    _require(started.status_code == 200, "start failed")
                    visual = _wait_visual_ready(client, instance_id)
                    first, first_png = _capture(
                        client, headers, instance_id, screenshot_provider
                    )
                    first_status = first["screenshot"]
                    first_capture = first["capture_id"]
                    first_hash = hashlib.sha256(first_png).hexdigest()
                    first_path = (
                        data_root
                        / "screenshots"
                        / "instances"
                        / str(instance_id)
                        / f"{first_capture}.png"
                    )
                    _require(first_path.is_file(), "first private cache missing")

                    second, second_png = _capture(
                        client, headers, instance_id, screenshot_provider
                    )
                    second_status = second["screenshot"]
                    second_capture = second["capture_id"]
                    _require(second_capture != first_capture, "capture UUID was reused")
                    _require(not first_path.exists(), "superseded PNG was retained")
                    second_hash = hashlib.sha256(second_png).hexdigest()

                    screenshot_provider.fail_next = True
                    failed = client.post(
                        f"/api/instances/{instance_id}/screenshots",
                        json={},
                        headers=headers,
                    )
                    _require(failed.status_code == 503, "injected failure was accepted")
                    retained = client.get(
                        f"/api/instances/{instance_id}/visual-status"
                    ).json()
                    _require(
                        retained["capture_id"] == second_capture
                        and retained["captured_at"] == second_status["captured_at"],
                        "failed refresh changed the current binding",
                    )

                restarted = create_app(
                    settings=settings,
                    session_factory=factory,
                    broker=broker,
                    auth=auth,
                )
                with TestClient(restarted, base_url=ORIGIN) as client:
                    headers = helper._login(client)
                    restored = client.get(
                        f"/api/instances/{instance_id}/visual-status"
                    ).json()
                    _require(
                        restored["capture_id"] == second_capture,
                        "restart lost current screenshot metadata",
                    )
                    restored_image = client.get(
                        f"/api/instances/{instance_id}/screenshots/latest"
                    )
                    _require(
                        hashlib.sha256(restored_image.content).hexdigest()
                        == second_hash,
                        "restart latest image differs",
                    )
                    queued = client.post(
                        f"/api/instances/{instance_id}/delete",
                        json={"confirmed": True, "preserve_snapshots": True},
                        headers=headers,
                    )
                    _require(queued.status_code == 202, "instance delete was rejected")
                    deleted = _wait_delete(client, queued.json()["operation_id"])
                    _require(deleted["state"] == "succeeded", "instance delete failed")
                with factory() as session:
                    migration_head = session.scalar(
                        text("SELECT version_num FROM alembic_version")
                    )
                    screenshot_row = session.get(ManagedScreenshotRow, str(instance_id))
                _require(
                    screenshot_row is None, "screenshot metadata survived deletion"
                )
                _require(
                    not (
                        data_root / "screenshots" / "instances" / str(instance_id)
                    ).exists(),
                    "screenshot bytes survived deletion",
                )
                evidence = {
                    "schema": "kodi-lab-phase10-slice1-live-v1",
                    "migration_head": migration_head,
                    "runtime": "Kodi 21.3 stable",
                    "instance_id": str(instance_id),
                    "visual_supported": visual["screenshot_supported"],
                    "visual_available": visual["screenshot_available_now"],
                    "captures": [
                        {
                            "capture_id": first_capture,
                            "captured_at": first_status["captured_at"],
                            "width": first_status["width"],
                            "height": first_status["height"],
                            "byte_size": first_status["byte_size"],
                            "sha256": first_hash,
                        },
                        {
                            "capture_id": second_capture,
                            "captured_at": second_status["captured_at"],
                            "width": second_status["width"],
                            "height": second_status["height"],
                            "byte_size": second_status["byte_size"],
                            "sha256": second_hash,
                        },
                    ],
                    "replacement_removed_prior": True,
                    "failed_refresh_retained_prior": True,
                    "restart_retained_latest": True,
                    "authenticated_latest_delivery": True,
                    "instance_deletion_removed_cache": True,
                }
            engine.dispose()
            engine = None
    finally:
        _cleanup_created(instance_id)
        if engine is not None:
            engine.dispose()

    _require(_managed_network_count() == 0, "managed network was not cleaned")
    protected_after = helper._protected_fingerprint()
    changed = sorted(
        key
        for key in protected_before
        if protected_before[key] != protected_after.get(key)
    )
    _require(not changed, f"protected categories changed: {changed!r}")
    evidence["protected_isolation"] = {
        "legacy_service_unchanged": True,
        "legacy_socket_unchanged": True,
        "protected_containers_unchanged": True,
        "protected_networks_unchanged": True,
        "production_roots_absent": True,
        "managed_temporary_resources_cleaned": True,
    }
    payload = (json.dumps(evidence, indent=2, sort_keys=True) + "\n").encode()
    arguments.evidence_path.write_bytes(payload)
    print(
        json.dumps(
            {
                "evidence_path": str(arguments.evidence_path),
                "sha256": hashlib.sha256(payload).hexdigest(),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
