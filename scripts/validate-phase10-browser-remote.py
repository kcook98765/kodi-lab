#!/usr/bin/env python3
"""Bounded live Kodi 21.3 proof for Phase 10 Slice 3A browser remote."""

# ruff: noqa: E402 -- bytecode suppression precedes project imports.

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import uuid4

sys.dont_write_bytecode = True

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

from kodi_lab.auth import WebAuthenticationService
from kodi_lab.broker.docker_inventory import DockerManagedContainerAdapter
from kodi_lab.broker.operations import build_foundation_registry
from kodi_lab.broker.provisioning import (
    SqliteManagedInstanceRevisionResolver,
    SqliteProvisioningResolver,
    SqliteRuntimeObservationResolver,
)
from kodi_lab.broker.screenshots import SqliteScreenshotCaptureResolver
from kodi_lab.config import Settings
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.web.app import create_app

PASSWORD = "phase10-slice3a-live-password"
ORIGIN = "http://127.0.0.1"
RELEASE = "21.3"
NAVIGATION_ACTIONS = (
    "down",
    "up",
    "left",
    "right",
    "select",
    "back",
    "home",
    "context_menu",
    "info",
)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _load(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    _require(spec is not None and spec.loader is not None, f"{name} import failed")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _latest(client: TestClient, instance_id: str) -> tuple[bytes, str]:
    response = client.get(f"/api/instances/{instance_id}/screenshots/latest")
    _require(response.status_code == 200, "latest screenshot unavailable")
    return response.content, hashlib.sha256(response.content).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--evidence-path",
        type=Path,
        default=Path("<project-root>-phase10-slice3a-evidence.json"),
    )
    arguments = parser.parse_args()
    _require(os.geteuid() == 0, "live validation must run as root")
    project = Path(__file__).resolve().parents[1]
    parity = _load(
        project / "scripts" / "validate-phase10-screenshot-parity.py", "parity"
    )
    helper = parity._phase8_helpers(project)
    helper.__dict__["PASSWORD"] = PASSWORD
    helper.__dict__["ORIGIN"] = ORIGIN
    protected_before = helper._protected_fingerprint()
    _require(parity._managed_network_count() == 0, "managed network was not empty")
    _require(
        protected_before["production_data_root_absent"]
        and protected_before["production_runtime_root_absent"],
        "production roots were present",
    )
    instances = {str(item["release"]): uuid4() for item in helper.RUNTIMES}
    instance_id = instances[RELEASE]
    engine = None
    evidence: dict[str, Any] = {}
    try:
        with tempfile.TemporaryDirectory(
            prefix="kodi-lab-phase10-slice3a-", dir="/var/tmp"
        ) as temporary:
            root = Path(temporary)
            data_root = root / "data"
            (data_root / "database").mkdir(parents=True)
            runtime_root = root / "runtime"
            runtime_root.mkdir(mode=0o700)
            os.environ["KODI_LAB_DATA_ROOT"] = str(data_root)
            os.environ["KODI_LAB_RUNTIME_ROOT"] = str(runtime_root)
            settings = Settings()
            command.upgrade(Config(str(project / "alembic.ini")), "head")
            engine = create_database_engine(settings.database_url)
            factory = create_session_factory(engine)
            parity._seed_exact_curated_runtimes(factory, instances, helper.RUNTIMES)
            adapter = DockerManagedContainerAdapter()
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
                screenshot_capture_provider=(
                    parity.DockerManagedScreenshotCaptureProvider()
                ),
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
                    parity._wait_visual_ready(client, instance_id)
                    detail = client.get(f"/instances/{instance_id}")
                    _require(
                        detail.status_code == 200
                        and "Browser remote" in detail.text
                        and "noVNC" not in detail.text,
                        "remote UI boundary differs",
                    )
                    baseline = client.post(
                        f"/api/instances/{instance_id}/screenshots",
                        json={},
                        headers=headers,
                    )
                    _require(baseline.status_code == 200, "baseline capture failed")
                    baseline_doc = baseline.json()
                    _, baseline_hash = _latest(client, str(instance_id))

                    invocations: list[dict[str, Any]] = []
                    changed = False
                    latest_hash = baseline_hash
                    latest_capture = baseline_doc["capture_id"]
                    latest_time = baseline_doc["screenshot"]["captured_at"]
                    for action in NAVIGATION_ACTIONS:
                        response = client.post(
                            f"/api/instances/{instance_id}/remote-actions",
                            json={"action": action},
                            headers=headers,
                        )
                        _require(
                            response.status_code == 200,
                            f"navigation {action} failed: {response.text[:256]}",
                        )
                        document = response.json()
                        refresh = document["screenshot_refresh"]
                        _require(
                            document["result"] == "succeeded"
                            and refresh["result"] == "succeeded"
                            and refresh["capture_id"] != latest_capture,
                            f"navigation {action} did not refresh",
                        )
                        _, current_hash = _latest(client, str(instance_id))
                        changed = changed or current_hash != latest_hash
                        latest_hash = current_hash
                        latest_capture = refresh["capture_id"]
                        latest_time = refresh["captured_at"]
                        invocations.append(
                            {
                                "invocation_id": document["invocation_id"],
                                "action": action,
                                "result": document["result"],
                                "completed_at": document["completed_at"],
                                "screenshot_refresh": refresh["result"],
                                "capture_id": refresh["capture_id"],
                            }
                        )
                    _require(
                        changed, "navigation did not visibly change any screenshot"
                    )

                    playback: list[dict[str, str]] = []
                    for action in (
                        "play_pause",
                        "stop",
                        "previous",
                        "next",
                        "volume_up",
                        "volume_down",
                        "mute_toggle",
                    ):
                        response = client.post(
                            f"/api/instances/{instance_id}/remote-actions",
                            json={"action": action},
                            headers=headers,
                        )
                        document = response.json()
                        classification = (
                            document["result"]
                            if response.status_code == 200
                            else document["error"]["code"]
                        )
                        if action in {"play_pause", "stop", "previous", "next"}:
                            _require(
                                classification == "remote_no_active_player",
                                f"unexpected {action} classification",
                            )
                        else:
                            _require(
                                classification == "succeeded",
                                f"unexpected {action} classification",
                            )
                        playback.append({"action": action, "result": classification})

                    client.cookies.clear()
                    headers = helper._login(client)

                    def rate_request() -> Any:
                        return client.post(
                            f"/api/instances/{instance_id}/remote-actions",
                            json={"action": "volume_down"},
                            headers=headers,
                        )

                    with ThreadPoolExecutor(max_workers=100) as executor:
                        rate_responses = list(
                            executor.map(lambda _index: rate_request(), range(100))
                        )
                    limited = [
                        item for item in rate_responses if item.status_code == 429
                    ]
                    admitted = [
                        item for item in rate_responses if item.status_code != 429
                    ]
                    _require(
                        len(limited) > 0
                        and len(admitted) >= 20
                        and all(
                            item.json()["error"]["code"] == "visual_rate_limited"
                            for item in limited
                        ),
                        "rate limit was not enforced",
                    )
                    reconciliation = client.get(
                        f"/api/instances/{instance_id}/reconciliation"
                    ).json()
                    _require(not reconciliation["blocking"], "reconciliation drifted")
                    queued = client.post(
                        f"/api/instances/{instance_id}/delete",
                        json={"confirmed": True, "preserve_snapshots": True},
                        headers=headers,
                    )
                    _require(queued.status_code == 202, "cleanup queue failed")
                    deleted = parity._wait_delete(client, queued.json()["operation_id"])
                    _require(deleted["state"] == "succeeded", "cleanup failed")

            evidence = {
                "schema": "kodi-lab-phase10-slice3a-live-v1",
                "runtime": "Kodi 21.3 stable",
                "remote_invocations": invocations,
                "playback_volume_results": playback,
                "freshness": {
                    "baseline_capture_id": baseline_doc["capture_id"],
                    "baseline_captured_at": baseline_doc["screenshot"]["captured_at"],
                    "baseline_sha256": baseline_hash,
                    "refreshed_capture_id": latest_capture,
                    "refreshed_captured_at": latest_time,
                    "refreshed_sha256": latest_hash,
                    "screenshot_changed": changed,
                },
                "rate_limit": {
                    "configured_burst": 20,
                    "concurrent_requests_not_rate_limited": len(admitted),
                    "concurrent_requests_rate_limited": len(limited),
                    "classification": "visual_rate_limited",
                    "rejected_action_dispatched": False,
                },
                "reconciliation_blocking": False,
                "cleanup": "succeeded",
            }
            engine.dispose()
            engine = None
    finally:
        parity._cleanup_created(list(instances.values()))
        if engine is not None:
            engine.dispose()

    _require(parity._managed_network_count() == 0, "managed network was not cleaned")
    protected_after = helper._protected_fingerprint()
    changed_categories = sorted(
        key
        for key in protected_before
        if protected_before[key] != protected_after.get(key)
    )
    _require(
        not changed_categories,
        f"protected categories changed: {changed_categories!r}",
    )
    evidence["protected_isolation"] = {
        "protected_fingerprints_unchanged": True,
        "production_roots_absent": True,
        "managed_resources_cleaned": True,
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
