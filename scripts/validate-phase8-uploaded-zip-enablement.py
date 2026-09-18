#!/usr/bin/env python3
"""Validate ZIP-specific enablement without any ZIP smoke execution."""

# ruff: noqa: E402 -- bytecode suppression must precede project imports.

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import UUID, uuid4

sys.dont_write_bytecode = True

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import text

from kodi_lab.application.addon_zip_enablements import (
    AddonZipEnablementOperationCoordinator,
    AddonZipEnablementService,
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
from kodi_lab.kodi.addons import KodiAddonEnablementAdapter
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.storage.repositories import (
    AddonZipEnablementOperationRepository,
    AddonZipInstallationEvidenceRepository,
    ManagedInstanceMutationLeaseRepository,
)
from kodi_lab.web.app import create_app

PASSWORD = "phase8-uploaded-zip-enablement-live-validation"
ORIGIN = "http://127.0.0.1"
DEEP_RELEASE = "21.3"


def _module(filename: str, name: str) -> ModuleType:
    path = Path(__file__).with_name(filename)
    specification = importlib.util.spec_from_file_location(name, path)
    if specification is None or specification.loader is None:
        raise RuntimeError(f"validation helper {filename} is unavailable")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


class CountingEnableAdapter:
    """Count only target enable dispatches while retaining the real adapter."""

    def __init__(
        self,
        observation: Any,
        profile: Any,
        calls: dict[str, int],
    ) -> None:
        self._inner = KodiAddonEnablementAdapter(observation, profile)
        self._calls = calls

    def get_addon_details(self, addon_id: str) -> Any:
        return self._inner.get_addon_details(addon_id)

    def enable(self, addon_id: str) -> None:
        self._calls[addon_id] = self._calls.get(addon_id, 0) + 1
        self._inner.enable(addon_id)


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
    return response.json()


def _confidence(factory: Any, instance_id: UUID, addon_id: str) -> str | None:
    with factory() as session:
        evidence = AddonZipInstallationEvidenceRepository(session).get(
            instance_id, addon_id
        )
    return None if evidence is None else evidence.confidence.value


def _fault() -> None:
    raise RuntimeError("deterministic post-enable validation fault")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--evidence-path",
        type=Path,
        default=Path(
            "evidence/kodi-lab-phase8-uploaded-zip-enablement-evidence.json"
        ),
    )
    arguments = parser.parse_args()
    helper = _module("validate-phase8-copied-enablement.py", "phase8_helpers")
    uploaded = _module("validate-phase8-uploaded-zip.py", "phase8_zip_helpers")
    uploaded.PASSWORD = PASSWORD
    uploaded.ORIGIN = ORIGIN
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
            prefix="kodi-lab-phase8-zip-enable-", dir="/var/tmp"
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

            docker_adapter = DockerManagedContainerAdapter()
            zip_resolver = SqliteAddonZipInstallationResolver(
                factory, data_root=data_root
            )
            registry = build_foundation_registry(
                docker_adapter,
                provisioning_resolver=SqliteProvisioningResolver(factory),
                provisioning_provider=docker_adapter,
                deprovisioning_provider=docker_adapter,
                revision_resolver=SqliteManagedInstanceRevisionResolver(factory),
                runtime_observation_resolver=SqliteRuntimeObservationResolver(factory),
                runtime_observation_provider=docker_adapter,
                addon_zip_installation_resolver=zip_resolver,
                addon_zip_installation_provider=docker_adapter,
            )
            with helper._broker_server(validation_root, registry) as broker:
                auth = WebAuthenticationService(factory)
                auth.set_password(PASSWORD)
                calls: dict[str, int] = {}
                stale = StaleOperationReconciliationDispatcher(
                    factory, broker=broker, data_root=data_root
                )
                reconciliation = ManagedInstanceReconciliationService(
                    factory, broker=broker, data_root=data_root
                )
                coordinator = AddonZipEnablementOperationCoordinator(
                    factory,
                    stale_dispatcher=stale,
                    reconciliation=reconciliation,
                )

                def adapter_factory(
                    observation: Any, profile: Any
                ) -> CountingEnableAdapter:
                    return CountingEnableAdapter(observation, profile, calls)

                enable_service = AddonZipEnablementService(
                    factory,
                    broker,
                    coordinator=coordinator,
                    enable_adapter_factory=adapter_factory,
                )
                app = create_app(
                    settings=settings,
                    session_factory=factory,
                    broker=broker,
                    readiness=ReadinessTaskRegistry(max_workers=6),
                    auth=auth,
                    addon_zip_enablement_service=enable_service,
                )
                with TestClient(app, base_url=ORIGIN) as client:
                    headers = uploaded._login(client)
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
                        uploaded._inventory(client, instance_id)

                    basic_id = "plugin.video.kodi-lab-zip-enable-basic"
                    basic_import = uploaded._upload(
                        client,
                        headers,
                        label="ZIP enable basic",
                        payload=uploaded._archive(basic_id, "ZIP Enable Basic"),
                    )
                    artifact_id = str(basic_import["artifact_id"])
                    cross_version: list[dict[str, Any]] = []
                    for runtime in helper.RUNTIMES:
                        release = str(runtime["release"])
                        instance_id = instances[release]
                        install = uploaded._install(
                            client, headers, instance_id, artifact_id
                        )
                        before_calls = calls.get(basic_id, 0)
                        result = _enable(client, headers, instance_id, artifact_id)
                        live = uploaded._item(
                            uploaded._inventory(client, instance_id), basic_id
                        )
                        _require(
                            install["state"] == "succeeded"
                            and result["result"] == "enabled"
                            and result["payload_verification"] == "matched"
                            and result["enable_performed"] is True
                            and calls.get(basic_id, 0) == before_calls + 1
                            and live is not None
                            and live["enabled"] is True
                            and _confidence(factory, instance_id, basic_id)
                            == "verified_at_last_operation",
                            f"cross-version ZIP enable failed for {release}",
                        )
                        cross_version.append(
                            {
                                "kodi_release": release,
                                "runtime_revision_uuid": runtime["revision"],
                                "instance_uuid": str(instance_id),
                                "artifact_uuid": artifact_id,
                                "enablement_operation_uuid": result["operation_id"],
                                "pre_enabled": result["prior_enabled"],
                                "post_enabled": result["result_enabled"],
                                "payload_verification": result["payload_verification"],
                                "dependency_block": None,
                                "enable_performed": result["enable_performed"],
                                "reconciliation": False,
                            }
                        )

                    deep_id = instances[DEEP_RELEASE]
                    before_container = helper._exact_container_id(broker, deep_id)
                    _require(
                        client.post(
                            f"/api/instances/{deep_id}/stop",
                            json={},
                            headers=headers,
                        ).status_code
                        == 200,
                        "restart-persistence stop failed",
                    )
                    _require(
                        client.post(
                            f"/api/instances/{deep_id}/start",
                            json={},
                            headers=headers,
                        ).status_code
                        == 200,
                        "restart-persistence start failed",
                    )
                    persisted = uploaded._item(
                        uploaded._inventory(client, deep_id), basic_id
                    )
                    _require(
                        helper._exact_container_id(broker, deep_id) == before_container
                        and persisted is not None
                        and persisted["enabled"] is True
                        and _confidence(factory, deep_id, basic_id)
                        == "verified_at_last_operation",
                        "same-container restart did not preserve enablement lineage",
                    )

                    already_calls = calls.get(basic_id, 0)
                    already = _enable(client, headers, deep_id, artifact_id)
                    _require(
                        already["result"] == "already_enabled"
                        and already["enable_performed"] is False
                        and already["payload_verification"] == "matched"
                        and calls.get(basic_id, 0) == already_calls,
                        "already-enabled path sent a second mutation",
                    )

                    missing_id = "plugin.video.kodi-lab-zip-enable-missing"
                    missing_dependency = "script.module.kodi-lab-absent"
                    missing_import = uploaded._upload(
                        client,
                        headers,
                        label="ZIP enable missing dependency",
                        payload=uploaded._archive(
                            missing_id,
                            "ZIP Enable Missing Dependency",
                            dependency_id=missing_dependency,
                        ),
                    )
                    uploaded._install(
                        client, headers, deep_id, str(missing_import["artifact_id"])
                    )
                    before_missing = calls.get(missing_id, 0)
                    missing = _enable(
                        client, headers, deep_id, str(missing_import["artifact_id"])
                    )
                    missing_live = uploaded._item(
                        uploaded._inventory(client, deep_id), missing_id
                    )
                    _require(
                        missing["result"] == "dependency_blocked"
                        and missing["failure_reason"] == "dependency_missing"
                        and calls.get(missing_id, 0) == before_missing
                        and missing_live is not None
                        and missing_live["enabled"] is False,
                        "missing dependency did not block before enable dispatch",
                    )

                    dependency_id = "plugin.video.kodi-lab-zip-dependency"
                    dependency_import = uploaded._upload(
                        client,
                        headers,
                        label="ZIP controlled disabled dependency",
                        payload=uploaded._archive(
                            dependency_id, "ZIP Controlled Disabled Dependency"
                        ),
                    )
                    dependency_install = uploaded._install(
                        client,
                        headers,
                        deep_id,
                        str(dependency_import["artifact_id"]),
                    )
                    dependency_before = uploaded._item(
                        uploaded._inventory(client, deep_id), dependency_id
                    )
                    _require(
                        dependency_install["state"] == "succeeded"
                        and dependency_before is not None
                        and dependency_before["enabled"] is False,
                        "controlled disabled dependency setup failed",
                    )
                    disabled_id = "plugin.video.kodi-lab-zip-enable-disabled"
                    disabled_import = uploaded._upload(
                        client,
                        headers,
                        label="ZIP enable disabled dependency",
                        payload=uploaded._archive(
                            disabled_id,
                            "ZIP Enable Disabled Dependency",
                            dependency_id=dependency_id,
                        ),
                    )
                    uploaded._install(
                        client, headers, deep_id, str(disabled_import["artifact_id"])
                    )
                    before_disabled = calls.get(disabled_id, 0)
                    disabled = _enable(
                        client,
                        headers,
                        deep_id,
                        str(disabled_import["artifact_id"]),
                    )
                    after_disabled = uploaded._inventory(client, deep_id)
                    disabled_live = uploaded._item(after_disabled, disabled_id)
                    dependency_live = uploaded._item(after_disabled, dependency_id)
                    _require(
                        disabled["result"] == "dependency_blocked"
                        and disabled["failure_reason"] == "dependency_disabled"
                        and calls.get(disabled_id, 0) == before_disabled
                        and disabled_live is not None
                        and disabled_live["enabled"] is False
                        and dependency_live is not None
                        and dependency_live["enabled"] is False,
                        "disabled dependency was mutated or failed to block",
                    )

                    drift_id = "plugin.video.kodi-lab-zip-enable-drift"
                    drift_import = uploaded._upload(
                        client,
                        headers,
                        label="ZIP enable destination drift",
                        payload=uploaded._archive(drift_id, "ZIP Enable Drift"),
                    )
                    uploaded._install(
                        client, headers, deep_id, str(drift_import["artifact_id"])
                    )
                    helper._add_external_drift(
                        helper._exact_container_id(broker, deep_id), drift_id
                    )
                    before_drift = calls.get(drift_id, 0)
                    drift = _enable(
                        client, headers, deep_id, str(drift_import["artifact_id"])
                    )
                    drift_live = uploaded._item(
                        uploaded._inventory(client, deep_id), drift_id
                    )
                    _require(
                        drift["payload_verification"] == "changed"
                        and drift["failure_reason"] == "installed_payload_changed"
                        and calls.get(drift_id, 0) == before_drift
                        and _confidence(factory, deep_id, drift_id) == "stale"
                        and drift_live is not None
                        and drift_live["enabled"] is False,
                        "destination drift did not stale and block",
                    )

                    stale_id = "plugin.video.kodi-lab-zip-enable-stale"
                    stale_instance = instances["20.5"]
                    stale_import = uploaded._upload(
                        client,
                        headers,
                        label="ZIP enable stale evidence",
                        payload=uploaded._archive(stale_id, "ZIP Enable Stale"),
                    )
                    uploaded._install(
                        client,
                        headers,
                        stale_instance,
                        str(stale_import["artifact_id"]),
                    )
                    _require(
                        client.post(
                            f"/api/instances/{stale_instance}/stop",
                            json={},
                            headers=headers,
                        ).status_code
                        == 200,
                        "stale-evidence stop failed",
                    )
                    _require(
                        client.post(
                            f"/api/instances/{stale_instance}/deprovision",
                            json={},
                            headers=headers,
                        ).status_code
                        == 200,
                        "stale-evidence deprovision failed",
                    )
                    before_stale = calls.get(stale_id, 0)
                    stale_result = _enable(
                        client,
                        headers,
                        stale_instance,
                        str(stale_import["artifact_id"]),
                    )
                    _require(
                        stale_result["failure_reason"]
                        == "zip_installation_evidence_stale"
                        and stale_result["payload_verification"] == "not_checked"
                        and calls.get(stale_id, 0) == before_stale,
                        "normally invalidated evidence did not block pre-verifier",
                    )

                    ambiguous_id = "plugin.video.kodi-lab-zip-enable-ambiguous"
                    ambiguous_import = uploaded._upload(
                        client,
                        headers,
                        label="ZIP enable ambiguous mutation",
                        payload=uploaded._archive(
                            ambiguous_id, "ZIP Enable Ambiguous Mutation"
                        ),
                    )
                    uploaded._install(
                        client,
                        headers,
                        deep_id,
                        str(ambiguous_import["artifact_id"]),
                    )

                fault_service = AddonZipEnablementService(
                    factory,
                    broker,
                    coordinator=coordinator,
                    enable_adapter_factory=adapter_factory,
                    after_enable_hook=_fault,
                )
                fault_app = create_app(
                    settings=settings,
                    session_factory=factory,
                    broker=broker,
                    readiness=ReadinessTaskRegistry(max_workers=2),
                    auth=auth,
                    addon_zip_enablement_service=fault_service,
                )
                with TestClient(fault_app, base_url=ORIGIN) as client:
                    headers = uploaded._login(client)
                    before_ambiguous = calls.get(ambiguous_id, 0)
                    ambiguous = _enable(
                        client,
                        headers,
                        deep_id,
                        str(ambiguous_import["artifact_id"]),
                    )
                    _require(
                        ambiguous["state"] == "reconciliation_required"
                        and ambiguous["result"] == "reconciliation_required"
                        and calls.get(ambiguous_id, 0) == before_ambiguous + 1
                        and _confidence(factory, deep_id, ambiguous_id) == "unknown",
                        "ambiguous dispatch was retried or misclassified",
                    )
                    with factory() as session:
                        retained_ambiguous = AddonZipEnablementOperationRepository(
                            session
                        ).get(UUID(str(ambiguous["operation_id"])))
                        active_lease = ManagedInstanceMutationLeaseRepository(
                            session
                        ).current(deep_id)
                    _require(
                        retained_ambiguous is not None
                        and retained_ambiguous.reconciliation_required
                        and active_lease is None,
                        "ambiguous history or lease release was invalid",
                    )

                with factory() as session:
                    migration_head = session.scalar(
                        text("SELECT version_num FROM alembic_version")
                    )
                evidence = {
                    "schema": "kodi-lab-phase8-uploaded-zip-enablement-live-v1",
                    "migration_head": migration_head,
                    "standard_enable": cross_version[2],
                    "restart_persistence": {
                        "instance_uuid": str(deep_id),
                        "artifact_uuid": artifact_id,
                        "enabled_after_same_container_restart": True,
                        "installation_confidence": "verified_at_last_operation",
                    },
                    "already_enabled": {
                        "instance_uuid": str(deep_id),
                        "artifact_uuid": artifact_id,
                        "enablement_operation_uuid": already["operation_id"],
                        "pre_enabled": True,
                        "post_enabled": True,
                        "payload_verification": "matched",
                        "dependency_block": None,
                        "enable_performed": False,
                        "reconciliation": False,
                    },
                    "missing_dependency": {
                        "artifact_uuid": missing_import["artifact_id"],
                        "instance_uuid": str(deep_id),
                        "enablement_operation_uuid": missing["operation_id"],
                        "pre_enabled": None,
                        "post_enabled": False,
                        "payload_verification": "matched",
                        "dependency_block": "dependency_missing",
                        "enable_performed": False,
                        "reconciliation": False,
                    },
                    "disabled_dependency": {
                        "artifact_uuid": disabled_import["artifact_id"],
                        "instance_uuid": str(deep_id),
                        "enablement_operation_uuid": disabled["operation_id"],
                        "pre_enabled": None,
                        "post_enabled": False,
                        "payload_verification": "matched",
                        "dependency_block": "dependency_disabled",
                        "enable_performed": False,
                        "dependency_remained_disabled": True,
                        "reconciliation": False,
                    },
                    "destination_drift": {
                        "artifact_uuid": drift_import["artifact_id"],
                        "instance_uuid": str(deep_id),
                        "enablement_operation_uuid": drift["operation_id"],
                        "pre_enabled": None,
                        "post_enabled": False,
                        "payload_verification": "changed",
                        "dependency_block": None,
                        "enable_performed": False,
                        "installation_confidence": "stale",
                        "reconciliation": False,
                    },
                    "stale_evidence": {
                        "artifact_uuid": stale_import["artifact_id"],
                        "instance_uuid": str(stale_instance),
                        "enablement_operation_uuid": stale_result["operation_id"],
                        "payload_verification": "not_checked",
                        "enable_performed": False,
                        "normal_invalidation": "deprovision",
                    },
                    "ambiguous_mutation": {
                        "artifact_uuid": ambiguous_import["artifact_id"],
                        "instance_uuid": str(deep_id),
                        "enablement_operation_uuid": ambiguous["operation_id"],
                        "pre_enabled": False,
                        "post_enabled": None,
                        "payload_verification": "matched",
                        "dependency_block": None,
                        "enable_performed": True,
                        "reconciliation": True,
                        "installation_confidence": "unknown",
                        "automatic_retry": False,
                        "automatic_disable": False,
                        "automatic_toggle": False,
                    },
                    "cross_version": cross_version,
                    "zip_smoke_execution": False,
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
        "temporary_zip_artifacts_and_staging_cleaned": True,
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
