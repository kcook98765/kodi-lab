"""Passive module intake and exact owned dependency deployment fences."""

import base64
import hashlib
import io
import zipfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from kodi_lab.application.agent_projects import AgentProjectService, ProjectImportInput
from kodi_lab.application.mcp_administration import McpAdministrationService
from kodi_lab.application.mcp_workspace_development import (
    BoundedIntervalRateLimiter,
    McpWorkspaceDevelopmentError,
)
from kodi_lab.application.mcp_workspace_mutations import McpWorkspaceCreationService
from kodi_lab.application.project_dependencies import verify_owned_dependencies
from kodi_lab.development.addon_zip import (
    AddonZipValidationError,
    read_editable_addon_zip,
)
from kodi_lab.kodi.addons import (
    KodiAddonDetailsRecord,
    KodiAddonInventoryRecord,
    KodiReportedAddonDependency,
)
from kodi_lab.storage.models import (
    AgentProjectRow,
    AgentWorkspaceRow,
    McpWorkspaceDeploymentOperationRow,
)
from kodi_lab.workspace import capture_workspace_deployment_tree
from tests.application import test_mcp_workspace_development as f

MODULE = "script.module.ownedhelper"


@pytest.mark.parametrize(
    ("release", "required"), [("19.4", "3.0.1"), ("22.0-rc.1", "3.2.0")]
)
def test_owned_plugin_checks_builtin_api_before_copy(
    tmp_path: Path,
    release: str,
    required: str,
) -> None:
    env = f._environment(
        tmp_path, kodi_release=release, dependency=("xbmc.python", required)
    )
    env.development.deploy(
        client_id=env.client_id,
        request_id=uuid4(),
        workspace_id=env.workspace_id,
        expected_generation=0,
        target_id=f.TARGET_ID,
    )
    env.development._deployment_rate = BoundedIntervalRateLimiter(
        300, clock=lambda: 2000
    )
    env.broker.stop_count = env.broker.sync_count = env.broker.start_count = 0
    admin = McpAdministrationService(env.sessions)
    admin.set_managed_lab_role(env.client_id, True)
    admin.grant_development_source(env.client_id, f.SOURCE_ID)
    with env.sessions.begin() as db:
        db.add(
            AgentProjectRow(
                source_id=str(f.SOURCE_ID),
                client_id=str(env.client_id),
                request_id=str(uuid4()),
                parameter_fingerprint="b" * 64,
                state="seeded",
                created_at=f.NOW_TEXT,
            )
        )
    with pytest.raises(
        McpWorkspaceDevelopmentError, match="project_dependency_incompatible"
    ):
        env.development.deploy(
            client_id=env.client_id,
            request_id=uuid4(),
            workspace_id=env.workspace_id,
            expected_generation=0,
            target_id=f.TARGET_ID,
        )
    assert env.broker.stop_count == env.broker.sync_count == env.broker.start_count == 0


def module_package(
    *,
    point: str = "xbmc.python.module",
    library: str = "lib",
    dependency: str = "xbmc.python",
) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            MODULE + "/addon.xml",
            f'<addon id="{MODULE}" name="Owned helper" version="1.0.0">'
            f'<requires><import addon="{dependency}" version="3.0.0"/></requires>'
            f'<extension point="{point}" library="{library}"/></addon>',
        )
        archive.writestr(
            MODULE + "/lib/helper/__init__.py", 'def value():\n    return "reviewed"\n'
        )
    return stream.getvalue()


def test_module_intake_is_passive_source_only_and_never_native_zip() -> None:
    payload = module_package()
    result = read_editable_addon_zip(payload, source_modules=True)
    assert result.descriptor.addon_id == MODULE
    assert result.files["lib/helper/__init__.py"].startswith(b"def value")
    with pytest.raises(AddonZipValidationError, match=r"passive script\.module"):
        read_editable_addon_zip(payload)


@pytest.mark.parametrize(
    ("point", "library", "dependency"),
    [
        ("xbmc.service", "lib", "xbmc.python"),
        ("xbmc.python.module", "../lib", "xbmc.python"),
        ("xbmc.python.module", "/lib", "xbmc.python"),
        ("xbmc.python.module", "lib", "script.module.peer"),
        ("xbmc.python.module", "lib", MODULE),
        ("xbmc.python.module", "missing", "xbmc.python"),
    ],
)
def test_unsafe_modules_and_module_graphs_are_refused(
    point: str, library: str, dependency: str
) -> None:
    with pytest.raises(AddonZipValidationError):
        read_editable_addon_zip(
            module_package(point=point, library=library, dependency=dependency),
            source_modules=True,
        )


def environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[f.Environment, UUID, str]:
    env = f._environment(
        tmp_path,
        dependency=(MODULE, "1.0.0"),
        dependency_inventory=KodiAddonInventoryRecord(
            addon_id=MODULE,
            name="Owned helper",
            version="1.0.0",
            enabled=True,
            kodi_type="xbmc.python.module",
        ),
    )
    # Ordinary registered-source receipt supplies the shared durable operation shape.
    baseline = env.development.deploy(
        client_id=env.client_id,
        request_id=uuid4(),
        workspace_id=env.workspace_id,
        expected_generation=0,
        target_id=f.TARGET_ID,
    )
    admin = McpAdministrationService(env.sessions)
    admin.set_managed_lab_role(env.client_id, True)
    admin.grant_development_source(env.client_id, f.SOURCE_ID)
    with env.sessions.begin() as db:
        db.add(
            AgentProjectRow(
                source_id=str(f.SOURCE_ID),
                client_id=str(env.client_id),
                request_id=str(uuid4()),
                parameter_fingerprint="a" * 64,
                state="seeded",
                created_at=f.NOW_TEXT,
            )
        )
    projects = AgentProjectService(
        env.workspaces,
        McpWorkspaceCreationService(env.sessions, env.workspaces, lambda _id: None),
    )
    payload = module_package()
    project = projects.import_project(
        env.client_id,
        ProjectImportInput(
            request_id=uuid4(),
            archive_base64=base64.b64encode(payload).decode(),
            archive_sha256=hashlib.sha256(payload).hexdigest(),
            intake_kind="source_zip",
            provenance_reference="reviewed immutable helper",
        ),
        authorize=lambda _db: None,
    )
    lease = env.workspaces.acquire(project.workspace.creation_operation_id)
    assert lease is not None
    env.workspaces.run(lease)
    validation = env.workspaces.validate_structure(
        project.workspace.workspace_id, expected_generation=0
    )
    env.development.record_validation(
        workspace_id=project.workspace.workspace_id,
        generation=0,
        source_id=project.project_id,
        addon_id=validation.addon_id,
        addon_version="1.0.0",
        extension_types=("xbmc.python.module",),
        validated_at=f.NOW,
    )
    tree = capture_workspace_deployment_tree(
        tmp_path / "data", project.workspace.workspace_id
    )
    with env.sessions.begin() as db:
        original = db.get(
            McpWorkspaceDeploymentOperationRow, str(baseline.deployment_id)
        )
        assert original is not None
        values = {
            c.name: getattr(original, c.name)
            for c in McpWorkspaceDeploymentOperationRow.__table__.columns
        }
        operation_id = str(uuid4())
        values.update(
            id=operation_id,
            request_id=str(uuid4()),
            workspace_id=str(project.workspace.workspace_id),
            development_source_id=str(project.project_id),
            addon_id=MODULE,
            source_manifest_digest=tree.digest,
            verified_destination_digest="sha256:" + "b" * 64,
        )
        db.add(McpWorkspaceDeploymentOperationRow(**values))
    verify = env.broker.verify_mcp_workspace_destination

    def verification(**kwargs: Any) -> Any:
        result = verify(**kwargs)
        if kwargs["deployment_operation_id"] == operation_id:
            return result.model_copy(
                update={
                    "addon_id": MODULE,
                    "development_source_id": str(project.project_id),
                    "video_plugin_descriptor_eligible": False,
                }
            )
        return result

    monkeypatch.setattr(env.broker, "verify_mcp_workspace_destination", verification)
    details = env.development._details_factory

    def factory(observed: Any, profile: Any) -> Any:
        original = details(observed, profile)
        return SimpleNamespace(
            get_addon_details=lambda addon: (
                KodiAddonDetailsRecord(
                    addon_id=MODULE,
                    name="Owned helper",
                    version="1.0.0",
                    enabled=True,
                    kodi_type="xbmc.python.module",
                    dependencies=(
                        KodiReportedAddonDependency("xbmc.python", "3.0.0", False),
                    ),
                )
                if addon == MODULE
                else original.get_addon_details(addon)
            )
        )

    monkeypatch.setattr(env.development, "_details_factory", factory)
    env.broker.stop_count = env.broker.sync_count = env.broker.start_count = 0
    return env, project.workspace.workspace_id, operation_id


@pytest.mark.parametrize(
    "defect",
    [
        "none",
        "missing",
        "disabled",
        "incompatible",
        "unowned",
        "edited",
        "changed_bytes",
        "newer_failed",
        "wrong_runtime",
        "wrong_container",
        "native_type",
        "native_version",
    ],
)
def test_consumer_requires_owned_exact_enabled_compatible_module_before_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    defect: str,
) -> None:
    env, module_workspace, module_operation = environment(tmp_path, monkeypatch)
    if defect in {"missing", "disabled", "incompatible"}:
        factory = env.development._inventory_factory

        def inventory(observed: Any, profile: Any) -> Any:
            original = factory(observed, profile).get_inventory()
            items = tuple(item for item in original.items if item.addon_id != MODULE)
            if defect != "missing":
                items += (
                    KodiAddonInventoryRecord(
                        addon_id=MODULE,
                        name="Helper",
                        version="0.1.0" if defect == "incompatible" else "1.0.0",
                        enabled=defect != "disabled",
                        kodi_type="xbmc.python.module",
                    ),
                )
            return SimpleNamespace(
                get_inventory=lambda: type(original)(original.identity, items)
            )

        monkeypatch.setattr(env.development, "_inventory_factory", inventory)
    elif defect in {
        "unowned",
        "edited",
        "newer_failed",
        "wrong_runtime",
        "wrong_container",
    }:
        with env.sessions.begin() as db:
            row = db.get(McpWorkspaceDeploymentOperationRow, module_operation)
            workspace = db.get(AgentWorkspaceRow, str(module_workspace))
            assert row is not None and workspace is not None
            if defect == "unowned":
                row.client_id = str(uuid4())
            elif defect == "edited":
                workspace.generation += 1
            elif defect == "wrong_runtime":
                row.runtime_revision_id = str(uuid4())
            elif defect == "wrong_container":
                row.original_container_id = "f" * 64
            else:
                row.state = "failed"
    elif defect == "changed_bytes":
        original_verify = env.broker.verify_mcp_workspace_destination

        def changed(**kwargs: Any) -> Any:
            if kwargs["deployment_operation_id"] == module_operation:
                raise ValueError("exact installed tree changed")
            return original_verify(**kwargs)

        monkeypatch.setattr(env.broker, "verify_mcp_workspace_destination", changed)
    elif defect in {"native_type", "native_version"}:
        factory_details = env.development._details_factory

        def changed_details(observed: Any, profile: Any) -> Any:
            original = factory_details(observed, profile)
            return SimpleNamespace(
                get_addon_details=lambda addon: replace(
                    original.get_addon_details(addon),
                    **(
                        {"kodi_type": "xbmc.service"}
                        if defect == "native_type"
                        else {"version": "2.0.0"}
                    ),
                )
            )

        monkeypatch.setattr(env.development, "_details_factory", changed_details)
    if defect == "none":
        verify_owned_dependencies(
            env.development,
            workspace_id=str(env.workspace_id),
            target_id=str(f.TARGET_ID),
            client_id=str(env.client_id),
        )
    else:
        expected = (
            "project_dependency_" + defect
            if defect in {"missing", "disabled", "incompatible"}
            else "exact installed tree changed"
            if defect == "changed_bytes"
            else "project_dependency_changed"
            if defect in {"native_type", "native_version"}
            else "project_dependency_not_deployed"
        )
        with pytest.raises((McpWorkspaceDevelopmentError, ValueError), match=expected):
            verify_owned_dependencies(
                env.development,
                workspace_id=str(env.workspace_id),
                target_id=str(f.TARGET_ID),
                client_id=str(env.client_id),
            )
    assert env.broker.stop_count == env.broker.sync_count == env.broker.start_count == 0
    with env.sessions() as db:
        assert (
            db.scalar(
                select(AgentProjectRow).where(
                    AgentProjectRow.source_id != str(f.SOURCE_ID)
                )
            )
            is not None
        )
