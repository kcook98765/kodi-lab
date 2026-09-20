#!/usr/bin/env python3
"""Run one isolated real-Kodi proof for Phase 12 workspace development."""

# ruff: noqa: E402 -- bytecode suppression must precede project imports.

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
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

sys.dont_write_bytecode = True

import docker  # type: ignore[import-untyped]
import uvicorn
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters
from sqlalchemy import func, select, text

from kodi_lab.application import (
    DevelopmentSourceAdministrationService,
    DevelopmentSourceAttachmentService,
)
from kodi_lab.application.mcp_administration import FULL_LAB_CONTROL_PRESET_ID
from kodi_lab.auth import WebAuthenticationService
from kodi_lab.broker.development_sources import (
    SqliteDevelopmentSourceDeploymentResolver,
)
from kodi_lab.broker.docker_inventory import DockerManagedContainerAdapter
from kodi_lab.broker.operations import build_foundation_registry
from kodi_lab.broker.provisioning import (
    SqliteManagedInstanceRevisionResolver,
    SqliteProvisioningResolver,
    SqliteRuntimeObservationResolver,
)
from kodi_lab.config import Settings
from kodi_lab.development import DevelopmentSourceValidator
from kodi_lab.kodi.addons import KodiAddonSmokeAdapter
from kodi_lab.mcp.stdio_proxy import (
    ENDPOINT_ENVIRONMENT_VARIABLE,
    TOKEN_ENVIRONMENT_VARIABLE,
)
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.storage.models import (
    McpWorkspaceDeploymentOperationRow,
    McpWorkspaceExecutionOperationRow,
)
from kodi_lab.web.app import create_app

PASSWORD = "phase12-workspace-development-live-validation"
ORIGIN = "http://127.0.0.1"
PROTOCOL = "2026-07-28"
LEGACY_PROTOCOL = "2025-11-25"
ADDON_ID = "plugin.video.kodi-lab-mcp-workspace-live"
ADDON_VERSION = "1.0.0"


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _load_helpers(project_root: Path) -> Any:
    path = project_root / "scripts" / "validate-phase8-copied-enablement.py"
    spec = importlib.util.spec_from_file_location("phase8_enablement_helpers", path)
    _require(spec is not None and spec.loader is not None, "helper unavailable")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_source(development_root: Path) -> None:
    source = development_root / ADDON_ID
    source.mkdir(parents=True)
    (source / "addon.xml").write_text(
        f'<addon id="{ADDON_ID}" name="Phase 12 Workspace Live" '
        f'version="{ADDON_VERSION}">'
        '<extension point="xbmc.python.pluginsource" library="default.py">'
        "<provides>video</provides></extension>"
        '<extension point="xbmc.addon.metadata"/></addon>\n',
        encoding="utf-8",
    )
    (source / "default.py").write_text(
        "import sys\nimport xbmcplugin\nxbmcplugin.endOfDirectory(int(sys.argv[1]))\n",
        encoding="utf-8",
    )


class _CountingBroker:
    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.workspace_sync_count = 0

    def copy_sync_mcp_workspace(self, **kwargs: Any) -> Any:
        self.workspace_sync_count += 1
        return self._inner.copy_sync_mcp_workspace(**kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _CountingSmoke:
    def __init__(self, inner: Any, counter: list[int]) -> None:
        self._inner = inner
        self._counter = counter

    def invoke(self, addon_id: str) -> Any:
        self._counter[0] += 1
        return self._inner.invoke(addon_id)


def _mcp_post(
    client: TestClient,
    token: str,
    protocol: str,
    method: str,
    *,
    params: dict[str, Any] | None = None,
    request_id: int,
) -> dict[str, Any]:
    materialized = {} if params is None else dict(params)
    if protocol == PROTOCOL:
        materialized["_meta"] = {
            "io.modelcontextprotocol/protocolVersion": PROTOCOL,
            "io.modelcontextprotocol/clientCapabilities": {},
            "io.modelcontextprotocol/clientInfo": {
                "name": "kodi-lab-live-proof",
                "version": "1",
            },
        }
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "MCP-Protocol-Version": protocol,
        "Mcp-Method": method,
    }
    name = materialized.get("name")
    if method == "tools/call" and isinstance(name, str):
        headers["Mcp-Name"] = name
    response = client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": materialized,
        },
        follow_redirects=False,
    )
    _require(response.status_code == 200, f"MCP HTTP failed: {response.text[:256]}")
    document = response.json()
    _require("result" in document, f"MCP response lacked result: {document!r}")
    return cast(dict[str, Any], document["result"])


@contextmanager
def _loopback_server(app: Any) -> Iterator[str]:
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
        _require(
            thread.is_alive() and time.monotonic() < deadline, "server start failed"
        )
        time.sleep(0.01)
    try:
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
        _require(not thread.is_alive(), "server did not stop")


async def _stdio_execute(
    *, endpoint: str, token: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    executable = Path(sys.executable).with_name("kodi-lab-mcp-stdio")
    parameters = StdioServerParameters(
        command=str(executable),
        env={
            TOKEN_ENVIRONMENT_VARIABLE: token,
            ENDPOINT_ENVIRONMENT_VARIABLE: endpoint,
        },
        cwd=Path.cwd(),
    )
    async with Client(parameters, mode="auto", cache=None) as stdio:
        result = await stdio.call_tool("workspace_execute", arguments)
        _require(not result.is_error, f"stdio execution failed: {result.content!r}")
        _require(result.structured_content is not None, "stdio result was unstructured")
        return cast(dict[str, Any], result.structured_content)


def _content(result: dict[str, Any]) -> dict[str, Any]:
    _require(result.get("isError") is False, f"tool failed: {result!r}")
    value = result.get("structuredContent")
    _require(isinstance(value, dict), "tool result was unstructured")
    return cast(dict[str, Any], value)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--evidence-path",
        type=Path,
        default=Path("<project-root>-phase12-workspace-evidence.json"),
    )
    arguments = parser.parse_args()
    _require(os.geteuid() == 0, "live validation must run as root")
    project_root = Path(__file__).resolve().parents[1]
    helpers = _load_helpers(project_root)
    helpers.PASSWORD = PASSWORD
    protected_before = helpers._protected_fingerprint()
    _require(helpers._managed_network_count() == 0, "managed network was not empty")
    runtime = next(item for item in helpers.RUNTIMES if item["release"] == "21.3")
    api = docker.APIClient()
    try:
        _require(
            api.inspect_image(runtime["image"])["Id"] == runtime["image"],
            "curated Kodi 21.3 image is absent",
        )
    finally:
        api.close()

    instances = {str(item["release"]): uuid4() for item in helpers.RUNTIMES}
    instances["21.3-default"] = uuid4()
    target_id = instances["21.3"]
    evidence: dict[str, Any] = {}
    try:
        with tempfile.TemporaryDirectory(
            prefix="kodi-lab-phase12-workspace-", dir="/var/tmp"
        ) as temporary:
            root = Path(temporary)
            development_root = root / "development"
            development_root.mkdir(mode=0o700)
            _write_source(development_root)
            data_root = root / "data"
            (data_root / "database").mkdir(parents=True)
            runtime_root = root / "web-runtime"
            runtime_root.mkdir(mode=0o700)
            os.environ["KODI_LAB_DATA_ROOT"] = str(data_root)
            os.environ["KODI_LAB_RUNTIME_ROOT"] = str(runtime_root)
            os.environ["KODI_LAB_DEVELOPMENT_ROOT"] = str(development_root)
            settings = Settings(_env_file=None)  # type: ignore[call-arg]
            command.upgrade(Config(str(project_root / "alembic.ini")), "head")
            engine = create_database_engine(settings.database_url)
            sessions = create_session_factory(engine)
            helpers._seed(sessions, instances)
            validator = DevelopmentSourceValidator(development_root)
            source = DevelopmentSourceAdministrationService(
                sessions, validator
            ).register_source(
                logical_name="Phase 12 workspace live source",
                relative_locator=ADDON_ID,
            )
            DevelopmentSourceAttachmentService(sessions, validator).attach_source(
                target_id, source.id
            )
            adapter = DockerManagedContainerAdapter()
            resolver = SqliteDevelopmentSourceDeploymentResolver(
                sessions, development_root=development_root, data_root=data_root
            )
            registry = build_foundation_registry(
                adapter,
                provisioning_resolver=SqliteProvisioningResolver(sessions),
                provisioning_provider=adapter,
                deprovisioning_provider=adapter,
                revision_resolver=SqliteManagedInstanceRevisionResolver(sessions),
                runtime_observation_resolver=SqliteRuntimeObservationResolver(sessions),
                runtime_observation_provider=adapter,
                development_source_deployment_resolver=resolver,
                development_source_deployment_provider=adapter,
            )
            with helpers._broker_server(root, registry) as raw_broker:
                broker = _CountingBroker(raw_broker)
                auth = WebAuthenticationService(sessions)
                auth.set_password(PASSWORD)
                app = create_app(
                    settings=settings,
                    session_factory=sessions,
                    broker=broker,
                    auth=auth,
                )
                with TestClient(app, base_url=ORIGIN) as client:
                    headers = helpers._login(client)
                    provision = client.post(
                        f"/api/instances/{target_id}/provision",
                        json={},
                        headers=headers,
                    )
                    _require(provision.status_code == 200, "provision failed")
                    helpers._copy(client, headers, target_id, source.id)
                    started = client.post(
                        f"/api/instances/{target_id}/start",
                        json={},
                        headers=headers,
                    )
                    _require(started.status_code == 200, "start failed")
                    helpers._wait_inventory(client, target_id)
                    enabled = helpers._post(client, headers, target_id, source.id)
                    _require(
                        enabled["result_classification"] == "enabled",
                        "baseline enable failed",
                    )

                    context = cast(FastAPI, client.app).state.kodi_lab
                    smoke_dispatch_count = [0]

                    def smoke_factory(observation: Any, profile: Any) -> Any:
                        return _CountingSmoke(
                            KodiAddonSmokeAdapter(observation, profile),
                            smoke_dispatch_count,
                        )

                    context.mcp_workspace_development._smoke_factory = smoke_factory
                    administration = context.mcp_administration
                    principal = administration.create_client(
                        display_name="Phase 12 workspace live proof",
                        preset_id=FULL_LAB_CONTROL_PRESET_ID,
                    )
                    administration.grant_development_source(principal.id, source.id)
                    administration.assign_target_preset(
                        target_id, FULL_LAB_CONTROL_PRESET_ID
                    )
                    administration.grant_target(principal.id, target_id)
                    token = administration.issue_credential(principal.id).token

                    created = _content(
                        _mcp_post(
                            client,
                            token,
                            PROTOCOL,
                            "tools/call",
                            params={
                                "name": "workspace_create",
                                "arguments": {
                                    "request_id": str(uuid4()),
                                    "source_id": str(source.id),
                                },
                            },
                            request_id=1,
                        )
                    )
                    workspace_id = UUID(created["workspace_id"])
                    creation_id = UUID(created["creation_operation_id"])
                    deadline = time.monotonic() + 10
                    while (
                        context.agent_workspaces.creation_status(creation_id).state
                        != "succeeded"
                    ):
                        _require(
                            time.monotonic() < deadline,
                            "workspace creation did not complete",
                        )
                        time.sleep(0.02)

                    edited = _content(
                        _mcp_post(
                            client,
                            token,
                            PROTOCOL,
                            "tools/call",
                            params={
                                "name": "workspace_edit",
                                "arguments": {
                                    "workspace_id": str(workspace_id),
                                    "expected_generation": 0,
                                    "operation": "replace",
                                    "relative_path": "default.py",
                                    "content": (
                                        "import sys\n"
                                        "import xbmcgui\n"
                                        "import xbmcplugin\n"
                                        "handle = int(sys.argv[1])\n"
                                        "item = xbmcgui.ListItem("
                                        "label='bounded-live-item')\n"
                                        "xbmcplugin.addDirectoryItem(handle, "
                                        "sys.argv[0] + '?fixed=1', item, False)\n"
                                        "xbmcplugin.endOfDirectory(handle)\n"
                                    ),
                                },
                            },
                            request_id=2,
                        )
                    )
                    _require(edited["new_generation"] == 1, "edit generation was wrong")
                    validated = _content(
                        _mcp_post(
                            client,
                            token,
                            PROTOCOL,
                            "tools/call",
                            params={
                                "name": "workspace_validate",
                                "arguments": {
                                    "workspace_id": str(workspace_id),
                                    "expected_generation": 1,
                                },
                            },
                            request_id=3,
                        )
                    )
                    _require(
                        validated["promotable"] is True
                        and validated["identity_classification"] == "stable",
                        "workspace validation failed",
                    )

                    readiness_deadline = time.monotonic() + 90
                    readiness: dict[str, Any] | None = None
                    readiness_request_id = 30
                    while time.monotonic() < readiness_deadline:
                        readiness = _content(
                            _mcp_post(
                                client,
                                token,
                                PROTOCOL,
                                "tools/call",
                                params={
                                    "name": "target_status",
                                    "arguments": {"target_id": str(target_id)},
                                },
                                request_id=readiness_request_id,
                            )
                        )
                        readiness_request_id += 1
                        if readiness["target"]["usability_state"] == "usable":
                            break
                        time.sleep(0.2)
                    _require(
                        readiness is not None
                        and readiness["target"]["usability_state"] == "usable",
                        f"target did not become usable: {readiness!r}",
                    )

                    deploy_request = uuid4()
                    deploy_arguments = {
                        "request_id": str(deploy_request),
                        "workspace_id": str(workspace_id),
                        "expected_generation": 1,
                        "target_id": str(target_id),
                    }
                    deployed = _content(
                        _mcp_post(
                            client,
                            token,
                            PROTOCOL,
                            "tools/call",
                            params={
                                "name": "workspace_deploy",
                                "arguments": deploy_arguments,
                            },
                            request_id=4,
                        )
                    )
                    replayed_deploy = _content(
                        _mcp_post(
                            client,
                            token,
                            LEGACY_PROTOCOL,
                            "tools/call",
                            params={
                                "name": "workspace_deploy",
                                "arguments": deploy_arguments,
                            },
                            request_id=5,
                        )
                    )
                    _require(deployed == replayed_deploy, "deploy replay differed")
                    _require(
                        broker.workspace_sync_count == 1,
                        "duplicate deploy caused another sync",
                    )
                    inventory = _content(
                        _mcp_post(
                            client,
                            token,
                            PROTOCOL,
                            "tools/call",
                            params={
                                "name": "addon_list",
                                "arguments": {"target_id": str(target_id)},
                            },
                            request_id=6,
                        )
                    )
                    _require(
                        any(
                            item["addon_id"] == ADDON_ID
                            and item["version"] == ADDON_VERSION
                            and item["enabled"] is True
                            for item in inventory["addons"]
                        ),
                        "deployed add-on was not enabled in inventory",
                    )

                    execute_arguments = {
                        **deploy_arguments,
                        "request_id": str(uuid4()),
                    }
                    with _loopback_server(client.app) as endpoint:
                        executed = asyncio.run(
                            _stdio_execute(
                                endpoint=endpoint,
                                token=token,
                                arguments=execute_arguments,
                            )
                        )
                    replayed_execute = _content(
                        _mcp_post(
                            client,
                            token,
                            PROTOCOL,
                            "tools/call",
                            params={
                                "name": "workspace_execute",
                                "arguments": execute_arguments,
                            },
                            request_id=7,
                        )
                    )
                    _require(executed == replayed_execute, "execution replay differed")
                    _require(
                        smoke_dispatch_count[0] == 1,
                        "duplicate execution caused another dispatch",
                    )
                    _require(
                        executed["result_classification"] == "smoke_passed"
                        and executed["item_count"] == 1,
                        "fixed smoke did not return bounded success",
                    )

                    second_edit = _content(
                        _mcp_post(
                            client,
                            token,
                            PROTOCOL,
                            "tools/call",
                            params={
                                "name": "workspace_edit",
                                "arguments": {
                                    "workspace_id": str(workspace_id),
                                    "expected_generation": 1,
                                    "operation": "create",
                                    "relative_path": "generation-two.txt",
                                    "content": "generation two\n",
                                },
                            },
                            request_id=8,
                        )
                    )
                    _require(
                        second_edit["new_generation"] == 2,
                        "second edit generation was wrong",
                    )
                    blocked = _mcp_post(
                        client,
                        token,
                        PROTOCOL,
                        "tools/call",
                        params={
                            "name": "workspace_execute",
                            "arguments": {
                                **execute_arguments,
                                "request_id": str(uuid4()),
                                "expected_generation": 2,
                            },
                        },
                        request_id=9,
                    )
                    _require(
                        blocked.get("isError") is True
                        and blocked.get("structuredContent")
                        == {"error": "execution_not_deployed"},
                        f"new generation was not blocked: {blocked!r}",
                    )
                    with sessions() as session:
                        deployment_rows = session.scalar(
                            select(func.count()).select_from(
                                McpWorkspaceDeploymentOperationRow
                            )
                        )
                        execution_rows = session.scalar(
                            select(func.count()).select_from(
                                McpWorkspaceExecutionOperationRow
                            )
                        )
                        migration_head = session.scalar(
                            text("SELECT version_num FROM alembic_version")
                        )
                    evidence = {
                        "schema": "kodi-lab-phase12-workspace-development-live-v1",
                        "migration_head": migration_head,
                        "runtime": "Kodi 21.3",
                        "target_class": "internal_managed_disposable",
                        "workspace_generation_deployed": 1,
                        "validation_classification": "passed",
                        "deployment": {
                            "operation_uuid": deployed["deployment_id"],
                            "state": deployed["state"],
                            "result_classification": deployed["result_classification"],
                            "modern_http_first": True,
                            "legacy_http_duplicate": True,
                            "actual_sync_count": broker.workspace_sync_count,
                            "durable_row_count": deployment_rows,
                        },
                        "execution": {
                            "operation_uuid": executed["execution_id"],
                            "state": executed["state"],
                            "result_classification": executed["result_classification"],
                            "item_count": executed["item_count"],
                            "modern_stdio_first": True,
                            "modern_http_duplicate": True,
                            "actual_dispatch_count": smoke_dispatch_count[0],
                            "durable_row_count": execution_rows,
                        },
                        "edit_after_deploy_new_generation_blocked": True,
                        "raw_listing_persisted_or_returned": False,
                    }
            engine.dispose()
    finally:
        helpers._cleanup_created(set(instances.values()))

    _require(helpers._managed_network_count() == 0, "managed network was not cleaned")
    protected_after = helpers._protected_fingerprint()
    changed = sorted(
        key
        for key in protected_before
        if protected_before[key] != protected_after.get(key)
    )
    _require(not changed, f"protected categories changed: {changed!r}")
    evidence["protected_isolation"] = {
        "fingerprints_unchanged": True,
        "production_roots_absent": True,
        "temporary_managed_resources_removed": True,
        "managed_network_empty": True,
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
