#!/usr/bin/env python3
"""Bounded live Kodi 21.3 proof for cross-protocol MCP remote control."""

# ruff: noqa: E402 -- bytecode suppression precedes project imports.

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import os
import socket
import sys
import tempfile
import threading
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from types import ModuleType
from typing import Any, cast
from uuid import UUID, uuid4

sys.dont_write_bytecode = True

import httpx2
import uvicorn
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from starlette.types import ASGIApp

from kodi_lab.application.mcp_administration import (
    FULL_LAB_CONTROL_PRESET_ID,
    McpAdministrationService,
)
from kodi_lab.auth import WebAuthenticationService
from kodi_lab.broker.docker_inventory import DockerManagedContainerAdapter
from kodi_lab.broker.operations import (
    ObserveManagedKodiRuntimeResult,
    build_foundation_registry,
)
from kodi_lab.broker.provisioning import (
    SqliteManagedInstanceRevisionResolver,
    SqliteProvisioningResolver,
    SqliteRuntimeObservationResolver,
)
from kodi_lab.broker.screenshots import SqliteScreenshotCaptureResolver
from kodi_lab.config import Settings
from kodi_lab.domain import RuntimeExecutionProfile
from kodi_lab.kodi.remote import BrowserRemoteAction, KodiBrowserRemoteAdapter
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.web.app import create_app

PASSWORD = "phase11-mcp-remote-live-password"
ORIGIN = "http://127.0.0.1"


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


class _CountingAdapter:
    def __init__(self, delegate: KodiBrowserRemoteAdapter, actions: list[str]) -> None:
        self._delegate = delegate
        self._actions = actions

    def dispatch(self, action: BrowserRemoteAction, invocation_id: UUID) -> None:
        self._actions.append(action.value)
        self._delegate.dispatch(action, invocation_id)


class _CountingAdapterFactory:
    def __init__(self) -> None:
        self.actions: list[str] = []

    def __call__(
        self,
        observation: ObserveManagedKodiRuntimeResult,
        profile: RuntimeExecutionProfile,
    ) -> _CountingAdapter:
        return _CountingAdapter(
            KodiBrowserRemoteAdapter(observation, profile), self.actions
        )


@contextmanager
def _live_loopback_server(app: ASGIApp) -> Iterator[str]:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = cast(tuple[str, int], listener.getsockname())[1]
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            log_config=None,
            access_log=False,
            lifespan="off",
            timeout_graceful_shutdown=1,
        )
    )
    thread = threading.Thread(
        target=server.run, kwargs={"sockets": [listener]}, daemon=False
    )
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started:
        if not thread.is_alive() or time.monotonic() >= deadline:
            raise RuntimeError("loopback MCP server did not start")
        time.sleep(0.01)
    try:
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
        _require(not thread.is_alive(), "loopback MCP server did not stop")


@asynccontextmanager
async def _direct_client(endpoint: str, token: str) -> AsyncIterator[Client]:
    async with httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {token}"}, follow_redirects=False
    ) as http:
        streams = streamable_http_client(
            endpoint, http_client=http, terminate_on_close=False
        )
        async with Client(streams, mode="2026-07-28", cache=None) as client:
            yield client


@asynccontextmanager
async def _legacy_client(endpoint: str, token: str) -> AsyncIterator[Client]:
    async with httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {token}"}, follow_redirects=False
    ) as http:
        streams = streamable_http_client(
            endpoint, http_client=http, terminate_on_close=False
        )
        async with Client(streams, mode="legacy", cache=None) as client:
            yield client


def _latest_hash(client: TestClient, instance_id: UUID) -> str:
    response = client.get(f"/api/instances/{instance_id}/screenshots/latest")
    _require(response.status_code == 200, "latest screenshot unavailable")
    return hashlib.sha256(response.content).hexdigest()


async def _exercise_transports(
    *,
    endpoint: str,
    token: str,
    target_id: UUID,
    dispatches: list[str],
) -> dict[str, Any]:
    async with (
        _direct_client(endpoint, token) as modern,
        _legacy_client(endpoint, token) as legacy,
    ):
        _require(modern.protocol_version == "2026-07-28", "modern revision changed")
        _require(legacy.protocol_version == "2025-11-25", "legacy revision changed")
        modern_tools = (await modern.list_tools()).tools
        legacy_tools = (await legacy.list_tools()).tools
        _require(
            [item.model_dump() for item in modern_tools]
            == [item.model_dump() for item in legacy_tools],
            "cross-protocol tool schemas changed",
        )
        tool_names = [item.name for item in modern_tools]
        _require(
            tool_names
            == [
                "addon_list",
                "remote_action",
                "screenshot_capture",
                "target_list",
                "target_status",
            ],
            "unexpected control-client catalog",
        )

        modern_request_id = str(uuid4())
        modern_arguments = {
            "request_id": modern_request_id,
            "target_id": str(target_id),
            "action": "down",
        }
        modern_first = await modern.call_tool("remote_action", modern_arguments)
        _require(not modern_first.is_error, "modern navigation action failed")
        _require(
            cast(dict[str, Any], modern_first.structured_content)["screenshot_refresh"][
                "result"
            ]
            == "succeeded",
            "modern navigation did not refresh the durable screenshot",
        )
        after_modern_count = len(dispatches)
        legacy_replay = await legacy.call_tool("remote_action", modern_arguments)
        _require(not legacy_replay.is_error, "legacy replay failed")
        _require(
            modern_first.structured_content == legacy_replay.structured_content,
            "modern to legacy replay result changed",
        )
        _require(
            len(dispatches) == after_modern_count == 1,
            "modern to legacy replay dispatched more than once",
        )

        legacy_request_id = str(uuid4())
        legacy_arguments = {
            "request_id": legacy_request_id,
            "target_id": str(target_id),
            "action": "up",
        }
        legacy_first = await legacy.call_tool("remote_action", legacy_arguments)
        _require(not legacy_first.is_error, "legacy navigation action failed")
        _require(
            cast(dict[str, Any], legacy_first.structured_content)["screenshot_refresh"][
                "result"
            ]
            == "succeeded",
            "legacy navigation did not refresh the durable screenshot",
        )
        after_legacy_count = len(dispatches)
        modern_replay = await modern.call_tool("remote_action", legacy_arguments)
        _require(not modern_replay.is_error, "modern replay failed")
        _require(
            legacy_first.structured_content == modern_replay.structured_content,
            "legacy to modern replay result changed",
        )
        _require(
            len(dispatches) == after_legacy_count == 2,
            "legacy to modern replay dispatched more than once",
        )

        return {
            "tool_catalog": tool_names,
            "protocol_revisions": ["2026-07-28", "2025-11-25"],
            "modern_first_legacy_replay": {
                "request_id": modern_request_id,
                "invocation_id": cast(
                    dict[str, object], modern_first.structured_content
                )["invocation_id"],
                "action": "down",
                "dispatch_count_after_pair": after_modern_count,
                "stored_result_equal": True,
            },
            "legacy_first_modern_replay": {
                "request_id": legacy_request_id,
                "invocation_id": cast(
                    dict[str, object], legacy_first.structured_content
                )["invocation_id"],
                "action": "up",
                "dispatch_count_after_pair": after_legacy_count,
                "stored_result_equal": True,
            },
        }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--evidence-path",
        type=Path,
        default=Path("evidence/kodi-lab-phase11-compatibility-evidence.json"),
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
    target_ids = {str(item["release"]): uuid4() for item in helper.RUNTIMES}
    target_id = target_ids["21.3"]
    engine = None
    evidence: dict[str, Any] = {}
    try:
        with tempfile.TemporaryDirectory(
            prefix="kodi-lab-phase11-mcp-compatibility-", dir="/var/tmp"
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
            parity._seed_exact_curated_runtimes(factory, target_ids, helper.RUNTIMES)
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
            counting = _CountingAdapterFactory()
            with helper._broker_server(root, registry) as broker:
                auth = WebAuthenticationService(factory)
                auth.set_password(PASSWORD)
                app = create_app(
                    settings=settings,
                    session_factory=factory,
                    broker=broker,
                    auth=auth,
                    browser_remote_adapter_factory=counting,
                )
                with TestClient(app, base_url=ORIGIN) as web:
                    headers = helper._login(web)
                    _require(
                        web.post(
                            f"/api/instances/{target_id}/provision",
                            json={},
                            headers=headers,
                        ).status_code
                        == 200,
                        "provision failed",
                    )
                    _require(
                        web.post(
                            f"/api/instances/{target_id}/start",
                            json={},
                            headers=headers,
                        ).status_code
                        == 200,
                        "start failed",
                    )
                    parity._wait_visual_ready(web, target_id)
                    baseline = web.post(
                        f"/api/instances/{target_id}/screenshots",
                        json={},
                        headers=headers,
                    )
                    _require(baseline.status_code == 200, "baseline capture failed")
                    baseline_hash = _latest_hash(web, target_id)
                    _require(
                        web.post(
                            "/jsonrpc",
                            json={
                                "jsonrpc": "2.0",
                                "id": 1,
                                "method": "Input.Down",
                            },
                            headers=headers,
                        ).status_code
                        == 404,
                        "raw JSON-RPC became reachable",
                    )

                    administration = cast(
                        McpAdministrationService,
                        app.state.kodi_lab.mcp_administration,
                    )
                    mcp_client = administration.create_client(
                        display_name="Phase 11 live control",
                        preset_id=FULL_LAB_CONTROL_PRESET_ID,
                    )
                    administration.assign_target_preset(
                        target_id, FULL_LAB_CONTROL_PRESET_ID
                    )
                    administration.grant_target(mcp_client.id, target_id)
                    issued = administration.issue_credential(mcp_client.id)
                    with _live_loopback_server(app) as endpoint:
                        transport = asyncio.run(
                            _exercise_transports(
                                endpoint=endpoint,
                                token=issued.token,
                                target_id=target_id,
                                dispatches=counting.actions,
                            )
                        )
                        navigation_hash = _latest_hash(web, target_id)
                        _require(
                            navigation_hash != baseline_hash,
                            "navigation did not change the visual hash",
                        )
                        administration.revoke_credential(
                            mcp_client.id, issued.credential.id
                        )

                        async def denied_after_revoke(revision: str) -> bool:
                            async with httpx2.AsyncClient(
                                headers={
                                    "Authorization": f"Bearer {issued.token}",
                                    "Accept": "application/json, text/event-stream",
                                    "Content-Type": "application/json",
                                    "MCP-Protocol-Version": revision,
                                    "Mcp-Method": "tools/call",
                                    "Mcp-Name": "remote_action",
                                },
                                follow_redirects=False,
                            ) as http:
                                response = await http.post(
                                    endpoint,
                                    json={
                                        "jsonrpc": "2.0",
                                        "id": 99,
                                        "method": "tools/call",
                                        "params": {
                                            "name": "remote_action",
                                            "arguments": {
                                                "request_id": str(uuid4()),
                                                "target_id": str(target_id),
                                                "action": "left",
                                            },
                                            **(
                                                {
                                                    "_meta": {
                                                        "io.modelcontextprotocol/"
                                                        "protocolVersion": revision,
                                                        "io.modelcontextprotocol/"
                                                        "clientCapabilities": {},
                                                        "io.modelcontextprotocol/"
                                                        "clientInfo": {
                                                            "name": "live-proof",
                                                            "version": "1",
                                                        },
                                                    }
                                                }
                                                if revision == "2026-07-28"
                                                else {}
                                            ),
                                        },
                                    },
                                )
                            return response.status_code == 401

                        _require(
                            asyncio.run(denied_after_revoke("2026-07-28")),
                            "revoked credential retained modern authority",
                        )
                        _require(
                            asyncio.run(denied_after_revoke("2025-11-25")),
                            "revoked credential retained legacy authority",
                        )
                    administration.remove_target_grant(mcp_client.id, target_id)
                    queued = web.post(
                        f"/api/instances/{target_id}/delete",
                        json={"confirmed": True, "preserve_snapshots": True},
                        headers=headers,
                    )
                    _require(queued.status_code == 202, "cleanup queue failed")
                    deleted = parity._wait_delete(web, queued.json()["operation_id"])
                    _require(deleted["state"] == "succeeded", "cleanup failed")

            evidence = {
                "schema": "kodi-lab-phase11-compatibility-live-v1",
                "runtime": "Kodi 21.3 Current",
                "target_id": str(target_id),
                "protocol_replay": transport,
                "adapter_actions": counting.actions,
                "adapter_dispatch_count": len(counting.actions),
                "navigation_refresh": {
                    "baseline_capture_id": baseline.json()["capture_id"],
                    "baseline_sha256": baseline_hash,
                    "final_sha256": navigation_hash,
                    "visual_hash_changed": True,
                    "refreshes_per_new_navigation": 1,
                    "refreshes_per_duplicate": 0,
                },
                "revocation": {
                    "modern_denied_next_request": True,
                    "legacy_denied_next_request": True,
                },
                "raw_jsonrpc_exposed": False,
            }
            engine.dispose()
            engine = None
    finally:
        parity._cleanup_created(list(target_ids.values()))
        if engine is not None:
            engine.dispose()

    _require(parity._managed_network_count() == 0, "managed network was not cleaned")
    protected_after = helper._protected_fingerprint()
    changed = sorted(
        key
        for key in protected_before
        if protected_before[key] != protected_after.get(key)
    )
    _require(not changed, f"protected categories changed: {changed!r}")
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
