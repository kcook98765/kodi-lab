#!/usr/bin/env python3
"""Run one isolated Kodi 21.3 proof for Phase 12 promotion request."""

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
from kodi_lab.broker.kodi_logs import (
    KodiLogWindowProvider,
    SqliteKodiLogAuthorityResolver,
)
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
    AgentWorkspacePromotionHandoffRow,
    AgentWorkspacePromotionOperationRow,
    AgentWorkspacePromotionPlanRow,
    DevelopmentSourceDeploymentRow,
    ManagedInstanceDevelopmentSourceRow,
    ManagedInstanceRow,
    McpDevelopmentRequestRow,
    McpWorkspaceDeploymentOperationRow,
    McpWorkspaceExecutionOperationRow,
    PromotionVerificationOperationRow,
    TargetRow,
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


def _verification_debug(sessions: Any, verification_id: str) -> dict[str, bool]:
    """Return path- and digest-free preflight equalities for live-proof failure."""

    with sessions() as session:
        verification = session.get(PromotionVerificationOperationRow, verification_id)
        promotion = (
            None
            if verification is None
            else session.get(
                AgentWorkspacePromotionOperationRow,
                verification.source_promotion_id,
            )
        )
        plan = (
            None
            if promotion is None
            else session.get(AgentWorkspacePromotionPlanRow, promotion.plan_id)
        )
        handoff = (
            None
            if promotion is None
            else session.scalar(
                select(AgentWorkspacePromotionHandoffRow).where(
                    AgentWorkspacePromotionHandoffRow.operation_id == promotion.id
                )
            )
        )
        evidence = (
            None
            if verification is None
            else session.get(
                DevelopmentSourceDeploymentRow,
                {
                    "managed_instance_id": verification.target_instance_id,
                    "development_source_id": verification.development_source_id,
                },
            )
        )
        instance = (
            None
            if verification is None
            else session.get(ManagedInstanceRow, verification.target_instance_id)
        )
        attachment = (
            None
            if verification is None
            else session.get(
                ManagedInstanceDevelopmentSourceRow,
                {
                    "managed_instance_id": verification.target_instance_id,
                    "development_source_id": verification.development_source_id,
                },
            )
        )
        deployment = (
            None
            if plan is None or plan.originating_mcp_deployment_id is None
            else session.get(
                McpWorkspaceDeploymentOperationRow,
                plan.originating_mcp_deployment_id,
            )
        )
        execution = (
            None
            if plan is None or plan.originating_mcp_execution_id is None
            else session.get(
                McpWorkspaceExecutionOperationRow,
                plan.originating_mcp_execution_id,
            )
        )
    present = all(
        item is not None
        for item in (
            verification,
            promotion,
            plan,
            handoff,
            evidence,
            instance,
            attachment,
            deployment,
            execution,
        )
    )
    if not present:
        return {"all_rows_present": False}
    assert verification is not None
    assert promotion is not None
    assert plan is not None
    assert handoff is not None
    assert evidence is not None
    assert instance is not None
    assert attachment is not None
    assert deployment is not None
    assert execution is not None
    return {
        "all_rows_present": True,
        "plan_handoff_operation_match": plan.target_deployment_operation_id
        == handoff.prior_deployment_operation_id,
        "plan_handoff_source_match": plan.target_source_digest
        == handoff.prior_source_digest,
        "plan_handoff_destination_match": plan.target_destination_digest
        == handoff.prior_destination_digest,
        "canonical_operation_anchor_match": evidence.last_successful_operation_id
        == handoff.prior_deployment_operation_id,
        "canonical_container_anchor_match": evidence.trusted_container_id
        == plan.target_container_id,
        "instance_is_disposable": instance.instance_type.value == "disposable",
        "instance_runtime_match": instance.runtime_revision_id
        == verification.runtime_revision_id,
        "mcp_source_match": deployment.source_manifest_digest
        == handoff.prior_source_digest,
        "mcp_destination_match": deployment.verified_destination_digest
        == handoff.prior_destination_digest,
        "mcp_execution_deployment_match": execution.deployment_id == deployment.id,
        "mcp_generation_match": deployment.generation
        == execution.generation
        == plan.workspace_generation,
        "mcp_client_match": deployment.client_id
        == execution.client_id
        == plan.originating_mcp_client_id,
        "mcp_states_succeeded": deployment.state == execution.state == "succeeded",
        "verification_links_match": handoff.verification_operation_id
        == verification.id,
        "promotion_state_succeeded": promotion.state.value == "succeeded",
        "promotion_plan_match": promotion.plan_id == plan.id,
        "promotion_workspace_match": promotion.workspace_id
        == verification.workspace_id
        == plan.workspace_id
        == handoff.workspace_id,
        "promotion_source_match": promotion.development_source_id
        == verification.development_source_id
        == plan.development_source_id
        == handoff.development_source_id,
        "promotion_target_match": promotion.target_instance_id
        == verification.target_instance_id
        == plan.target_instance_id
        == handoff.target_instance_id,
        "runtime_match": plan.target_runtime_revision_id
        == verification.runtime_revision_id
        == handoff.runtime_revision_id
        == evidence.runtime_revision_id,
        "handoff_operation_match": handoff.operation_id == promotion.id,
        "handoff_unconsumed": handoff.consumed_at is None,
        "mcp_workspace_match": deployment.workspace_id
        == execution.workspace_id
        == verification.workspace_id,
        "mcp_source_identity_match": deployment.development_source_id
        == execution.development_source_id
        == verification.development_source_id,
        "mcp_target_match": deployment.target_id
        == execution.target_id
        == verification.target_instance_id,
        "mcp_addon_match": deployment.addon_id == execution.addon_id,
        "expected_identity_match": handoff.expected_addon_id == verification.addon_id
        and handoff.expected_name == verification.expected_name
        and handoff.expected_version == verification.expected_version
        and handoff.expected_enabled is verification.expected_enabled,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--evidence-path",
        type=Path,
        default=Path("evidence/kodi-lab-phase12-promotion-evidence.json"),
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
            prefix="kodi-lab-phase12-promotion-", dir="/var/tmp"
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
            with sessions.begin() as session:
                session.query(TargetRow).filter(
                    TargetRow.id.in_([str(item) for item in instances.values()])
                ).update(
                    {
                        "availability": "available",
                        "health": "healthy",
                        "connection_state": "connected",
                    },
                    synchronize_session=False,
                )
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
                kodi_log_resolver=SqliteKodiLogAuthorityResolver(sessions),
                kodi_log_provider=KodiLogWindowProvider(),
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

                    promotion_request_id = uuid4()
                    promotion_arguments = {
                        "request_id": str(promotion_request_id),
                        "workspace_id": str(workspace_id),
                        "expected_generation": 1,
                        "target_id": str(target_id),
                    }
                    requested = _content(
                        _mcp_post(
                            client,
                            token,
                            PROTOCOL,
                            "tools/call",
                            params={
                                "name": "promotion_request",
                                "arguments": promotion_arguments,
                            },
                            request_id=8,
                        )
                    )
                    replayed_request = _content(
                        _mcp_post(
                            client,
                            token,
                            LEGACY_PROTOCOL,
                            "tools/call",
                            params={
                                "name": "promotion_request",
                                "arguments": promotion_arguments,
                            },
                            request_id=9,
                        )
                    )
                    _require(
                        requested == replayed_request,
                        "promotion request replay differed",
                    )
                    plan_id = requested["promotion_plan_id"]
                    plan_base = (
                        f"/api/instances/{target_id}/agent-workspaces/"
                        f"{workspace_id}/promotion-plans/{plan_id}"
                    )
                    review = client.get(plan_base)
                    _require(
                        review.status_code == 200,
                        f"Web plan review unavailable: {review.text[:256]}",
                    )
                    reviewed = review.json()
                    _require(
                        reviewed["originating_actor"] == "mcp_client"
                        and reviewed["originating_mcp_client_id"] == str(principal.id)
                        and reviewed["validation_classification"] == "passed"
                        and reviewed["deployment_classification"] == "succeeded"
                        and reviewed["execution_classification"] == "succeeded",
                        "Web review did not show exact MCP evidence",
                    )
                    mcp_approval = client.post(
                        f"{plan_base}/approve-and-execute",
                        json={"confirmed": True},
                        headers={
                            "Origin": ORIGIN,
                            "Authorization": f"Bearer {token}",
                            "Cookie": "",
                        },
                    )
                    _require(
                        mcp_approval.status_code == 401,
                        "MCP bearer entered Web approval",
                    )
                    approved = client.post(
                        f"{plan_base}/approve-and-execute",
                        json={"confirmed": True},
                        headers=headers,
                    )
                    _require(
                        approved.status_code == 200,
                        f"Web approval failed: {approved.text[:256]}",
                    )
                    promotion = approved.json()
                    _require(
                        promotion["state"] == "succeeded"
                        and promotion["handoff_published"] is True,
                        "canonical source promotion did not succeed",
                    )
                    verification_url = (
                        f"/api/instances/{target_id}/agent-workspaces/"
                        f"{workspace_id}/promotion-operations/"
                        f"{promotion['operation_id']}/verification"
                    )
                    verification: dict[str, Any] | None = None
                    verification_deadline = time.monotonic() + 90
                    while time.monotonic() < verification_deadline:
                        response = client.get(verification_url)
                        _require(
                            response.status_code == 200,
                            "verification status unavailable",
                        )
                        verification = response.json()
                        if verification["state"] not in {"queued", "running"}:
                            break
                        time.sleep(0.1)
                    if (
                        verification is not None
                        and verification["state"] != "succeeded"
                    ):
                        print(
                            json.dumps(
                                {
                                    "verification_preflight_debug": _verification_debug(
                                        sessions, verification["verification_id"]
                                    )
                                },
                                sort_keys=True,
                            ),
                            file=sys.stderr,
                        )
                    _require(
                        verification is not None
                        and verification["state"] == "succeeded",
                        f"mandatory verification did not succeed: {verification!r}",
                    )
                    source_body = (
                        development_root / ADDON_ID / "default.py"
                    ).read_text(encoding="utf-8")
                    _require(
                        "bounded-live-item" in source_body,
                        "temporary canonical source was not promoted",
                    )
                    status = _content(
                        _mcp_post(
                            client,
                            token,
                            PROTOCOL,
                            "tools/call",
                            params={
                                "name": "workspace_status",
                                "arguments": {"workspace_id": str(workspace_id)},
                            },
                            request_id=10,
                        )
                    )
                    _require(
                        len(status["workspaces"]) == 1,
                        "workspace status did not return the exact workspace",
                    )
                    summary = status["workspaces"][0]
                    _require(
                        summary["latest_promotion_plan_id"] == plan_id
                        and summary["latest_promotion_operation_id"]
                        == promotion["operation_id"]
                        and summary["latest_promotion_verification_id"]
                        == verification["verification_id"]
                        and summary["latest_promotion_final_classification"]
                        == "source_promoted_and_verified",
                        "workspace status did not project final promotion",
                    )
                    tools = _mcp_post(
                        client,
                        token,
                        PROTOCOL,
                        "tools/list",
                        request_id=11,
                    )
                    tool_names = [item["name"] for item in tools["tools"]]
                    _require(
                        "promotion_request" in tool_names
                        and "promotion_approve" not in tool_names,
                        "catalog promotion boundary was wrong",
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
                        plan_rows = session.scalar(
                            select(func.count()).select_from(
                                AgentWorkspacePromotionPlanRow
                            )
                        )
                        request_rows = session.scalar(
                            select(func.count())
                            .select_from(McpDevelopmentRequestRow)
                            .where(
                                McpDevelopmentRequestRow.operation_kind
                                == "promotion_request"
                            )
                        )
                        verification_rows = session.scalar(
                            select(func.count()).select_from(
                                PromotionVerificationOperationRow
                            )
                        )
                        migration_head = session.scalar(
                            text("SELECT version_num FROM alembic_version")
                        )
                    _require(deployment_rows == 1, "deployment replay duplicated state")
                    _require(execution_rows == 1, "execution replay duplicated state")
                    _require(plan_rows == 1, "promotion replay duplicated the plan")
                    _require(
                        request_rows == 1, "promotion replay duplicated the request"
                    )
                    _require(
                        verification_rows == 1,
                        "promotion did not create exactly one verification child",
                    )
                    _require(
                        migration_head == "0043_mcp_promotion_request",
                        "unexpected migration head",
                    )
                    evidence = {
                        "schema": "kodi-lab-phase12-promotion-live-v1",
                        "workspace_uuid": str(workspace_id),
                        "source_uuid": str(source.id),
                        "target_uuid": str(target_id),
                        "client_uuid": str(principal.id),
                        "request_uuid": str(promotion_request_id),
                        "generation": 1,
                        "plan_uuid": plan_id,
                        "plan_state_at_review": reviewed["state"],
                        "request_replay_counts": {
                            "modern_http": 1,
                            "legacy_http_duplicate": 1,
                            "durable_request_rows": request_rows,
                            "durable_plan_rows": plan_rows,
                        },
                        "approval_actor_classification": "web_administrator",
                        "mcp_approval_refused": True,
                        "promotion_operation_uuid": promotion["operation_id"],
                        "verification_operation_uuid": verification["verification_id"],
                        "final_semantic_outcome": summary[
                            "latest_promotion_final_classification"
                        ],
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
    evidence["cleanup_status"] = {
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
