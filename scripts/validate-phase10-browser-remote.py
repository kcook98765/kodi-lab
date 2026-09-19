#!/usr/bin/env python3
"""Bounded live browser-remote proof across the curated Kodi runtime matrix."""

# ruff: noqa: E402 -- bytecode suppression precedes project imports.

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import UUID, uuid4

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
FIXED_ACTIONS = (
    "play_pause",
    "stop",
    "previous",
    "next",
    "volume_up",
    "volume_down",
    "mute_toggle",
)
RUNTIME_NAMES = {
    "19.5": "Kodi 19.5 Legacy",
    "20.5": "Kodi 20.5 Current",
    "21.3": "Kodi 21.3 Current",
    "22.0-beta.2": "Kodi 22 Beta 2 Preview",
}


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


def _validate_runtime(
    *,
    parity: ModuleType,
    client: TestClient,
    headers: dict[str, str],
    instance_id: str,
    release: str,
) -> dict[str, Any]:
    provisioned = client.post(
        f"/api/instances/{instance_id}/provision", json={}, headers=headers
    )
    _require(provisioned.status_code == 200, f"{release} provision failed")
    started = client.post(
        f"/api/instances/{instance_id}/start", json={}, headers=headers
    )
    _require(started.status_code == 200, f"{release} start failed")
    parity._wait_visual_ready(client, UUID(instance_id))
    visual = client.get(f"/api/instances/{instance_id}/visual-status")
    _require(visual.status_code == 200, f"{release} visual status failed")
    parity_status = visual.json()
    detail = client.get(f"/instances/{instance_id}")
    _require(
        detail.status_code == 200
        and "Browser remote" in detail.text
        and "noVNC" not in detail.text
        and "/jsonrpc" not in detail.text
        and ":8080" not in detail.text,
        f"{release} remote UI boundary differs",
    )
    direct = client.post(
        "/jsonrpc",
        json={"jsonrpc": "2.0", "id": 1, "method": "Input.Up"},
        headers=headers,
    )
    _require(direct.status_code == 404, f"{release} exposed direct JSON-RPC")

    baseline = client.post(
        f"/api/instances/{instance_id}/screenshots", json={}, headers=headers
    )
    _require(baseline.status_code == 200, f"{release} baseline capture failed")
    baseline_doc = baseline.json()
    _, baseline_hash = _latest(client, instance_id)

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
            f"{release} navigation {action} failed: {response.text[:256]}",
        )
        document = response.json()
        refresh = document["screenshot_refresh"]
        _require(
            document["result"] == "succeeded"
            and refresh["result"] == "succeeded"
            and refresh["capture_id"] != latest_capture
            and refresh["captured_at"] > latest_time,
            f"{release} navigation {action} did not produce a fresh capture",
        )
        _, current_hash = _latest(client, instance_id)
        hash_changed = current_hash != latest_hash
        changed = changed or hash_changed
        invocations.append(
            {
                "invocation_id": document["invocation_id"],
                "action": action,
                "result": document["result"],
                "completed_at": document["completed_at"],
                "screenshot_refresh": refresh["result"],
                "capture_id": refresh["capture_id"],
                "captured_at": refresh["captured_at"],
                "png_sha256": current_hash,
                "png_hash_changed": hash_changed,
            }
        )
        latest_hash = current_hash
        latest_capture = refresh["capture_id"]
        latest_time = refresh["captured_at"]
    _require(changed, f"{release} navigation never visibly changed the framebuffer")

    fixed_results: list[dict[str, str]] = []
    for action in FIXED_ACTIONS:
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
                response.status_code == 409
                and classification == "remote_no_active_player",
                f"{release} returned unexpected {action} classification",
            )
        else:
            _require(
                response.status_code == 200 and classification == "succeeded",
                f"{release} returned unexpected {action} classification",
            )
        fixed_results.append({"action": action, "result": classification})

    queued = client.post(
        f"/api/instances/{instance_id}/delete",
        json={"confirmed": True, "preserve_snapshots": True},
        headers=headers,
    )
    _require(queued.status_code == 202, f"{release} cleanup queue failed")
    deleted = parity._wait_delete(client, queued.json()["operation_id"])
    _require(deleted["state"] == "succeeded", f"{release} cleanup failed")
    return {
        "runtime": RUNTIME_NAMES[release],
        "canonical_release": release,
        "ready_usable": (
            parity_status["screenshot_supported"]
            and parity_status["screenshot_available_now"]
        ),
        "baseline": {
            "capture_id": baseline_doc["capture_id"],
            "captured_at": baseline_doc["screenshot"]["captured_at"],
            "png_sha256": baseline_hash,
        },
        "navigation": invocations,
        "visible_change_observed": changed,
        "fixed_action_results": fixed_results,
        "active_players": {
            "observed_shape": "empty_list",
            "selection": "server_derived_only",
            "classification": "remote_no_active_player",
        },
        "direct_browser_kodi_endpoint": False,
        "cleanup": "succeeded",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--evidence-path",
        type=Path,
        default=Path("evidence/kodi-lab-phase10-browser-remote-parity-evidence.json"),
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
                    runtime_evidence = [
                        _validate_runtime(
                            parity=parity,
                            client=client,
                            headers=headers,
                            instance_id=str(instances[str(runtime["release"])]),
                            release=str(runtime["release"]),
                        )
                        for runtime in helper.RUNTIMES
                    ]

            evidence = {
                "schema": "kodi-lab-phase10-browser-remote-parity-live-v1",
                "runtimes": runtime_evidence,
                "mapping": "common_fixed_server_side",
                "screenshot_refreshes_per_navigation": 1,
                "interactive_display_implemented": False,
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
