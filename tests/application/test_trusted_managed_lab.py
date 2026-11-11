"""Managed role convenience preserves external isolation and current admission."""

from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.mcp_administration import (
    FULL_LAB_CONTROL_PRESET_ID,
    READ_ONLY_INSPECTION_PRESET_ID,
    McpAdministrationService,
    McpAuthenticationService,
    McpPrincipal,
)
from kodi_lab.domain.managed_lab import TRUSTED_MANAGED_LAB_PERMISSIONS
from kodi_lab.mcp.authorization import (
    McpAuthorizationError,
    McpAuthorizationService,
    TargetTechnicalAuthority,
)
from kodi_lab.mcp.registry import TargetClass, build_mcp_tool_registry
from kodi_lab.storage.managed_version_slots import managed_version_slots
from kodi_lab.storage.models import (
    ManagedInstanceRow,
    McpClientTargetGrantRow,
    McpTargetPresetAssignmentRow,
    TargetRow,
)
from tests.application.test_mcp_foundation import (
    REVISION_ID,
    TARGET_ID,
    FixedTargetAuthority,
)
from tests.application.test_mcp_foundation import mcp_database as mcp_database


def actor(
    factory: sessionmaker[Session],
) -> tuple[McpAdministrationService, McpPrincipal]:
    admin = McpAdministrationService(factory)
    preset = admin.create_custom_preset(
        display_name="Independent base authority",
        template_preset_id=FULL_LAB_CONTROL_PRESET_ID,
        permissions=frozenset({"workspace.inspect"}),
    )
    client = admin.create_client(
        display_name="Coding agent", preset_id=preset.id, trusted_managed_lab=True
    )
    token = admin.issue_credential(client.id).token
    principal = McpAuthenticationService(factory).authenticate(token)
    assert principal is not None
    return admin, principal


def authorization(factory: sessionmaker[Session]) -> McpAuthorizationService:
    return McpAuthorizationService(
        factory,
        build_mcp_tool_registry(),
        targets=FixedTargetAuthority(
            TargetTechnicalAuthority(
                TargetClass.INTERNAL_LAB,
                frozenset(
                    {"managed_runtime_status", "target_inspect", "semantic_remote"}
                ),
                frozenset({"running", "ready", "reconciled"}),
            )
        ),
    )


def add_target(
    factory: sessionmaker[Session], target_id: UUID, kind: str = "managed"
) -> None:
    with factory.begin() as session:
        session.add(
            TargetRow(
                id=str(target_id),
                name="Same friendly name",
                target_type=kind,
                kodi_version="21.3",
                availability="available",
                health="healthy",
                connection_state="connected",
            )
        )
        session.flush()
        if kind == "managed":
            session.add(
                ManagedInstanceRow(
                    target_id=str(target_id),
                    instance_type="disposable",
                    runtime_revision_id=str(REVISION_ID),
                    runtime_state="stopped",
                    start_on_host_boot=False,
                    notes=None,
                )
            )


def test_role_covers_later_installed_managed_without_grants(
    mcp_database: sessionmaker[Session],
) -> None:
    admin, principal = actor(mcp_database)
    auth = authorization(mcp_database)
    auth.authorize_target(principal, "target_status", TARGET_ID)
    auth.authorize_target(principal, "remote_action", TARGET_ID)
    later = uuid4()
    add_target(mcp_database, later)
    assert set(auth.granted_target_ids(principal)) == {TARGET_ID, later}
    auth.authorize_target(principal, "target_status", later)
    client = admin.get_client(principal.client_id)
    assert client.target_grants == ()
    assert client.managed_lab_role_version == 1
    assert frozenset(client.managed_lab_permissions) == TRUSTED_MANAGED_LAB_PERMISSIONS
    assert {
        "managed_reset",
        "managed_start",
        "workspace_edit",
        "development_launch",
        "remote_action",
    } <= set(auth.discoverable_tools(principal))


def test_role_reset_and_lifecycle_recheck_current_authority(
    mcp_database: sessionmaker[Session],
) -> None:
    admin, principal = actor(mcp_database)
    auth = authorization(mcp_database)
    omega = next(
        s.canonical_target_id
        for s in managed_version_slots()
        if s.kodi_version == "21.3"
    )
    add_target(mcp_database, omega)
    with mcp_database() as session:
        auth.recheck_managed_versions(session, principal, "managed_reset", omega)
        auth.recheck_managed_lifecycle(session, principal, "managed_release", omega)
    admin.set_managed_lab_role(principal.client_id, False)
    with mcp_database() as session, pytest.raises(McpAuthorizationError):
        auth.recheck_managed_versions(session, principal, "managed_reset", omega)
    assert admin.get_client(principal.client_id).managed_lab_role_version is None


@pytest.mark.parametrize("kind", ["external_development", "external_production"])
def test_role_denies_external_even_with_explicit_generic_grant(
    mcp_database: sessionmaker[Session], kind: str
) -> None:
    _admin, principal = actor(mcp_database)
    target = uuid4()
    add_target(mcp_database, target, kind)
    # A malformed managed association cannot confer authority on an external target.
    with mcp_database.begin() as session:
        session.add(
            McpClientTargetGrantRow(
                client_id=str(principal.client_id),
                target_id=str(target),
                created_at="2026-11-11T00:00:00.000000Z",
                grant_id=str(uuid4()),
            )
        )
        session.add(
            McpTargetPresetAssignmentRow(
                target_id=str(target),
                preset_id=str(READ_ONLY_INSPECTION_PRESET_ID),
                assigned_at="2026-11-11T00:00:00.000000Z",
            )
        )
        session.add(
            ManagedInstanceRow(
                target_id=str(target),
                instance_type="disposable",
                runtime_revision_id=str(REVISION_ID),
                runtime_state="stopped",
                start_on_host_boot=False,
                notes=None,
            )
        )
    auth = authorization(mcp_database)
    with pytest.raises(McpAuthorizationError):
        auth.authorize_target_eligibility(principal, "target_list", target)
    for tool in ("target_status", "remote_action"):
        with pytest.raises(McpAuthorizationError):
            auth.authorize_target(principal, tool, target)


@pytest.mark.parametrize("change", ["credential", "disabled", "expired"])
def test_role_rechecks_credential_at_admission(
    mcp_database: sessionmaker[Session], change: str
) -> None:
    admin, principal = actor(mcp_database)
    auth = authorization(mcp_database)
    if change == "credential":
        admin.revoke_credential(principal.client_id, principal.credential_id)
    elif change == "disabled":
        admin.set_client_enabled(principal.client_id, False)
    else:
        token = admin.issue_credential(
            principal.client_id, lifetime=timedelta(minutes=1)
        )
        principal = replace(principal, credential_id=token.credential.id)
        from datetime import UTC, datetime

        auth = McpAuthorizationService(
            mcp_database,
            build_mcp_tool_registry(),
            clock=lambda: datetime.now(UTC) + timedelta(minutes=2),
        )
    with pytest.raises(McpAuthorizationError):
        auth.authorize_target(principal, "target_status", TARGET_ID)
    with mcp_database() as session, pytest.raises(McpAuthorizationError):
        auth.recheck_managed_lifecycle(session, principal, "managed_release", TARGET_ID)


def test_legacy_clients_and_role_templates_remain_exact(
    mcp_database: sessionmaker[Session],
) -> None:
    admin, _ = actor(mcp_database)
    legacy = admin.create_client(
        display_name="Legacy", preset_id=READ_ONLY_INSPECTION_PRESET_ID
    )
    token = admin.issue_credential(legacy.id).token
    principal = McpAuthenticationService(mcp_database).authenticate(token)
    assert principal is not None
    with pytest.raises(McpAuthorizationError):
        authorization(mcp_database).authorize_target(
            principal, "target_status", TARGET_ID
        )
    clone = admin.create_custom_preset(
        display_name="Permission clone",
        template_preset_id=FULL_LAB_CONTROL_PRESET_ID,
        permissions=frozenset({"target.inspect"}),
    )
    copied = admin.create_client(display_name="Untrusted clone", preset_id=clone.id)
    assert copied.managed_lab_role_version is None
    assert legacy.managed_lab_role_version is None


def test_separately_granted_external_access_survives_role_revocation(
    mcp_database: sessionmaker[Session],
) -> None:
    admin, principal = actor(mcp_database)
    # The managed role supplies no external permission. The original preset does.
    admin.assign_client_preset(principal.client_id, READ_ONLY_INSPECTION_PRESET_ID)
    target = uuid4()
    add_target(mcp_database, target, "external_development")
    with mcp_database.begin() as session:
        session.add(
            McpClientTargetGrantRow(
                client_id=str(principal.client_id),
                target_id=str(target),
                created_at="2026-11-11T00:00:00.000000Z",
                grant_id=str(uuid4()),
            )
        )
        session.add(
            McpTargetPresetAssignmentRow(
                target_id=str(target),
                preset_id=str(READ_ONLY_INSPECTION_PRESET_ID),
                assigned_at="2026-11-11T00:00:00.000000Z",
            )
        )
    auth = McpAuthorizationService(
        mcp_database,
        build_mcp_tool_registry(),
        targets=FixedTargetAuthority(
            TargetTechnicalAuthority(
                TargetClass.EXTERNAL_DEVELOPMENT,
                frozenset({"managed_runtime_status", "target_inspect"}),
                frozenset(),
            )
        ),
    )
    auth.authorize_target(principal, "target_status", target)
    with pytest.raises(McpAuthorizationError):
        auth.authorize_target(principal, "remote_action", target)
    admin.set_managed_lab_role(principal.client_id, False)
    auth.authorize_target(principal, "target_status", target)


def test_role_workspace_creation_owner_scope_and_mutation_time_revocation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kodi_lab.mcp.tools import WorkspaceEditInput
    from tests.application.test_mcp_workspace_mutations import (
        SOURCE_ID,
        _environment,
        _finish,
    )

    sessions, workspaces, admin, tools, _submitted = _environment(tmp_path)
    client = admin.create_client(
        display_name="Managed owner",
        preset_id=READ_ONLY_INSPECTION_PRESET_ID,
        trusted_managed_lab=True,
    )
    principal = McpAuthenticationService(sessions).authenticate(
        admin.issue_credential(client.id).token
    )
    assert principal is not None
    admin.grant_development_source(client.id, SOURCE_ID)
    created = tools.workspace_create(principal, request_id=uuid4(), source_id=SOURCE_ID)
    _finish(workspaces, created.creation_operation_id)
    other = admin.create_client(
        display_name="Other managed owner",
        preset_id=READ_ONLY_INSPECTION_PRESET_ID,
        trusted_managed_lab=True,
    )
    other_principal = McpAuthenticationService(sessions).authenticate(
        admin.issue_credential(other.id).token
    )
    assert other_principal is not None
    with pytest.raises(McpAuthorizationError):
        tools.workspace_status(other_principal, workspace_id=created.workspace_id)

    tools.workspace_edit(
        principal,
        WorkspaceEditInput(
            workspace_id=created.workspace_id,
            expected_generation=0,
            operation="create",
            relative_path="accepted.py",
            content="# authorized\n",
        ),
    )
    assert workspaces.get(created.workspace_id).generation == 1

    original = workspaces._mutation_workspace

    def revoke_after_admission(workspace_id: UUID, generation: int) -> object:
        result = original(workspace_id, generation)
        admin.set_managed_lab_role(client.id, False)
        return result

    monkeypatch.setattr(workspaces, "_mutation_workspace", revoke_after_admission)
    with pytest.raises(McpAuthorizationError):
        tools.workspace_edit(
            principal,
            WorkspaceEditInput(
                workspace_id=created.workspace_id,
                expected_generation=1,
                operation="create",
                relative_path="revoked.py",
                content="# never published\n",
            ),
        )
    assert workspaces.get(created.workspace_id).generation == 1
    assert all(
        item.path != "revoked.py"
        for item in workspaces.list_changes(created.workspace_id).changes
    )
