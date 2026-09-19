#!/usr/bin/env python3
"""Isolated live proof for Phase 9 promotion-bound Kodi verification."""

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

from kodi_lab.application import (
    DevelopmentSourceAdministrationService,
    DevelopmentSourceAttachmentService,
)
from kodi_lab.application.readiness_tasks import ReadinessTaskRegistry
from kodi_lab.auth import WebAuthenticationService
from kodi_lab.broker.development_sources import (
    SqliteDevelopmentSourceDeploymentResolver,
)
from kodi_lab.broker.docker_inventory import DockerManagedContainerAdapter
from kodi_lab.broker.errors import DockerDevelopmentSourceDeploymentError
from kodi_lab.broker.kodi_logs import (
    KodiLogWindow,
    KodiLogWindowProvider,
    SqliteKodiLogAuthorityResolver,
)
from kodi_lab.broker.operations import build_foundation_registry
from kodi_lab.broker.provisioning import (
    MANAGED_NETWORK_NAME,
    SqliteManagedInstanceRevisionResolver,
    SqliteProvisioningResolver,
    SqliteRuntimeObservationResolver,
)
from kodi_lab.config import Settings
from kodi_lab.development import DevelopmentSourceValidator
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.storage.models import (
    AgentWorkspacePromotionHandoffRow,
    PromotionVerificationOperationRow,
    TargetRow,
)
from kodi_lab.web.app import create_app

PASSWORD = "phase9-promotion-verification-live-password"
ORIGIN = "http://127.0.0.1"


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _managed_network_count() -> int:
    api = docker.APIClient()
    try:
        try:
            network = api.inspect_network(MANAGED_NETWORK_NAME)
        except docker.errors.NotFound:
            return 0
        return len(network.get("Containers") or {})
    finally:
        api.close()


def _cleanup_created(instances: set[UUID]) -> None:
    api = docker.APIClient()
    try:
        for summary in api.containers(
            all=True, filters={"label": "io.kodi-lab.managed=true"}
        ):
            labels = summary.get("Labels") or {}
            raw_instance = labels.get("io.kodi-lab.instance-id")
            if raw_instance is None or UUID(raw_instance) not in instances:
                continue
            container_id = summary["Id"]
            if summary.get("State") == "running":
                api.stop(container_id, timeout=10)
            api.remove_container(container_id)
    finally:
        api.close()


def _phase8_helpers(project_root: Path) -> ModuleType:
    path = project_root / "scripts" / "validate-phase8-step5.py"
    spec = importlib.util.spec_from_file_location("phase8_step5_helpers", path)
    _require(spec is not None and spec.loader is not None, "helper import failed")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class InjectingDeploymentProvider:
    def __init__(self, delegate: DockerManagedContainerAdapter) -> None:
        self.delegate = delegate
        self.ambiguous_after_apply = False

    def copy_sync_attached_development_source(self, **kwargs: Any) -> Any:
        result = self.delegate.copy_sync_attached_development_source(**kwargs)
        if self.ambiguous_after_apply:
            self.ambiguous_after_apply = False
            raise DockerDevelopmentSourceDeploymentError(
                "development_source_apply_unverified",
                "Injected post-application uncertainty.",
                reconciliation_required=True,
            )
        return result

    def verify_attached_development_source_destination(self, **kwargs: Any) -> Any:
        return self.delegate.verify_attached_development_source_destination(**kwargs)


class InjectingLogProvider(KodiLogWindowProvider):
    def __init__(self) -> None:
        super().__init__()
        self.next_result: str | None = None

    def read(self, authority: Any, cursor: str) -> KodiLogWindow:
        observed = super().read(authority, cursor)
        injected = self.next_result
        self.next_result = None
        if injected == "target_errors":
            return KodiLogWindow(
                True,
                "target_errors",
                1,
                observed.unrelated_warning_count,
                (f"ERROR {authority.addon_id} injected startup failure",),
                observed.scanned_bytes,
            )
        if injected == "incomplete":
            return KodiLogWindow(False, "incomplete", 0, 0, (), observed.scanned_bytes)
        return observed


def _write_source(root: Path, key: str, addon_id: str) -> str:
    locator = f"sources/{key}"
    source = root.joinpath(*locator.split("/"))
    source.mkdir(parents=True)
    (source / "addon.xml").write_text(
        f'<addon id="{addon_id}" name="Phase 9 {key}" version="1.0.0">'
        '<extension point="xbmc.python.pluginsource" library="default.py" />'
        "</addon>\n",
        encoding="utf-8",
    )
    (source / "default.py").write_text("print('before')\n", encoding="utf-8")
    return locator


def _wait(client: TestClient, url: str, terminal: set[str]) -> dict[str, Any]:
    last: dict[str, Any] | None = None
    for _ in range(1800):
        response = client.get(url)
        _require(response.status_code == 200, f"status failed: {response.text[:256]}")
        last = response.json()
        if last.get("state") in terminal:
            return last
        time.sleep(0.05)
    raise RuntimeError(f"operation did not terminalize: {last!r}")


def _copy(
    client: TestClient,
    headers: dict[str, str],
    instance_id: UUID,
    source_id: UUID,
) -> dict[str, Any]:
    response = client.post(
        f"/api/instances/{instance_id}/development-sources/{source_id}/copy-sync",
        json={},
        headers=headers,
    )
    _require(response.status_code == 202, f"copy rejected: {response.text[:256]}")
    result = _wait(
        client,
        response.json()["status_url"],
        {"succeeded", "failed", "reconciliation_required"},
    )
    _require(result["state"] == "succeeded", f"initial copy failed: {result!r}")
    return result


def _wait_inventory(client: TestClient, instance_id: UUID) -> dict[str, Any]:
    last: dict[str, Any] | None = None
    for _ in range(600):
        response = client.get(f"/api/instances/{instance_id}/addons")
        _require(response.status_code == 200, "inventory status failed")
        last = response.json()
        if last["query_status"] == "available":
            return last
        time.sleep(0.2)
    raise RuntimeError(f"inventory did not become available: {last!r}")


def _workspace(
    client: TestClient,
    headers: dict[str, str],
    source_id: UUID,
    addon_id: str,
) -> UUID:
    response = client.post(
        "/api/agent-workspaces",
        json={"development_source_id": str(source_id)},
        headers=headers,
    )
    _require(response.status_code == 202, f"workspace rejected: {response.text[:256]}")
    result = _wait(client, response.json()["status_url"], {"succeeded", "failed"})
    _require(result["state"] == "succeeded", f"workspace failed: {result!r}")
    workspace_id = UUID(result["workspace_id"])
    descriptor = (
        f'<addon id="{addon_id}" name="Phase 9 promoted" version="1.0.1">'
        '<extension point="xbmc.python.pluginsource" library="default.py" />'
        "</addon>\n"
    )
    changed = client.put(
        f"/api/agent-workspaces/{workspace_id}/files",
        json={
            "expected_generation": 0,
            "path": "addon.xml",
            "content": descriptor,
        },
        headers=headers,
    )
    _require(changed.status_code == 200, f"workspace edit failed: {changed.text[:256]}")
    return workspace_id


def _promote(
    client: TestClient,
    headers: dict[str, str],
    instance_id: UUID,
    workspace_id: UUID,
) -> tuple[dict[str, Any], dict[str, Any]]:
    base = f"/api/instances/{instance_id}/agent-workspaces/{workspace_id}"
    plan = client.post(f"{base}/promotion-plans", json={}, headers=headers)
    _require(plan.status_code == 201, f"plan failed: {plan.text[:256]}")
    plan_id = plan.json()["plan_id"]
    approved = client.post(
        f"{base}/promotion-plans/{plan_id}/approve-and-execute",
        json={"confirmed": True},
        headers=headers,
    )
    _require(approved.status_code == 200, f"promotion failed: {approved.text[:256]}")
    promotion = approved.json()
    _require(promotion["state"] == "succeeded", "source promotion did not succeed")
    verification = client.get(
        f"{base}/promotion-operations/{promotion['operation_id']}/verification"
    )
    _require(verification.status_code == 200, "verification status unavailable")
    return promotion, verification.json()


def _consumption_evidence(factory: Any, verification_id: str) -> dict[str, bool]:
    with factory() as session:
        operation = session.get(PromotionVerificationOperationRow, verification_id)
        _require(operation is not None, "verification operation was not retained")
        assert operation is not None
        handoff = session.get(AgentWorkspacePromotionHandoffRow, operation.handoff_id)
        _require(handoff is not None, "verification handoff was not retained")
        assert handoff is not None
        reserved = handoff.verification_operation_id == verification_id
        consumed = handoff.consumed_at is not None
        marker = bool(operation.handoff_consumed)
        _require(reserved, "verification handoff reservation was lost")
        _require(consumed is marker, "handoff consumption markers diverged")
        return {
            "handoff_reserved": reserved,
            "handoff_consumed": consumed,
        }


def _provision_start_copy(
    client: TestClient,
    headers: dict[str, str],
    instance_id: UUID,
    source_id: UUID,
) -> None:
    provisioned = client.post(
        f"/api/instances/{instance_id}/provision", json={}, headers=headers
    )
    _require(provisioned.status_code == 200, "provision failed")
    _copy(client, headers, instance_id, source_id)
    started = client.post(
        f"/api/instances/{instance_id}/start", json={}, headers=headers
    )
    _require(started.status_code == 200, "start failed")
    _wait_inventory(client, instance_id)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--evidence-path",
        type=Path,
        default=Path("evidence/kodi-lab-phase9-slice4-evidence.json"),
    )
    arguments = parser.parse_args()
    _require(os.geteuid() == 0, "live validation must run as root")
    project_root = Path(__file__).resolve().parents[1]
    helper = _phase8_helpers(project_root)
    helper.PASSWORD = PASSWORD
    helper.ORIGIN = ORIGIN
    protected_before = helper._protected_fingerprint()
    _require(_managed_network_count() == 0, "managed network was not empty")
    _require(
        protected_before["production_data_root_absent"]
        and protected_before["production_runtime_root_absent"],
        "production roots were present",
    )
    instances = {str(item["release"]): uuid4() for item in helper.RUNTIMES}
    instance_set = set(instances.values())
    evidence: dict[str, Any] = {}
    engine = None
    try:
        with tempfile.TemporaryDirectory(
            prefix="kodi-lab-phase9-slice4-", dir="/var/tmp"
        ) as temporary:
            root = Path(temporary)
            development_root = root / "development"
            development_root.mkdir(mode=0o700)
            data_root = root / "data"
            (data_root / "database").mkdir(parents=True)
            runtime_root = root / "runtime"
            runtime_root.mkdir(mode=0o700)
            os.environ["KODI_LAB_DATA_ROOT"] = str(data_root)
            os.environ["KODI_LAB_RUNTIME_ROOT"] = str(runtime_root)
            os.environ["KODI_LAB_DEVELOPMENT_ROOT"] = str(development_root)
            settings = Settings()
            command.upgrade(Config(str(project_root / "alembic.ini")), "head")
            engine = create_database_engine(settings.database_url)
            factory = create_session_factory(engine)
            helper._seed(factory, instances)
            with factory.begin() as session:
                session.query(TargetRow).filter(
                    TargetRow.id.in_([str(item) for item in instance_set])
                ).update(
                    {
                        "availability": "available",
                        "health": "healthy",
                        "connection_state": "connected",
                    },
                    synchronize_session=False,
                )
            validator = DevelopmentSourceValidator(development_root)
            administration = DevelopmentSourceAdministrationService(factory, validator)
            attachment = DevelopmentSourceAttachmentService(factory, validator)
            sources: dict[str, tuple[Any, str]] = {}
            for runtime in helper.RUNTIMES:
                release = str(runtime["release"])
                key = release.replace(".", "-")
                addon_id = f"plugin.video.phase9.{key}"
                locator = _write_source(development_root, key, addon_id)
                source = administration.register_source(
                    logical_name=f"Phase 9 {release}", relative_locator=locator
                )
                attachment.attach_source(instances[release], source.id)
                sources[release] = (source, addon_id)
            adapter = DockerManagedContainerAdapter()
            deployment_provider = InjectingDeploymentProvider(adapter)
            log_provider = InjectingLogProvider()
            resolver = SqliteDevelopmentSourceDeploymentResolver(
                factory, development_root=development_root, data_root=data_root
            )
            registry = build_foundation_registry(
                adapter,
                provisioning_resolver=SqliteProvisioningResolver(factory),
                provisioning_provider=adapter,
                deprovisioning_provider=adapter,
                revision_resolver=SqliteManagedInstanceRevisionResolver(factory),
                runtime_observation_resolver=SqliteRuntimeObservationResolver(factory),
                runtime_observation_provider=adapter,
                development_source_deployment_resolver=resolver,
                development_source_deployment_provider=deployment_provider,
                kodi_log_resolver=SqliteKodiLogAuthorityResolver(factory),
                kodi_log_provider=log_provider,
            )
            with helper._broker_server(root, registry) as broker:
                auth = WebAuthenticationService(factory)
                auth.set_password(PASSWORD)
                readiness = ReadinessTaskRegistry(max_workers=4)
                app = create_app(
                    settings=settings,
                    session_factory=factory,
                    broker=broker,
                    readiness=readiness,
                    auth=auth,
                )
                with TestClient(app, base_url=ORIGIN) as client:
                    headers = helper._login(client)
                    cross_version: list[dict[str, Any]] = []
                    for runtime in helper.RUNTIMES:
                        release = str(runtime["release"])
                        instance_id = instances[release]
                        source, addon_id = sources[release]
                        _provision_start_copy(client, headers, instance_id, source.id)
                        workspace_id = _workspace(client, headers, source.id, addon_id)
                        promotion, verification = _promote(
                            client,
                            headers,
                            instance_id,
                            workspace_id,
                        )
                        _require(
                            verification["state"] == "succeeded",
                            f"{release} verification failed: {verification!r}",
                        )
                        consumption = _consumption_evidence(
                            factory, verification["verification_id"]
                        )
                        _require(
                            consumption["handoff_consumed"],
                            f"{release} exact sync did not consume the handoff",
                        )
                        cross_version.append(
                            {
                                "runtime_version": release,
                                "source_promotion_uuid": promotion["operation_id"],
                                "verification_uuid": verification["verification_id"],
                                "target_semantic_identity": str(instance_id),
                                "sync_result": verification["sync_result"],
                                "reload_result": verification["reload_result"],
                                "inventory_result": verification["inventory_result"],
                                "expected_version": verification["expected_version"],
                                "current_version": verification["current_version"],
                                "expected_enabled": verification["expected_enabled"],
                                "current_enabled": verification["current_enabled"],
                                "log_result": verification["log_result"],
                                "target_error_count": verification[
                                    "target_error_count"
                                ],
                                "unrelated_warning_count": verification[
                                    "unrelated_warning_count"
                                ],
                                "reconciliation_state": False,
                                **consumption,
                            }
                        )
                        if release != "21.3":
                            stopped = client.post(
                                f"/api/instances/{instance_id}/stop",
                                json={},
                                headers=headers,
                            )
                            _require(stopped.status_code == 200, "stop failed")
                            response = client.post(
                                f"/api/instances/{instance_id}/deprovision",
                                json={},
                                headers=headers,
                            )
                            _require(response.status_code == 200, "deprovision failed")

                    deep_instance = instances["21.3"]
                    deep: dict[str, Any] = {}
                    for scenario in ("target_errors", "incomplete", "sync_ambiguity"):
                        stopped = client.post(
                            f"/api/instances/{deep_instance}/stop",
                            json={},
                            headers=headers,
                        )
                        _require(stopped.status_code == 200, "deep stop failed")
                        addon_id = f"plugin.video.phase9.deep.{scenario}"
                        locator = _write_source(
                            development_root, f"deep-{scenario}", addon_id
                        )
                        source = administration.register_source(
                            logical_name=f"Phase 9 deep {scenario}",
                            relative_locator=locator,
                        )
                        attachment.attach_source(deep_instance, source.id)
                        _copy(client, headers, deep_instance, source.id)
                        started = client.post(
                            f"/api/instances/{deep_instance}/start",
                            json={},
                            headers=headers,
                        )
                        _require(started.status_code == 200, "deep start failed")
                        _wait_inventory(client, deep_instance)
                        workspace_id = _workspace(client, headers, source.id, addon_id)
                        if scenario == "target_errors":
                            log_provider.next_result = "target_errors"
                        elif scenario == "incomplete":
                            log_provider.next_result = "incomplete"
                        else:
                            deployment_provider.ambiguous_after_apply = True
                        promotion, verification = _promote(
                            client,
                            headers,
                            deep_instance,
                            workspace_id,
                        )
                        expected = {
                            "target_errors": "failed",
                            "incomplete": "incomplete",
                            "sync_ambiguity": "reconciliation_required",
                        }[scenario]
                        _require(
                            verification["state"] == expected,
                            f"deep {scenario} classification was wrong",
                        )
                        consumption = _consumption_evidence(
                            factory, verification["verification_id"]
                        )
                        _require(
                            consumption["handoff_consumed"]
                            is (scenario != "sync_ambiguity"),
                            f"deep {scenario} handoff consumption was wrong",
                        )
                        deep[scenario] = {
                            "source_promotion_uuid": promotion["operation_id"],
                            "verification_uuid": verification["verification_id"],
                            "source_result": promotion["state"],
                            "verification_state": verification["state"],
                            "sync_result": verification["sync_result"],
                            "reload_result": verification["reload_result"],
                            "inventory_result": verification["inventory_result"],
                            "log_result": verification["log_result"],
                            "target_error_count": verification["target_error_count"],
                            **consumption,
                        }
                        if scenario == "sync_ambiguity":
                            break
                    with factory() as session:
                        migration_head = session.scalar(
                            text("SELECT version_num FROM alembic_version")
                        )
                    evidence = {
                        "schema": "kodi-lab-phase9-slice4-live-v1",
                        "migration_head": migration_head,
                        "cross_version": cross_version,
                        "deep_21_3": deep,
                    }
            engine.dispose()
            engine = None
    finally:
        _cleanup_created(instance_set)
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
