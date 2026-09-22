from __future__ import annotations

import json
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.external_targets import ExternalTargetAdministrationService
from kodi_lab.application.mcp_administration import (
    READ_ONLY_INSPECTION_PRESET_ID,
    McpAdministrationError,
    McpAdministrationService,
    McpAuthenticationService,
    McpPrincipal,
)
from kodi_lab.application.mcp_target_adapters import (
    KodiTargetAdapterError,
    KodiTargetAdapterService,
    _ExternalOperationGate,
)
from kodi_lab.application.production_inspection import (
    PRODUCTION_INSPECTION_PRESET_ID,
    ProductionInspectionError,
    ProductionReadRateLimiter,
)
from kodi_lab.domain import (
    ExternalAdministrativeState,
    ExternalAuthenticationMode,
    ExternalProtocolKind,
    ProductionDesignationState,
    TargetType,
)
from kodi_lab.kodi.external import (
    ExternalEndpointPolicy,
    ExternalKodiProbeResult,
    ValidatedExternalEndpoint,
)
from kodi_lab.mcp.authorization import McpAuthorizationError, McpAuthorizationService
from kodi_lab.mcp.registry import TargetClass, build_mcp_tool_registry
from kodi_lab.mcp.tools import McpTargetToolService
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.storage.metadata import Base
from kodi_lab.storage.models import (
    ExternalKodiTargetObservationRow,
    ExternalKodiTargetRow,
    ExternalProductionPolicyRow,
    ProductionMutationControlRow,
)
from kodi_lab.storage.secrets import ExternalCredentialStore, ExternalKodiCredential


class _Verifier:
    def probe(
        self,
        endpoint: ValidatedExternalEndpoint,
        credential: ExternalKodiCredential | None,
        configured: frozenset[str],
    ) -> ExternalKodiProbeResult:
        return ExternalKodiProbeResult(
            addresses=(endpoint.address,),
            kodi_name="Kodi",
            kodi_version="21.3",
            protocol_version="13.0.0",
            verified_capabilities=tuple(
                item
                for item in ("target_inspect", "addon_inventory")
                if item in configured
            ),
        )


@dataclass
class _Environment:
    factory: sessionmaker[Session]
    administration: McpAdministrationService
    authorization: McpAuthorizationService
    tools: McpTargetToolService
    adapters: KodiTargetAdapterService
    principal: McpPrincipal
    target_id: UUID
    client_id: UUID
    scope_id: UUID
    calls: list[dict[str, object]]
    live_name: list[str]
    now: list[float]


@pytest.fixture
def production_inspection(tmp_path: Path) -> _Environment:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'inspection.db'}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    with factory.begin() as session:
        session.add(
            ProductionMutationControlRow(
                id=1,
                production_mutations_blocked=True,
                policy_epoch=1,
                updated_by="system",
                updated_at="2026-09-21T12:00:00.000000Z",
            )
        )
    credential_store = ExternalCredentialStore(
        tmp_path / "secrets", master_key=None, key_version=1
    )
    endpoint_policy = ExternalEndpointPolicy(allowed_ports=(8080,))
    targets = ExternalTargetAdministrationService(
        factory,
        endpoint_policy=endpoint_policy,
        credential_store=credential_store,
        verifier=cast(Any, _Verifier()),
        target_type=TargetType.EXTERNAL_PRODUCTION,
    )
    target = targets.register(
        logical_name="Private production Kodi",
        protocol_kind=ExternalProtocolKind.JSONRPC_HTTP,
        host="192.168.50.21",
        port=8080,
        authentication_mode=ExternalAuthenticationMode.NONE,
        configured_capabilities={"target_inspect", "addon_inventory"},
    )
    observed = targets.probe(target.target_id)
    assert observed.pending_observation_id is not None
    targets.confirm(target.target_id, observed.pending_observation_id)
    targets.confirm_production_designation(
        target.target_id, observed.pending_observation_id
    )
    targets.set_enabled(target.target_id, enabled=True)

    administration = McpAdministrationService(factory)
    client = administration.create_client(
        display_name="Production reader",
        preset_id=PRODUCTION_INSPECTION_PRESET_ID,
    )
    issued = administration.issue_credential(client.id)
    administration.assign_target_preset(
        target.target_id, PRODUCTION_INSPECTION_PRESET_ID
    )
    administration.grant_target(client.id, target.target_id)
    scope = administration.grant_production_inspection_scope(
        client.id, target.target_id, identity_generation=1
    )
    principal = McpAuthenticationService(factory).authenticate(issued.token)
    assert principal is not None

    calls: list[dict[str, object]] = []
    live_name = ["Kodi"]

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        method = body["method"]
        if method == "JSONRPC.Ping":
            result: object = "pong"
        elif method == "JSONRPC.Version":
            result = {"version": {"major": 13, "minor": 0, "patch": 0}}
        elif method == "Application.GetProperties":
            result = {
                "name": live_name[0],
                "version": {
                    "major": 21,
                    "minor": 3,
                    "tag": "stable",
                    "tagversion": None,
                },
            }
        elif method == "Addons.GetAddons":
            assert body["params"] == {
                "type": "unknown",
                "installed": True,
                "enabled": "all",
                "properties": ["name", "version", "enabled"],
                "limits": {"start": 0, "end": 64},
            }
            result = {
                "addons": [
                    {
                        "addonid": "plugin.video.safe",
                        "name": "Safe add-on",
                        "version": "1.0.0",
                        "enabled": True,
                        "type": "xbmc.python.pluginsource",
                    }
                ],
                "limits": {"start": 0, "end": 1, "total": 1},
            }
        else:  # pragma: no cover - the closed adapter must never get here
            raise AssertionError(method)
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": body["id"], "result": result},
        )

    adapters = KodiTargetAdapterService(
        factory,
        managed_status=cast(Any, None),
        managed_inventory=cast(Any, None),
        managed_remote=cast(Any, None),
        managed_authority=cast(Any, None),
        endpoint_policy=endpoint_policy,
        credential_store=credential_store,
        external_transport=httpx.MockTransport(handler),
    )
    now = [0.0]
    authorization = McpAuthorizationService(
        factory,
        build_mcp_tool_registry(),
        targets=adapters,
        production_rates=ProductionReadRateLimiter(clock=lambda: now[0]),
    )
    tools = McpTargetToolService(
        factory,
        authorization,
        cast(Any, None),
        cast(Any, None),
        cast(Any, None),
        target_adapters=adapters,
    )
    return _Environment(
        factory=factory,
        administration=administration,
        authorization=authorization,
        tools=tools,
        adapters=adapters,
        principal=principal,
        target_id=target.target_id,
        client_id=client.id,
        scope_id=scope.scope_id,
        calls=calls,
        live_name=live_name,
        now=now,
    )


def test_exact_intersection_list_status_and_inventory_are_bounded_and_private(
    production_inspection: _Environment,
) -> None:
    env = production_inspection
    listed = env.tools.target_list(env.principal)
    assert env.calls == []
    assert len(listed.targets) == 1
    summary = listed.targets[0]
    assert summary.target_class == "external_production"
    assert summary.process_state == "unknown"
    assert summary.jsonrpc_state == "not_checked"
    serialized = listed.model_dump_json()
    assert "192.168.50.21" not in serialized
    assert "observation" not in serialized
    assert "credential" not in serialized

    status = env.tools.target_status(env.principal, target_id=env.target_id)
    assert status.target.readiness_state == "inspection_ready"
    assert [call["method"] for call in env.calls] == [
        "JSONRPC.Ping",
        "JSONRPC.Version",
        "Application.GetProperties",
    ]
    env.calls.clear()

    inventory = env.tools.addon_list(env.principal, target_id=env.target_id)
    assert inventory.total_count == 1
    assert inventory.addons[0].addon_id == "plugin.video.safe"
    assert [call["method"] for call in env.calls] == [
        "JSONRPC.Ping",
        "JSONRPC.Version",
        "Application.GetProperties",
        "Addons.GetAddons",
    ]
    assert "/" not in inventory.addons[0].addon_id


def test_generic_grant_scope_and_exact_presets_are_all_mandatory(
    production_inspection: _Environment,
) -> None:
    env = production_inspection
    env.administration.remove_target_grant(env.client_id, env.target_id)
    with pytest.raises(
        McpAuthorizationError, match="target_not_found_or_not_authorized"
    ):
        env.authorization.authorize_target(
            env.principal, "target_status", env.target_id
        )
    env.administration.grant_target(env.client_id, env.target_id)
    env.authorization.authorize_target(env.principal, "target_status", env.target_id)

    env.administration.revoke_production_inspection_scope(env.scope_id)
    with pytest.raises(
        McpAuthorizationError, match="target_not_found_or_not_authorized"
    ):
        env.authorization.authorize_target(
            env.principal, "target_status", env.target_id
        )

    with pytest.raises(McpAdministrationError, match="mcp_preset_template_invalid"):
        env.administration.create_custom_preset(
            display_name="Production clone",
            template_preset_id=PRODUCTION_INSPECTION_PRESET_ID,
        )


def test_wrong_client_target_and_client_preset_are_denied(
    production_inspection: _Environment,
) -> None:
    env = production_inspection
    other = env.administration.create_client(
        display_name="Other production reader",
        preset_id=PRODUCTION_INSPECTION_PRESET_ID,
    )
    other_credential = env.administration.issue_credential(other.id)
    other_principal = McpAuthenticationService(env.factory).authenticate(
        other_credential.token
    )
    assert other_principal is not None
    with pytest.raises(
        McpAuthorizationError, match="target_not_found_or_not_authorized"
    ):
        env.authorization.authorize_target(
            other_principal, "target_status", env.target_id
        )

    with pytest.raises(
        McpAuthorizationError, match="target_not_found_or_not_authorized"
    ):
        env.authorization.authorize_target(
            env.principal,
            "target_status",
            UUID("14000000-0000-4000-8000-000000000099"),
        )

    env.administration.assign_client_preset(
        env.client_id, READ_ONLY_INSPECTION_PRESET_ID
    )
    with pytest.raises(McpAuthorizationError, match="permission_denied"):
        env.authorization.authorize_target(
            env.principal, "target_status", env.target_id
        )


def test_mutation_vetoes_do_not_block_reads_but_target_state_does(
    production_inspection: _Environment,
) -> None:
    env = production_inspection
    with env.factory() as session:
        policy = session.get(ExternalProductionPolicyRow, str(env.target_id))
        control = session.get(ProductionMutationControlRow, 1)
        assert policy is not None and policy.mutations_suspended is True
        assert control is not None and control.production_mutations_blocked is True
    env.authorization.authorize_target(env.principal, "target_status", env.target_id)

    with env.factory.begin() as session:
        row = session.get(ExternalKodiTargetRow, str(env.target_id))
        assert row is not None
        row.administrative_state = ExternalAdministrativeState.DISABLED
    with pytest.raises(McpAuthorizationError, match="target_disabled"):
        env.authorization.authorize_target(
            env.principal, "target_status", env.target_id
        )


def test_invalidated_designation_and_missing_capability_are_denied(
    production_inspection: _Environment,
) -> None:
    env = production_inspection
    with env.factory.begin() as session:
        policy = session.get(ExternalProductionPolicyRow, str(env.target_id))
        assert policy is not None
        policy.designation_state = ProductionDesignationState.INVALIDATED
    with pytest.raises(McpAuthorizationError, match="production_policy_not_confirmed"):
        env.authorization.authorize_target(
            env.principal, "target_status", env.target_id
        )

    with env.factory.begin() as session:
        policy = session.get(ExternalProductionPolicyRow, str(env.target_id))
        row = session.get(ExternalKodiTargetRow, str(env.target_id))
        assert policy is not None and row is not None
        policy.designation_state = ProductionDesignationState.CONFIRMED
        row.configured_capabilities_json = '["target_inspect"]'
    with pytest.raises(McpAuthorizationError, match="capability_unavailable"):
        env.authorization.authorize_target(env.principal, "addon_list", env.target_id)


def test_live_identity_drift_revokes_trust_and_advances_generation_once(
    production_inspection: _Environment,
) -> None:
    env = production_inspection
    env.live_name[0] = "Not Kodi"
    with pytest.raises(McpAuthorizationError, match="identity_mismatch"):
        env.tools.target_status(env.principal, target_id=env.target_id)
    with env.factory() as session:
        policy = session.get(ExternalProductionPolicyRow, str(env.target_id))
        row = session.get(ExternalKodiTargetRow, str(env.target_id))
        assert policy is not None and policy.identity_generation == 2
        assert policy.designation_state.value == "invalidated"
        assert policy.mutations_suspended is True
        assert row is not None and row.administrative_state.value == "disabled"
        observations = session.scalars(
            select(ExternalKodiTargetObservationRow).where(
                ExternalKodiTargetObservationRow.target_id == str(env.target_id),
                ExternalKodiTargetObservationRow.error_code == "identity_mismatch",
            )
        ).all()
        assert len(observations) == 1
    env.now[0] += 6
    with pytest.raises(McpAuthorizationError, match="target_disabled"):
        env.tools.target_status(env.principal, target_id=env.target_id)
    with env.factory() as session:
        policy = session.get(ExternalProductionPolicyRow, str(env.target_id))
        assert policy is not None and policy.identity_generation == 2


def test_old_generation_scope_is_visible_as_stale_and_never_reactivates(
    production_inspection: _Environment,
) -> None:
    env = production_inspection
    with env.factory.begin() as session:
        policy = session.get(ExternalProductionPolicyRow, str(env.target_id))
        assert policy is not None
        policy.identity_generation = 2
    scopes = env.administration.list_production_inspection_scopes(
        client_id=env.client_id, target_id=env.target_id
    )
    assert [(scope.identity_generation, scope.effective_state) for scope in scopes] == [
        (1, "stale")
    ]
    with pytest.raises(
        McpAuthorizationError, match="target_not_found_or_not_authorized"
    ):
        env.authorization.authorize_target(
            env.principal, "target_status", env.target_id
        )


def test_revocation_during_read_suppresses_result(
    production_inspection: _Environment,
) -> None:
    env = production_inspection
    revoked = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal revoked
        body = json.loads(request.content)
        if body["method"] == "Application.GetProperties" and not revoked:
            env.administration.revoke_production_inspection_scope(env.scope_id)
            revoked = True
        result: object
        if body["method"] == "JSONRPC.Ping":
            result = "pong"
        elif body["method"] == "JSONRPC.Version":
            result = {"version": {"major": 13, "minor": 0, "patch": 0}}
        else:
            result = {
                "name": "Kodi",
                "version": {"major": 21, "minor": 3, "tag": "stable"},
            }
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": body["id"], "result": result},
        )

    env.adapters._external_transport = httpx.MockTransport(handler)
    with pytest.raises(
        McpAuthorizationError, match="target_not_found_or_not_authorized"
    ):
        env.tools.target_status(env.principal, target_id=env.target_id)


def test_generation_change_during_read_suppresses_result(
    production_inspection: _Environment,
) -> None:
    env = production_inspection
    changed = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal changed
        body = json.loads(request.content)
        method = body["method"]
        if method == "JSONRPC.Ping":
            result: object = "pong"
        elif method == "JSONRPC.Version":
            result = {"version": {"major": 13, "minor": 0, "patch": 0}}
        else:
            result = {
                "name": "Kodi",
                "version": {"major": 21, "minor": 3, "tag": "stable"},
            }
            if not changed:
                with env.factory.begin() as session:
                    policy = session.get(
                        ExternalProductionPolicyRow, str(env.target_id)
                    )
                    assert policy is not None
                    policy.identity_generation += 1
                changed = True
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": body["id"], "result": result},
        )

    env.adapters._external_transport = httpx.MockTransport(handler)
    with pytest.raises(
        McpAuthorizationError, match="target_not_found_or_not_authorized"
    ):
        env.tools.target_status(env.principal, target_id=env.target_id)


def test_database_failure_during_post_read_authorization_suppresses_result(
    production_inspection: _Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = production_inspection
    original = env.authorization.authorize_target
    calls = 0

    def authorize(principal: McpPrincipal, tool_name: str, target_id: UUID) -> object:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise SQLAlchemyError("database unavailable")
        return original(principal, tool_name, target_id)

    monkeypatch.setattr(env.authorization, "authorize_target", authorize)
    with pytest.raises(SQLAlchemyError, match="database unavailable"):
        env.tools.target_status(env.principal, target_id=env.target_id)
    assert calls == 2


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (httpx.Response(401), "authentication_failed"),
        (
            httpx.Response(
                200,
                content=b"{" + (b"x" * 65_536),
                headers={"content-type": "application/json"},
            ),
            "invalid_kodi_response",
        ),
    ],
)
def test_production_native_failures_are_bounded(
    production_inspection: _Environment,
    response: httpx.Response,
    expected: str,
) -> None:
    env = production_inspection
    env.adapters._external_transport = httpx.MockTransport(lambda _request: response)
    with pytest.raises(McpAuthorizationError, match=expected):
        env.tools.target_status(env.principal, target_id=env.target_id)


def test_production_native_timeout_is_target_unreachable(
    production_inspection: _Environment,
) -> None:
    env = production_inspection

    def timeout(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("bounded timeout")

    env.adapters._external_transport = httpx.MockTransport(timeout)
    with pytest.raises(McpAuthorizationError, match="target_unreachable"):
        env.tools.target_status(env.principal, target_id=env.target_id)


def test_registry_allows_exactly_four_production_tools() -> None:
    registry = build_mcp_tool_registry()
    eligible = {
        definition.name
        for definition in (registry.require(name) for name in registry.names())
        if definition.production_eligible
        and TargetClass.EXTERNAL_PRODUCTION in definition.allowed_target_classes
    }
    assert len(registry.names()) == 15
    assert eligible == {
        "target_list",
        "target_status",
        "addon_list",
        "screenshot_capture",
    }


def test_production_read_rate_maps_are_bounded_and_idle_only() -> None:
    now = [0.0]
    limiter = ProductionReadRateLimiter(
        maximum_entries=1, idle_seconds=10, clock=lambda: now[0]
    )
    first = UUID("14000000-0000-4000-8000-000000000001")
    second = UUID("14000000-0000-4000-8000-000000000002")
    target = UUID("14000000-0000-4000-8000-000000000003")
    limiter.require_target_list(first)
    limiter.require_target_list(first)
    with pytest.raises(ProductionInspectionError, match="rate_limited"):
        limiter.require_target_list(first)
    with pytest.raises(ProductionInspectionError, match="rate_limited"):
        limiter.require_target_list(second)
    now[0] = 11
    limiter.require_target_list(second)
    limiter.require_target_read("target_status", second, target)
    with pytest.raises(ProductionInspectionError, match="rate_limited"):
        limiter.require_target_read("target_status", second, target)
    limiter.require_target_read("addon_list", second, target)
    with pytest.raises(ProductionInspectionError, match="rate_limited"):
        limiter.require_target_read("addon_list", second, target)
    now[0] += 5
    limiter.require_target_read("target_status", second, target)
    now[0] += 25
    limiter.require_target_read("addon_list", second, target)


def test_production_native_gate_is_four_global_and_one_per_target() -> None:
    gate = _ExternalOperationGate(global_limit=4)
    targets = [UUID(f"14000000-0000-4000-8000-{index:012d}") for index in range(1, 6)]
    with ExitStack() as stack:
        for target_id in targets[:4]:
            stack.enter_context(gate.enter(target_id))
        with (
            pytest.raises(KodiTargetAdapterError, match="rate_limited"),
            gate.enter(targets[4]),
        ):
            pass

    with (
        gate.enter(targets[0]),
        pytest.raises(KodiTargetAdapterError, match="rate_limited"),
        gate.enter(targets[0]),
    ):
        pass
