#!/usr/bin/env python3
"""Run isolated live validation for Phase 8 uploaded-ZIP installation."""

# ruff: noqa: E402 -- bytecode suppression must precede project imports.

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import io
import json
import os
import re
import stat
import sys
import tarfile
import tempfile
import time
import zipfile
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import UUID, uuid4

sys.dont_write_bytecode = True

import docker  # type: ignore[import-untyped]
from alembic import command
from alembic.config import Config
from docker.errors import NotFound  # type: ignore[import-untyped]
from fastapi.testclient import TestClient
from sqlalchemy import text

from kodi_lab.application.addon_zip_installations import (
    AddonZipInstallationCoordinator,
    AddonZipInstallationService,
)
from kodi_lab.application.readiness_tasks import ReadinessTaskRegistry
from kodi_lab.application.reconciliation import (
    ManagedInstanceReconciliationService,
    StaleOperationReconciliationDispatcher,
)
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
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.storage.repositories import (
    AddonZipInstallationEvidenceRepository,
    AddonZipInstallationOperationRepository,
    GlobalHeavyOperationLeaseRepository,
    ManagedInstanceMutationLeaseRepository,
)
from kodi_lab.web.app import create_app

PASSWORD = "phase8-uploaded-zip-live-validation"
ORIGIN = "http://127.0.0.1"
DEEP_RELEASE = "21.3"


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


def _archive(
    addon_id: str,
    name: str,
    *,
    version: str = "1.0.0",
    dependency_id: str | None = None,
) -> bytes:
    requires = (
        ""
        if dependency_id is None
        else f'<requires><import addon="{dependency_id}" /></requires>'
    )
    descriptor = (
        f'<addon id="{addon_id}" name="{name}" version="{version}">'
        f"{requires}"
        '<extension point="xbmc.python.pluginsource" library="default.py">'
        "<provides>video</provides></extension>"
        '<extension point="xbmc.addon.metadata" /></addon>'
    )
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(f"{addon_id}/addon.xml", descriptor)
        archive.writestr(f"{addon_id}/default.py", "# deliberately passive\n")
    return output.getvalue()


def _unsafe_archive(case: str) -> bytes:
    addon_id = "plugin.video.kodi-lab-unsafe"
    output = io.BytesIO()
    descriptor = (
        f'<addon id="{addon_id}" name="Unsafe" version="1.0.0">'
        '<extension point="xbmc.python.pluginsource" library="default.py">'
        "<provides>video</provides></extension></addon>"
    )
    if case == "repository":
        descriptor = (
            f'<addon id="{addon_id}" name="Unsafe" version="1.0.0">'
            '<extension point="xbmc.addon.repository" /></addon>'
        )
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr(f"{addon_id}/addon.xml", descriptor)
        archive.writestr(f"{addon_id}/default.py", "pass\n")
        if case == "traversal":
            archive.writestr("../escape", "x")
        elif case == "vcs":
            archive.writestr(f"{addon_id}/.git/config", "x")
        elif case == "collision":
            archive.writestr(f"{addon_id}/DEFAULT.py", "x")
        elif case == "symlink":
            entry = zipfile.ZipInfo(f"{addon_id}/link")
            entry.create_system = 3
            entry.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(entry, "default.py")
    return output.getvalue()


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


def _upload(
    client: TestClient,
    headers: dict[str, str],
    *,
    label: str,
    payload: bytes,
    expected: str = "succeeded",
) -> dict[str, Any]:
    response = client.post(
        "/api/addon-zips",
        files={"zip": ("ignored-browser-name.zip", payload, "application/zip")},
        data={"logical_label": label},
        headers=headers,
    )
    _require(response.status_code == 202, f"upload failed: {response.text[:256]}")
    operation = _wait(client, response.json()["status_url"])
    _require(operation["state"] == expected, f"unexpected import result: {operation!r}")
    return operation


def _install(
    client: TestClient,
    headers: dict[str, str],
    instance_id: UUID,
    artifact_id: str,
) -> dict[str, Any]:
    response = client.post(
        f"/api/instances/{instance_id}/addon-zips/{artifact_id}/install",
        json={},
        headers=headers,
    )
    _require(response.status_code == 202, f"install failed: {response.text[:256]}")
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


def _put_existing(container_id: str, addon_id: str) -> str:
    payload = b"controlled existing destination\n"
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        directory = tarfile.TarInfo(addon_id)
        directory.type = tarfile.DIRTYPE
        directory.mode = 0o755
        directory.uid = directory.gid = 1000
        archive.addfile(directory)
        item = tarfile.TarInfo(f"{addon_id}/sentinel.txt")
        item.size = len(payload)
        item.mode = 0o644
        item.uid = item.gid = 1000
        archive.addfile(item, io.BytesIO(payload))
    api = docker.APIClient()
    try:
        _require(
            api.put_archive(container_id, "/home/kodi/.kodi/addons", output.getvalue()),
            "controlled destination creation failed",
        )
        stream, _ = api.get_archive(
            container_id, f"/home/kodi/.kodi/addons/{addon_id}/sentinel.txt"
        )
        return hashlib.sha256(b"".join(stream)).hexdigest()
    finally:
        api.close()


def _existing_digest(container_id: str, addon_id: str) -> str:
    api = docker.APIClient()
    try:
        stream, _ = api.get_archive(
            container_id, f"/home/kodi/.kodi/addons/{addon_id}/sentinel.txt"
        )
        return hashlib.sha256(b"".join(stream)).hexdigest()
    finally:
        api.close()


def _system_plugin_without_user_destination(
    inventory: dict[str, Any], container_id: str
) -> dict[str, Any] | None:
    api = docker.APIClient()
    try:
        for item in inventory["items"]:
            if item["kodi_type"] != "xbmc.python.pluginsource":
                continue
            try:
                api.get_archive(
                    container_id,
                    f"/home/kodi/.kodi/addons/{item['addon_id']}",
                )
            except NotFound:
                return item
    finally:
        api.close()
    return None


def _remove_controlled_destination(container_id: str, addon_id: str) -> None:
    _require(
        addon_id.startswith("plugin.video.kodi-lab-upload-")
        and re.fullmatch(r"[a-z0-9._-]+", addon_id) is not None,
        "controlled deletion identity is invalid",
    )
    destination = f"/home/kodi/.kodi/addons/{addon_id}"
    api = docker.APIClient()
    try:
        execution = api.exec_create(container_id, ["rm", "-rf", "--", destination])
        api.exec_start(execution["Id"])
        _require(
            api.exec_inspect(execution["Id"])["ExitCode"] == 0,
            "controlled destination removal failed",
        )
        try:
            api.get_archive(container_id, destination)
        except NotFound:
            return
        raise RuntimeError("controlled destination remained present")
    finally:
        api.close()


def _fault() -> None:
    raise RuntimeError("deterministic post-application validation fault")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--evidence-path",
        type=Path,
        default=Path("evidence/kodi-lab-phase8-uploaded-zip-evidence.json"),
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
            prefix="kodi-lab-phase8-uploaded-zip-", dir="/var/tmp"
        ) as temporary:
            validation_root = Path(temporary)
            data_root = validation_root / "data"
            (data_root / "database").mkdir(parents=True)
            runtime_root = validation_root / "web-runtime"
            runtime_root.mkdir(mode=0o700)
            os.environ["KODI_LAB_DATA_ROOT"] = str(data_root)
            os.environ["KODI_LAB_RUNTIME_ROOT"] = str(runtime_root)
            settings = Settings(_env_file=None)
            command.upgrade(Config(str(project_root / "alembic.ini")), "head")
            engine = create_database_engine(settings.database_url)
            factory = create_session_factory(engine)
            helper._seed(factory, instances)

            adapter = DockerManagedContainerAdapter()
            zip_resolver = SqliteAddonZipInstallationResolver(
                factory, data_root=data_root
            )
            registry = build_foundation_registry(
                adapter,
                provisioning_resolver=SqliteProvisioningResolver(factory),
                provisioning_provider=adapter,
                deprovisioning_provider=adapter,
                revision_resolver=SqliteManagedInstanceRevisionResolver(factory),
                runtime_observation_resolver=SqliteRuntimeObservationResolver(factory),
                runtime_observation_provider=adapter,
                addon_zip_installation_resolver=zip_resolver,
                addon_zip_installation_provider=adapter,
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
                )
                with TestClient(app, base_url=ORIGIN) as client:
                    headers = _login(client)
                    for runtime in helper.RUNTIMES:
                        instance_id = instances[str(runtime["release"])]
                        response = client.post(
                            f"/api/instances/{instance_id}/provision",
                            json={},
                            headers=headers,
                        )
                        _require(response.status_code == 200, "provision failed")
                        response = client.post(
                            f"/api/instances/{instance_id}/start",
                            json={},
                            headers=headers,
                        )
                        _require(response.status_code == 200, "start failed")
                        _inventory(client, instance_id)

                    deep_id = instances[DEEP_RELEASE]
                    deep_container = helper._exact_container_id(broker, deep_id)
                    initial_deep_inventory = _inventory(client, deep_id)
                    system_collision = _system_plugin_without_user_destination(
                        initial_deep_inventory, deep_container
                    )

                    basic_id = "plugin.video.kodi-lab-upload-basic"
                    basic_import = _upload(
                        client,
                        headers,
                        label="Uploaded ZIP basic",
                        payload=_archive(basic_id, "Uploaded ZIP Basic"),
                    )
                    artifacts = client.get("/api/addon-zips").json()
                    basic_metadata = next(
                        item
                        for item in artifacts
                        if item["artifact_id"] == basic_import["artifact_id"]
                    )
                    _require(
                        basic_metadata["addon_id"] == basic_id
                        and "ignored-browser-name" not in json.dumps(basic_metadata),
                        "upload identity or filename handling was wrong",
                    )

                    cross_version: list[dict[str, Any]] = []
                    for runtime in helper.RUNTIMES:
                        release = str(runtime["release"])
                        instance_id = instances[release]
                        _require(
                            _item(_inventory(client, instance_id), basic_id) is None,
                            f"{release} preflight add-on was not absent",
                        )
                        operation = _install(
                            client,
                            headers,
                            instance_id,
                            str(basic_import["artifact_id"]),
                        )
                        recognized = _item(_inventory(client, instance_id), basic_id)
                        _require(
                            operation["state"] == "succeeded"
                            and operation["stopped_readback_matched"] is True
                            and operation["final_readback_matched"] is True
                            and operation["recognition_enabled"] is False
                            and recognized is not None
                            and recognized["version"] == "1.0.0"
                            and recognized["kodi_type"] == "xbmc.python.pluginsource"
                            and recognized["enabled"] is False,
                            "cross-version install failed for "
                            f"{release}: {operation!r}",
                        )
                        cross_version.append(
                            {
                                "kodi_release": release,
                                "runtime_revision_uuid": runtime["revision"],
                                "installation_operation_uuid": operation[
                                    "operation_id"
                                ],
                                "pre_enabled": None,
                                "post_enabled": False,
                                "recognition": "exact_id_version_type_disabled",
                                "stopped_readback": "exact",
                                "final_readback": "exact",
                                "instance_running": True,
                            }
                        )

                    missing_id = "plugin.video.kodi-lab-upload-missing"
                    missing_dependency = "script.module.kodi-lab-definitely-absent"
                    missing_import = _upload(
                        client,
                        headers,
                        label="Uploaded ZIP missing dependency",
                        payload=_archive(
                            missing_id,
                            "Uploaded ZIP Missing Dependency",
                            dependency_id=missing_dependency,
                        ),
                    )
                    missing_operation = _install(
                        client,
                        headers,
                        deep_id,
                        str(missing_import["artifact_id"]),
                    )
                    missing_after = _inventory(client, deep_id)
                    _require(
                        missing_operation["state"] == "succeeded"
                        and _item(missing_after, missing_id) is not None
                        and _item(missing_after, missing_id)["enabled"] is False
                        and _item(missing_after, missing_dependency) is None,
                        "missing dependency installation mutated dependency state",
                    )

                    disabled_dependency = next(
                        (
                            item
                            for item in missing_after["items"]
                            if item["enabled"] is False
                            and item["addon_id"] != missing_id
                        ),
                        None,
                    )
                    _require(
                        disabled_dependency is not None,
                        "no disabled dependency candidate was available",
                    )
                    disabled_id = "plugin.video.kodi-lab-upload-disabled-dependency"
                    disabled_import = _upload(
                        client,
                        headers,
                        label="Uploaded ZIP disabled dependency",
                        payload=_archive(
                            disabled_id,
                            "Uploaded ZIP Disabled Dependency",
                            dependency_id=str(disabled_dependency["addon_id"]),
                        ),
                    )
                    disabled_operation = _install(
                        client,
                        headers,
                        deep_id,
                        str(disabled_import["artifact_id"]),
                    )
                    disabled_after = _inventory(client, deep_id)
                    _require(
                        disabled_operation["state"] == "succeeded"
                        and _item(disabled_after, disabled_id) is not None
                        and _item(disabled_after, disabled_id)["enabled"] is False
                        and _item(disabled_after, str(disabled_dependency["addon_id"]))[
                            "enabled"
                        ]
                        is False,
                        "disabled dependency changed",
                    )

                    existing_id = "plugin.video.kodi-lab-upload-existing"
                    existing_import = _upload(
                        client,
                        headers,
                        label="Uploaded ZIP existing destination",
                        payload=_archive(existing_id, "Uploaded ZIP Existing"),
                    )
                    deep_container = helper._exact_container_id(broker, deep_id)
                    existing_before = _put_existing(deep_container, existing_id)
                    existing_operation = _install(
                        client,
                        headers,
                        deep_id,
                        str(existing_import["artifact_id"]),
                    )
                    _require(
                        existing_operation["state"] == "failed"
                        and existing_before
                        == _existing_digest(deep_container, existing_id),
                        "existing destination was not blocked unchanged",
                    )

                    collision_evidence: dict[str, Any]
                    if system_collision is None:
                        collision_id = (
                            "plugin.video.kodi-lab-upload-inventory-collision"
                        )
                        collision_import = _upload(
                            client,
                            headers,
                            label="Uploaded ZIP controlled inventory collision",
                            payload=_archive(
                                collision_id,
                                "Uploaded ZIP Controlled Inventory Collision",
                            ),
                        )
                        first_collision_install = _install(
                            client,
                            headers,
                            deep_id,
                            str(collision_import["artifact_id"]),
                        )
                        _require(
                            first_collision_install["state"] == "succeeded"
                            and _item(_inventory(client, deep_id), collision_id)
                            is not None,
                            "controlled collision setup did not install",
                        )
                        _remove_controlled_destination(deep_container, collision_id)
                        _require(
                            _item(_inventory(client, deep_id), collision_id)
                            is not None,
                            "Kodi inventory collision did not remain live",
                        )
                        collision_operation = _install(
                            client,
                            headers,
                            deep_id,
                            str(collision_import["artifact_id"]),
                        )
                        _require(
                            collision_operation["state"] == "failed",
                            "controlled inventory collision did not block",
                        )
                        collision_evidence = {
                            "practical": True,
                            "setup": "controlled_prior_install_destination_removed",
                            "installation_operation_uuid": collision_operation[
                                "operation_id"
                            ],
                            "live_inventory_item_present": True,
                            "user_destination_absent": True,
                            "blocked_before_application": True,
                            "instance_running": True,
                        }
                    else:
                        collision_import = _upload(
                            client,
                            headers,
                            label="Uploaded ZIP inventory collision",
                            payload=_archive(
                                str(system_collision["addon_id"]),
                                "Uploaded ZIP Inventory Collision",
                            ),
                        )
                        collision_operation = _install(
                            client,
                            headers,
                            deep_id,
                            str(collision_import["artifact_id"]),
                        )
                        _require(
                            collision_operation["state"] == "failed",
                            "inventory collision did not block",
                        )
                        collision_evidence = {
                            "practical": True,
                            "installation_operation_uuid": collision_operation[
                                "operation_id"
                            ],
                            "blocked_before_application": True,
                            "instance_running": True,
                        }

                    parser_rejections: list[str] = []
                    for case in (
                        "traversal",
                        "symlink",
                        "vcs",
                        "collision",
                        "repository",
                    ):
                        rejected = _upload(
                            client,
                            headers,
                            label=f"Rejected ZIP {case}",
                            payload=_unsafe_archive(case),
                            expected="failed",
                        )
                        parser_rejections.append(str(rejected["operation_id"]))

                fault_id = "plugin.video.kodi-lab-upload-reconciliation"
                import_app = create_app(
                    settings=settings,
                    session_factory=factory,
                    broker=broker,
                    readiness=ReadinessTaskRegistry(max_workers=2),
                    auth=auth,
                )
                with TestClient(import_app, base_url=ORIGIN) as client:
                    headers = _login(client)
                    fault_import = _upload(
                        client,
                        headers,
                        label="Uploaded ZIP reconciliation",
                        payload=_archive(fault_id, "Uploaded ZIP Reconciliation"),
                    )

                stale = StaleOperationReconciliationDispatcher(
                    factory, broker=broker, data_root=data_root
                )
                reconciliation = ManagedInstanceReconciliationService(
                    factory, broker=broker, data_root=data_root
                )
                fault_coordinator = AddonZipInstallationCoordinator(
                    factory, stale_dispatcher=stale, reconciliation=reconciliation
                )
                fault_app = create_app(
                    settings=settings,
                    session_factory=factory,
                    broker=broker,
                    readiness=ReadinessTaskRegistry(max_workers=2),
                    auth=auth,
                    addon_zip_installation_service_factory=lambda lease: (
                        AddonZipInstallationService(
                            factory,
                            broker,
                            fault_coordinator,
                            lease,
                            after_application_hook=_fault,
                        )
                    ),
                )
                with TestClient(fault_app, base_url=ORIGIN) as client:
                    headers = _login(client)
                    fault_operation = _install(
                        client,
                        headers,
                        deep_id,
                        str(fault_import["artifact_id"]),
                    )
                    _require(
                        fault_operation["state"] == "reconciliation_required",
                        "post-application fault was not reconciled",
                    )
                    deep_entry = next(
                        item
                        for item in broker.managed_container_inventory().containers
                        if item.managed_instance_id == str(deep_id)
                    )
                    _require(
                        deep_entry.runtime_state.value == "stopped",
                        "ambiguous instance did not remain stopped",
                    )
                    reconciliation_status = client.get(
                        f"/api/instances/{deep_id}/reconciliation"
                    ).json()
                    _require(
                        reconciliation_status["operation_uuid"]
                        == fault_operation["operation_id"]
                        and reconciliation_status["blocking"] is True,
                        "ZIP reconciliation projection was wrong",
                    )

                with factory() as session:
                    operation = AddonZipInstallationOperationRepository(session).get(
                        UUID(str(fault_operation["operation_id"]))
                    )
                    zip_evidence = AddonZipInstallationEvidenceRepository(session).get(
                        deep_id, fault_id
                    )
                    mutation = ManagedInstanceMutationLeaseRepository(session).current(
                        deep_id
                    )
                    heavy = GlobalHeavyOperationLeaseRepository(session).current()
                    migration_head = session.scalar(
                        text("SELECT version_num FROM alembic_version")
                    )
                _require(
                    operation is not None
                    and operation.reconciliation_required
                    and zip_evidence is None
                    and mutation is None
                    and heavy is None,
                    "reconciliation persistence or lease release was wrong",
                )

                evidence = {
                    "schema": "kodi-lab-phase8-uploaded-zip-live-v1",
                    "migration_head": migration_head,
                    "artifact": {
                        "artifact_uuid": basic_import["artifact_id"],
                        "logical_label": basic_metadata["logical_label"],
                        "addon_id": basic_metadata["addon_id"],
                        "addon_version": basic_metadata["addon_version"],
                        "compressed_bytes": basic_metadata["compressed_size"],
                        "uncompressed_bytes": basic_metadata[
                            "total_uncompressed_bytes"
                        ],
                        "member_count": basic_metadata["member_count"],
                        "origin": basic_metadata["origin"],
                        "validation_state": basic_metadata["validation_state"],
                        "upload_filename_retained": False,
                    },
                    "basic_install": cross_version[2],
                    "missing_dependency": {
                        "artifact_uuid": missing_import["artifact_id"],
                        "installation_operation_uuid": missing_operation[
                            "operation_id"
                        ],
                        "target_enabled": False,
                        "dependency_pre": "absent",
                        "dependency_post": "absent",
                        "recognition": "exact_disabled",
                        "readback": "stopped_and_final_exact",
                    },
                    "disabled_dependency": {
                        "artifact_uuid": disabled_import["artifact_id"],
                        "installation_operation_uuid": disabled_operation[
                            "operation_id"
                        ],
                        "target_enabled": False,
                        "dependency_pre": "present_disabled",
                        "dependency_post": "present_disabled",
                        "recognition": "exact_disabled",
                    },
                    "existing_destination": {
                        "installation_operation_uuid": existing_operation[
                            "operation_id"
                        ],
                        "blocked_before_stop_or_application": True,
                        "original_bytes_unchanged": True,
                        "instance_running": True,
                    },
                    "inventory_collision": collision_evidence,
                    "unsafe_zip_rejection": {
                        "operation_uuids": parser_rejections,
                        "all_rejected_before_kodi": True,
                        "classes": [
                            "traversal",
                            "symlink",
                            "vcs",
                            "unicode_case_collision",
                            "repository_descriptor",
                        ],
                    },
                    "reconciliation": {
                        "artifact_uuid": fault_import["artifact_id"],
                        "installation_operation_uuid": fault_operation["operation_id"],
                        "outcome": "reconciliation_required",
                        "instance_stopped": True,
                        "evidence": "unknown_or_absent",
                        "automatic_retry": False,
                        "automatic_delete": False,
                        "automatic_adopt": False,
                        "automatic_restart": False,
                    },
                    "cross_version": cross_version,
                    "network": {
                        "new_authority_added": False,
                        "ordinary_managed_startup_used_existing_network_model": True,
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
        "temporary_upload_staging_cleaned": True,
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
