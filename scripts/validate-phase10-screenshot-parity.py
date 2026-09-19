#!/usr/bin/env python3
"""Isolated live proof for Phase 10 managed screenshot runtime parity."""

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
from kodi_lab.kodi.screenshots import (
    X11FrameLayout,
    inspect_x11_frame,
    png_pixel_statistics,
    validate_png,
)
from kodi_lab.runtime.curated_catalog import CURATED_SUPPRESSION_REVISIONS
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.storage.models import (
    ManagedInstanceRow,
    ManagedScreenshotRow,
    RuntimeContainerArtifactRow,
    RuntimeDefinitionRow,
    RuntimeExecutionProfileRow,
    RuntimeRevisionRow,
    TargetRow,
)
from kodi_lab.web.app import create_app

PASSWORD = "phase10-screenshot-parity-live-password"
ORIGIN = "http://127.0.0.1"
FAILURE_RETENTION_RELEASE = "20.5"
RUNTIME_NAMES = {
    "19.5": "Kodi 19.5 Legacy",
    "20.5": "Kodi 20.5 Current",
    "21.3": "Kodi 21.3 Current",
    "22.0-beta.2": "Kodi 22 Beta 2 Preview",
}


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


def _cleanup_created(instance_ids: list[UUID]) -> None:
    api = docker.APIClient()
    try:
        for instance_id in instance_ids:
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


def _seed_exact_curated_runtimes(
    factory: Any, instances: dict[str, UUID], runtimes: tuple[dict[str, object], ...]
) -> None:
    timestamp = "2026-09-19T12:00:00.000000Z"
    _require(
        len(runtimes) == len(CURATED_SUPPRESSION_REVISIONS),
        "curated runtime inventory differs",
    )
    with factory.begin() as session:
        for index, (runtime, lineage) in enumerate(
            zip(runtimes, CURATED_SUPPRESSION_REVISIONS, strict=True), start=1
        ):
            release = str(runtime["release"])
            _require(release == lineage.canonical_release, "curated lineage differs")
            slug_release = release.replace(".", "-").replace("beta-", "beta")
            session.add(
                RuntimeDefinitionRow(
                    id=str(lineage.runtime_definition_id),
                    slug=f"screenshot-{slug_release}",
                    kodi_version=release,
                    support_tier=runtime["support"],
                )
            )
            session.flush()
            session.add(
                RuntimeRevisionRow(
                    id=str(lineage.suppression_revision_id),
                    runtime_definition_id=str(lineage.runtime_definition_id),
                    revision_number=lineage.suppression_revision_number,
                    source_kind="curated-validation",
                    source_reference="repository-manifest",
                    content_digest="sha256:" + f"{index}" * 64,
                    manifest_reference=f"runtime/{release}/manifest.json",
                    manifest_digest="sha256:" + f"{index + 4}" * 64,
                    built_at=timestamp,
                    approval_state="approved",
                    approved_at=timestamp,
                    requires_trusted_build_confirmation=False,
                )
            )
            session.flush()
            session.add(
                RuntimeContainerArtifactRow(
                    runtime_revision_id=str(lineage.suppression_revision_id),
                    image_id=lineage.image_id,
                )
            )
            expected_version = release.split("-")[0]
            session.add(
                RuntimeExecutionProfileRow(
                    runtime_revision_id=str(lineage.suppression_revision_id),
                    runtime_kind="kodi",
                    runtime_uid=1000,
                    runtime_gid=1000,
                    python_bytecode_policy="suppress",
                    expected_application_name="Kodi",
                    expected_kodi_version=expected_version,
                    expected_version_tag=runtime["tag"],
                    expected_prerelease_ordinal=runtime["ordinal"],
                    jsonrpc_port=8080,
                    jsonrpc_path="/jsonrpc",
                    process_observation="docker_healthcheck",
                    readiness_probe="kodi_jsonrpc",
                    readiness_timeout_seconds=90,
                    poll_interval_seconds=1.0,
                    http_timeout_seconds=3.0,
                )
            )
            instance_id = instances[release]
            session.add(
                TargetRow(
                    id=str(instance_id),
                    name=f"Screenshot Kodi {release}",
                    target_type="managed",
                    kodi_version=release,
                    availability="available",
                    health="healthy",
                    connection_state="connected",
                )
            )
            session.flush()
            session.add(
                ManagedInstanceRow(
                    target_id=str(instance_id),
                    instance_type="disposable",
                    runtime_revision_id=str(lineage.suppression_revision_id),
                    runtime_state="stopped",
                    start_on_host_boot=False,
                )
            )


class InspectingScreenshotProvider:
    def __init__(self) -> None:
        self._delegate = DockerManagedScreenshotCaptureProvider()
        self.fail_next = False
        self.last_failure_class: str | None = None
        self.last_layout: X11FrameLayout | None = None

    def capture_managed_kodi_screenshot(
        self, *, spec: ManagedScreenshotCaptureSpec, expected_container_id: str
    ) -> Any:
        if self.fail_next:
            self.fail_next = False
            raise ScreenshotCaptureError("injected fixed-backend failure")
        try:
            result = self._delegate.capture_managed_kodi_screenshot(
                spec=spec, expected_container_id=expected_container_id
            )
            self.last_layout = inspect_x11_frame(spec.artifact_path.read_bytes())
            return result
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
    provider: InspectingScreenshotProvider,
) -> tuple[dict[str, Any], bytes, X11FrameLayout]:
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
    _require(image.headers.get("content-type") == "image/png", "PNG MIME missing")
    _require(image.headers.get("cache-control") == "no-store", "no-store missing")
    _require(
        image.headers.get("x-content-type-options") == "nosniff", "nosniff missing"
    )
    validated = validate_png(image.content)
    _require(validated.width == 1280 and validated.height == 720, "geometry differs")
    statistics = png_pixel_statistics(image.content)
    _require(
        statistics.has_pixel_variation and statistics.rgb_max > statistics.rgb_min,
        "captured framebuffer is not plausibly initialized",
    )
    _require(provider.last_layout is not None, "X11 layout was not inspected")
    assert provider.last_layout is not None
    return document, image.content, provider.last_layout


def _validate_runtime(
    *,
    client: TestClient,
    headers: dict[str, str],
    factory: Any,
    data_root: Path,
    release: str,
    instance_id: UUID,
    provider: InspectingScreenshotProvider,
) -> dict[str, Any]:
    provisioned = client.post(
        f"/api/instances/{instance_id}/provision", json={}, headers=headers
    )
    _require(provisioned.status_code == 200, f"{release} provision failed")
    started = client.post(
        f"/api/instances/{instance_id}/start", json={}, headers=headers
    )
    _require(started.status_code == 200, f"{release} start failed")
    visual = _wait_visual_ready(client, instance_id)
    _require(
        visual["screenshot_supported"] and visual["screenshot_available_now"],
        f"{release} screenshot capability unavailable",
    )
    detail = client.get(f"/instances/{instance_id}")
    _require(
        detail.status_code == 200
        and "Refresh screenshot" in detail.text
        and "Browser remote" not in detail.text
        and "noVNC" not in detail.text,
        f"{release} instance-detail boundary differs",
    )

    first, first_png, first_layout = _capture(client, headers, instance_id, provider)
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
    _require(first_path.is_file(), f"{release} first private cache missing")

    second, second_png, second_layout = _capture(client, headers, instance_id, provider)
    second_status = second["screenshot"]
    second_capture = second["capture_id"]
    second_hash = hashlib.sha256(second_png).hexdigest()
    _require(second_capture != first_capture, f"{release} capture UUID was reused")
    _require(not first_path.exists(), f"{release} superseded PNG was retained")
    _require(
        first_layout == second_layout,
        f"{release} X11 layout changed between captures",
    )

    failure_retention = False
    if release == FAILURE_RETENTION_RELEASE:
        provider.fail_next = True
        failed = client.post(
            f"/api/instances/{instance_id}/screenshots", json={}, headers=headers
        )
        _require(failed.status_code == 503, "injected failure was accepted")
        retained = client.get(f"/api/instances/{instance_id}/visual-status").json()
        retained_image = client.get(f"/api/instances/{instance_id}/screenshots/latest")
        _require(
            retained["capture_id"] == second_capture
            and retained["captured_at"] == second_status["captured_at"]
            and retained["last_error_code"] == "screenshot_capture_unavailable"
            and hashlib.sha256(retained_image.content).hexdigest() == second_hash,
            "failed refresh changed the current screenshot",
        )
        failure_retention = True

    queued = client.post(
        f"/api/instances/{instance_id}/delete",
        json={"confirmed": True, "preserve_snapshots": True},
        headers=headers,
    )
    _require(queued.status_code == 202, f"{release} delete was rejected")
    deleted = _wait_delete(client, queued.json()["operation_id"])
    _require(deleted["state"] == "succeeded", f"{release} delete failed")
    with factory() as session:
        screenshot_row = session.get(ManagedScreenshotRow, str(instance_id))
    _require(screenshot_row is None, f"{release} screenshot metadata survived delete")
    _require(
        not (data_root / "screenshots" / "instances" / str(instance_id)).exists(),
        f"{release} screenshot bytes survived delete",
    )
    statistics = png_pixel_statistics(second_png)
    return {
        "runtime": RUNTIME_NAMES[release],
        "canonical_release": release,
        "instance_id": str(instance_id),
        "visual_supported": True,
        "visual_available": True,
        "x11_layout": second_layout.classification,
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
        "pixel_plausibility": {
            "has_pixel_variation": statistics.has_pixel_variation,
            "rgb_min": statistics.rgb_min,
            "rgb_max": statistics.rgb_max,
        },
        "replacement_removed_prior": True,
        "authenticated_latest_delivery": True,
        "failure_retention_exercised": failure_retention,
        "instance_deletion_removed_cache": True,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--evidence-path",
        type=Path,
        default=Path("evidence/kodi-lab-phase10-slice2-evidence.json"),
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
    instance_ids = list(instances.values())
    evidence: dict[str, Any] = {}
    engine = None
    try:
        with tempfile.TemporaryDirectory(
            prefix="kodi-lab-phase10-slice2-", dir="/var/tmp"
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
            _seed_exact_curated_runtimes(factory, instances, helper.RUNTIMES)
            adapter = DockerManagedContainerAdapter()
            screenshot_provider = InspectingScreenshotProvider()
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
            runtime_evidence: list[dict[str, Any]] = []
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
                    for runtime in helper.RUNTIMES:
                        release = str(runtime["release"])
                        runtime_evidence.append(
                            _validate_runtime(
                                client=client,
                                headers=headers,
                                factory=factory,
                                data_root=data_root,
                                release=release,
                                instance_id=instances[release],
                                provider=screenshot_provider,
                            )
                        )
                with factory() as session:
                    migration_head = session.scalar(
                        text("SELECT version_num FROM alembic_version")
                    )
            evidence = {
                "schema": "kodi-lab-phase10-slice2-live-v1",
                "migration_head": migration_head,
                "runtimes": runtime_evidence,
                "common_abstraction": "ManagedScreenshotService",
                "freshness": {
                    "independent_repeated_captures": True,
                    "screen_change_exercised": False,
                    "reason": (
                        "No preexisting permitted deterministic UI mutation exists; "
                        "Slice 2 did not add remote-control authority."
                    ),
                },
            }
            engine.dispose()
            engine = None
    finally:
        _cleanup_created(instance_ids)
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
