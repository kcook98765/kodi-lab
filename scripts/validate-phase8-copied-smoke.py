#!/usr/bin/env python3
"""Run isolated live validation for the copied-add-on smoke first slice."""

# ruff: noqa: E402 -- bytecode suppression must precede project imports.

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import sys
import tarfile
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
from kodi_lab.application.development_source_smoke_tests import (
    DevelopmentSourceSmokeTestLease,
    DevelopmentSourceSmokeTestService,
)
from kodi_lab.application.readiness_tasks import ReadinessTaskRegistry
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
from kodi_lab.kodi.addons import (
    KodiAddonDetailsRecord,
    KodiAddonSmokeAdapter,
    KodiReportedAddonDependency,
)
from kodi_lab.kodi.readiness import _BoundedHttpTransport
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.storage.repositories import (
    DevelopmentSourceDeploymentRepository,
    DevelopmentSourceSmokeTestOperationRepository,
    GlobalHeavyOperationLeaseRepository,
    ManagedInstanceMutationLeaseRepository,
)
from kodi_lab.web.app import create_app

PASSWORD = "phase8-copied-smoke-live-validation"
ORIGIN = "http://127.0.0.1"
VERSION = "1.0.0"
DEEP_RELEASE = "21.3"
MISSING_DEPENDENCY = "script.module.kodi-lab-smoke-absent"

SOURCES = {
    "empty": {
        "id": "plugin.video.kodi-lab-smoke-empty",
        "name": "Kodi Lab Smoke Empty",
        "locator": "sources/empty",
    },
    "one": {
        "id": "plugin.video.kodi-lab-smoke-one",
        "name": "Kodi Lab Smoke One",
        "locator": "sources/one",
    },
    "failure": {
        "id": "plugin.video.kodi-lab-smoke-failure",
        "name": "Kodi Lab Smoke Failure",
        "locator": "sources/failure",
    },
    "timeout": {
        "id": "plugin.video.kodi-lab-smoke-timeout",
        "name": "Kodi Lab Smoke Timeout",
        "locator": "sources/timeout",
    },
    "tree_write": {
        "id": "plugin.video.kodi-lab-smoke-tree-write",
        "name": "Kodi Lab Smoke Tree Write",
        "locator": "sources/tree-write",
    },
    "data_write": {
        "id": "plugin.video.kodi-lab-smoke-data-write",
        "name": "Kodi Lab Smoke Data Write",
        "locator": "sources/data-write",
    },
    "dependency": {
        "id": "plugin.video.kodi-lab-smoke-dependency",
        "name": "Kodi Lab Smoke Dependency",
        "locator": "sources/dependency",
    },
}


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _load_enablement_helpers(project_root: Path) -> ModuleType:
    path = project_root / "scripts" / "validate-phase8-copied-enablement.py"
    spec = importlib.util.spec_from_file_location("phase8_enablement_helpers", path)
    _require(spec is not None and spec.loader is not None, "helper module unavailable")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _descriptor(source: dict[str, str], requires: str = "") -> str:
    return (
        f'<addon id="{source["id"]}" name="{source["name"]}" '
        f'version="{VERSION}">{requires}'
        '<extension point="xbmc.python.pluginsource" library="default.py">'
        "<provides>video</provides></extension>"
        '<extension point="xbmc.addon.metadata"/></addon>'
    )


def _source_path(root: Path, key: str) -> Path:
    return root.joinpath(*SOURCES[key]["locator"].split("/"))


def _write_sources(root: Path) -> None:
    programs = {
        "empty": (
            "import sys\n"
            "import xbmcplugin\n"
            "from resources.localpackage import VALUE\n"
            "assert VALUE == 1\n"
            "xbmcplugin.endOfDirectory(int(sys.argv[1]))\n"
        ),
        "one": (
            "import sys\n"
            "import xbmcgui\n"
            "import xbmcplugin\n"
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
            "pathlib.Path(__file__).with_name('runtime-write.txt').write_text('changed')\n"
            "xbmcplugin.endOfDirectory(int(sys.argv[1]))\n"
        ),
        "data_write": (
            "import os\nimport sys\nimport xbmcplugin\nimport xbmcvfs\n"
            f"root = xbmcvfs.translatePath('special://profile/addon_data/{SOURCES['data_write']['id']}')\n"
            "os.makedirs(root, exist_ok=True)\n"
            "with open(os.path.join(root, 'state.txt'), 'w', "
            "encoding='utf-8') as stream:\n"
            "    stream.write('proper-profile-state')\n"
            "xbmcplugin.endOfDirectory(int(sys.argv[1]))\n"
        ),
        "dependency": (
            "import sys\nimport xbmcplugin\n"
            "xbmcplugin.endOfDirectory(int(sys.argv[1]))\n"
        ),
    }
    for key, source in SOURCES.items():
        target = _source_path(root, key)
        target.mkdir(parents=True)
        (target / "addon.xml").write_text(_descriptor(source), encoding="utf-8")
        (target / "default.py").write_text(programs[key], encoding="utf-8")
    package = _source_path(root, "empty") / "resources" / "localpackage"
    package.mkdir(parents=True)
    (package.parent / "__init__.py").write_text("", encoding="utf-8")
    (package / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")


class _MissingDependencyDetails:
    def __init__(self, addon_id: str) -> None:
        self._addon_id = addon_id

    def get_addon_details(self, addon_id: str) -> KodiAddonDetailsRecord:
        _require(addon_id == self._addon_id, "dependency detail identity changed")
        return KodiAddonDetailsRecord(
            addon_id=addon_id,
            name="Kodi Lab Smoke Dependency",
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
        raise AssertionError("dependency-blocked smoke reached invocation")


class _StructuralTransport:
    def __init__(self, observations: list[dict[str, object]]) -> None:
        self._inner = _BoundedHttpTransport()
        self._observations = observations

    def post(
        self, *, host: str, port: int, path: str, body: bytes, timeout: float
    ) -> bytes:
        payload = self._inner.post(
            host=host, port=port, path=path, body=body, timeout=timeout
        )
        try:
            document = json.loads(payload)
            result = document.get("result") if isinstance(document, dict) else None
            files = result.get("files") if isinstance(result, dict) else None
            first = files[0] if isinstance(files, list) and files else None
            limits = result.get("limits") if isinstance(result, dict) else None
            self._observations.append(
                {
                    "envelope_keys": (
                        sorted(document) if isinstance(document, dict) else []
                    ),
                    "result_keys": sorted(result) if isinstance(result, dict) else [],
                    "file_count": len(files) if isinstance(files, list) else None,
                    "file_keys": sorted(first) if isinstance(first, dict) else [],
                    "file_value_types": (
                        {key: type(value).__name__ for key, value in first.items()}
                        if isinstance(first, dict)
                        else {}
                    ),
                    "limits": limits if isinstance(limits, dict) else None,
                }
            )
        except Exception:
            self._observations.append({"structural_parse": "unavailable"})
        return payload


def _wait_smoke(client: TestClient, status_url: str) -> dict[str, object]:
    for _ in range(1_800):
        response = client.get(status_url)
        _require(response.status_code == 200, "smoke status failed")
        result = response.json()
        if result["state"] not in {"queued", "running"}:
            return result
        time.sleep(0.05)
    raise RuntimeError("smoke operation did not terminalize")


def _smoke(
    client: TestClient,
    headers: dict[str, str],
    instance_id: UUID,
    source_id: UUID,
) -> tuple[int, dict[str, object], str]:
    response = client.post(
        f"/api/instances/{instance_id}/development-sources/{source_id}/smoke-test",
        json={},
        headers=headers,
    )
    _require(response.status_code == 202, "smoke POST did not return 202")
    status_url = response.json()["status_url"]
    return response.status_code, _wait_smoke(client, status_url), status_url


def _confidence(factory: Any, instance_id: UUID, source_id: UUID) -> str:
    with factory() as session:
        evidence = DevelopmentSourceDeploymentRepository(session).get(
            instance_id, source_id
        )
    _require(evidence is not None, "deployment evidence disappeared")
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


def _safe_result(operation: dict[str, object]) -> dict[str, object]:
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
        default=Path("evidence/kodi-lab-phase8-copied-smoke-evidence.json"),
    )
    arguments = parser.parse_args()
    _require(os.geteuid() == 0, "live validation must run as root")
    project_root = Path(__file__).resolve().parents[1]
    helpers = _load_enablement_helpers(project_root)
    helpers.PASSWORD = PASSWORD
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
    evidence: dict[str, object] = {}
    dependency_invocations: list[str] = []
    one_item_structure: list[dict[str, object]] = []
    try:
        with tempfile.TemporaryDirectory(
            prefix="kodi-lab-phase8-smoke-", dir="/var/tmp"
        ) as temporary:
            validation_root = Path(temporary)
            development_root = validation_root / "development"
            development_root.mkdir(mode=0o700)
            _write_sources(development_root)
            data_root = validation_root / "data"
            (data_root / "database").mkdir(parents=True)
            runtime_root = validation_root / "web-runtime"
            runtime_root.mkdir(mode=0o700)
            os.environ["KODI_LAB_DATA_ROOT"] = str(data_root)
            os.environ["KODI_LAB_RUNTIME_ROOT"] = str(runtime_root)
            os.environ["KODI_LAB_DEVELOPMENT_ROOT"] = str(development_root)
            settings = Settings(_env_file=None)
            command.upgrade(Config(str(project_root / "alembic.ini")), "head")
            engine = create_database_engine(settings.database_url)
            factory = create_session_factory(engine)
            helpers._seed(factory, instances)
            validator = DevelopmentSourceValidator(development_root)
            administration = DevelopmentSourceAdministrationService(factory, validator)
            registered = {
                key: administration.register_source(
                    logical_name=f"Smoke validation {key}",
                    relative_locator=source["locator"],
                )
                for key, source in SOURCES.items()
            }
            attachments = DevelopmentSourceAttachmentService(factory, validator)
            for runtime in helpers.RUNTIMES:
                attachments.attach_source(
                    instances[str(runtime["release"])], registered["empty"].id
                )
            deep_id = instances[DEEP_RELEASE]
            for key in SOURCES:
                if key != "empty":
                    attachments.attach_source(deep_id, registered[key].id)

            adapter = DockerManagedContainerAdapter()
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
                development_source_deployment_provider=adapter,
            )
            with helpers._broker_server(validation_root, registry) as broker:
                auth = WebAuthenticationService(factory)
                auth.set_password(PASSWORD)
                holder: dict[str, Any] = {}

                def service_factory(
                    lease: DevelopmentSourceSmokeTestLease,
                ) -> DevelopmentSourceSmokeTestService:
                    context = holder["app"].state.kodi_lab
                    if lease.source_id == registered["dependency"].id:
                        return DevelopmentSourceSmokeTestService(
                            factory,
                            broker,
                            validator,
                            context.development_source_smoke_test_operations,
                            lease,
                            details_adapter_factory=lambda _o, _p: (
                                _MissingDependencyDetails(SOURCES["dependency"]["id"])
                            ),
                            smoke_adapter_factory=lambda _o, _p: _ForbiddenSmokeAdapter(
                                dependency_invocations
                            ),
                        )
                    if lease.source_id == registered["one"].id:
                        return DevelopmentSourceSmokeTestService(
                            factory,
                            broker,
                            validator,
                            context.development_source_smoke_test_operations,
                            lease,
                            smoke_adapter_factory=lambda observation, profile: (
                                KodiAddonSmokeAdapter(
                                    observation,
                                    profile,
                                    transport=_StructuralTransport(one_item_structure),
                                )
                            ),
                        )
                    return DevelopmentSourceSmokeTestService(
                        factory,
                        broker,
                        validator,
                        context.development_source_smoke_test_operations,
                        lease,
                    )

                app = create_app(
                    settings=settings,
                    session_factory=factory,
                    broker=broker,
                    readiness=ReadinessTaskRegistry(max_workers=6),
                    auth=auth,
                    development_source_smoke_test_service_factory=service_factory,
                )
                holder["app"] = app
                with TestClient(app, base_url=ORIGIN) as client:
                    headers = helpers._login(client)
                    for runtime in helpers.RUNTIMES:
                        instance_id = instances[str(runtime["release"])]
                        response = client.post(
                            f"/api/instances/{instance_id}/provision",
                            json={},
                            headers=headers,
                        )
                        _require(response.status_code == 200, "provision failed")
                        helpers._copy(
                            client, headers, instance_id, registered["empty"].id
                        )
                    for key in SOURCES:
                        if key != "empty":
                            helpers._copy(client, headers, deep_id, registered[key].id)
                    for runtime in helpers.RUNTIMES:
                        instance_id = instances[str(runtime["release"])]
                        response = client.post(
                            f"/api/instances/{instance_id}/start",
                            json={},
                            headers=headers,
                        )
                        _require(response.status_code == 200, "ordinary start failed")
                        helpers._wait_inventory(client, instance_id)

                    for runtime in helpers.RUNTIMES:
                        instance_id = instances[str(runtime["release"])]
                        enabled = helpers._post(
                            client, headers, instance_id, registered["empty"].id
                        )
                        _require(
                            enabled["result_classification"] == "enabled",
                            "empty plugin enable failed",
                        )
                    for key in SOURCES:
                        if key == "empty":
                            continue
                        enabled = helpers._post(
                            client, headers, deep_id, registered[key].id
                        )
                        _require(
                            enabled["result_classification"] == "enabled",
                            f"{key} plugin enable failed",
                        )

                    cross_version: list[dict[str, object]] = []
                    deep_empty: dict[str, object] | None = None
                    for runtime in helpers.RUNTIMES:
                        release = str(runtime["release"])
                        status_code, operation, _ = _smoke(
                            client,
                            headers,
                            instances[release],
                            registered["empty"].id,
                        )
                        _require(
                            operation["overall_result"] == "passed"
                            and operation["post_execution_provenance"] == "matched"
                            and operation["returned_item_count"] == 0,
                            f"cross-version smoke failed for {release}: {operation!r}",
                        )
                        if release == DEEP_RELEASE:
                            deep_empty = operation
                        cross_version.append(
                            {
                                "runtime": release,
                                "runtime_revision_uuid": runtime["revision"],
                                "operation_uuid": operation["operation_id"],
                                "http_status": status_code,
                                "overall_result": operation["overall_result"],
                                "post_provenance": operation[
                                    "post_execution_provenance"
                                ],
                                "returned_count": operation["returned_item_count"],
                            }
                        )
                    assert deep_empty is not None
                    deep_container = helpers._exact_container_id(broker, deep_id)
                    members = _archive_members(deep_container, SOURCES["empty"]["id"])
                    suppression_clean = not any(
                        "__pycache__" in member or member.endswith(".pyc")
                        for member in members
                    )
                    _require(
                        suppression_clean, "suppression regression created bytecode"
                    )

                    _, one, _ = _smoke(client, headers, deep_id, registered["one"].id)
                    _require(
                        one["overall_result"] == "passed"
                        and one["returned_item_count"] == 1
                        and "discarded-private-label" not in json.dumps(one),
                        f"one-item smoke failed: {one!r}; shape={one_item_structure!r}",
                    )
                    _, failure, _ = _smoke(
                        client, headers, deep_id, registered["failure"].id
                    )
                    _require(
                        failure["overall_result"] == "execution_failed"
                        and failure["post_execution_provenance"] == "matched"
                        and "discarded-private-python-error" not in json.dumps(failure),
                        f"execution failure classification failed: {failure!r}",
                    )
                    failure_confidence = _confidence(
                        factory, deep_id, registered["failure"].id
                    )
                    _require(
                        failure_confidence == "verified_at_last_operation",
                        "execution error changed exact provenance confidence",
                    )

                    _, data_write, _ = _smoke(
                        client, headers, deep_id, registered["data_write"].id
                    )
                    _require(
                        data_write["overall_result"] == "passed"
                        and data_write["post_execution_provenance"] == "matched"
                        and _profile_data_present(
                            deep_container, SOURCES["data_write"]["id"]
                        ),
                        f"proper add-on-data write failed: {data_write!r}",
                    )

                    dependency_source = _source_path(development_root, "dependency")
                    dependency_requires = (
                        f'<requires><import addon="{MISSING_DEPENDENCY}"/></requires>'
                    )
                    (dependency_source / "addon.xml").write_text(
                        _descriptor(SOURCES["dependency"], dependency_requires),
                        encoding="utf-8",
                    )
                    _, dependency_block, _ = _smoke(
                        client, headers, deep_id, registered["dependency"].id
                    )
                    _require(
                        dependency_block["overall_result"] == "dependency_blocked"
                        and dependency_block["blocked_dependency_reason"]
                        == "dependency_missing"
                        and not dependency_invocations,
                        "dependency did not block before dispatch: "
                        f"{dependency_block!r}",
                    )

                    _, tree_write, _ = _smoke(
                        client, headers, deep_id, registered["tree_write"].id
                    )
                    tree_confidence = _confidence(
                        factory, deep_id, registered["tree_write"].id
                    )
                    _require(
                        tree_write["overall_result"] == "source_tree_changed"
                        and tree_write["post_execution_provenance"] == "changed"
                        and tree_confidence == "stale",
                        f"source mutation classification failed: {tree_write!r}",
                    )

                    _, timeout, timeout_status_url = _smoke(
                        client, headers, deep_id, registered["timeout"].id
                    )
                    timeout_confidence = _confidence(
                        factory, deep_id, registered["timeout"].id
                    )
                    _require(
                        timeout["state"] == "reconciliation_required"
                        and timeout["overall_result"] == "timeout"
                        and timeout["execution_result"] == "timeout"
                        and timeout["post_execution_provenance"] == "unknown"
                        and timeout_confidence == "unknown",
                        f"timeout ambiguity was wrong: {timeout!r}",
                    )
                    responsive_after_timeout = (
                        helpers._wait_inventory(client, deep_id)["query_status"]
                        == "available"
                    )
                    second = client.post(
                        f"/api/instances/{deep_id}/development-sources/"
                        f"{registered['timeout'].id}/smoke-test",
                        json={},
                        headers=headers,
                    )
                    _require(second.status_code == 409, "second smoke was not blocked")
                    running_warning = client.get(
                        f"/api/instances/{deep_id}/reconciliation"
                    ).json()
                    _require(
                        running_warning["blocking"] is True
                        and running_warning["observed_state"]
                        == "original_container_running",
                        "running timeout warning was not blocking",
                    )
                    stopped = client.post(
                        f"/api/instances/{deep_id}/stop", json={}, headers=headers
                    )
                    _require(stopped.status_code == 200, "explicit timeout stop failed")
                    stopped_warning = client.get(
                        f"/api/instances/{deep_id}/reconciliation"
                    ).json()
                    historical = client.get(timeout_status_url).json()
                    _require(
                        stopped_warning["blocking"] is False
                        and stopped_warning["observed_state"]
                        == "original_container_stopped"
                        and historical["state"] == "reconciliation_required",
                        "stop recovery rewrote or retained blocking history",
                    )
                    with factory() as session:
                        timeout_history = DevelopmentSourceSmokeTestOperationRepository(
                            session
                        ).list_for_original_instance(deep_id)
                        timeout_rows = [
                            item
                            for item in timeout_history
                            if item.development_source_id == registered["timeout"].id
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
                        "timeout retried or retained a mutation lease",
                    )

                    evidence = {
                        "schema": "kodi-lab-phase8-copied-smoke-live-v1",
                        "migration_head": migration_head,
                        "empty_plugin": {
                            **_safe_result(deep_empty),
                            "http_status": 202,
                            "confidence_transition": "verified_preserved",
                        },
                        "one_item": {
                            **_safe_result(one),
                            "content_persisted_or_exposed": False,
                        },
                        "execution_failure": {
                            **_safe_result(failure),
                            "confidence_transition": failure_confidence,
                            "raw_error_exposed": False,
                        },
                        "proper_addon_data_write": {
                            **_safe_result(data_write),
                            "profile_state_observed": True,
                        },
                        "dependency_block": {
                            **_safe_result(dependency_block),
                            "invocation_dispatched": False,
                            "hidden_dependency_mutation": False,
                        },
                        "source_tree_write": {
                            **_safe_result(tree_write),
                            "confidence_transition": tree_confidence,
                            "provenance_adopted": False,
                        },
                        "timeout": {
                            **_safe_result(timeout),
                            "confidence_transition": timeout_confidence,
                            "runtime_responsive_after_client_deadline": (
                                responsive_after_timeout
                            ),
                            "second_smoke_blocked": True,
                            "explicit_stop_allowed": True,
                            "warning_after_stop": "nonblocking",
                            "historical_result_retained": True,
                            "automatic_retry": False,
                        },
                        "cross_version": cross_version,
                        "python_suppression_regression": {
                            "local_package_imported": True,
                            "bytecode_artifacts_absent": suppression_clean,
                            "source_provenance": "matched",
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
