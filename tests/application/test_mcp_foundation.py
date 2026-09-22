from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from starlette.testclient import TestClient
from starlette.types import Receive, Scope, Send

from kodi_lab.application.mcp_administration import (
    ADDON_DEVELOPMENT_PRESET_ID,
    FULL_LAB_CONTROL_PRESET_ID,
    PRODUCTION_REMOTE_KODI_PRESET_ID,
    READ_ONLY_INSPECTION_PRESET_ID,
    McpAdministrationError,
    McpAdministrationService,
    McpAuthenticationService,
    McpPrincipal,
)
from kodi_lab.application.production_inspection import PRODUCTION_INSPECTION_PRESET_ID
from kodi_lab.application.screenshots import ManagedScreenshotService
from kodi_lab.kodi.screenshots import SUPPORTED_SCREENSHOT_RUNTIME_PROFILES
from kodi_lab.mcp.authorization import (
    InternalManagedTargetAuthorityProvider,
    McpAuthorizationError,
    McpAuthorizationService,
    TargetTechnicalAuthority,
)
from kodi_lab.mcp.registry import (
    McpRegistryError,
    McpToolDefinition,
    McpToolRegistry,
    TargetClass,
    ToolEffect,
    ToolRateClass,
    build_mcp_tool_registry,
)
from kodi_lab.mcp.transport import (
    BoundedConcurrencyLimiter,
    BoundedTokenBucketLimiter,
    ModernMcpAuthMiddleware,
)
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.storage.metadata import Base
from kodi_lab.storage.models import (
    ManagedInstanceRow,
    McpClientTargetGrantRow,
    McpCredentialRow,
    McpPresetPermissionRow,
    McpPresetRow,
    McpSecurityEventRow,
    RuntimeDefinitionRow,
    RuntimeExecutionProfileRow,
    RuntimeRevisionRow,
    TargetRow,
)

TARGET_ID = UUID("12000000-0000-4000-8000-000000000001")
REVISION_ID = UUID("12000000-0000-4000-8000-000000000002")
DEFINITION_ID = UUID("12000000-0000-4000-8000-000000000003")


class MutableClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)
        self.monotonic = 100.0

    def utc(self) -> datetime:
        return self.now

    def tick(self) -> float:
        return self.monotonic


class TokenSource:
    def __init__(self) -> None:
        self.counter = 0

    def __call__(self, size: int) -> bytes:
        self.counter += 1
        return bytes([self.counter]) * size


@pytest.fixture
def mcp_database(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'mcp.db'}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    _seed_target(factory)
    yield factory
    engine.dispose()


def _seed_target(factory: sessionmaker[Session]) -> None:
    digest = "sha256:" + "a" * 64
    with factory.begin() as session:
        session.add(
            RuntimeDefinitionRow(
                id=str(DEFINITION_ID), slug="mcp-kodi", kodi_version="21.3"
            )
        )
        session.flush()
        session.add(
            RuntimeRevisionRow(
                id=str(REVISION_ID),
                runtime_definition_id=str(DEFINITION_ID),
                revision_number=1,
                source_kind="test",
                source_reference="test",
                content_digest=digest,
                manifest_reference="test",
                manifest_digest=digest,
                built_at="2026-09-19T00:00:00.000000Z",
                build_log_reference=None,
                approval_state="approved",
                approved_at="2026-09-19T00:00:00.000000Z",
                approval_note=None,
                requires_trusted_build_confirmation=False,
                trusted_build_confirmed_at=None,
            )
        )
        session.flush()
        session.add(
            TargetRow(
                id=str(TARGET_ID),
                name="MCP target",
                target_type="managed",
                kodi_version="21.3",
                availability="available",
                health="healthy",
                connection_state="connected",
            )
        )
        session.flush()
        session.add(
            ManagedInstanceRow(
                target_id=str(TARGET_ID),
                instance_type="disposable",
                runtime_revision_id=str(REVISION_ID),
                runtime_state="stopped",
                start_on_host_boot=False,
                notes=None,
            )
        )


def test_tokens_are_256_bit_verifier_only_expiring_and_show_once(
    mcp_database: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = MutableClock()
    source = TokenSource()
    administration = McpAdministrationService(
        mcp_database, clock=clock.utc, token_bytes=source
    )
    client = administration.create_client(
        display_name="Token client", preset_id=READ_ONLY_INSPECTION_PRESET_ID
    )
    issued = administration.issue_credential(client.id)
    raw = base64.urlsafe_b64decode(issued.token + "=")
    assert len(raw) == 32
    assert issued.credential.expires_at == clock.now + timedelta(hours=24)
    assert issued.token not in administration.get_client(client.id).model_dump_json()

    compared: list[tuple[str, str]] = []
    original = hmac.compare_digest

    def compare(left: str, right: str) -> bool:
        compared.append((left, right))
        return original(left, right)

    monkeypatch.setattr(hmac, "compare_digest", compare)
    authentication = McpAuthenticationService(mcp_database, clock=clock.utc)
    assert authentication.authenticate(issued.token) is not None
    assert compared and compared[-1][0] == compared[-1][1]
    compared.clear()
    assert authentication.authenticate("A" * 43) is None
    invalid_digest = hashlib.sha256(
        base64.urlsafe_b64decode("A" * 43 + "=")
    ).hexdigest()
    assert compared == [("0" * 64, invalid_digest)]

    with mcp_database() as session:
        row = session.get(McpCredentialRow, str(issued.credential.id))
        assert row is not None
        assert len(row.token_digest) == 64
        assert issued.token not in row.token_digest
        events = session.scalars(select(McpSecurityEventRow)).all()
        assert all(issued.token not in repr(event.__dict__) for event in events)

    shorter = administration.issue_credential(client.id, lifetime=timedelta(minutes=5))
    assert shorter.credential.expires_at == clock.now + timedelta(minutes=5)
    non_expiring = administration.issue_credential(client.id, lifetime=None)
    assert non_expiring.credential.expires_at is None
    clock.now += timedelta(hours=24)
    assert authentication.authenticate(issued.token) is None


def test_revoke_rotate_disable_and_grant_cascade_recompute_current_state(
    mcp_database: sessionmaker[Session],
) -> None:
    clock = MutableClock()
    administration = McpAdministrationService(
        mcp_database, clock=clock.utc, token_bytes=TokenSource()
    )
    client = administration.create_client(
        display_name="Lifecycle client", preset_id=READ_ONLY_INSPECTION_PRESET_ID
    )
    administration.assign_target_preset(TARGET_ID, READ_ONLY_INSPECTION_PRESET_ID)
    administration.grant_target(client.id, TARGET_ID)
    old = administration.issue_credential(client.id)
    authentication = McpAuthenticationService(mcp_database, clock=clock.utc)
    assert authentication.authenticate(old.token) is not None

    rotated = administration.rotate_credential(client.id, old.credential.id)
    assert authentication.authenticate(old.token) is None
    assert authentication.authenticate(rotated.token) is not None
    administration.revoke_credential(client.id, rotated.credential.id)
    assert authentication.authenticate(rotated.token) is None

    active = administration.issue_credential(client.id)
    administration.set_client_enabled(client.id, False)
    assert authentication.authenticate(active.token) is None
    administration.set_client_enabled(client.id, True)
    assert authentication.authenticate(active.token) is not None

    with mcp_database.begin() as session:
        session.delete(session.get(TargetRow, str(TARGET_ID)))
    with mcp_database() as session:
        assert (
            session.get(
                McpClientTargetGrantRow,
                {"client_id": str(client.id), "target_id": str(TARGET_ID)},
            )
            is None
        )
    replacement_id = uuid4()
    with mcp_database.begin() as session:
        session.add(
            TargetRow(
                id=str(replacement_id),
                name="Replacement MCP target",
                target_type="managed",
                kodi_version="21.3",
                availability="available",
                health="healthy",
                connection_state="connected",
            )
        )
        session.flush()
        session.add(
            ManagedInstanceRow(
                target_id=str(replacement_id),
                instance_type="disposable",
                runtime_revision_id=str(REVISION_ID),
                runtime_state="stopped",
                start_on_host_boot=False,
                notes=None,
            )
        )
    assert administration.get_client(client.id).target_grants == ()


def test_client_authority_revision_changes_only_for_enablement_and_preset(
    mcp_database: sessionmaker[Session],
) -> None:
    administration = McpAdministrationService(
        mcp_database, clock=MutableClock().utc, token_bytes=TokenSource()
    )
    client = administration.create_client(
        display_name="Revision client", preset_id=READ_ONLY_INSPECTION_PRESET_ID
    )
    assert client.authority_revision == 1
    assert administration.rename_client(client.id, "Renamed").authority_revision == 1
    issued = administration.issue_credential(client.id)
    assert administration.get_client(client.id).authority_revision == 1
    administration.rotate_credential(client.id, issued.credential.id)
    assert administration.get_client(client.id).authority_revision == 1

    administration.grant_target(client.id, TARGET_ID)
    with mcp_database() as session:
        first = session.get(
            McpClientTargetGrantRow,
            {"client_id": str(client.id), "target_id": str(TARGET_ID)},
        )
        assert first is not None
        first_grant_id = first.grant_id
    assert administration.get_client(client.id).authority_revision == 1
    administration.remove_target_grant(client.id, TARGET_ID)
    administration.grant_target(client.id, TARGET_ID)
    with mcp_database() as session:
        second = session.get(
            McpClientTargetGrantRow,
            {"client_id": str(client.id), "target_id": str(TARGET_ID)},
        )
        assert second is not None
        assert second.grant_id != first_grant_id
    assert administration.get_client(client.id).authority_revision == 1

    assert administration.set_client_enabled(client.id, False).authority_revision == 2
    assert administration.set_client_enabled(client.id, True).authority_revision == 3
    assert (
        administration.assign_client_preset(
            client.id, FULL_LAB_CONTROL_PRESET_ID
        ).authority_revision
        == 4
    )


def test_builtin_presets_activate_exact_non_clonable_production_remote_maximum(
    mcp_database: sessionmaker[Session],
) -> None:
    administration = McpAdministrationService(mcp_database)
    presets = administration.list_presets()
    assert [item.display_name for item in presets] == [
        "Add-on Development",
        "Full Lab Control",
        "Production Inspection",
        "Production Remote Kodi",
        "Production Visual Inspection",
        "Read-only Inspection",
    ]
    production = next(
        item for item in presets if item.id == PRODUCTION_REMOTE_KODI_PRESET_ID
    )
    permissions = {item.id: item.permissions for item in presets}
    assert permissions[ADDON_DEVELOPMENT_PRESET_ID] == (
        "development.execute",
        "development.request_promotion",
        "target.control",
        "target.inspect",
        "target.visual",
        "workspace.deploy",
        "workspace.edit",
        "workspace.inspect",
    )
    assert permissions[FULL_LAB_CONTROL_PRESET_ID] == (
        "development.execute",
        "development.request_promotion",
        "target.control",
        "target.inspect",
        "target.visual",
        "workspace.deploy",
        "workspace.edit",
        "workspace.inspect",
    )
    assert permissions[READ_ONLY_INSPECTION_PRESET_ID] == (
        "target.inspect",
        "target.visual",
    )
    assert permissions[PRODUCTION_INSPECTION_PRESET_ID] == ("target.inspect",)
    assert production.permissions == (
        "target.control",
        "target.inspect",
        "target.visual",
    )
    assert production.assignable
    assert (
        administration.create_client(
            display_name="Production", preset_id=PRODUCTION_REMOTE_KODI_PRESET_ID
        ).preset.id
        == PRODUCTION_REMOTE_KODI_PRESET_ID
    )


def test_exactly_five_of_fifteen_tools_are_production_eligible() -> None:
    registry = build_mcp_tool_registry()
    assert len(registry.names()) == 15
    eligible = {
        "addon_list",
        "screenshot_capture",
        "remote_action",
        "target_list",
        "target_status",
    }
    for name in registry.names():
        definition = registry.require(name)
        assert (
            TargetClass.EXTERNAL_PRODUCTION in definition.allowed_target_classes
        ) is (name in eligible)
        assert definition.production_eligible is (name in eligible)

    target = TargetRow(
        id="14000000-0000-4000-8000-000000000001",
        name="Production target",
        target_type="external_production",
        kodi_version=None,
        availability="unavailable",
        health="warning",
        connection_state="disconnected",
    )
    for tool_name, expected in (
        (None, "permission_denied"),
        ("target_status", "permission_denied"),
        ("workspace_deploy", "deployment_not_allowed"),
        ("workspace_execute", "deployment_not_allowed"),
        ("promotion_request", "promotion_not_allowed"),
    ):
        with pytest.raises(McpAuthorizationError, match=expected):
            McpAuthorizationService._enforce_workspace_production_veto(
                target, tool_name=tool_name
            )


def test_custom_presets_are_bounded_builtin_permission_snapshots(
    mcp_database: sessionmaker[Session],
) -> None:
    administration = McpAdministrationService(mcp_database)
    custom = administration.create_custom_preset(
        display_name="  Visual inspection  ",
        template_preset_id=READ_ONLY_INSPECTION_PRESET_ID,
    )
    assert custom.display_name == "Visual inspection"
    assert custom.builtin is False
    assert custom.assignable is True
    assert custom.permissions == ("target.inspect", "target.visual")
    assert custom.id not in {
        ADDON_DEVELOPMENT_PRESET_ID,
        READ_ONLY_INSPECTION_PRESET_ID,
        FULL_LAB_CONTROL_PRESET_ID,
        PRODUCTION_REMOTE_KODI_PRESET_ID,
    }
    development_snapshot = administration.create_custom_preset(
        display_name="Development snapshot",
        template_preset_id=ADDON_DEVELOPMENT_PRESET_ID,
    )
    assert development_snapshot.permissions == (
        "development.execute",
        "development.request_promotion",
        "target.control",
        "target.inspect",
        "target.visual",
        "workspace.deploy",
        "workspace.edit",
        "workspace.inspect",
    )

    client = administration.create_client(
        display_name="Custom preset client", preset_id=custom.id
    )
    administration.assign_target_preset(TARGET_ID, custom.id)
    token = administration.issue_credential(client.id).token
    principal = McpAuthenticationService(mcp_database).authenticate(token)
    assert principal is not None
    assert principal.preset_id == custom.id
    assert principal.permissions == frozenset({"target.inspect", "target.visual"})

    # A later built-in permission is not retroactively inherited by the clone.
    with mcp_database.begin() as session:
        session.add(
            McpPresetPermissionRow(
                preset_id=str(READ_ONLY_INSPECTION_PRESET_ID),
                permission="target.control",
            )
        )
    assert next(
        item for item in administration.list_presets() if item.id == custom.id
    ).permissions == ("target.inspect", "target.visual")

    with pytest.raises(McpAdministrationError, match="mcp_preset_name_conflict"):
        administration.create_custom_preset(
            display_name="Visual inspection",
            template_preset_id=FULL_LAB_CONTROL_PRESET_ID,
        )
    for invalid_name in ("", "   ", "x" * 121):
        with pytest.raises(McpAdministrationError, match="mcp_preset_name_invalid"):
            administration.create_custom_preset(
                display_name=invalid_name,
                template_preset_id=FULL_LAB_CONTROL_PRESET_ID,
            )
    for invalid_template in (PRODUCTION_REMOTE_KODI_PRESET_ID, custom.id, uuid4()):
        with pytest.raises(McpAdministrationError, match="mcp_preset_template_invalid"):
            administration.create_custom_preset(
                display_name=f"Invalid {invalid_template}",
                template_preset_id=invalid_template,
            )

    with mcp_database() as session:
        events = session.scalars(
            select(McpSecurityEventRow).where(
                McpSecurityEventRow.preset_id == str(custom.id)
            )
        ).all()
    assert {event.event_kind for event in events} == {
        "preset_created",
        "client_created",
        "target_preset_assigned",
    }
    assert all(event.client_id in {None, str(client.id)} for event in events)


def test_custom_preset_reassignment_is_effective_on_next_authentication(
    mcp_database: sessionmaker[Session],
) -> None:
    administration = McpAdministrationService(mcp_database)
    inspect = administration.create_custom_preset(
        display_name="Inspection snapshot",
        template_preset_id=READ_ONLY_INSPECTION_PRESET_ID,
    )
    control = administration.create_custom_preset(
        display_name="Control snapshot",
        template_preset_id=FULL_LAB_CONTROL_PRESET_ID,
    )
    client = administration.create_client(
        display_name="Immediate preset client", preset_id=control.id
    )
    issued = administration.issue_credential(client.id)
    authentication = McpAuthenticationService(mcp_database)
    before = authentication.authenticate(issued.token)
    assert before is not None and "target.control" in before.permissions

    administration.assign_client_preset(client.id, inspect.id)
    after = authentication.authenticate(issued.token)
    assert after is not None and "target.control" not in after.permissions

    administration.assign_target_preset(TARGET_ID, control.id)
    administration.assign_target_preset(TARGET_ID, inspect.id)
    with mcp_database() as session:
        event_kinds = list(session.scalars(select(McpSecurityEventRow.event_kind)))
    assert "client_preset_unassigned" in event_kinds
    assert "target_preset_unassigned" in event_kinds


def test_workspace_inspect_snapshot_does_not_discover_slice2_mutations(
    mcp_database: sessionmaker[Session],
) -> None:
    administration = McpAdministrationService(mcp_database)
    preset_id = uuid4()
    with mcp_database.begin() as session:
        session.add(
            McpPresetRow(
                id=str(preset_id),
                display_name="Pre-Slice-2 inspection snapshot",
                builtin=False,
                assignable=True,
            )
        )
        session.flush()
        session.add(
            McpPresetPermissionRow(
                preset_id=str(preset_id), permission="workspace.inspect"
            )
        )
    client = administration.create_client(
        display_name="Inspection snapshot client", preset_id=preset_id
    )
    token = administration.issue_credential(client.id).token
    principal = McpAuthenticationService(mcp_database).authenticate(token)
    assert principal is not None
    authorization = McpAuthorizationService(mcp_database, build_mcp_tool_registry())
    assert authorization.discoverable_tools(principal) == (
        "development_source_list",
        "workspace_read",
        "workspace_status",
        "workspace_tree",
        "workspace_validate",
    )


def test_builtin_permission_drift_fails_closed(
    mcp_database: sessionmaker[Session],
) -> None:
    McpAdministrationService(mcp_database)
    with mcp_database.begin() as session:
        row = session.get(
            McpPresetPermissionRow,
            {
                "preset_id": str(READ_ONLY_INSPECTION_PRESET_ID),
                "permission": "target.visual",
            },
        )
        assert row is not None
        session.delete(row)
    with pytest.raises(RuntimeError, match="permissions have drifted"):
        McpAdministrationService(mcp_database)


def test_mcp_projects_screenshot_capability_for_all_four_curated_profiles(
    mcp_database: sessionmaker[Session], tmp_path: Path
) -> None:
    target_ids: list[UUID] = []
    with mcp_database.begin() as session:
        for index, (revision_id, profile) in enumerate(
            SUPPORTED_SCREENSHOT_RUNTIME_PROFILES.items(), start=1
        ):
            canonical, application, version, tag, ordinal = profile
            definition_id = UUID(f"13000000-0000-4000-8000-{index:012d}")
            target_id = UUID(f"14000000-0000-4000-8000-{index:012d}")
            target_ids.append(target_id)
            session.add(
                RuntimeDefinitionRow(
                    id=str(definition_id),
                    slug=f"mcp-screenshot-{index}",
                    kodi_version=canonical,
                )
            )
            session.flush()
            session.add(
                RuntimeRevisionRow(
                    id=revision_id,
                    runtime_definition_id=str(definition_id),
                    revision_number=1,
                    source_kind="test",
                    source_reference="test",
                    content_digest="sha256:" + (str(index) * 64),
                    manifest_reference="test",
                    manifest_digest="sha256:" + (str(index) * 64),
                    built_at="2026-09-19T00:00:00.000000Z",
                    build_log_reference=None,
                    approval_state="approved",
                    approved_at="2026-09-19T00:00:00.000000Z",
                    approval_note=None,
                    requires_trusted_build_confirmation=False,
                    trusted_build_confirmed_at=None,
                )
            )
            session.flush()
            session.add(
                RuntimeExecutionProfileRow(
                    runtime_revision_id=revision_id,
                    runtime_kind="kodi",
                    runtime_uid=1000,
                    runtime_gid=1000,
                    python_bytecode_policy="interpreter_default",
                    expected_application_name=application,
                    expected_kodi_version=version,
                    expected_version_tag=tag,
                    expected_prerelease_ordinal=ordinal,
                    jsonrpc_port=8080,
                    jsonrpc_path="/jsonrpc",
                    process_observation="docker_healthcheck",
                    readiness_probe="kodi_jsonrpc",
                    readiness_timeout_seconds=90,
                    poll_interval_seconds=1.0,
                    http_timeout_seconds=3.0,
                )
            )
            session.add(
                TargetRow(
                    id=str(target_id),
                    name=f"Screenshot target {index}",
                    target_type="managed",
                    kodi_version=canonical,
                    availability="available",
                    health="healthy",
                    connection_state="connected",
                )
            )
            session.flush()
            session.add(
                ManagedInstanceRow(
                    target_id=str(target_id),
                    instance_type="disposable",
                    runtime_revision_id=revision_id,
                    runtime_state="stopped",
                    start_on_host_boot=False,
                    notes=None,
                )
            )

    screenshots = ManagedScreenshotService(
        mcp_database,
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        data_root=tmp_path,
    )
    provider = InternalManagedTargetAuthorityProvider(
        mcp_database, screenshots=screenshots
    )
    assert all(
        {"managed_screenshot", "screenshot"} <= provider.resolve(target_id).capabilities
        for target_id in target_ids
    )


class FixedTargetAuthority:
    def __init__(self, authority: TargetTechnicalAuthority) -> None:
        self.authority = authority

    def resolve(self, _target_id: UUID) -> TargetTechnicalAuthority:
        return self.authority


def _definition(
    name: str,
    permission: str,
    *,
    target_class: TargetClass = TargetClass.INTERNAL_LAB,
    capability: str = "managed_runtime_status",
    states: frozenset[str] = frozenset(),
) -> McpToolDefinition:
    return McpToolDefinition(
        name=name,
        schema_revision=1,
        permissions=frozenset({permission}),
        effect=ToolEffect.READ_ONLY,
        allowed_target_classes=frozenset({target_class}),
        required_capabilities=frozenset({capability}),
        required_states=states,
        rate_class=ToolRateClass.READ_LIGHT,
        adapter=f"test.{name}",
        production_eligible=False,
        target_scope_required=True,
    )


def test_registry_supports_future_permissions_target_classes_and_capabilities(
    mcp_database: sessionmaker[Session],
) -> None:
    registry = McpToolRegistry(
        known_permissions=frozenset({"target.inspect", "target.library"})
    )
    registry.register(_definition("z_future", "target.library"))
    registry.register(_definition("a_future", "target.library"))
    registry.register(
        _definition(
            "reconciled_future",
            "target.library",
            states=frozenset({"reconciled"}),
        )
    )
    assert registry.names() == ("a_future", "reconciled_future", "z_future")
    with pytest.raises(McpRegistryError, match="invalid_or_unknown_tool_metadata"):
        registry.register(_definition("unknown", "target.settings.read"))
    clock = MutableClock()
    administration = McpAdministrationService(
        mcp_database, clock=clock.utc, token_bytes=TokenSource()
    )
    client = administration.create_client(
        display_name="Future client", preset_id=READ_ONLY_INSPECTION_PRESET_ID
    )
    administration.assign_target_preset(TARGET_ID, READ_ONLY_INSPECTION_PRESET_ID)
    administration.grant_target(client.id, TARGET_ID)
    with mcp_database.begin() as session:
        session.add(
            McpPresetPermissionRow(
                preset_id=str(READ_ONLY_INSPECTION_PRESET_ID),
                permission="target.library",
            )
        )
    issued = administration.issue_credential(client.id)
    principal = McpAuthenticationService(mcp_database, clock=clock.utc).authenticate(
        issued.token
    )
    assert principal is not None

    denied_class = McpAuthorizationService(
        mcp_database,
        registry,
        targets=FixedTargetAuthority(
            TargetTechnicalAuthority(
                target_class=TargetClass.EXTERNAL_DEVELOPMENT,
                capabilities=frozenset({"managed_runtime_status"}),
                current_states=frozenset(),
            )
        ),
    )
    invoked = False

    def adapter() -> str:
        nonlocal invoked
        invoked = True
        return "not reached"

    with pytest.raises(McpAuthorizationError, match="permission_denied"):
        denied_class.invoke_target(principal, "a_future", TARGET_ID, adapter)
    assert not invoked

    missing_capability = McpAuthorizationService(
        mcp_database,
        registry,
        targets=FixedTargetAuthority(
            TargetTechnicalAuthority(
                target_class=TargetClass.INTERNAL_LAB,
                capabilities=frozenset(),
                current_states=frozenset(),
            )
        ),
    )
    with pytest.raises(McpAuthorizationError, match="capability_unavailable"):
        missing_capability.invoke_target(principal, "a_future", TARGET_ID, adapter)
    assert not invoked

    missing_state = McpAuthorizationService(mcp_database, registry)
    with pytest.raises(McpAuthorizationError, match="target_reconciliation_required"):
        missing_state.invoke_target(principal, "reconciled_future", TARGET_ID, adapter)
    assert not invoked


def test_production_registry_marks_remote_action_as_internal_mutating_control() -> None:
    registry = build_mcp_tool_registry()
    remote = registry.require("remote_action")
    assert registry.names() == (
        "addon_list",
        "development_source_list",
        "promotion_request",
        "remote_action",
        "screenshot_capture",
        "target_list",
        "target_status",
        "workspace_create",
        "workspace_deploy",
        "workspace_edit",
        "workspace_execute",
        "workspace_read",
        "workspace_status",
        "workspace_tree",
        "workspace_validate",
    )
    assert remote.permissions == frozenset({"target.control"})
    assert remote.effect is ToolEffect.MUTATING
    assert remote.rate_class is ToolRateClass.REMOTE_CONTROL
    assert remote.allowed_target_classes == frozenset(
        {
            TargetClass.INTERNAL_LAB,
            TargetClass.EXTERNAL_DEVELOPMENT,
            TargetClass.EXTERNAL_PRODUCTION,
        }
    )
    for name in (
        "target_list",
        "target_status",
        "addon_list",
        "screenshot_capture",
    ):
        assert registry.require(name).allowed_target_classes == frozenset(
            {
                TargetClass.INTERNAL_LAB,
                TargetClass.EXTERNAL_DEVELOPMENT,
                TargetClass.EXTERNAL_PRODUCTION,
            }
        )
    for name in (
        "workspace_deploy",
        "workspace_execute",
        "promotion_request",
    ):
        assert registry.require(name).allowed_target_classes == frozenset(
            {TargetClass.INTERNAL_LAB, TargetClass.EXTERNAL_DEVELOPMENT}
        )
    assert remote.production_eligible
    for name in (
        "development_source_list",
        "workspace_read",
        "workspace_status",
        "workspace_tree",
    ):
        development = registry.require(name)
        assert development.rate_class is ToolRateClass.DEVELOPMENT_READ
        assert development.effect is ToolEffect.READ_ONLY
        assert not development.production_eligible
    validation = registry.require("workspace_validate")
    assert validation.rate_class is ToolRateClass.DEVELOPMENT_VALIDATE
    assert validation.effect is ToolEffect.READ_ONLY
    assert not validation.production_eligible
    assert (
        registry.require("workspace_create").rate_class
        is ToolRateClass.DEVELOPMENT_CREATE
    )
    assert (
        registry.require("workspace_edit").rate_class is ToolRateClass.DEVELOPMENT_EDIT
    )
    deployment = registry.require("workspace_deploy")
    assert deployment.permissions == frozenset({"workspace.deploy"})
    assert deployment.rate_class is ToolRateClass.DEVELOPMENT_DEPLOY
    assert deployment.required_capabilities == frozenset({"workspace_deployment"})
    assert deployment.required_states == frozenset({"running", "ready", "reconciled"})
    execution = registry.require("workspace_execute")
    assert execution.permissions == frozenset({"development.execute"})
    assert execution.rate_class is ToolRateClass.DEVELOPMENT_EXECUTE
    assert execution.required_capabilities == frozenset({"workspace_execution"})
    assert not deployment.production_eligible and not execution.production_eligible
    promotion = registry.require("promotion_request")
    assert promotion.permissions == frozenset({"development.request_promotion"})
    assert promotion.rate_class is ToolRateClass.DEVELOPMENT_PROMOTION_REQUEST
    assert promotion.required_capabilities == frozenset(
        {"workspace_deployment", "workspace_execution"}
    )
    assert promotion.effect is ToolEffect.MUTATING
    assert not promotion.production_eligible
    assert "promotion_approve" not in registry.names()


def test_authorization_enforces_registry_scope_classification(
    mcp_database: sessionmaker[Session],
) -> None:
    registry = McpToolRegistry(known_permissions=frozenset({"target.inspect"}))
    registry.register(_definition("scoped", "target.inspect"))
    registry.register(
        McpToolDefinition(
            name="global",
            schema_revision=1,
            permissions=frozenset({"target.inspect"}),
            effect=ToolEffect.READ_ONLY,
            allowed_target_classes=frozenset({TargetClass.INTERNAL_LAB}),
            required_capabilities=frozenset(),
            required_states=frozenset(),
            rate_class=ToolRateClass.READ_LIGHT,
            adapter="test.global",
            production_eligible=False,
            target_scope_required=False,
        )
    )
    administration = McpAdministrationService(mcp_database, token_bytes=TokenSource())
    client = administration.create_client(
        display_name="Scope client", preset_id=READ_ONLY_INSPECTION_PRESET_ID
    )
    administration.assign_target_preset(TARGET_ID, READ_ONLY_INSPECTION_PRESET_ID)
    administration.grant_target(client.id, TARGET_ID)
    principal = McpAuthenticationService(mcp_database).authenticate(
        administration.issue_credential(client.id).token
    )
    assert principal is not None
    authorization = McpAuthorizationService(mcp_database, registry)
    with pytest.raises(McpAuthorizationError, match="permission_denied"):
        authorization.invoke_unscoped(principal, "scoped", lambda: None)
    with pytest.raises(McpAuthorizationError, match="permission_denied"):
        authorization.invoke_target(principal, "global", TARGET_ID, lambda: None)


def test_preset_policy_intersection_fails_closed(
    mcp_database: sessionmaker[Session],
) -> None:
    administration = McpAdministrationService(mcp_database, token_bytes=TokenSource())
    empty_id = uuid4()
    with mcp_database.begin() as session:
        session.add(
            McpPresetRow(
                id=str(empty_id),
                display_name="Test deny policy",
                builtin=False,
                assignable=True,
            )
        )
    registry = McpToolRegistry(known_permissions=frozenset({"target.inspect"}))
    registry.register(_definition("inspect_future", "target.inspect"))
    client = administration.create_client(
        display_name="Intersection client", preset_id=READ_ONLY_INSPECTION_PRESET_ID
    )
    administration.grant_target(client.id, TARGET_ID)
    administration.assign_target_preset(TARGET_ID, empty_id)
    issued = administration.issue_credential(client.id)
    principal = McpAuthenticationService(mcp_database).authenticate(issued.token)
    assert principal is not None
    authorization = McpAuthorizationService(mcp_database, registry)
    assert authorization.discoverable_tools(principal) == ("inspect_future",)
    with pytest.raises(McpAuthorizationError, match="permission_denied"):
        authorization.invoke_target(
            principal, "inspect_future", TARGET_ID, lambda: "not reached"
        )
    administration.assign_target_preset(TARGET_ID, READ_ONLY_INSPECTION_PRESET_ID)
    administration.assign_client_preset(client.id, empty_id)
    assert authorization.discoverable_tools(principal) == ()
    with pytest.raises(McpAuthorizationError, match="permission_denied"):
        authorization.invoke_target(
            principal, "inspect_future", TARGET_ID, lambda: "not reached"
        )


def test_bounded_token_buckets_refill_expire_and_preserve_hot_entries() -> None:
    clock = MutableClock()
    limiter = BoundedTokenBucketLimiter(
        rate=2,
        burst=2,
        maximum_buckets=2,
        idle_seconds=10,
        clock=clock.tick,
    )
    assert limiter.allow("hot")
    assert limiter.allow("hot")
    assert not limiter.allow("hot")
    assert limiter.allow("cold")
    assert not limiter.allow("overflow")
    clock.monotonic += 0.5
    assert limiter.allow("hot")
    clock.monotonic += 10
    assert limiter.allow("replacement")
    assert limiter.bucket_count <= 2


class AlwaysInvalidAuthentication:
    def authenticate(self, _token: str) -> None:
        return None


class NeverReachedApp:
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        raise AssertionError("authentication failure reached the application")


class FixedAuthentication:
    def __init__(self, principal: McpPrincipal) -> None:
        self.principal = principal

    def authenticate(self, _token: str) -> McpPrincipal:
        return self.principal


class ExplodingApp:
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        raise RuntimeError("adapter failed")


def test_failed_auth_limiter_uses_peer_not_attacker_token() -> None:
    clock = MutableClock()
    limiter = BoundedTokenBucketLimiter(
        rate=5,
        burst=20,
        maximum_buckets=1024,
        idle_seconds=900,
        clock=clock.tick,
    )
    middleware = ModernMcpAuthMiddleware(
        NeverReachedApp(),
        AlwaysInvalidAuthentication(),
        failed_auth=limiter,
    )
    client = TestClient(middleware, base_url="http://127.0.0.1")
    responses = [
        client.post(
            "/mcp",
            headers={
                "MCP-Protocol-Version": "2026-07-28",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {'a' * 42}{index % 10}",
            },
        )
        for index in range(21)
    ]
    client.close()
    assert [item.status_code for item in responses] == [401] * 20 + [429]
    assert limiter.bucket_count == 1


def test_concurrency_slots_release_after_application_exception() -> None:
    limiter = BoundedConcurrencyLimiter(per_client=1, global_limit=1)
    principal = McpPrincipal(
        client_id=uuid4(),
        credential_id=uuid4(),
        preset_id=uuid4(),
        permissions=frozenset({"target.inspect"}),
    )
    middleware = ModernMcpAuthMiddleware(
        ExplodingApp(),
        FixedAuthentication(principal),
        concurrency=limiter,
    )
    client = TestClient(middleware, base_url="http://127.0.0.1")
    with pytest.raises(RuntimeError, match="adapter failed"):
        client.post(
            "/mcp",
            headers={
                "MCP-Protocol-Version": "2026-07-28",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {'a' * 43}",
            },
        )
    client.close()
    assert asyncio.run(limiter.acquire(principal.client_id))
    asyncio.run(limiter.release(principal.client_id))
