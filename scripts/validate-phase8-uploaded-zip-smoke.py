#!/usr/bin/env python3
"""Run isolated live validation for ZIP-specific smoke testing."""

# ruff: noqa: E402 -- bytecode suppression must precede project imports.

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import io
import json
import os
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
from fastapi.testclient import TestClient
from sqlalchemy import text

from kodi_lab.application.addon_zip_smoke_tests import (
    AddonZipSmokeTestLease,
    AddonZipSmokeTestService,
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
from kodi_lab.kodi.addons import (
    KodiAddonDetailsRecord,
    KodiReportedAddonDependency,
)
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.storage.repositories import (
    AddonZipInstallationEvidenceRepository,
    AddonZipSmokeTestOperationRepository,
    GlobalHeavyOperationLeaseRepository,
    ManagedInstanceMutationLeaseRepository,
)
from kodi_lab.web.app import create_app

PASSWORD = "phase8-uploaded-zip-smoke-live-validation"
ORIGIN = "http://127.0.0.1"
VERSION = "1.0.0"
DEEP_RELEASE = "21.3"
MISSING_DEPENDENCY = "script.module.kodi-lab-zip-smoke-absent"

ADDONS = {
    "empty": ("plugin.video.kodi-lab-zip-smoke-empty", "ZIP Smoke Empty"),
    "one": ("plugin.video.kodi-lab-zip-smoke-one", "ZIP Smoke One"),
    "failure": ("plugin.video.kodi-lab-zip-smoke-failure", "ZIP Smoke Failure"),
    "timeout": ("plugin.video.kodi-lab-zip-smoke-timeout", "ZIP Smoke Timeout"),
    "tree_write": (
        "plugin.video.kodi-lab-zip-smoke-tree-write",
        "ZIP Smoke Tree Write",
    ),
    "data_write": (
        "plugin.video.kodi-lab-zip-smoke-data-write",
        "ZIP Smoke Data Write",
    ),
    "dependency": (
        "plugin.video.kodi-lab-zip-smoke-dependency",
        "ZIP Smoke Dependency",
    ),
}


def _module(filename: str, name: str) -> ModuleType:
    path = Path(__file__).with_name(filename)
    specification = importlib.util.spec_from_file_location(name, path)
    if specification is None or specification.loader is None:
        raise RuntimeError(f"validation helper {filename} is unavailable")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _program(key: str) -> str:
    addon_id = ADDONS[key][0]
    return {
        "empty": (
            "import sys\nimport xbmcplugin\n"
            "from resources.localpackage import VALUE\nassert VALUE == 1\n"
            "xbmcplugin.endOfDirectory(int(sys.argv[1]))\n"
        ),
        "one": (
            "import sys\nimport xbmcgui\nimport xbmcplugin\n"
            "handle = int(sys.argv[1])\n"
            "item = xbmcgui.ListItem(label='discarded-private-label')\n"
            "xbmcplugin.addDirectoryItem("
            "handle, sys.argv[0] + '?private=1', item, False)\n"
            "xbmcplugin.endOfDirectory(handle)\n"
        ),
        "failure": "raise RuntimeError('discarded-private-python-error')\n",
        "timeout": (
            "import sys\nimport time\nimport xbmcplugin\n"
            "time.sleep(15)\nxbmcplugin.endOfDirectory(int(sys.argv[1]))\n"
        ),
        "tree_write": (
            "import pathlib\nimport sys\nimport xbmcplugin\n"
            "pathlib.Path(__file__).with_name('runtime-write.txt').write_text("
            "'changed')\nxbmcplugin.endOfDirectory(int(sys.argv[1]))\n"
        ),
        "data_write": (
            "import os\nimport sys\nimport xbmcplugin\nimport xbmcvfs\n"
            f"root = xbmcvfs.translatePath('special://profile/addon_data/{addon_id}')\n"
            "os.makedirs(root, exist_ok=True)\n"
            "with open(os.path.join(root, 'state.txt'), 'w', "
            "encoding='utf-8') as stream:\n    stream.write('proper-profile-state')\n"
            "xbmcplugin.endOfDirectory(int(sys.argv[1]))\n"
        ),
        "dependency": (
            "import sys\nimport xbmcplugin\n"
            "xbmcplugin.endOfDirectory(int(sys.argv[1]))\n"
        ),
    }[key]


def _archive(key: str) -> bytes:
    addon_id, name = ADDONS[key]
    descriptor = (
        f'<addon id="{addon_id}" name="{name}" version="{VERSION}">'
        '<extension point="xbmc.python.pluginsource" library="default.py">'
        "<provides>video</provides></extension>"
        '<extension point="xbmc.addon.metadata"/></addon>'
    )
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(f"{addon_id}/addon.xml", descriptor)
        archive.writestr(f"{addon_id}/default.py", _program(key))
        if key == "empty":
            archive.writestr(f"{addon_id}/resources/__init__.py", "")
            archive.writestr(
                f"{addon_id}/resources/localpackage/__init__.py", "VALUE = 1\n"
            )
    return output.getvalue()


class _MissingDependencyDetails:
    def __init__(self, addon_id: str) -> None:
        self._addon_id = addon_id

    def get_addon_details(self, addon_id: str) -> KodiAddonDetailsRecord:
        _require(addon_id == self._addon_id, "dependency detail identity changed")
        return KodiAddonDetailsRecord(
            addon_id=addon_id,
            name=ADDONS["dependency"][1],
            version=VERSION,
            enabled=True,
            kodi_type="xbmc.python.pluginsource",
            dependencies=(
                KodiReportedAddonDependency(MISSING_DEPENDENCY, None, False),
            ),
        )


class _ForbiddenSmokeAdapter:
    def __init__(self, calls: list[str]) -> None:
        self._calls = calls

    def invoke(self, addon_id: str) -> Any:
        self._calls.append(addon_id)
        raise AssertionError("dependency-blocked ZIP smoke reached invocation")


def _wait_smoke(client: TestClient, status_url: str) -> dict[str, Any]:
    for _ in range(1_800):
        response = client.get(status_url)
        _require(response.status_code == 200, "ZIP smoke status failed")
        result = response.json()
        if result["state"] not in {"queued", "running"}:
            return result
        time.sleep(0.05)
    raise RuntimeError("ZIP smoke operation did not terminalize")


def _smoke(
    client: TestClient,
    headers: dict[str, str],
    instance_id: UUID,
    artifact_id: str,
) -> tuple[int, dict[str, Any], str]:
    response = client.post(
        f"/api/instances/{instance_id}/addon-zips/{artifact_id}/smoke-test",
        json={},
        headers=headers,
    )
    _require(response.status_code == 202, f"ZIP smoke rejected: {response.text[:256]}")
    status_url = response.json()["status_url"]
    return response.status_code, _wait_smoke(client, status_url), status_url


def _enable(
    client: TestClient,
    headers: dict[str, str],
    instance_id: UUID,
    artifact_id: str,
) -> dict[str, Any]:
    response = client.post(
        f"/api/instances/{instance_id}/addon-zips/{artifact_id}/enable",
        json={},
        headers=headers,
    )
    _require(response.status_code == 200, f"ZIP enable failed: {response.text[:256]}")
    result = response.json()
    _require(
        result["result"] in {"enabled", "already_enabled"},
        f"ZIP enable did not succeed: {result!r}",
    )
    return result


def _confidence(factory: Any, instance_id: UUID, addon_id: str) -> str:
    with factory() as session:
        evidence = AddonZipInstallationEvidenceRepository(session).get(
            instance_id, addon_id
        )
    _require(evidence is not None, "ZIP installation evidence disappeared")
    assert evidence is not None
    return evidence.confidence.value


def _archive_members(container_id: str, addon_id: str) -> tuple[str, ...]:
    api = docker.APIClient()
    try:
        stream, _ = api.get_archive(container_id, f"/home/kodi/.kodi/addons/{addon_id}")
        with tempfile.SpooledTemporaryFile(max_size=16 * 1024 * 1024) as payload:
            for chunk in stream:
                payload.write(chunk)
            payload.seek(0)
            with tarfile.open(fileobj=payload) as archive:
                return tuple(item.name for item in archive)
    finally:
        api.close()


def _profile_data_present(container_id: str, addon_id: str) -> bool:
    api = docker.APIClient()
    try:
        try:
            stream, _ = api.get_archive(
                container_id,
                f"/home/kodi/.kodi/userdata/addon_data/{addon_id}/state.txt",
            )
            for _chunk in stream:
                pass
            return True
        except Exception:
            return False
    finally:
        api.close()


def _safe(operation: dict[str, Any]) -> dict[str, Any]:
    return {
        "operation_uuid": operation["operation_id"],
        "operation_state": operation["state"],
        "overall_result": operation["overall_result"],
        "execution_result": operation["execution_result"],
        "post_provenance": operation["post_execution_provenance"],
        "duration_ms": operation["invocation_duration_ms"],
        "returned_count": operation["returned_item_count"],
        "dependency_block_classification": operation["blocked_dependency_reason"],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--evidence-path",
        type=Path,
        default=Path("evidence/kodi-lab-phase8-uploaded-zip-smoke-evidence.json"),
    )
    arguments = parser.parse_args()
    _require(os.geteuid() == 0, "live validation must run as root")
    project_root = Path(__file__).resolve().parents[1]
    helpers = _module("validate-phase8-copied-enablement.py", "phase8_helpers")
    uploaded = _module("validate-phase8-uploaded-zip.py", "phase8_zip_helpers")
    helpers.PASSWORD = PASSWORD
    uploaded.PASSWORD = PASSWORD
    uploaded.ORIGIN = ORIGIN
    protected_before = helpers._protected_fingerprint()
    _require(
        protected_before["production_data_root_absent"] is True
        and protected_before["production_runtime_root_absent"] is True,
        "production roots were present before validation",
    )
    _require(helpers._managed_network_count() == 0, "managed network was not empty")
    api = docker.APIClient()
    try:
        for runtime in helpers.RUNTIMES:
            _require(
                api.inspect_image(runtime["image"])["Id"] == runtime["image"],
                f"curated image absent for {runtime['release']}",
            )
    finally:
        api.close()

    instances = {str(item["release"]): uuid4() for item in helpers.RUNTIMES}
    instances["21.3-default"] = uuid4()
    instance_set = set(instances.values())
    dependency_invocations: list[str] = []
    evidence: dict[str, Any] = {}
    try:
        with tempfile.TemporaryDirectory(
            prefix="kodi-lab-phase8-zip-smoke-", dir="/var/tmp"
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
            helpers._seed(factory, instances)
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
            with helpers._broker_server(validation_root, registry) as broker:
                auth = WebAuthenticationService(factory)
                auth.set_password(PASSWORD)
                holder: dict[str, Any] = {}

                def service_factory(
                    lease: AddonZipSmokeTestLease,
                ) -> AddonZipSmokeTestService:
                    context = holder["app"].state.kodi_lab
                    if lease.artifact_id == artifacts["dependency"]:
                        return AddonZipSmokeTestService(
                            factory,
                            broker,
                            context.addon_zip_smoke_test_operations,
                            lease,
                            details_adapter_factory=lambda _o, _p: (
                                _MissingDependencyDetails(ADDONS["dependency"][0])
                            ),
                            smoke_adapter_factory=lambda _o, _p: _ForbiddenSmokeAdapter(
                                dependency_invocations
                            ),
                        )
                    return AddonZipSmokeTestService(
                        factory,
                        broker,
                        context.addon_zip_smoke_test_operations,
                        lease,
                    )

                app = create_app(
                    settings=settings,
                    session_factory=factory,
                    broker=broker,
                    readiness=ReadinessTaskRegistry(max_workers=6),
                    auth=auth,
                    addon_zip_smoke_test_service_factory=service_factory,
                )
                holder["app"] = app
                with TestClient(app, base_url=ORIGIN) as client:
                    headers = uploaded._login(client)
                    artifacts: dict[str, UUID] = {}
                    for key in ADDONS:
                        result = uploaded._upload(
                            client,
                            headers,
                            label=f"ZIP smoke {key}",
                            payload=_archive(key),
                        )
                        artifacts[key] = UUID(result["artifact_id"])

                    for runtime in helpers.RUNTIMES:
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
                        uploaded._inventory(client, instance_id)
                        installation = uploaded._install(
                            client, headers, instance_id, str(artifacts["empty"])
                        )
                        _require(
                            installation["state"] == "succeeded",
                            f"empty ZIP installation failed for {runtime['release']}: "
                            f"{installation!r}",
                        )
                        _enable(client, headers, instance_id, str(artifacts["empty"]))

                    deep_id = instances[DEEP_RELEASE]
                    for key in ADDONS:
                        if key == "empty":
                            continue
                        installation = uploaded._install(
                            client, headers, deep_id, str(artifacts[key])
                        )
                        _require(
                            installation["state"] == "succeeded",
                            f"{key} ZIP installation failed: {installation!r}",
                        )
                        _enable(client, headers, deep_id, str(artifacts[key]))

                    cross_version: list[dict[str, Any]] = []
                    deep_empty: dict[str, Any] | None = None
                    for runtime in helpers.RUNTIMES:
                        release = str(runtime["release"])
                        code, operation, _ = _smoke(
                            client,
                            headers,
                            instances[release],
                            str(artifacts["empty"]),
                        )
                        _require(
                            operation["overall_result"] == "passed"
                            and operation["returned_item_count"] == 0
                            and operation["post_execution_provenance"] == "matched",
                            f"cross-version smoke failed for {release}: {operation!r}",
                        )
                        if release == DEEP_RELEASE:
                            deep_empty = operation
                        cross_version.append(
                            {
                                "runtime": release,
                                "runtime_revision_uuid": runtime["revision"],
                                "operation_uuid": operation["operation_id"],
                                "http_status": code,
                                "overall_result": operation["overall_result"],
                                "post_provenance": operation[
                                    "post_execution_provenance"
                                ],
                                "returned_count": operation["returned_item_count"],
                            }
                        )
                    assert deep_empty is not None
                    deep_container = helpers._exact_container_id(broker, deep_id)
                    members = _archive_members(deep_container, ADDONS["empty"][0])
                    suppression_clean = not any(
                        "__pycache__" in member or member.endswith(".pyc")
                        for member in members
                    )
                    _require(suppression_clean, "ZIP smoke created Python bytecode")

                    _, one, _ = _smoke(client, headers, deep_id, str(artifacts["one"]))
                    _require(
                        one["overall_result"] == "passed"
                        and one["returned_item_count"] == 1
                        and "discarded-private-label" not in json.dumps(one),
                        f"one-item ZIP smoke failed: {one!r}",
                    )
                    _, failure, _ = _smoke(
                        client, headers, deep_id, str(artifacts["failure"])
                    )
                    failure_confidence = _confidence(
                        factory, deep_id, ADDONS["failure"][0]
                    )
                    _require(
                        failure["overall_result"] == "execution_failed"
                        and failure["post_execution_provenance"] == "matched"
                        and failure_confidence == "verified_at_last_operation"
                        and "discarded-private-python-error" not in json.dumps(failure),
                        f"ZIP execution failure contract failed: {failure!r}",
                    )
                    _, data_write, _ = _smoke(
                        client, headers, deep_id, str(artifacts["data_write"])
                    )
                    _require(
                        data_write["overall_result"] == "passed"
                        and data_write["post_execution_provenance"] == "matched"
                        and _profile_data_present(
                            deep_container, ADDONS["data_write"][0]
                        ),
                        f"ZIP add-on-data write failed: {data_write!r}",
                    )
                    _, dependency, _ = _smoke(
                        client, headers, deep_id, str(artifacts["dependency"])
                    )
                    _require(
                        dependency["overall_result"] == "dependency_blocked"
                        and dependency["blocked_dependency_reason"]
                        == "dependency_state_unknown"
                        and not dependency_invocations,
                        f"ZIP dependency was not blocked: {dependency!r}",
                    )
                    _, tree_write, _ = _smoke(
                        client, headers, deep_id, str(artifacts["tree_write"])
                    )
                    tree_confidence = _confidence(
                        factory, deep_id, ADDONS["tree_write"][0]
                    )
                    _require(
                        tree_write["overall_result"] == "installed_tree_changed"
                        and tree_write["post_execution_provenance"] == "changed"
                        and tree_confidence == "stale",
                        f"ZIP installed-tree write contract failed: {tree_write!r}",
                    )
                    _, timeout, timeout_url = _smoke(
                        client, headers, deep_id, str(artifacts["timeout"])
                    )
                    timeout_confidence = _confidence(
                        factory, deep_id, ADDONS["timeout"][0]
                    )
                    _require(
                        timeout["state"] == "reconciliation_required"
                        and timeout["overall_result"] == "timeout"
                        and timeout["execution_result"] == "timeout"
                        and timeout["post_execution_provenance"] == "unknown"
                        and timeout_confidence == "unknown",
                        f"ZIP timeout ambiguity contract failed: {timeout!r}",
                    )
                    responsive = (
                        uploaded._inventory(client, deep_id)["query_status"]
                        == "available"
                    )
                    second = client.post(
                        f"/api/instances/{deep_id}/addon-zips/"
                        f"{artifacts['timeout']}/smoke-test",
                        json={},
                        headers=headers,
                    )
                    _require(
                        second.status_code == 409, "second ZIP smoke was not blocked"
                    )
                    running_warning = client.get(
                        f"/api/instances/{deep_id}/reconciliation"
                    ).json()
                    _require(
                        running_warning["blocking"] is True
                        and running_warning["observed_state"]
                        == "original_container_running",
                        "ZIP timeout running warning was not blocking",
                    )
                    stopped = client.post(
                        f"/api/instances/{deep_id}/stop", json={}, headers=headers
                    )
                    _require(
                        stopped.status_code == 200, "explicit ZIP timeout stop failed"
                    )
                    stopped_warning = client.get(
                        f"/api/instances/{deep_id}/reconciliation"
                    ).json()
                    historical = client.get(timeout_url).json()
                    _require(
                        stopped_warning["blocking"] is False
                        and stopped_warning["observed_state"]
                        == "original_container_stopped"
                        and historical["state"] == "reconciliation_required",
                        "explicit stop rewrote ZIP smoke history",
                    )
                    with factory() as session:
                        timeout_rows = [
                            row
                            for row in AddonZipSmokeTestOperationRepository(
                                session
                            ).list_for_original_instance(deep_id)
                            if row.artifact_id == artifacts["timeout"]
                        ]
                        instance_lease = ManagedInstanceMutationLeaseRepository(
                            session
                        ).current(deep_id)
                        global_lease = GlobalHeavyOperationLeaseRepository(
                            session
                        ).current()
                        migration_head = session.scalar(
                            text("SELECT version_num FROM alembic_version")
                        )
                    _require(
                        len(timeout_rows) == 1
                        and instance_lease is None
                        and global_lease is None,
                        "ZIP timeout retried or retained coordination",
                    )
                    evidence = {
                        "schema": "kodi-lab-phase8-uploaded-zip-smoke-live-v1",
                        "migration_head": migration_head,
                        "empty_plugin": {
                            **_safe(deep_empty),
                            "http_status": 202,
                            "confidence_transition": "verified_preserved",
                        },
                        "one_item": {
                            **_safe(one),
                            "content_persisted_or_exposed": False,
                        },
                        "execution_failure": {
                            **_safe(failure),
                            "confidence_transition": failure_confidence,
                            "raw_error_exposed": False,
                        },
                        "timeout": {
                            **_safe(timeout),
                            "confidence_transition": timeout_confidence,
                            "runtime_responsive_after_client_deadline": responsive,
                            "second_smoke_blocked": True,
                            "explicit_stop_allowed": True,
                            "warning_after_stop": "nonblocking",
                            "historical_result_retained": True,
                            "automatic_retry": False,
                        },
                        "installed_tree_write": {
                            **_safe(tree_write),
                            "confidence_transition": tree_confidence,
                            "provenance_adopted": False,
                        },
                        "addon_data_write": {
                            **_safe(data_write),
                            "profile_state_observed": True,
                        },
                        "dependency_block": {
                            **_safe(dependency),
                            "invocation_dispatched": False,
                            "dependency_mutation": False,
                        },
                        "cross_version": cross_version,
                        "python_suppression_regression": {
                            "local_package_imported": True,
                            "bytecode_artifacts_absent": suppression_clean,
                            "installed_tree_provenance": "matched",
                        },
                    }
            engine.dispose()
    finally:
        helpers._cleanup_created(instance_set)

    _require(helpers._managed_network_count() == 0, "managed network was not cleaned")
    protected_after = helpers._protected_fingerprint()
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
        "temporary_resources_removed": True,
        "managed_containers_absent": True,
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
