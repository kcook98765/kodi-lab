#!/usr/bin/env python3
"""Bounded isolated Kodi 21.3 proof for mediated live display."""

# ruff: noqa: E402 -- bytecode suppression precedes project imports.

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import resource
import sys
import tempfile
import time
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import UUID, uuid4

sys.dont_write_bytecode = True

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from kodi_lab.auth import WebAuthenticationService
from kodi_lab.broker.docker_inventory import DockerManagedContainerAdapter
from kodi_lab.broker.operations import build_foundation_registry
from kodi_lab.broker.provisioning import (
    SqliteManagedInstanceRevisionResolver,
    SqliteProvisioningResolver,
    SqliteRuntimeObservationResolver,
)
from kodi_lab.broker.screenshots import (
    DockerManagedScreenshotCaptureProvider,
    ManagedScreenshotCaptureSpec,
    SqliteScreenshotCaptureResolver,
)
from kodi_lab.config import Settings
from kodi_lab.kodi.screenshots import validate_png
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.storage.models import ManagedScreenshotRow
from kodi_lab.web.app import create_app

PASSWORD = "phase10-slice4-live-password"
ORIGIN = "http://127.0.0.1"
RELEASE = "21.3"
REVISION_ID = "3a000000-0000-4000-8000-000000000006"


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


class TimedScreenshotProvider:
    def __init__(self) -> None:
        self._delegate = DockerManagedScreenshotCaptureProvider()
        self.durations: list[float] = []

    def capture_managed_kodi_screenshot(
        self, *, spec: ManagedScreenshotCaptureSpec, expected_container_id: str
    ) -> Any:
        started = time.monotonic()
        try:
            return self._delegate.capture_managed_kodi_screenshot(
                spec=spec, expected_container_id=expected_container_id
            )
        finally:
            self.durations.append(time.monotonic() - started)


def _websocket_headers(client: TestClient) -> dict[str, str]:
    token = client.cookies.get("kodi_lab_session")
    _require(token is not None, "authenticated cookie missing")
    return {
        "Origin": ORIGIN,
        "Host": "127.0.0.1",
        "Cookie": f"kodi_lab_session={token}",
    }


def _cached_capture(factory: Any, instance_id: UUID) -> str | None:
    with factory() as session:
        row = session.get(ManagedScreenshotRow, str(instance_id))
        return None if row is None else row.capture_id


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--evidence-path",
        type=Path,
        default=Path("evidence/kodi-lab-phase10-slice4-evidence.json"),
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
    instance_ids = list(instances.values())
    engine = None
    evidence: dict[str, Any] = {}
    try:
        with tempfile.TemporaryDirectory(
            prefix="kodi-lab-phase10-slice4-", dir="/var/tmp"
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
            provider = TimedScreenshotProvider()
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
                screenshot_capture_provider=provider,
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
                    visual = parity._wait_visual_ready(client, instance_id)
                    _require(
                        visual["display_supported"] is True
                        and visual["display_available_now"] is True
                        and visual["display_backend"] == "live_screenshot_stream",
                        "display projection differs",
                    )
                    durable = client.post(
                        f"/api/instances/{instance_id}/screenshots",
                        json={},
                        headers=headers,
                    )
                    _require(durable.status_code == 200, "baseline capture failed")
                    baseline_capture = durable.json()["capture_id"]
                    cpu_before = resource.getrusage(resource.RUSAGE_SELF)
                    opened_at = datetime.now(UTC)
                    hashes: list[str] = []
                    frame_times: list[float] = []
                    frame_bytes: list[int] = []
                    action_used = ""
                    hash_changed = False
                    websocket_headers = _websocket_headers(client)
                    with client.websocket_connect(
                        f"/api/instances/{instance_id}/display",
                        headers=websocket_headers,
                    ) as websocket:
                        display_service = app.state.kodi_lab.live_display
                        display_session_id = display_service.active_session_id(
                            instance_id
                        )
                        _require(display_session_id is not None, "session UUID missing")
                        for _ in range(3):
                            frame = websocket.receive_bytes()
                            validated = validate_png(frame)
                            _require(
                                validated.width == 1280 and validated.height == 720,
                                "live frame dimensions differ",
                            )
                            frame_times.append(time.monotonic())
                            frame_bytes.append(len(frame))
                            hashes.append(hashlib.sha256(frame).hexdigest())
                        _require(
                            _cached_capture(factory, instance_id) == baseline_capture,
                            "stream frames changed durable cache",
                        )
                        try:
                            with client.websocket_connect(
                                f"/api/instances/{instance_id}/display",
                                headers=websocket_headers,
                            ):
                                raise RuntimeError("second same-instance stream opened")
                        except WebSocketDisconnect as error:
                            _require(
                                error.reason == "display_conflict",
                                "same-instance conflict classification differs",
                            )
                        for action in ("down", "right", "left", "up"):
                            remote = client.post(
                                f"/api/instances/{instance_id}/remote-actions",
                                json={"action": action},
                                headers=headers,
                            )
                            _require(remote.status_code == 200, "remote action failed")
                            _require(
                                remote.json()["screenshot_refresh"]["result"]
                                == "succeeded",
                                "remote screenshot refresh failed",
                            )
                            try:
                                changed_frame = websocket.receive_bytes()
                            except WebSocketDisconnect as error:
                                raise RuntimeError(
                                    "display closed during remote proof: "
                                    f"{error.reason}"
                                ) from error
                            validate_png(changed_frame)
                            changed_hash = hashlib.sha256(changed_frame).hexdigest()
                            frame_times.append(time.monotonic())
                            frame_bytes.append(len(changed_frame))
                            hashes.append(changed_hash)
                            action_used = action
                            if changed_hash != hashes[0]:
                                hash_changed = True
                                break
                        _require(
                            hash_changed,
                            "remote navigation did not change live frame",
                        )
                        post_remote_capture = _cached_capture(factory, instance_id)
                        _require(
                            post_remote_capture is not None
                            and post_remote_capture != baseline_capture,
                            "remote durable refresh was not distinct",
                        )
                        later = websocket.receive_bytes()
                        validate_png(later)
                        frame_times.append(time.monotonic())
                        frame_bytes.append(len(later))
                        hashes.append(hashlib.sha256(later).hexdigest())
                        current_cached = _cached_capture(factory, instance_id)
                        _require(
                            current_cached == post_remote_capture,
                            "later stream frame changed durable cache",
                        )
                    closed_at = datetime.now(UTC)

                    with client.websocket_connect(
                        f"/api/instances/{instance_id}/display",
                        headers=websocket_headers,
                    ) as violating:
                        violating.receive_bytes()
                        violating.send_text('{"key":"left"}')
                        try:
                            violating.receive_bytes()
                        except WebSocketDisconnect as error:
                            _require(
                                error.reason == "display_unavailable",
                                "client-message close classification differs",
                            )
                        else:
                            raise RuntimeError("client data was accepted")

                    with client.websocket_connect(
                        f"/api/instances/{instance_id}/display",
                        headers=websocket_headers,
                    ) as lifecycle_stream:
                        lifecycle_stream.receive_bytes()
                        stopped = client.post(
                            f"/api/instances/{instance_id}/stop",
                            json={},
                            headers=headers,
                        )
                        _require(stopped.status_code == 200, "safe stop failed")
                        try:
                            lifecycle_stream.receive_bytes()
                        except WebSocketDisconnect as error:
                            _require(
                                error.reason
                                in {
                                    "display_unavailable",
                                    "display_instance_not_running",
                                },
                                "lifecycle close classification differs",
                            )
                        else:
                            raise RuntimeError("lifecycle did not cancel display")
                    restarted = client.post(
                        f"/api/instances/{instance_id}/start",
                        json={},
                        headers=headers,
                    )
                    _require(restarted.status_code == 200, "safe restart failed")
                    parity._wait_visual_ready(client, instance_id)
                    with client.websocket_connect(
                        f"/api/instances/{instance_id}/display",
                        headers=websocket_headers,
                    ) as final_stream:
                        final_stream.receive_bytes()
                        final_stream.close()

                    deleted = client.post(
                        f"/api/instances/{instance_id}/delete",
                        json={"confirmed": True, "preserve_snapshots": True},
                        headers=headers,
                    )
                    _require(deleted.status_code == 202, "cleanup queue failed")
                    terminal = parity._wait_delete(
                        client, deleted.json()["operation_id"]
                    )
                    _require(terminal["state"] == "succeeded", "cleanup failed")
                    cpu_after = resource.getrusage(resource.RUSAGE_SELF)

            intervals = [later - earlier for earlier, later in pairwise(frame_times)]
            _require(all(interval >= 0.90 for interval in intervals), "rate exceeded")
            delivered_fps = (len(frame_times) - 1) / (frame_times[-1] - frame_times[0])
            staging = data_root / "screenshots" / "staging"
            instance_cache = data_root / "screenshots" / "instances" / str(instance_id)
            _require(
                (not staging.exists() or not any(staging.iterdir()))
                and not instance_cache.exists(),
                "frame/cache staging residue remained",
            )
            evidence = {
                "schema": "kodi-lab-phase10-slice4-live-v1",
                "runtime": {
                    "canonical_release": RELEASE,
                    "runtime_revision_id": REVISION_ID,
                    "profile": "Kodi 21.3 stable",
                },
                "display_session_id": str(display_session_id),
                "started_at": opened_at.isoformat(),
                "ended_at": closed_at.isoformat(),
                "frame_count": len(frame_times),
                "aggregate_bytes": sum(frame_bytes),
                "representative_frame_hashes": list(dict.fromkeys(hashes))[:4],
                "delivered_fps": round(delivered_fps, 4),
                "remote_action_proof": {
                    "transport": "existing_semantic_http_route",
                    "action": action_used,
                    "result": "succeeded",
                },
                "hash_change_proof": hash_changed,
                "session_close_reason": "display_stopped",
                "concurrency_result": "display_conflict",
                "client_message_result": "display_unavailable",
                "lifecycle_cancellation": "succeeded",
                "durable_cache_stream_churn": False,
                "performance": {
                    "average_capture_seconds": round(
                        sum(provider.durations) / len(provider.durations), 4
                    ),
                    "process_cpu_seconds": round(
                        (cpu_after.ru_utime + cpu_after.ru_stime)
                        - (cpu_before.ru_utime + cpu_before.ru_stime),
                        4,
                    ),
                },
                "cleanup_result": "succeeded_no_residue",
            }
            engine.dispose()
            engine = None
    finally:
        parity._cleanup_created(instance_ids)
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
