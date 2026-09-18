#!/usr/bin/env python3
"""Run isolated live validation for Phase 8 repository URL testing."""

# ruff: noqa: E402 -- bytecode suppression must precede project imports.

from __future__ import annotations

import argparse
import grp
import hashlib
import importlib.util
import io
import json
import os
import re
import sys
import tempfile
import time
import traceback
import zipfile
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import UUID, uuid4

sys.dont_write_bytecode = True

import docker
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import text

from kodi_lab.application.addon_repositories import (
    AddonRepositoryFetchCoordinator,
    AddonRepositoryRefreshCoordinator,
)
from kodi_lab.application.readiness_tasks import ReadinessTaskRegistry
from kodi_lab.application.repository_replacements import RepositoryReplacementService
from kodi_lab.application.snapshots import DisposableSnapshotCreationService
from kodi_lab.auth import WebAuthenticationService
from kodi_lab.broker.addon_zips import SqliteAddonZipInstallationResolver
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
from kodi_lab.broker.snapshots import (
    SqliteSnapshotCaptureResolver,
    SqliteSnapshotRestoreResolver,
)
from kodi_lab.config import Settings
from kodi_lab.development import (
    AddonRepositoryError,
    PinnedHttpsTransport,
    RepositoryHttpResponse,
    parse_repository_metadata,
)
from kodi_lab.development.deployment import capture_destination_archive
from kodi_lab.kodi.readiness import KodiReadinessCoordinator, ReadinessResult
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.storage.repositories import (
    AddonZipArtifactRepository,
    AddonZipInstallationEvidenceRepository,
    DevelopmentSourceDeploymentRepository,
    ManagedInstanceAddonRepositorySourceRepository,
    RepositoryReplacementOperationRepository,
    RepositoryReplacementPlanRepository,
)
from kodi_lab.web.app import create_app

PASSWORD = "phase8-repository-live-validation"
ORIGIN = "http://127.0.0.1"
DEEP_RELEASE = "21.3"
BASIC_ID = "plugin.video.kodi-lab-repository-basic"
COLLISION_ID = "plugin.video.kodi-lab-repository-collision"


class _RecordingReadiness:
    def __init__(
        self,
        coordinator: KodiReadinessCoordinator,
        instance_id: UUID,
        sink: dict[str, str],
    ) -> None:
        self._coordinator = coordinator
        self._instance_id = instance_id
        self._sink = sink

    def wait_until_usable(self, managed_instance_id: str) -> ReadinessResult:
        result = self._coordinator.wait_until_usable(managed_instance_id)
        self._sink[str(self._instance_id)] = ":".join(
            (
                result.usability_state.value,
                result.container_state.value,
                result.process_state.value,
                result.jsonrpc_state.value,
                (
                    "none"
                    if result.last_observed_failure is None
                    else result.last_observed_failure.value
                ),
            )
        )
        return result


class _DiagnosticSnapshotCreationService(DisposableSnapshotCreationService):
    def __init__(self, *args: Any, diagnostic: dict[str, str], **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._diagnostic = diagnostic

    def create(self) -> Any:
        try:
            return super().create()
        except Exception as error:
            cause = error.__cause__ or error
            frames = traceback.extract_tb(cause.__traceback__)
            marker = "unknown"
            if frames:
                frame = frames[-1]
                marker = f"{frame.name}:{frame.lineno}"
            self._diagnostic["snapshot_exception"] = f"{type(cause).__name__}:{marker}"
            raise


class _DiagnosticSnapshotProvider:
    def __init__(self, provider: Any, diagnostic: dict[str, str]) -> None:
        self._provider = provider
        self._diagnostic = diagnostic

    def capture_managed_instance_snapshot(self, **kwargs: Any) -> Any:
        try:
            return self._provider.capture_managed_instance_snapshot(**kwargs)
        except Exception as error:
            cause = error.__cause__ or error
            code = getattr(error, "code", "no_code")
            frames = traceback.extract_tb(cause.__traceback__)
            marker = "unknown"
            if frames:
                frame = frames[-1]
                marker = f"{frame.name}:{frame.lineno}"
            self._diagnostic["snapshot_provider"] = (
                f"{type(error).__name__}:{code}:{type(cause).__name__}:{marker}"
            )
            raise


def _lifecycle(
    client: TestClient,
    headers: dict[str, str],
    instance_id: UUID,
    action: str,
) -> None:
    response = client.post(
        f"/api/instances/{instance_id}/{action}", json={}, headers=headers
    )
    _require(response.status_code == 200, f"{action} failed")


def _copy_source(
    client: TestClient,
    headers: dict[str, str],
    instance_id: UUID,
    source_id: str,
) -> dict[str, Any]:
    response = client.post(
        f"/api/instances/{instance_id}/development-sources/{source_id}/copy-sync",
        json={},
        headers=headers,
    )
    _require(response.status_code == 202, "copy/sync was not queued")
    result = _wait(client, response.json()["status_url"])
    _require(result["state"] == "succeeded", "copy/sync failed")
    return result


def _enable_source(
    client: TestClient,
    headers: dict[str, str],
    instance_id: UUID,
    source_id: str,
) -> None:
    _inventory(client, instance_id)
    response = client.post(
        f"/api/instances/{instance_id}/development-sources/{source_id}/enable",
        json={},
        headers=headers,
    )
    result = response.json()
    _require(
        response.status_code == 200
        and result["result_classification"] in {"enabled", "already_enabled"}
        and result["result_enabled"] is True,
        f"source enable failed: {result!r}",
    )
    inventory = _inventory(client, instance_id)
    item = _item(inventory, result["addon_id"])
    _require(
        item is not None and item["enabled"] is True,
        "source enablement was not visible in live inventory",
    )


def _snapshot(
    client: TestClient,
    headers: dict[str, str],
    instance_id: UUID,
    readiness_diagnostic: dict[str, str],
) -> dict[str, Any]:
    response = client.post(
        f"/api/instances/{instance_id}/snapshots",
        json={"label": "Repository replacement protection"},
        headers=headers,
    )
    _require(response.status_code == 202, "Snapshot was not queued")
    operation = response.json()["operation"]
    status_url = (
        f"/api/instances/{instance_id}/snapshot-create-operations/"
        f"{operation['operation_id']}"
    )
    result = _wait(client, status_url)
    diagnostic = readiness_diagnostic.pop(str(instance_id), "not_recorded")
    exception_diagnostic = readiness_diagnostic.pop(
        "snapshot_exception", "not_recorded"
    )
    provider_diagnostic = readiness_diagnostic.pop("snapshot_provider", "not_recorded")
    _require(
        result["state"] == "succeeded",
        f"Snapshot creation failed: {result!r}; readiness={diagnostic}; "
        f"exception={exception_diagnostic}; provider={provider_diagnostic}",
    )
    return {"snapshot_id": response.json()["snapshot_id"], "operation": result}


def _replace(
    client: TestClient,
    headers: dict[str, str],
    instance_id: UUID,
    artifact_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    prepared = client.post(
        f"/api/instances/{instance_id}/repository-artifacts/{artifact_id}/"
        "replacement-plans",
        json={},
        headers=headers,
    )
    _require(prepared.status_code == 201, f"replacement plan failed: {prepared.text}")
    plan = prepared.json()
    approved = client.post(
        f"/api/instances/{instance_id}/replacement-plans/{plan['plan_id']}/"
        "approve-and-execute",
        json={"confirmed": True},
        headers=headers,
    )
    _require(approved.status_code == 202, "replacement approval failed")
    return plan, _wait(client, approved.json()["status_url"])


def _helpers() -> ModuleType:
    path = Path(__file__).with_name("validate-phase8-copied-enablement.py")
    specification = importlib.util.spec_from_file_location("phase8_helpers", path)
    if specification is None or specification.loader is None:
        raise RuntimeError("validation helpers are unavailable")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _descriptor(addon_id: str, name: str) -> str:
    return (
        f'<addon id="{addon_id}" name="{name}" version="1.0.0">'
        '<extension point="xbmc.python.pluginsource" library="default.py">'
        "<provides>video</provides></extension>"
        '<extension point="xbmc.addon.metadata" /></addon>'
    )


def _archive(addon_id: str, name: str) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(f"{addon_id}/addon.xml", _descriptor(addon_id, name))
        archive.writestr(f"{addon_id}/default.py", "# deliberately passive\n")
    return output.getvalue()


class SyntheticRepositoryTransport:
    """In-process transport fixture; production HTTPS policy stays unchanged."""

    def get(
        self, url: str, *, maximum_body: int, require_index: bool = True
    ) -> RepositoryHttpResponse:
        del maximum_body
        if require_index:
            body = (
                "<addons>"
                + _descriptor(BASIC_ID, "Repository Basic")
                + _descriptor(COLLISION_ID, "Repository Collision")
                + "</addons>"
            ).encode()
            return RepositoryHttpResponse(200, "application/xml", body, url)
        addon_id = COLLISION_ID if COLLISION_ID in url else BASIC_ID
        name = (
            "Repository Collision" if addon_id == COLLISION_ID else "Repository Basic"
        )
        return RepositoryHttpResponse(
            200, "application/zip", _archive(addon_id, name), url
        )


def _login(client: TestClient) -> dict[str, str]:
    response = client.post(
        "/login",
        data={"password": PASSWORD},
        headers={"Origin": ORIGIN},
        follow_redirects=False,
    )
    _require(response.status_code == 303, "Web login failed")
    match = re.search(r'name="kodi-lab-csrf" content="([^"]+)"', client.get("/").text)
    _require(match is not None, "CSRF token was not rendered")
    assert match is not None
    return {"Origin": ORIGIN, "X-Kodi-Lab-CSRF": match.group(1)}


def _wait(client: TestClient, status_url: str) -> dict[str, Any]:
    last: dict[str, Any] = {}
    for _ in range(1800):
        response = client.get(status_url)
        _require(response.status_code == 200, "operation status failed")
        last = response.json()
        if last["state"] in {"succeeded", "failed", "reconciliation_required"}:
            return last
        time.sleep(0.05)
    raise RuntimeError(f"operation did not terminalize: {last!r}")


def _post_operation(
    client: TestClient, headers: dict[str, str], path: str
) -> dict[str, Any]:
    response = client.post(path, json={}, headers=headers)
    _require(response.status_code == 202, f"operation rejected: {response.text[:256]}")
    return _wait(client, response.json()["status_url"])


def _inventory(client: TestClient, instance_id: UUID) -> dict[str, Any]:
    last: dict[str, Any] = {}
    for _ in range(600):
        response = client.get(f"/api/instances/{instance_id}/addons")
        _require(response.status_code == 200, "inventory query failed")
        last = response.json()
        if last["query_status"] == "available":
            return last
        time.sleep(0.2)
    raise RuntimeError(f"inventory unavailable: {last!r}")


def _item(inventory: dict[str, Any], addon_id: str) -> dict[str, Any] | None:
    return next(
        (item for item in inventory["items"] if item["addon_id"] == addon_id), None
    )


def _security_rejections() -> dict[str, str]:
    outcomes: dict[str, str] = {}
    for name, answers in {
        "private_target": ("127.0.0.1",),
        "mixed_dns": ("8.8.8.8", "127.0.0.1"),
    }.items():
        transport = PinnedHttpsTransport(
            resolver=lambda _hostname, values=answers: values,
            connector=lambda *_args: (_ for _ in ()).throw(
                RuntimeError("connector must not run")
            ),
        )
        try:
            transport.get("https://repository.example/addons.xml")
        except AddonRepositoryError as error:
            outcomes[name] = error.code

    class RedirectSocket:
        value = bytearray(
            b"HTTP/1.1 302 Found\r\n"
            b"Location: https://other.example/addons.xml\r\n"
            b"Content-Length: 0\r\nContent-Type: text/xml\r\n\r\n"
        )

        def settimeout(self, _timeout: float) -> None:
            pass

        def sendall(self, _value: bytes) -> None:
            pass

        def recv(self, count: int) -> bytes:
            result = bytes(self.value[:count])
            del self.value[:count]
            return result

        def close(self) -> None:
            pass

    try:
        PinnedHttpsTransport(
            resolver=lambda _host: ("8.8.8.8",),
            connector=lambda *_args: RedirectSocket(),
        ).get("https://repository.example/addons.xml")
    except AddonRepositoryError as error:
        outcomes["cross_origin_redirect"] = error.code
    try:
        parse_repository_metadata(
            b"x" * (8 * 1024 * 1024 + 1),
            request_path="/addons.xml",
            content_type="application/xml",
        )
    except AddonRepositoryError as error:
        outcomes["oversized_metadata"] = error.code
    _require(len(outcomes) == 4, "unsafe network harness did not reject every case")
    return outcomes


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--evidence-path",
        type=Path,
        default=Path("evidence/kodi-lab-phase8-repository-evidence.json"),
    )
    arguments = parser.parse_args()
    helper = _helpers()
    _require(os.geteuid() == 0, "live validation must run as root")
    project_root = Path(__file__).resolve().parents[1]
    protected_before = helper._protected_fingerprint()
    _require(
        protected_before["production_data_root_absent"] is True
        and protected_before["production_runtime_root_absent"] is True,
        "production roots were present before validation",
    )
    _require(helper._managed_network_count() == 0, "managed network was not empty")
    instances = {str(item["release"]): uuid4() for item in helper.RUNTIMES}
    instances["21.3-default"] = uuid4()
    scenarios = {name: uuid4() for name in ("a", "topology", "drift", "failure")}
    instance_set = set(instances.values()) | set(scenarios.values())
    evidence: dict[str, Any] = {}
    try:
        with tempfile.TemporaryDirectory(
            prefix="kodi-lab-phase8-repository-", dir="/var/tmp"
        ) as temporary:
            validation_root = Path(temporary)
            data_root = validation_root / "data"
            (data_root / "database").mkdir(parents=True)
            runtime_root = validation_root / "runtime"
            runtime_root.mkdir(mode=0o700)
            development_root = validation_root / "development"
            source_root = development_root / "collision-source"
            source_root.mkdir(parents=True)
            (source_root / "addon.xml").write_text(
                _descriptor(COLLISION_ID, "Protected Collision"), encoding="utf-8"
            )
            (source_root / "default.py").write_text("# passive\n", encoding="utf-8")
            os.environ["KODI_LAB_DATA_ROOT"] = str(data_root)
            os.environ["KODI_LAB_RUNTIME_ROOT"] = str(runtime_root)
            os.environ["KODI_LAB_DEVELOPMENT_ROOT"] = str(development_root)
            settings = Settings(_env_file=None)
            command.upgrade(Config(str(project_root / "alembic.ini")), "head")
            engine = create_database_engine(settings.database_url)
            factory = create_session_factory(engine)
            helper._seed(factory, instances)
            deep_revision = next(
                str(item["revision"])
                for item in helper.RUNTIMES
                if item["release"] == DEEP_RELEASE
            )
            with factory.begin() as session:
                for name, instance_id in scenarios.items():
                    helper._add_instance(
                        session,
                        instance_id,
                        f"21.3 replacement {name}",
                        deep_revision,
                    )
            adapter = DockerManagedContainerAdapter()
            resolver = SqliteAddonZipInstallationResolver(factory, data_root=data_root)
            development_resolver = SqliteDevelopmentSourceDeploymentResolver(
                factory, development_root=development_root, data_root=data_root
            )
            snapshot_gid = grp.getgrnam("kodi-lab").gr_gid
            readiness_diagnostic: dict[str, str] = {}
            registry = build_foundation_registry(
                adapter,
                provisioning_resolver=SqliteProvisioningResolver(factory),
                provisioning_provider=adapter,
                deprovisioning_provider=adapter,
                revision_resolver=SqliteManagedInstanceRevisionResolver(factory),
                runtime_observation_resolver=SqliteRuntimeObservationResolver(factory),
                runtime_observation_provider=adapter,
                snapshot_capture_resolver=SqliteSnapshotCaptureResolver(
                    factory,
                    data_root=data_root,
                    owner_gid=snapshot_gid,
                ),
                snapshot_capture_provider=_DiagnosticSnapshotProvider(
                    adapter, readiness_diagnostic
                ),
                snapshot_restore_resolver=SqliteSnapshotRestoreResolver(
                    factory,
                    data_root=data_root,
                    owner_gid=snapshot_gid,
                ),
                snapshot_restore_provider=adapter,
                addon_zip_installation_resolver=resolver,
                addon_zip_installation_provider=adapter,
                development_source_deployment_resolver=development_resolver,
                development_source_deployment_provider=adapter,
            )
            transport = SyntheticRepositoryTransport()
            refreshes = AddonRepositoryRefreshCoordinator(factory, transport=transport)
            fetches = AddonRepositoryFetchCoordinator(
                factory, data_root=data_root, transport=transport
            )
            with helper._broker_server(validation_root, registry) as broker:
                auth = WebAuthenticationService(factory)
                auth.set_password(PASSWORD)
                failure_injection = {"enabled": False}

                def replacement_service(lease: Any) -> RepositoryReplacementService:
                    return RepositoryReplacementService(
                        factory,
                        broker,
                        app.state.kodi_lab.repository_replacements,
                        lease,
                        after_application_hook=(
                            (lambda: (_ for _ in ()).throw(RuntimeError("injected")))
                            if failure_injection["enabled"]
                            else None
                        ),
                    )

                def snapshot_service(lease: Any) -> DisposableSnapshotCreationService:
                    def readiness_factory(
                        readiness_broker: Any, profile: Any
                    ) -> _RecordingReadiness:
                        return _RecordingReadiness(
                            KodiReadinessCoordinator(readiness_broker, profile),
                            lease.managed_instance_id,
                            readiness_diagnostic,
                        )

                    return _DiagnosticSnapshotCreationService(
                        factory,
                        broker,
                        app.state.kodi_lab.snapshot_operations,
                        lease,
                        diagnostic=readiness_diagnostic,
                        data_root=data_root,
                        owner_gid=snapshot_gid,
                        readiness_factory=readiness_factory,
                    )

                app = create_app(
                    settings=settings,
                    session_factory=factory,
                    broker=broker,
                    readiness=ReadinessTaskRegistry(max_workers=6),
                    auth=auth,
                    addon_repository_refresh_coordinator=refreshes,
                    addon_repository_fetch_coordinator=fetches,
                    repository_replacement_service_factory=replacement_service,
                    snapshot_service_factory=snapshot_service,
                )
                with TestClient(app, base_url=ORIGIN) as client:
                    headers = _login(client)
                    for instance_id in instances.values():
                        _require(
                            client.post(
                                f"/api/instances/{instance_id}/provision",
                                json={},
                                headers=headers,
                            ).status_code
                            == 200,
                            "provision failed",
                        )
                        _require(
                            client.post(
                                f"/api/instances/{instance_id}/start",
                                json={},
                                headers=headers,
                            ).status_code
                            == 200,
                            "start failed",
                        )
                        _inventory(client, instance_id)
                    registered = client.post(
                        "/api/addon-repositories",
                        json={
                            "logical_name": "Controlled synthetic repository",
                            "index_url": "https://repository.example/addons.xml",
                        },
                        headers=headers,
                    )
                    _require(registered.status_code == 201, "registration failed")
                    repository_id = registered.json()["repository_id"]
                    for instance_id in instances.values():
                        attached = client.post(
                            f"/api/instances/{instance_id}/addon-repositories/"
                            f"{repository_id}/attach",
                            json={},
                            headers=headers,
                        )
                        _require(attached.status_code == 200, "attachment failed")
                    refresh = _post_operation(
                        client,
                        headers,
                        f"/api/addon-repositories/{repository_id}/refresh",
                    )
                    _require(
                        refresh["state"] == "succeeded",
                        "refresh failed: "
                        f"{refresh.get('failure_reason', refresh['state'])}",
                    )
                    candidates = client.get(
                        f"/api/addon-repositories/{repository_id}/candidates"
                    ).json()
                    by_id = {item["addon_id"]: item for item in candidates}
                    fetched: dict[str, dict[str, Any]] = {}
                    for addon_id in (BASIC_ID, COLLISION_ID):
                        candidate = by_id[addon_id]
                        result = _post_operation(
                            client,
                            headers,
                            f"/api/addon-repositories/{repository_id}/candidates/"
                            f"{candidate['candidate_id']}/fetch",
                        )
                        _require(
                            result["state"] == "succeeded",
                            "package fetch failed: "
                            f"{result.get('failure_reason', result['state'])}",
                        )
                        fetched[addon_id] = result
                    cross_version: list[dict[str, Any]] = []
                    for runtime in helper.RUNTIMES:
                        release = str(runtime["release"])
                        instance_id = instances[release]
                        result = _post_operation(
                            client,
                            headers,
                            f"/api/instances/{instance_id}/repository-artifacts/"
                            f"{fetched[BASIC_ID]['artifact_id']}/install",
                        )
                        item = _item(_inventory(client, instance_id), BASIC_ID)
                        _require(
                            result["state"] == "succeeded"
                            and result["recognition_enabled"] is False
                            and item is not None
                            and item["version"] == "1.0.0"
                            and item["enabled"] is False
                            and item["provenance"]["classification"]
                            == "prior_verified_repository_install",
                            f"repository install failed for {release}",
                        )
                        cross_version.append(
                            {
                                "kodi_release": release,
                                "instance_uuid": str(instance_id),
                                "runtime_revision_uuid": runtime["revision"],
                                "installation_operation_uuid": result["operation_id"],
                                "recognition": "exact_id_version_type_disabled",
                                "provenance": "prior_verified_repository_install",
                            }
                        )
                    collision_instance = instances[DEEP_RELEASE]
                    source = client.post(
                        "/api/development-sources",
                        json={
                            "logical_name": "Protected collision source",
                            "relative_locator": "collision-source",
                        },
                        headers=headers,
                    )
                    _require(source.status_code == 201, "source registration failed")
                    source_id = source.json()["id"]
                    attached = client.post(
                        f"/api/instances/{collision_instance}/development-sources/"
                        f"{source_id}/attach",
                        json={},
                        headers=headers,
                    )
                    _require(attached.status_code == 200, "source attachment failed")
                    collision = _post_operation(
                        client,
                        headers,
                        f"/api/instances/{collision_instance}/repository-artifacts/"
                        f"{fetched[COLLISION_ID]['artifact_id']}/install",
                    )
                    _require(
                        collision["state"] == "failed"
                        and collision["failure_reason"]
                        == "protected_development_source_collision"
                        and _item(_inventory(client, collision_instance), COLLISION_ID)
                        is None,
                        "DevelopmentSource collision did not block",
                    )
                    replacement_cross_version: list[dict[str, Any]] = []
                    bound_snapshots: dict[str, dict[str, Any]] = {}
                    for runtime in helper.RUNTIMES:
                        release = str(runtime["release"])
                        instance_id = instances[release]
                        if instance_id != collision_instance:
                            attached = client.post(
                                f"/api/instances/{instance_id}/development-sources/"
                                f"{source_id}/attach",
                                json={},
                                headers=headers,
                            )
                            _require(
                                attached.status_code == 200,
                                "replacement source attachment failed",
                            )
                        _lifecycle(client, headers, instance_id, "stop")
                        deployment = _copy_source(
                            client, headers, instance_id, source_id
                        )
                        _lifecycle(client, headers, instance_id, "start")
                        _enable_source(client, headers, instance_id, source_id)
                        bound = _snapshot(
                            client, headers, instance_id, readiness_diagnostic
                        )
                        bound_snapshots[release] = bound
                        plan, replaced = _replace(
                            client,
                            headers,
                            instance_id,
                            fetched[COLLISION_ID]["artifact_id"],
                        )
                        inventory = _inventory(client, instance_id)
                        item = _item(inventory, COLLISION_ID)
                        _require(
                            plan["incumbent_class"] == "verified_copied"
                            and plan["snapshot_ready"] is True
                            and replaced["state"] == "succeeded"
                            and replaced["recognition_enabled"] is True
                            and item is not None
                            and item["enabled"] is True
                            and item["repository_override_active"] is True
                            and item["provenance"]["classification"]
                            == "prior_verified_repository_install",
                            f"replacement failed for {release}: "
                            f"operation={replaced!r}; item={item!r}",
                        )
                        replacement_cross_version.append(
                            {
                                "kodi_release": release,
                                "instance_uuid": str(instance_id),
                                "runtime_revision_uuid": runtime["revision"],
                                "deployment_operation_uuid": deployment["operation_id"],
                                "snapshot_uuid": bound["snapshot_id"],
                                "approval_uuid": plan["plan_id"],
                                "replacement_operation_uuid": replaced["operation_id"],
                                "incumbent_class": "verified_copied",
                                "incoming_version": plan["incoming_version"],
                                "enabled_preserved": True,
                                "topology": "overwrite_add_only",
                                "override_active": True,
                            }
                        )
                    blocked_copy = client.post(
                        f"/api/instances/{collision_instance}/development-sources/"
                        f"{source_id}/copy-sync",
                        json={},
                        headers=headers,
                    )
                    _require(
                        blocked_copy.status_code == 409
                        and blocked_copy.json()["error"]["code"]
                        == "repository_override_active",
                        "active repository override did not block copy/sync",
                    )
                    deep_snapshot_id = bound_snapshots[DEEP_RELEASE]["snapshot_id"]
                    restore = client.post(
                        f"/api/instances/{collision_instance}/snapshots/"
                        f"{deep_snapshot_id}/restore",
                        json={"confirmed": True},
                        headers=headers,
                    )
                    _require(
                        restore.status_code == 202, "Snapshot restore was rejected"
                    )
                    restore_status = _wait(
                        client,
                        f"/api/instances/{collision_instance}/"
                        "snapshot-restore-operations/"
                        f"{restore.json()['operation']['operation_id']}",
                    )
                    restored_item = _item(
                        _inventory(client, collision_instance), COLLISION_ID
                    )
                    _require(
                        restore_status["state"] == "succeeded"
                        and restored_item is not None
                        and restored_item["enabled"] is True,
                        "Snapshot restore did not recover enabled incumbent",
                    )
                    deep_container_id = next(
                        item.container_id
                        for item in broker.managed_container_inventory().containers
                        if item.managed_instance_id == str(collision_instance)
                    )
                    docker_api = docker.APIClient()
                    try:
                        restored_stream, _ = docker_api.get_archive(
                            deep_container_id,
                            f"/home/kodi/.kodi/addons/{COLLISION_ID}",
                        )
                        restored_destination = capture_destination_archive(
                            restored_stream, root_name=COLLISION_ID
                        )
                    finally:
                        docker_api.close()
                    with factory() as session:
                        restored_source_evidence = (
                            DevelopmentSourceDeploymentRepository(session).get(
                                collision_instance, UUID(source_id)
                            )
                        )
                    _require(
                        restored_source_evidence is not None
                        and restored_source_evidence.confidence.value == "stale"
                        and restored_destination.digest
                        == restored_source_evidence.verified_destination_digest,
                        "Snapshot did not restore the exact prior source tree",
                    )

                    def attach_scenario(instance_id: UUID) -> None:
                        _lifecycle(client, headers, instance_id, "provision")
                        _lifecycle(client, headers, instance_id, "start")
                        repository_attachment = client.post(
                            f"/api/instances/{instance_id}/addon-repositories/"
                            f"{repository_id}/attach",
                            json={},
                            headers=headers,
                        )
                        source_attachment = client.post(
                            f"/api/instances/{instance_id}/development-sources/"
                            f"{source_id}/attach",
                            json={},
                            headers=headers,
                        )
                        _require(
                            repository_attachment.status_code == 200
                            and source_attachment.status_code == 200,
                            "scenario attachment failed",
                        )
                        _inventory(client, instance_id)

                    attach_scenario(scenarios["a"])
                    plan_a, result_a = _replace(
                        client,
                        headers,
                        scenarios["a"],
                        fetched[COLLISION_ID]["artifact_id"],
                    )
                    _require(
                        plan_a["incumbent_class"] == "attached_unmaterialized"
                        and result_a["state"] == "succeeded",
                        f"Class A replacement execution failed: {result_a!r}",
                    )
                    item_a = _item(_inventory(client, scenarios["a"]), COLLISION_ID)
                    _require(
                        result_a["recognition_enabled"] is False
                        and item_a is not None
                        and item_a["enabled"] is False
                        and item_a["repository_override_active"] is True,
                        "Class A replacement failed",
                    )
                    _lifecycle(client, headers, scenarios["a"], "stop")
                    _lifecycle(client, headers, scenarios["a"], "deprovision")

                    obsolete = source_root / "obsolete.txt"
                    obsolete.write_text("must not be deleted\n", encoding="utf-8")
                    attach_scenario(scenarios["topology"])
                    _lifecycle(client, headers, scenarios["topology"], "stop")
                    _copy_source(client, headers, scenarios["topology"], source_id)
                    _lifecycle(client, headers, scenarios["topology"], "start")
                    _enable_source(client, headers, scenarios["topology"], source_id)
                    _snapshot(
                        client,
                        headers,
                        scenarios["topology"],
                        readiness_diagnostic,
                    )
                    topology = client.post(
                        f"/api/instances/{scenarios['topology']}/repository-artifacts/"
                        f"{fetched[COLLISION_ID]['artifact_id']}/replacement-plans",
                        json={},
                        headers=headers,
                    )
                    _require(
                        topology.status_code == 422
                        and topology.json()["error"]["code"]
                        == "unsupported_replacement_topology",
                        "deleting topology was not rejected",
                    )
                    _lifecycle(client, headers, scenarios["topology"], "stop")
                    _lifecycle(client, headers, scenarios["topology"], "deprovision")
                    obsolete.unlink()

                    attach_scenario(scenarios["drift"])
                    drift_plan_response = client.post(
                        f"/api/instances/{scenarios['drift']}/repository-artifacts/"
                        f"{fetched[COLLISION_ID]['artifact_id']}/replacement-plans",
                        json={},
                        headers=headers,
                    )
                    _require(
                        drift_plan_response.status_code == 201, "drift plan failed"
                    )
                    drift_plan = drift_plan_response.json()
                    detached = client.post(
                        f"/api/instances/{scenarios['drift']}/development-sources/"
                        f"{source_id}/detach",
                        json={},
                        headers=headers,
                    )
                    reattached = client.post(
                        f"/api/instances/{scenarios['drift']}/development-sources/"
                        f"{source_id}/attach",
                        json={},
                        headers=headers,
                    )
                    _require(
                        detached.status_code == 200 and reattached.status_code == 200,
                        "controlled attachment drift failed",
                    )
                    drift_approval = client.post(
                        f"/api/instances/{scenarios['drift']}/replacement-plans/"
                        f"{drift_plan['plan_id']}/approve-and-execute",
                        json={"confirmed": True},
                        headers=headers,
                    )
                    _require(drift_approval.status_code == 202, "drift approval failed")
                    drift_result = _wait(client, drift_approval.json()["status_url"])
                    _require(
                        drift_result["state"] == "failed"
                        and drift_result["application_started"] is False,
                        "state drift was not invalidated before application",
                    )
                    _lifecycle(client, headers, scenarios["drift"], "stop")
                    _lifecycle(client, headers, scenarios["drift"], "deprovision")

                    attach_scenario(scenarios["failure"])
                    _lifecycle(client, headers, scenarios["failure"], "stop")
                    _copy_source(client, headers, scenarios["failure"], source_id)
                    _lifecycle(client, headers, scenarios["failure"], "start")
                    _enable_source(client, headers, scenarios["failure"], source_id)
                    failure_snapshot = _snapshot(
                        client,
                        headers,
                        scenarios["failure"],
                        readiness_diagnostic,
                    )
                    failure_injection["enabled"] = True
                    failure_plan, failure_result = _replace(
                        client,
                        headers,
                        scenarios["failure"],
                        fetched[COLLISION_ID]["artifact_id"],
                    )
                    failure_injection["enabled"] = False
                    failure_container = next(
                        item
                        for item in broker.managed_container_inventory().containers
                        if item.managed_instance_id == str(scenarios["failure"])
                    )
                    _require(
                        failure_result["state"] == "reconciliation_required"
                        and failure_result["application_started"] is True
                        and failure_container.runtime_state.value == "stopped",
                        "post-application failure did not reconcile stopped",
                    )
                with factory() as session:
                    basic_artifact = AddonZipArtifactRepository(session).get(
                        UUID(str(fetched[BASIC_ID]["artifact_id"]))
                    )
                    deep_evidence = AddonZipInstallationEvidenceRepository(session).get(
                        instances[DEEP_RELEASE], BASIC_ID
                    )
                    attachment = ManagedInstanceAddonRepositorySourceRepository(
                        session
                    ).get(instances[DEEP_RELEASE], UUID(repository_id))
                    migration_head = session.scalar(
                        text("SELECT version_num FROM alembic_version")
                    )
                    restored_repository_evidence = (
                        AddonZipInstallationEvidenceRepository(session).get(
                            collision_instance, COLLISION_ID
                        )
                    )
                    failure_repository_evidence = (
                        AddonZipInstallationEvidenceRepository(session).get(
                            scenarios["failure"], COLLISION_ID
                        )
                    )
                    failure_source_evidence = DevelopmentSourceDeploymentRepository(
                        session
                    ).get(scenarios["failure"], UUID(source_id))
                    failure_operation = RepositoryReplacementOperationRepository(
                        session
                    ).get(UUID(failure_result["operation_id"]))
                    failure_persisted_plan = RepositoryReplacementPlanRepository(
                        session
                    ).get(UUID(failure_plan["plan_id"]))
                    drift_persisted_plan = RepositoryReplacementPlanRepository(
                        session
                    ).get(UUID(drift_plan["plan_id"]))
                    topology_plan = RepositoryReplacementPlanRepository(
                        session
                    ).list_for_original_instance(scenarios["topology"])[0]
                _require(
                    basic_artifact is not None
                    and basic_artifact.origin.value == "repository_download"
                    and deep_evidence is not None
                    and deep_evidence.artifact_origin.value == "repository_download"
                    and attachment is not None,
                    "repository lineage persistence was incomplete",
                )
                _require(
                    restored_repository_evidence is not None
                    and restored_repository_evidence.confidence.value == "stale"
                    and failure_repository_evidence is not None
                    and failure_repository_evidence.confidence.value == "unknown"
                    and failure_source_evidence is not None
                    and failure_source_evidence.confidence.value == "unknown"
                    and failure_operation is not None
                    and failure_operation.reconciliation_required
                    and failure_persisted_plan is not None
                    and failure_persisted_plan.state.value == "consumed"
                    and drift_persisted_plan is not None
                    and drift_persisted_plan.state.value == "invalidated"
                    and topology_plan.state.value == "invalidated"
                    and (source_root / "default.py").read_text(encoding="utf-8")
                    == "# passive\n",
                    "replacement evidence transitions were incomplete",
                )
                docker_api = docker.APIClient()
                try:
                    managed_user_profiles = [
                        docker_api.inspect_container(item.container_id)["Config"][
                            "User"
                        ]
                        for item in broker.managed_container_inventory().containers
                        if UUID(item.managed_instance_id) in instance_set
                    ]
                finally:
                    docker_api.close()
                _require(
                    bool(managed_user_profiles)
                    and all(value == "1000:1000" for value in managed_user_profiles),
                    "managed container runtime user was not exact",
                )
                root_owned_repository_files = [
                    path
                    for path in project_root.rglob("*")
                    if not path.is_symlink() and path.lstat().st_uid == 0
                ]
                _require(
                    not root_owned_repository_files,
                    "live validation created root-owned repository content",
                )
                evidence = {
                    "schema": "kodi-lab-phase8-repository-replacement-live-v1",
                    "migration_head": migration_head,
                    "repository": {
                        "repository_uuid": repository_id,
                        "logical_name": "Controlled synthetic repository",
                        "registration": "succeeded",
                        "attachment": "succeeded",
                    },
                    "refresh": {
                        "operation_uuid": refresh["operation_id"],
                        "snapshot_uuid": refresh["snapshot_id"],
                        "classification": "succeeded",
                        "candidate_count": len(candidates),
                        "transport": "isolated_injected_no_internet",
                    },
                    "fetch": {
                        "operation_uuid": fetched[BASIC_ID]["operation_id"],
                        "candidate_uuid": by_id[BASIC_ID]["candidate_id"],
                        "artifact_uuid": fetched[BASIC_ID]["artifact_id"],
                        "classification": "succeeded_repository_download",
                    },
                    "cross_version": cross_version,
                    "replacement_cross_version": replacement_cross_version,
                    "live_a": {
                        "instance_uuid": str(scenarios["a"]),
                        "approval_uuid": plan_a["plan_id"],
                        "operation_uuid": result_a["operation_id"],
                        "incumbent_class": "attached_unmaterialized",
                        "incoming_version": plan_a["incoming_version"],
                        "snapshot_qualification": "not_required",
                        "enabled_preserved": "recognized_disabled",
                        "topology": "absent_materialization",
                        "attachment_retained": True,
                        "override_active": True,
                    },
                    "live_b": {
                        "classification": "verified_copied_cross_version",
                        "enabled_preserved": True,
                        "dependency_state_preserved": True,
                        "source_tree_unchanged": True,
                        "source_evidence": "stale_after_success",
                        "repository_evidence": "verified_after_success",
                        "repository_provenance_only": True,
                    },
                    "live_c": {
                        "classification": "repository_override_active",
                        "destination_mutation": False,
                    },
                    "live_d": {
                        "snapshot_uuid": deep_snapshot_id,
                        "restore_operation_uuid": restore_status["operation_id"],
                        "prior_tree_restored": True,
                        "enabled_restored": True,
                        "attachment_retained": True,
                        "repository_evidence": "stale",
                        "development_source_evidence": "stale_not_reattributed",
                    },
                    "live_e": {
                        "plan_uuid": str(topology_plan.id),
                        "classification": "unsupported_replacement_topology",
                        "topology": "deletion_required",
                        "destination_mutation": False,
                    },
                    "live_f": {
                        "plan_uuid": drift_plan["plan_id"],
                        "operation_uuid": drift_result["operation_id"],
                        "classification": "attachment_state_changed",
                        "application_started": False,
                        "approval_state": "invalidated",
                    },
                    "live_g": {
                        "plan_uuid": failure_plan["plan_id"],
                        "operation_uuid": failure_result["operation_id"],
                        "snapshot_uuid": failure_snapshot["snapshot_id"],
                        "classification": "reconciliation_required",
                        "application_started": True,
                        "instance_state": "stopped",
                        "repository_evidence": "unknown",
                        "development_source_evidence": "unknown",
                        "retry": False,
                        "automatic_restore": False,
                    },
                    "development_source_collision": {
                        "instance_uuid": str(collision_instance),
                        "candidate_uuid": by_id[COLLISION_ID]["candidate_id"],
                        "artifact_uuid": fetched[COLLISION_ID]["artifact_id"],
                        "operation_uuid": collision["operation_id"],
                        "classification": "protected_development_source_collision",
                        "blocked_before_lifecycle_mutation": True,
                        "destination_and_inventory_absent": True,
                    },
                    "unsafe_network": _security_rejections(),
                    "repository_enable_smoke": "deferred_authority_not_widened",
                    "privilege_isolation": {
                        "routine_checks_user": "non_root_separate_gate",
                        "validator_bytecode_suppressed_before_project_imports": True,
                        "managed_config_user": "exact_non_root_profile_uid_gid",
                        "managed_container_count_checked": len(managed_user_profiles),
                        "root_owned_repository_files": 0,
                    },
                }
            engine.dispose()
    finally:
        helper._cleanup_created(instance_set)
    _require(helper._managed_network_count() == 0, "managed network was not cleaned")
    protected_after = helper._protected_fingerprint()
    changed = sorted(
        key
        for key in protected_before
        if protected_before[key] != protected_after.get(key)
    )
    _require(not changed, f"protected resource categories changed: {changed!r}")
    evidence["protected_isolation"] = {
        "protected_containers_unchanged": True,
        "protected_networks_unchanged": True,
        "legacy_service_unchanged": True,
        "legacy_socket_unchanged": True,
        "production_roots_absent": True,
        "temporary_managed_containers_removed": True,
        "managed_network_empty": True,
        "temporary_repository_storage_cleaned": True,
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
