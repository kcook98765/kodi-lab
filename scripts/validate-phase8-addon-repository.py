#!/usr/bin/env python3
"""Run isolated live validation for Phase 8 repository URL testing."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import io
import json
import os
import re
import tempfile
import time
import zipfile
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import UUID, uuid4

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import text

from kodi_lab.application.addon_repositories import (
    AddonRepositoryFetchCoordinator,
    AddonRepositoryRefreshCoordinator,
)
from kodi_lab.application.readiness_tasks import ReadinessTaskRegistry
from kodi_lab.auth import WebAuthenticationService
from kodi_lab.broker.addon_zips import SqliteAddonZipInstallationResolver
from kodi_lab.broker.docker_inventory import DockerManagedContainerAdapter
from kodi_lab.broker.operations import build_foundation_registry
from kodi_lab.broker.provisioning import (
    SqliteManagedInstanceRevisionResolver,
    SqliteProvisioningResolver,
    SqliteRuntimeObservationResolver,
)
from kodi_lab.config import Settings
from kodi_lab.development import (
    AddonRepositoryError,
    PinnedHttpsTransport,
    RepositoryHttpResponse,
    parse_repository_metadata,
)
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.storage.repositories import (
    AddonZipArtifactRepository,
    AddonZipInstallationEvidenceRepository,
    ManagedInstanceAddonRepositorySourceRepository,
)
from kodi_lab.web.app import create_app

PASSWORD = "phase8-repository-live-validation"
ORIGIN = "http://127.0.0.1"
DEEP_RELEASE = "21.3"
BASIC_ID = "plugin.video.kodi-lab-repository-basic"
COLLISION_ID = "plugin.video.kodi-lab-repository-collision"


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
    instance_set = set(instances.values())
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
            adapter = DockerManagedContainerAdapter()
            resolver = SqliteAddonZipInstallationResolver(factory, data_root=data_root)
            registry = build_foundation_registry(
                adapter,
                provisioning_resolver=SqliteProvisioningResolver(factory),
                provisioning_provider=adapter,
                deprovisioning_provider=adapter,
                revision_resolver=SqliteManagedInstanceRevisionResolver(factory),
                runtime_observation_resolver=SqliteRuntimeObservationResolver(factory),
                runtime_observation_provider=adapter,
                addon_zip_installation_resolver=resolver,
                addon_zip_installation_provider=adapter,
            )
            transport = SyntheticRepositoryTransport()
            refreshes = AddonRepositoryRefreshCoordinator(factory, transport=transport)
            fetches = AddonRepositoryFetchCoordinator(
                factory, data_root=data_root, transport=transport
            )
            with helper._broker_server(validation_root, registry) as broker:
                auth = WebAuthenticationService(factory)
                auth.set_password(PASSWORD)
                app = create_app(
                    settings=settings,
                    session_factory=factory,
                    broker=broker,
                    readiness=ReadinessTaskRegistry(max_workers=6),
                    auth=auth,
                    addon_repository_refresh_coordinator=refreshes,
                    addon_repository_fetch_coordinator=fetches,
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
                _require(
                    basic_artifact is not None
                    and basic_artifact.origin.value == "repository_download"
                    and deep_evidence is not None
                    and deep_evidence.artifact_origin.value == "repository_download"
                    and attachment is not None,
                    "repository lineage persistence was incomplete",
                )
                evidence = {
                    "schema": "kodi-lab-phase8-repository-live-v1",
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
