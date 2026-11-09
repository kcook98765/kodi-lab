from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy import select

from kodi_lab.application.external_targets import ExternalTargetAdministrationService
from kodi_lab.application.mcp_remote_actions import (
    McpRemoteActionReceiptService,
    McpRemoteActionResult,
)
from kodi_lab.application.production_inspection import PRODUCTION_INSPECTION_PRESET_ID
from kodi_lab.application.production_navigation import (
    PRODUCTION_NAVIGATION_ACTIONS,
    PRODUCTION_PROHIBITED_ACTIONS,
    PRODUCTION_TRANSIENT_CONTROL_ACTIONS,
    ProductionNavigationPreferenceService,
)
from kodi_lab.domain import TargetType
from kodi_lab.kodi.external import ExternalEndpointPolicy
from kodi_lab.kodi.remote import BrowserRemoteAction
from kodi_lab.mcp.authorization import McpAuthorizationError
from kodi_lab.mcp.tools import McpTargetToolService
from kodi_lab.storage.models import (
    ExternalProductionPolicyRow,
    McpProductionTargetScopeRow,
    ProductionMutationControlRow,
    ProductionNativeControlHttpAcknowledgementRow,
)
from kodi_lab.storage.secrets import ExternalCredentialStore
from tests.application.test_production_navigation import (
    _authority_environment,
    _OuterLimiter,
    _Remote,
)


def _environment(tmp_path: Path, *, enabled: bool = True) -> Any:
    return _authority_environment(
        tmp_path,
        preset_id=PRODUCTION_INSPECTION_PRESET_ID,
        navigation_enabled=enabled,
        acknowledge_http=False,
    )


def _tools(env: Any, remote: Any, *, transient: Any = None) -> McpTargetToolService:
    receipts = McpRemoteActionReceiptService(env.factory, remote, _OuterLimiter())
    return McpTargetToolService(
        env.factory,
        env.authorization,
        cast(Any, None),
        cast(Any, None),
        cast(Any, None),
        remote_actions=receipts,
        production_transient_approvals=transient,
    )


def test_default_off_and_no_navigation_grants_or_control_consent(
    tmp_path: Path,
) -> None:
    env = _environment(tmp_path, enabled=False)
    preference = ProductionNavigationPreferenceService(env.factory)
    assert not preference.read(env.target_id).enabled
    assert "remote_action" in env.authorization.discoverable_tools(env.principal)
    assert env.administration.get_client(env.client_id).preset.permissions == (
        "target.inspect",
    )
    with pytest.raises(McpAuthorizationError, match="remote_navigation_disabled"):
        _tools(env, _Remote()).remote_action(
            env.principal,
            target_id=env.target_id,
            request_id=uuid4(),
            action=BrowserRemoteAction.HOME,
        )
    before = preference.read(env.target_id)
    on = preference.update(
        env.target_id, enabled=True, expected_revision=before.revision
    )
    assert on.enabled and on.revision == before.revision + 1
    with env.factory() as session:
        assert set(session.scalars(select(McpProductionTargetScopeRow.scope))) == {
            "inspect"
        }
        assert (
            session.scalar(select(ProductionNativeControlHttpAcknowledgementRow))
            is None
        )


def _set_block(env: Any, block: str, *, enabled: bool = True) -> None:
    from datetime import UTC, datetime

    from kodi_lab.application.production_policy import advance_production_policy

    with env.factory.begin() as session:
        if block == "global":
            control = session.get(ProductionMutationControlRow, 1)
            assert control is not None
            control.production_mutations_blocked = enabled
            control.policy_epoch += 1
        else:
            policy = session.get(ExternalProductionPolicyRow, str(env.target_id))
            assert policy is not None
            policy.mutations_suspended = enabled
            advance_production_policy(policy, now=datetime.now(UTC))


def _block_code(block: str) -> str:
    return (
        "production_mutations_blocked"
        if block == "global"
        else "production_target_mutations_suspended"
    )


@pytest.mark.parametrize("action", sorted(PRODUCTION_NAVIGATION_ACTIONS, key=str))
def test_inspection_client_can_send_only_seven_actions_when_unblocked(
    tmp_path: Path, action: BrowserRemoteAction
) -> None:
    env = _environment(tmp_path)
    remote = _Remote()
    result = _tools(env, remote).remote_action(
        env.principal, target_id=env.target_id, request_id=uuid4(), action=action
    )
    assert isinstance(result, McpRemoteActionResult)
    assert result.outcome == "succeeded" and remote.sends == 1


@pytest.mark.parametrize("block", ["global", "target"])
@pytest.mark.parametrize("action", sorted(PRODUCTION_NAVIGATION_ACTIONS, key=str))
def test_both_vetoes_deny_every_navigation_action_with_preference_on(
    tmp_path: Path, block: str, action: BrowserRemoteAction
) -> None:
    env = _environment(tmp_path)
    _set_block(env, block)
    assert (
        ProductionNavigationPreferenceService(env.factory).read(env.target_id).enabled
    )
    remote = _Remote()
    with pytest.raises(McpAuthorizationError, match=_block_code(block)):
        _tools(env, remote).remote_action(
            env.principal, target_id=env.target_id, request_id=uuid4(), action=action
        )
    assert remote.calls == 0 and remote.sends == 0


@pytest.mark.parametrize("block", ["global", "target"])
@pytest.mark.parametrize("initially_enabled", [False, True])
def test_toggle_on_cannot_override_blocks_and_off_remains_available(
    tmp_path: Path, block: str, initially_enabled: bool
) -> None:
    from kodi_lab.application.production_inspection import ProductionInspectionError

    env = _environment(tmp_path, enabled=initially_enabled)
    preference = ProductionNavigationPreferenceService(env.factory)
    _set_block(env, block)
    before = preference.read(env.target_id)
    assert before.blocked_reason == _block_code(block)
    with pytest.raises(ProductionInspectionError, match=_block_code(block)):
        preference.update(
            env.target_id, enabled=True, expected_revision=before.revision
        )
    assert preference.read(env.target_id) == before
    off = preference.update(
        env.target_id, enabled=False, expected_revision=before.revision
    )
    assert off.enabled is False
    _set_block(env, block, enabled=False)
    current = preference.read(env.target_id)
    assert current.enabled is False and current.blocked_reason is None
    assert preference.update(
        env.target_id, enabled=True, expected_revision=current.revision
    ).enabled


@pytest.mark.parametrize("block", ["global", "target"])
def test_active_blocks_deny_navigation_replay_and_do_not_revive_old_requests(
    tmp_path: Path, block: str
) -> None:
    env = _environment(tmp_path)
    remote = _Remote()
    tools = _tools(env, remote)
    request_id = uuid4()
    first = tools.remote_action(
        env.principal,
        target_id=env.target_id,
        request_id=request_id,
        action=BrowserRemoteAction.HOME,
    )
    assert (
        tools.remote_action(
            env.principal,
            target_id=env.target_id,
            request_id=request_id,
            action=BrowserRemoteAction.HOME,
        )
        == first
    )
    _set_block(env, block)
    with pytest.raises(McpAuthorizationError, match=_block_code(block)):
        tools.remote_action(
            env.principal,
            target_id=env.target_id,
            request_id=request_id,
            action=BrowserRemoteAction.HOME,
        )
    _set_block(env, block, enabled=False)
    with pytest.raises(McpAuthorizationError, match="production_authority_changed"):
        tools.remote_action(
            env.principal,
            target_id=env.target_id,
            request_id=request_id,
            action=BrowserRemoteAction.HOME,
        )
    assert remote.sends == 1


@pytest.mark.parametrize("block", ["global", "target"])
def test_block_applied_during_inflight_request_prevents_dispatch(
    tmp_path: Path, block: str
) -> None:
    from kodi_lab.storage.models import McpRemoteActionReceiptRow

    env = _environment(tmp_path)

    class Remote(_Remote):
        def invoke(self, *args: Any, **kwargs: Any) -> Any:
            _set_block(env, block)
            return super().invoke(*args, **kwargs)

    remote = Remote()
    with pytest.raises(McpAuthorizationError, match=_block_code(block)):
        _tools(env, remote).remote_action(
            env.principal,
            target_id=env.target_id,
            request_id=uuid4(),
            action=BrowserRemoteAction.HOME,
        )
    assert remote.sends == 0
    with env.factory() as session:
        receipt = session.scalar(select(McpRemoteActionReceiptRow))
        assert receipt is not None and receipt.state == "completed"
        assert receipt.final_result == _block_code(block)


@pytest.mark.parametrize("block", ["global", "target"])
@pytest.mark.parametrize(
    "action", [BrowserRemoteAction.PLAY_PAUSE, BrowserRemoteAction.VOLUME_UP]
)
def test_blocks_guard_higher_risk_requests_and_cached_replay_without_new_permissions(
    tmp_path: Path, block: str, action: BrowserRemoteAction
) -> None:
    from kodi_lab.application.production_transient_approvals import (
        ProductionApprovalRequiredResult,
    )
    from tests.application.test_production_transient_approvals import _service

    env, _scope, _clock, remote, _preflight, transient = _service(tmp_path)
    tools = _tools(env, remote, transient=transient)
    request_id = uuid4()
    pending = tools.remote_action(
        env.principal, target_id=env.target_id, request_id=request_id, action=action
    )
    assert isinstance(pending, ProductionApprovalRequiredResult)
    transient.approve(pending.approval_id, action=action)
    first = tools.remote_action(
        env.principal, target_id=env.target_id, request_id=request_id, action=action
    )
    assert isinstance(first, McpRemoteActionResult) and first.outcome == "succeeded"
    assert (
        tools.remote_action(
            env.principal, target_id=env.target_id, request_id=request_id, action=action
        )
        == first
    )
    _set_block(env, block)
    for identity in (request_id, uuid4()):
        with pytest.raises(McpAuthorizationError, match=_block_code(block)):
            tools.remote_action(
                env.principal,
                target_id=env.target_id,
                request_id=identity,
                action=action,
            )
    assert remote.sends == 1


@pytest.mark.parametrize(
    "action",
    sorted(
        PRODUCTION_PROHIBITED_ACTIONS | PRODUCTION_TRANSIENT_CONTROL_ACTIONS, key=str
    ),
)
def test_inspection_navigation_never_grants_other_actions(
    tmp_path: Path, action: BrowserRemoteAction
) -> None:
    env = _environment(tmp_path)
    remote = _Remote()
    with pytest.raises(McpAuthorizationError):
        _tools(env, remote).remote_action(
            env.principal, target_id=env.target_id, request_id=uuid4(), action=action
        )
    assert remote.sends == 0


def test_off_on_cannot_revive_old_request_and_current_revocation_blocks_replay(
    tmp_path: Path,
) -> None:
    env = _environment(tmp_path)
    remote = _Remote()
    tools = _tools(env, remote)
    request_id = uuid4()

    def invoke() -> Any:
        return tools.remote_action(
            env.principal,
            target_id=env.target_id,
            request_id=request_id,
            action=BrowserRemoteAction.HOME,
        )

    first = invoke()
    assert invoke() == first and remote.sends == 1
    preferences = ProductionNavigationPreferenceService(env.factory)
    current = preferences.read(env.target_id)
    off = preferences.update(
        env.target_id, enabled=False, expected_revision=current.revision
    )
    with pytest.raises(McpAuthorizationError, match="remote_navigation_disabled"):
        invoke()
    preferences.update(env.target_id, enabled=True, expected_revision=off.revision)
    with pytest.raises(McpAuthorizationError, match="production_authority_changed"):
        invoke()
    assert remote.sends == 1
    env.administration.remove_target_grant(env.client_id, env.target_id)
    with pytest.raises(McpAuthorizationError, match="not_found_or_not_authorized"):
        invoke()


@pytest.mark.parametrize("cycle_on", [False, True])
def test_off_during_inflight_read_blocks_dispatch(
    tmp_path: Path, cycle_on: bool
) -> None:
    env = _environment(tmp_path)
    preference = ProductionNavigationPreferenceService(env.factory)

    class Remote(_Remote):
        def invoke(self, *args: Any, **kwargs: Any) -> Any:
            saved = preference.read(env.target_id)
            off = preference.update(
                env.target_id, enabled=False, expected_revision=saved.revision
            )
            if cycle_on:
                preference.update(
                    env.target_id, enabled=True, expected_revision=off.revision
                )
            return super().invoke(*args, **kwargs)

    remote = Remote()
    with pytest.raises(
        McpAuthorizationError,
        match="production_authority_changed"
        if cycle_on
        else "remote_navigation_disabled",
    ):
        _tools(env, remote).remote_action(
            env.principal,
            target_id=env.target_id,
            request_id=uuid4(),
            action=BrowserRemoteAction.HOME,
        )
    assert remote.sends == 0


def test_emergency_block_turns_navigation_off_even_when_mutations_already_blocked(
    tmp_path: Path,
) -> None:
    env = _environment(tmp_path)
    preferences = ProductionNavigationPreferenceService(env.factory)
    service = ExternalTargetAdministrationService(
        env.factory,
        endpoint_policy=ExternalEndpointPolicy(allowed_ports=(8080,)),
        credential_store=cast(Any, None),
        target_type=TargetType.EXTERNAL_PRODUCTION,
    )
    service.set_mutation_block(blocked=True)
    assert not preferences.read(env.target_id).enabled
    saved = preferences.read(env.target_id)
    from kodi_lab.application.production_inspection import ProductionInspectionError

    with pytest.raises(ProductionInspectionError, match="production_mutations_blocked"):
        preferences.update(
            env.target_id, enabled=True, expected_revision=saved.revision
        )
    service.set_mutation_block(blocked=True)
    assert preferences.read(env.target_id) == saved
    service.set_mutation_block(blocked=False)
    assert not preferences.read(env.target_id).enabled


@pytest.mark.parametrize("change", ["credential", "grant", "scope", "client", "target"])
def test_revoked_authority_blocks_new_requests_and_replay(
    tmp_path: Path, change: str
) -> None:
    env = _environment(tmp_path)
    remote = _Remote()
    tools = _tools(env, remote)
    request_id = uuid4()
    tools.remote_action(
        env.principal,
        target_id=env.target_id,
        request_id=request_id,
        action=BrowserRemoteAction.HOME,
    )
    if change == "credential":
        env.administration.revoke_credential(env.client_id, env.principal.credential_id)
    elif change == "grant":
        env.administration.remove_target_grant(env.client_id, env.target_id)
    elif change == "scope":
        env.administration.revoke_production_inspection_scope(env.inspect_scope_id)
    elif change == "client":
        env.administration.set_client_enabled(env.client_id, enabled=False)
    else:
        from datetime import UTC, datetime

        from kodi_lab.application.production_policy import (
            bind_external_production_transition,
        )
        from kodi_lab.domain import ExternalAdministrativeState
        from kodi_lab.storage.models import ExternalKodiTargetRow

        with env.factory.begin() as session:
            row = session.get(ExternalKodiTargetRow, str(env.target_id))
            policy = session.get(ExternalProductionPolicyRow, str(env.target_id))
            assert row is not None and policy is not None
            row.administrative_state = ExternalAdministrativeState.DISABLED
            bind_external_production_transition(
                session, row, policy, now=datetime.now(UTC)
            )
    for identity in (request_id, uuid4()):
        with pytest.raises(McpAuthorizationError):
            tools.remote_action(
                env.principal,
                target_id=env.target_id,
                request_id=identity,
                action=BrowserRemoteAction.HOME,
            )
    assert remote.sends == 1


@pytest.mark.parametrize("failure", [None, "identity", "bridge", "unsupported"])
def test_native_dispatch_proves_exact_action_support_and_identity(
    tmp_path: Path, failure: str | None
) -> None:
    import json

    import httpx

    from kodi_lab.application.external_bridges import ExternalBridgeServiceError
    from kodi_lab.application.mcp_target_adapters import KodiTargetAdapterService

    env = _authority_environment(
        tmp_path,
        preset_id=PRODUCTION_INSPECTION_PRESET_ID,
        acknowledge_http=False,
        native_navigation=False,
    )
    calls: list[str] = []

    class Bridge:
        def authenticate_production_identity(self, target_id: Any) -> None:
            assert target_id == env.target_id
            if failure == "bridge":
                raise ExternalBridgeServiceError("bridge_identity_mismatch")

        def invalidate_production_native_identity(self, target_id: Any) -> None:
            assert target_id == env.target_id

    def native(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        method = body["method"]
        calls.append(method)
        assert request.url.host == "192.168.50.50"
        if method == "JSONRPC.Ping":
            result: object = "pong"
        elif method == "JSONRPC.Version":
            result = {"version": {"major": 13, "minor": 0, "patch": 0}}
        elif method == "Application.GetProperties":
            result = {
                "name": "Other" if failure == "identity" else "Kodi",
                "version": {"major": 21, "minor": 3, "tag": "stable"},
            }
        elif method == "JSONRPC.Introspect":
            assert body["params"]["filter"]["id"] == "Input.Home"
            result = {
                "methods": {}
                if failure == "unsupported"
                else {"Input.Home": {"type": "method"}}
            }
        else:
            assert method == "Input.Home"
            result = "OK"
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": body["id"], "result": result}
        )

    adapters = KodiTargetAdapterService(
        env.factory,
        managed_status=cast(Any, None),
        managed_inventory=cast(Any, None),
        managed_remote=cast(Any, None),
        managed_authority=cast(Any, None),
        endpoint_policy=ExternalEndpointPolicy(allowed_ports=(8080,)),
        credential_store=cast(Any, None),
        external_bridges=cast(Any, Bridge()),
        external_transport=httpx.MockTransport(native),
    )
    tools = _tools(env, adapters)
    if failure:
        with pytest.raises(McpAuthorizationError):
            tools.remote_action(
                env.principal,
                target_id=env.target_id,
                request_id=uuid4(),
                action=BrowserRemoteAction.HOME,
            )
        assert "Input.Home" not in calls
    else:
        result = tools.remote_action(
            env.principal,
            target_id=env.target_id,
            request_id=uuid4(),
            action=BrowserRemoteAction.HOME,
        )
        assert isinstance(result, McpRemoteActionResult)
        assert result.outcome == "succeeded"
        assert calls == [
            "JSONRPC.Ping",
            "JSONRPC.Version",
            "Application.GetProperties",
            "JSONRPC.Introspect",
            "Input.Home",
        ]


def test_other_client_and_target_do_not_inherit_navigation_opt_in(
    tmp_path: Path,
) -> None:
    from kodi_lab.application.mcp_administration import McpAuthenticationService

    env = _environment(tmp_path)
    remote = _Remote()
    tools = _tools(env, remote)
    other = env.administration.create_client(
        display_name="Other inspector", preset_id=PRODUCTION_INSPECTION_PRESET_ID
    )
    issued = env.administration.issue_credential(other.id)
    principal = McpAuthenticationService(env.factory).authenticate(issued.token)
    assert principal is not None
    with pytest.raises(McpAuthorizationError):
        tools.remote_action(
            principal,
            target_id=env.target_id,
            request_id=uuid4(),
            action=BrowserRemoteAction.HOME,
        )
    with pytest.raises(McpAuthorizationError):
        tools.remote_action(
            env.principal,
            target_id=uuid4(),
            request_id=uuid4(),
            action=BrowserRemoteAction.HOME,
        )
    assert remote.sends == 0


def test_native_opt_in_policy_changes_only_the_selected_kodi(tmp_path: Path) -> None:
    from kodi_lab.domain import ExternalAuthenticationMode, ExternalProtocolKind

    env = _environment(tmp_path)
    service = ExternalTargetAdministrationService(
        env.factory,
        endpoint_policy=ExternalEndpointPolicy(allowed_ports=(8080,)),
        credential_store=ExternalCredentialStore(
            tmp_path / "other-secrets", master_key=None, key_version=1
        ),
        target_type=TargetType.EXTERNAL_PRODUCTION,
    )
    other = service.register(
        logical_name="Other household Kodi",
        protocol_kind=ExternalProtocolKind.JSONRPC_HTTP,
        host="192.168.50.51",
        port=8080,
        authentication_mode=ExternalAuthenticationMode.NONE,
        configured_capabilities={"target_inspect"},
    )
    preferences = ProductionNavigationPreferenceService(env.factory)
    before = preferences.read(other.target_id)
    selected = preferences.read(env.target_id)
    preferences.update(
        env.target_id, enabled=False, expected_revision=selected.revision
    )
    assert preferences.read(other.target_id) == before
    assert before.enabled is False
