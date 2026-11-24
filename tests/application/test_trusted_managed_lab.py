"""Managed role convenience preserves external isolation and current admission."""

from dataclasses import replace
from datetime import timedelta
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
from kodi_lab.mcp.authorization import (
    McpAuthorizationError,
    McpAuthorizationService,
    TargetTechnicalAuthority,
)
from kodi_lab.mcp.registry import TargetClass, build_mcp_tool_registry
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
        auth.managed_lab_enabled(principal)


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


def test_role_revocation_stops_managed_gateway_admission(
    mcp_database: sessionmaker[Session],
) -> None:
    admin, principal = actor(mcp_database)
    auth = authorization(mcp_database)
    assert auth.managed_lab_enabled(principal)
    admin.set_managed_lab_role(principal.client_id, False)
    assert not auth.managed_lab_enabled(principal)
