from __future__ import annotations

import json
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from threading import Barrier, Event
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.external_targets import ExternalTargetAdministrationService
from kodi_lab.application.mcp_administration import (
    PRODUCTION_REMOTE_KODI_PRESET_ID,
    READ_ONLY_INSPECTION_PRESET_ID,
    McpAdministrationError,
    McpAdministrationService,
    McpAuthenticationService,
    McpPrincipal,
    McpProductionReadOnlyRead,
    McpProductionReadOnlyRequest,
)
from kodi_lab.application.mcp_target_adapters import KodiTargetAdapterService
from kodi_lab.application.production_inspection import (
    PRODUCTION_INSPECTION_PRESET_ID,
    PRODUCTION_VISUAL_PRESET_ID,
)
from kodi_lab.application.production_screenshots import ProductionScreenshotService
from kodi_lab.application.screenshot_preferences import ScreenshotPreferenceService
from kodi_lab.domain import ExternalAuthenticationMode, ExternalProtocolKind, TargetType
from kodi_lab.kodi.external import ExternalEndpointPolicy
from kodi_lab.kodi.remote import BrowserRemoteAction
from kodi_lab.mcp.authorization import McpAuthorizationError, McpAuthorizationService
from kodi_lab.mcp.registry import build_mcp_tool_registry
from kodi_lab.mcp.tools import McpTargetToolService
from kodi_lab.storage.models import (
    ExternalProductionPolicyRow,
    McpClientRow,
    McpProductionTargetScopeRow,
    McpSecurityEventRow,
)
from kodi_lab.storage.secrets import ExternalCredentialStore
from tests.application.test_production_bridges import _Verifier
from tests.application.test_production_navigation import _authority_environment
from tests.application.test_screenshot_preferences import _paired, _rows
from tests.kodi.test_ui_state import native_fixture


@dataclass
class _Environment:
    sessions: sessionmaker[Session]
    administration: McpAdministrationService
    client_id: UUID
    target_id: UUID
    principal: McpPrincipal
    authorization: McpAuthorizationService
    tools: McpTargetToolService
    credential_id: UUID
    bridges: Any
    fake: Any
    calls: list[str]

    def expected(self, *, visual: bool = True) -> McpProductionReadOnlyRequest:
        with self.sessions() as session:
            client = session.get(McpClientRow, str(self.client_id))
            policy = session.get(ExternalProductionPolicyRow, str(self.target_id))
            assert client is not None and policy is not None
            return McpProductionReadOnlyRequest(
                expected_client_authority_revision=client.authority_revision,
                expected_target_policy_revision=policy.policy_revision,
                expected_identity_generation=policy.identity_generation,
                expected_bridge_trust_revision=1 if visual else None,
            )

    def authorize(self, *, visual: bool = True) -> McpProductionReadOnlyRead:
        return self.administration.authorize_production_read_only(
            self.client_id, self.target_id, self.expected(visual=visual)
        )


@pytest.fixture
def environment(tmp_path: Path) -> Iterator[_Environment]:
    with _paired(tmp_path, True) as paired:
        administration = McpAdministrationService(paired.sessions)
        client = administration.create_client(
            display_name="Existing agent", preset_id=PRODUCTION_VISUAL_PRESET_ID
        )
        credential = administration.issue_credential(client.id)
        principal = McpAuthenticationService(paired.sessions).authenticate(
            credential.token
        )
        assert principal is not None
        calls: list[str] = []

        def native(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            method = body["method"]
            calls.append(method)
            if method == "JSONRPC.Ping":
                result: object = "pong"
            elif method == "JSONRPC.Version":
                result = {"version": {"major": 13, "minor": 0, "patch": 0}}
            elif method == "Application.GetProperties":
                result = {
                    "name": "Kodi",
                    "version": {
                        "major": 21,
                        "minor": 3,
                        "tag": "stable",
                        "tagversion": None,
                    },
                }
            elif method == "Addons.GetAddons":
                result = {"addons": [], "limits": {"start": 0, "end": 0, "total": 0}}
            else:
                result = native_fixture(method, body.get("params"))
            return httpx.Response(
                200, json={"jsonrpc": "2.0", "id": body["id"], "result": result}
            )

        adapters = KodiTargetAdapterService(
            paired.sessions,
            managed_status=cast(Any, None),
            managed_inventory=cast(Any, None),
            managed_remote=cast(Any, None),
            managed_authority=cast(Any, None),
            endpoint_policy=ExternalEndpointPolicy(
                allowed_ports=(8080,), test_loopback_enabled=True
            ),
            credential_store=cast(Any, None),
            external_bridges=paired.bridges,
            external_transport=httpx.MockTransport(native),
        )
        authorization = McpAuthorizationService(
            paired.sessions, build_mcp_tool_registry(), targets=adapters
        )
        tools = McpTargetToolService(
            paired.sessions,
            authorization,
            cast(Any, None),
            cast(Any, None),
            cast(Any, None),
            target_adapters=adapters,
            production_screenshots=ProductionScreenshotService(
                paired.sessions, paired.bridges
            ),
        )
        yield _Environment(
            paired.sessions,
            administration,
            client.id,
            paired.target_id,
            principal,
            authorization,
            tools,
            credential.credential.id,
            paired.bridges,
            paired.fake,
            calls,
        )


def test_grouped_authorization_enables_only_supported_reads(
    environment: _Environment,
) -> None:
    env = environment
    retired = env.administration.issue_credential(env.client_id)
    env.administration.revoke_credential(env.client_id, retired.credential.id)
    before_client = env.administration.get_client(env.client_id)
    before_trust = env.bridges.status(env.target_id)
    result = env.authorize()
    assert result.inspection_scope.effective_state == "active"
    assert (
        result.visual_scope is not None
        and result.visual_scope.effective_state == "active"
    )
    assert result.target_preset_id == PRODUCTION_VISUAL_PRESET_ID
    assert (
        env.calls == [] and env.fake.capture_count == 0
    )  # No onboarding or reads during authorization.
    assert (
        env.tools.target_status(env.principal, target_id=env.target_id).target.target_id
        == env.target_id
    )
    assert env.tools.addon_list(env.principal, target_id=env.target_id).total_count == 0
    observation = env.tools.inspect_ui_state(env.principal, target_id=env.target_id)
    assert observation.window.id == 10025
    assert observation.focus.label == "Blade Runner"
    assert env.tools.screenshot_capture(
        env.principal, target_id=env.target_id, request_id=uuid4()
    ).png
    after_client = env.administration.get_client(env.client_id)
    assert after_client.preset == before_client.preset
    assert after_client.credentials == before_client.credentials
    assert after_client.authority_revision == before_client.authority_revision
    assert env.bridges.status(env.target_id) == before_trust
    with env.sessions() as session:
        assert set(session.scalars(select(McpProductionTargetScopeRow.scope))) == {
            "inspect",
            "visual",
        }
        kinds = set(session.scalars(select(McpSecurityEventRow.event_kind)))
        assert {
            "target_granted",
            "target_preset_assigned",
            "production_inspection_scope_granted",
            "production_visual_scope_granted",
            "production_read_only_authorized",
        } <= kinds


def test_screenshot_off_does_not_prevent_read_relationship(
    environment: _Environment,
) -> None:
    env = environment
    preference = ScreenshotPreferenceService(env.sessions)
    preference.update(
        env.target_id,
        enabled=False,
        expected_revision=1,
        administrator_reference="test",
    )
    result = env.authorize()
    assert result.visual_scope is not None and not result.capture_preference_enabled
    env.authorization.authorize_target(env.principal, "inspect_ui_state", env.target_id)
    with pytest.raises(McpAuthorizationError, match="screenshots_disabled"):
        env.tools.screenshot_capture(
            env.principal, target_id=env.target_id, request_id=uuid4()
        )
    assert env.fake.capture_count == 0


def test_inspection_only_client_gets_no_visual_scope(environment: _Environment) -> None:
    env = environment
    env.administration.assign_client_preset(
        env.client_id, PRODUCTION_INSPECTION_PRESET_ID
    )
    result = env.authorize(visual=False)
    assert (
        result.visual_scope is None
        and result.visual_unavailable_reason == "client_missing_visual_permission"
    )
    principal = McpPrincipal(
        env.client_id,
        env.credential_id,
        PRODUCTION_INSPECTION_PRESET_ID,
        frozenset({"target.inspect"}),
    )
    env.authorization.authorize_target(principal, "target_status", env.target_id)
    with pytest.raises(McpAuthorizationError, match="permission_denied"):
        env.authorization.authorize_target(
            principal, "screenshot_capture", env.target_id
        )


@pytest.mark.parametrize("missing_inspect", [False, True])
def test_incompatible_preset_never_changes_client_or_grants(
    environment: _Environment, missing_inspect: bool
) -> None:
    env = environment
    preset_id = READ_ONLY_INSPECTION_PRESET_ID
    if missing_inspect:
        preset_id = env.administration.create_custom_preset(
            display_name="Visual only",
            template_preset_id=preset_id,
            permissions=frozenset({"target.visual"}),
        ).id
    env.administration.assign_client_preset(env.client_id, preset_id)
    before = _rows(env.sessions)
    with pytest.raises(
        McpAdministrationError,
        match="read_only_inspection_permission_required"
        if missing_inspect
        else "read_only_preset_incompatible",
    ):
        env.authorize()
    assert _rows(env.sessions) == before


def test_exact_target_and_agent_isolation(
    environment: _Environment, tmp_path: Path
) -> None:
    env = environment
    targets = ExternalTargetAdministrationService(
        env.sessions,
        endpoint_policy=ExternalEndpointPolicy(allowed_ports=(8080,)),
        credential_store=ExternalCredentialStore(
            tmp_path / "second-native", master_key=None, key_version=1
        ),
        verifier=cast(Any, _Verifier()),
        target_type=TargetType.EXTERNAL_PRODUCTION,
    )
    second = targets.register(
        logical_name="Unrelated Kodi",
        protocol_kind=ExternalProtocolKind.JSONRPC_HTTP,
        host="192.168.50.42",
        port=8080,
        authentication_mode=ExternalAuthenticationMode.NONE,
        configured_capabilities={"target_inspect", "addon_inventory"},
    )
    observed = targets.probe(second.target_id)
    assert observed.pending_observation_id is not None
    targets.confirm(second.target_id, observed.pending_observation_id)
    targets.confirm_production_designation(
        second.target_id, observed.pending_observation_id
    )
    targets.set_enabled(second.target_id, enabled=True)
    before_second = env.administration.get_target_policy(second.target_id)
    env.authorize()
    other = env.administration.create_client(
        display_name="Unenrolled agent", preset_id=PRODUCTION_VISUAL_PRESET_ID
    )
    issued = env.administration.issue_credential(other.id)
    principal = McpAuthenticationService(env.sessions).authenticate(issued.token)
    assert principal is not None
    with pytest.raises(
        McpAuthorizationError, match="target_not_found_or_not_authorized"
    ):
        env.authorization.authorize_target(principal, "target_status", env.target_id)
    with pytest.raises(
        McpAuthorizationError, match="target_not_found_or_not_authorized"
    ):
        env.authorization.authorize_target(
            env.principal, "target_status", second.target_id
        )
    assert env.administration.get_target_policy(second.target_id) == before_second
    assert env.administration.get_client(other.id).target_grants == ()


def test_repeated_requests_reuse_authority_and_audit(environment: _Environment) -> None:
    env = environment
    initial_expected = env.expected()
    first = env.authorize()
    before = _rows(env.sessions)
    assert env.authorize() == first
    assert _rows(env.sessions) == before
    # The initial preset insert advanced target policy. An old request cannot
    # masquerade as a retry after arbitrary policy changes.
    with pytest.raises(McpAdministrationError, match="read_only_state_changed"):
        env.administration.authorize_production_read_only(
            env.client_id, env.target_id, initial_expected
        )
    assert _rows(env.sessions) == before


@pytest.mark.parametrize(
    "field",
    [
        "expected_identity_generation",
        "expected_bridge_trust_revision",
        "expected_target_policy_revision",
        "expected_client_authority_revision",
    ],
)
def test_stale_expected_state_rolls_back(environment: _Environment, field: str) -> None:
    env = environment
    expected = env.expected().model_copy(update={field: 999})
    before = _rows(env.sessions)
    with pytest.raises(McpAdministrationError, match="read_only_state_changed"):
        env.administration.authorize_production_read_only(
            env.client_id, env.target_id, expected
        )
    assert _rows(env.sessions) == before


@pytest.mark.parametrize("scope", ["inspect", "visual"])
def test_revoked_scopes_are_not_recreated(
    environment: _Environment, scope: str
) -> None:
    env = environment
    result = env.authorize()
    row = result.inspection_scope if scope == "inspect" else result.visual_scope
    assert row is not None
    env.administration.revoke_production_inspection_scope(row.scope_id)
    before = _rows(env.sessions)
    with pytest.raises(McpAdministrationError, match="read_only_scope_review_required"):
        env.authorize()
    assert _rows(env.sessions) == before


def test_revoked_credentials_and_removed_grants_require_intervention(
    environment: _Environment,
) -> None:
    env = environment
    env.authorize()
    env.administration.remove_target_grant(env.client_id, env.target_id)
    before = _rows(env.sessions)
    with pytest.raises(McpAdministrationError, match="read_only_grant_review_required"):
        env.authorize()
    assert _rows(env.sessions) == before
    env.administration.revoke_credential(env.client_id, env.credential_id)
    before = _rows(env.sessions)
    with pytest.raises(
        McpAdministrationError, match="read_only_client_credential_required"
    ):
        env.authorize()
    assert _rows(env.sessions) == before


def test_revoked_bridge_is_not_repaired(environment: _Environment) -> None:
    env = environment
    env.bridges.revoke(env.target_id)
    before = _rows(env.sessions)
    with pytest.raises(McpAdministrationError, match="bridge_not_paired"):
        env.authorize()
    assert _rows(env.sessions) == before


def test_scope_failure_rolls_back_preset_grant_scope_and_audit(
    environment: _Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = environment
    original = env.administration._new_read_scope

    def fail_visual(
        session: Session, client: UUID, target: UUID, scope: str, *args: Any
    ) -> McpProductionTargetScopeRow:
        if scope == "visual":
            session.flush()  # Prove even already-written rows roll back.
            raise McpAdministrationError("injected_visual_failure")
        return original(session, client, target, scope, *args)

    before = _rows(env.sessions)
    monkeypatch.setattr(env.administration, "_new_read_scope", fail_visual)
    with pytest.raises(McpAdministrationError, match="injected_visual_failure"):
        env.authorize()
    assert _rows(env.sessions) == before


def test_concurrent_initial_authorization_has_one_current_policy_winner(
    environment: _Environment,
) -> None:
    env = environment
    barrier = Barrier(2)
    expected = env.expected()

    def authorize() -> str:
        barrier.wait(timeout=5)
        try:
            env.administration.authorize_production_read_only(
                env.client_id, env.target_id, expected
            )
            return "authorized"
        except McpAdministrationError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as workers:
        assert sorted(workers.map(lambda _: authorize(), range(2))) == [
            "authorized",
            "read_only_state_changed",
        ]
    with env.sessions() as session:
        assert len(session.scalars(select(McpProductionTargetScopeRow)).all()) == 2


def test_concurrent_client_disable_is_observed_before_any_authorization(
    environment: _Environment,
) -> None:
    env = environment
    expected = env.expected()
    started = Event()
    with env.sessions.begin() as session:
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        client = session.get(McpClientRow, str(env.client_id))
        assert client is not None
        client.enabled = False
        client.disabled_at = "2026-10-07T12:00:00.000000Z"
        client.authority_revision += 1

        def authorize() -> str:
            started.set()
            try:
                env.administration.authorize_production_read_only(
                    env.client_id, env.target_id, expected
                )
                return "authorized"
            except McpAdministrationError as error:
                return error.code

        workers = ThreadPoolExecutor(max_workers=1)
        future = workers.submit(authorize)
        assert started.wait(5)
    try:
        assert future.result(timeout=5) == "read_only_client_disabled"
    finally:
        workers.shutdown()
    assert env.administration.get_client(env.client_id).target_grants == ()


def test_remote_client_retains_preset_but_gets_no_control_scopes(
    environment: _Environment,
) -> None:
    env = environment
    env.administration.assign_client_preset(
        env.client_id, PRODUCTION_REMOTE_KODI_PRESET_ID
    )
    env.authorize()
    principal = McpPrincipal(
        env.client_id,
        env.credential_id,
        PRODUCTION_REMOTE_KODI_PRESET_ID,
        frozenset({"target.inspect", "target.visual", "target.control"}),
    )
    for action in (
        BrowserRemoteAction.HOME,
        BrowserRemoteAction.PLAY_PAUSE,
        BrowserRemoteAction.VOLUME_UP,
    ):
        with pytest.raises(McpAuthorizationError):
            env.authorization.authorize_production_navigation(
                principal, env.target_id, action
            )
    for tool in (
        "remote_action",
        "workspace_deploy",
        "workspace_execute",
        "workspace_edit",
        "instance_start",
    ):
        with pytest.raises(McpAuthorizationError):
            env.authorization.authorize_target(principal, tool, env.target_id)
    assert (
        env.administration.get_client(env.client_id).preset.id
        == PRODUCTION_REMOTE_KODI_PRESET_ID
    )


def test_existing_inspection_only_target_policy_is_not_widened(
    environment: _Environment,
) -> None:
    env = environment
    env.administration.assign_target_preset(
        env.target_id, PRODUCTION_INSPECTION_PRESET_ID
    )
    before = _rows(env.sessions)
    with pytest.raises(
        McpAdministrationError, match="read_only_target_policy_review_required"
    ):
        env.authorize()
    assert _rows(env.sessions) == before


def test_disabled_target_and_identity_drift_deny_without_partial_grants(
    environment: _Environment,
) -> None:
    env = environment
    env.bridges.invalidate_production_native_identity(env.target_id)
    before = _rows(env.sessions)
    with pytest.raises(McpAdministrationError):
        env.authorize()
    assert _rows(env.sessions) == before


def test_old_bridge_bound_scope_needs_explicit_review(
    environment: _Environment,
) -> None:
    env = environment
    # A historical Bridge binding is never rewritten to fit current trust.
    with env.sessions.begin() as session:
        session.execute(
            text(
                "INSERT INTO mcp_production_target_scopes "
                "(id,client_id,target_id,identity_generation,bridge_trust_revision,"
                "scope,state,created_at,created_by) VALUES "
                "(:id,:client,:target,1,99,'visual','active',"
                "'2026-10-07T12:00:00.000000Z','test')"
            ),
            {
                "id": str(uuid4()),
                "client": str(env.client_id),
                "target": str(env.target_id),
            },
        )
    before = _rows(env.sessions)
    with pytest.raises(McpAdministrationError, match="read_only_scope_review_required"):
        env.authorize()
    assert _rows(env.sessions) == before


def test_existing_control_relationship_requires_explicit_review(tmp_path: Path) -> None:
    env = _authority_environment(tmp_path / "navigation")
    with env.factory() as session:
        policy = session.get(ExternalProductionPolicyRow, str(env.target_id))
        assert policy is not None
        expected = McpProductionReadOnlyRequest(
            expected_client_authority_revision=1,
            expected_identity_generation=1,
            expected_target_policy_revision=policy.policy_revision,
        )
    before = _rows(env.factory)
    with pytest.raises(
        McpAdministrationError, match="read_only_existing_control_authority"
    ):
        env.administration.authorize_production_read_only(
            env.client_id, env.target_id, expected
        )
    assert _rows(env.factory) == before
    env.authorization.authorize_production_navigation(
        env.principal, env.target_id, BrowserRemoteAction.HOME
    )


def test_target_without_screenshot_maximum_keeps_native_inspection(
    tmp_path: Path,
) -> None:
    env = _authority_environment(tmp_path / "inspection")
    env.administration.revoke_production_inspection_scope(env.navigate_scope_id)
    with env.factory() as session:
        policy = session.get(ExternalProductionPolicyRow, str(env.target_id))
        assert policy is not None
        expected = McpProductionReadOnlyRequest(
            expected_client_authority_revision=1,
            expected_identity_generation=1,
            expected_target_policy_revision=policy.policy_revision,
        )
    before = _rows(env.factory)
    result = env.administration.authorize_production_read_only(
        env.client_id, env.target_id, expected
    )
    assert result.visual_scope is None
    assert result.visual_unavailable_reason == "target_not_configured_for_screenshots"
    assert not result.capture_preference_enabled
    assert _rows(env.factory) == before
    env.authorization.authorize_target(env.principal, "target_status", env.target_id)
