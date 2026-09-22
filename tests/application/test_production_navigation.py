from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.browser_remote import (
    BrowserRemoteError,
    BrowserRemoteResult,
    ScreenshotRefreshResult,
)
from kodi_lab.application.mcp_administration import (
    PRODUCTION_REMOTE_KODI_PRESET_ID,
    McpAdministrationService,
    McpAuthenticationService,
    McpPrincipal,
)
from kodi_lab.application.mcp_remote_actions import (
    McpRemoteActionReceiptError,
    McpRemoteActionReceiptService,
)
from kodi_lab.application.production_inspection import (
    ProductionInspectionError,
    production_observation_digest,
)
from kodi_lab.application.production_navigation import (
    PRODUCTION_NAVIGATION_ACTIONS,
    PRODUCTION_PROHIBITED_ACTIONS,
    PRODUCTION_TRANSIENT_CONTROL_ACTIONS,
    ProductionActionClass,
    ProductionNativeControlAcknowledgementService,
    ProductionNavigationAuthority,
    ProductionNavigationRateLimiter,
    classify_production_action,
)
from kodi_lab.application.production_policy import advance_production_policy
from kodi_lab.domain import (
    ConnectionState,
    ExternalAdministrativeState,
    ExternalAuthenticationMode,
    ExternalBridgePairingState,
    ExternalBridgeReachabilityState,
    ExternalObservationState,
    ExternalProtocolKind,
    ExternalVerificationState,
    HealthStatus,
    ProductionDesignationState,
    TargetAvailability,
    TargetType,
)
from kodi_lab.kodi.remote import BrowserRemoteAction
from kodi_lab.mcp.authorization import McpAuthorizationError, McpAuthorizationService
from kodi_lab.mcp.registry import build_mcp_tool_registry
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.storage.metadata import Base
from kodi_lab.storage.models import (
    ExternalBridgePairingRow,
    ExternalKodiTargetObservationRow,
    ExternalKodiTargetRow,
    ExternalProductionPolicyRow,
    McpClientRow,
    McpProductionNavigationAuditRow,
    McpProductionRemoteReceiptBindingRow,
    McpRemoteActionReceiptRow,
    ProductionMutationControlRow,
    ReviewedBridgeBuildRow,
    TargetRow,
)

NOW = "2026-09-21T12:00:00.000000Z"
TARGET_ID = UUID("14000000-0000-4000-8000-000000000050")
BUILD_ID = UUID("15000000-0000-4000-8000-000000000050")
BRIDGE_ID = UUID("16000000-0000-4000-8000-000000000050")


@dataclass
class _AuthorityEnvironment:
    factory: sessionmaker[Session]
    administration: McpAdministrationService
    authorization: McpAuthorizationService
    acknowledgement: ProductionNativeControlAcknowledgementService
    principal: McpPrincipal
    client_id: UUID
    target_id: UUID
    inspect_scope_id: UUID
    navigate_scope_id: UUID


def _authority_environment(
    tmp_path: Path,
    *,
    protocol: ExternalProtocolKind = ExternalProtocolKind.JSONRPC_HTTP,
    acknowledge_http: bool = True,
) -> _AuthorityEnvironment:
    tmp_path.mkdir(parents=True, exist_ok=True)
    engine = create_database_engine(f"sqlite:///{tmp_path / f'{protocol.value}.db'}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    observation_id = uuid4()
    fingerprint = "a" * 64
    with factory.begin() as session:
        target = TargetRow(
            id=str(TARGET_ID),
            name="Production navigation target",
            target_type=TargetType.EXTERNAL_PRODUCTION,
            kodi_version="21.3",
            availability=TargetAvailability.AVAILABLE,
            health=HealthStatus.HEALTHY,
            connection_state=ConnectionState.CONNECTED,
        )
        external = ExternalKodiTargetRow(
            target_id=str(TARGET_ID),
            protocol_kind=protocol,
            host="192.168.50.50",
            port=8080,
            authentication_mode=ExternalAuthenticationMode.NONE,
            secret_reference=None,
            configured_capabilities_json='["semantic_remote","target_inspect"]',
            verified_capabilities_json='["semantic_remote","target_inspect"]',
            administrative_state=ExternalAdministrativeState.ENABLED,
            verification_state=ExternalVerificationState.VERIFIED,
            observation_state=ExternalObservationState.REACHABLE,
            verified_addresses_json='["192.168.50.50"]',
            verified_kodi_name="Kodi",
            verified_kodi_version="21.3",
            observed_kodi_name="Kodi",
            observed_kodi_version="21.3",
            observed_protocol_version="13.0.0",
            pending_observation_id=str(observation_id),
            production_policy_revision=1,
            created_at=NOW,
            verified_at=NOW,
            deactivated_at=None,
            last_observed_at=NOW,
        )
        observation = ExternalKodiTargetObservationRow(
            id=str(observation_id),
            target_id=str(TARGET_ID),
            outcome="reachable",
            addresses_json='["192.168.50.50"]',
            kodi_name="Kodi",
            kodi_version="21.3",
            protocol_version="13.0.0",
            verified_capabilities_json='["semantic_remote","target_inspect"]',
            error_code=None,
            attempted_at=NOW,
            completed_at=NOW,
        )
        session.add(target)
        session.flush()
        session.add(external)
        session.flush()
        session.add(observation)
        session.flush()
        session.add(
            ExternalProductionPolicyRow(
                target_id=str(TARGET_ID),
                designation_state=ProductionDesignationState.CONFIRMED,
                confirmed_observation_id=str(observation_id),
                confirmed_observation_digest=production_observation_digest(
                    external, observation
                ),
                identity_generation=1,
                policy_revision=1,
                mutations_suspended=False,
                designation_confirmed_by="local_administrator",
                designation_confirmed_at=NOW,
                designation_invalidated_at=None,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.add(
            ProductionMutationControlRow(
                id=1,
                production_mutations_blocked=False,
                policy_epoch=1,
                updated_by="local_administrator",
                updated_at=NOW,
            )
        )
        session.add(
            ReviewedBridgeBuildRow(
                id=str(BUILD_ID),
                semantic_fingerprint=fingerprint,
                zip_sha256="b" * 64,
                addon_id="service.kodi_lab_bridge",
                addon_version="1.1.7",
                protocol_version="1",
                capabilities_json='["screenshot"]',
                supported_kodi_versions_json='["21.3"]',
                source_reference="reviewed-release-1.1.7",
                reviewed_by="local_administrator",
                reviewed_at=NOW,
                state="active",
                retired_by=None,
                retired_at=None,
            )
        )
        session.flush()
        endpoint_digest = hashlib.sha256(
            json.dumps(
                {
                    "bridge_port": 9090,
                    "host": external.host,
                    "native_port": external.port,
                    "protocol": external.protocol_kind.value,
                    "verified_addresses": external.verified_addresses_json,
                    "verified_kodi_name": external.verified_kodi_name,
                    "verified_kodi_version": external.verified_kodi_version,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        session.add(
            ExternalBridgePairingRow(
                target_id=str(TARGET_ID),
                bridge_port=9090,
                pairing_state=ExternalBridgePairingState.PAIRED,
                reachability_state=ExternalBridgeReachabilityState.REACHABLE,
                pairing_token_reference=None,
                bridge_secret_reference=str(uuid4()),
                pairing_token_expires_at=None,
                observed_bridge_id=str(BRIDGE_ID),
                observed_protocol_version="1",
                observed_addon_id="service.kodi_lab_bridge",
                observed_addon_version="1.1.7",
                observed_kodi_version="21.3",
                observed_build_fingerprint=fingerprint,
                observed_capabilities_json='["screenshot"]',
                pinned_bridge_id=str(BRIDGE_ID),
                pinned_protocol_version="1",
                pinned_addon_id="service.kodi_lab_bridge",
                pinned_addon_version="1.1.7",
                pinned_kodi_version="21.3",
                pinned_build_fingerprint=fingerprint,
                verified_capabilities_json='["screenshot"]',
                endpoint_revision_digest=endpoint_digest,
                token_generated_at=None,
                pairing_attempted_at=NOW,
                paired_at=NOW,
                verified_at=NOW,
                revoked_at=None,
                production_identity_generation=1,
                production_bridge_trust_revision=1,
                reviewed_build_id=str(BUILD_ID),
                production_confirmed_by="local_administrator",
                production_confirmed_at=NOW,
            )
        )

    administration = McpAdministrationService(factory)
    client = administration.create_client(
        display_name="Production navigator",
        preset_id=PRODUCTION_REMOTE_KODI_PRESET_ID,
    )
    issued = administration.issue_credential(client.id)
    administration.assign_target_preset(TARGET_ID, PRODUCTION_REMOTE_KODI_PRESET_ID)
    administration.grant_target(client.id, TARGET_ID)
    acknowledgement = ProductionNativeControlAcknowledgementService(factory)
    if protocol is ExternalProtocolKind.JSONRPC_HTTP and acknowledge_http:
        acknowledgement.acknowledge(TARGET_ID)
    inspect = administration.grant_production_inspection_scope(
        client.id, TARGET_ID, identity_generation=1
    )
    navigate = administration.grant_production_navigate_scope(
        client.id,
        TARGET_ID,
        identity_generation=1,
        bridge_trust_revision=1,
    )
    principal = McpAuthenticationService(factory).authenticate(issued.token)
    assert principal is not None
    return _AuthorityEnvironment(
        factory=factory,
        administration=administration,
        authorization=McpAuthorizationService(factory, build_mcp_tool_registry()),
        acknowledgement=acknowledgement,
        principal=principal,
        client_id=client.id,
        target_id=TARGET_ID,
        inspect_scope_id=inspect.scope_id,
        navigate_scope_id=navigate.scope_id,
    )


def test_production_action_taxonomy_is_total_and_exact() -> None:
    assert len(BrowserRemoteAction) == 16
    assert {
        BrowserRemoteAction.UP,
        BrowserRemoteAction.DOWN,
        BrowserRemoteAction.LEFT,
        BrowserRemoteAction.RIGHT,
        BrowserRemoteAction.BACK,
        BrowserRemoteAction.HOME,
        BrowserRemoteAction.INFO,
    } == PRODUCTION_NAVIGATION_ACTIONS
    assert {
        BrowserRemoteAction.CONTEXT_MENU,
        BrowserRemoteAction.SELECT,
    } == PRODUCTION_PROHIBITED_ACTIONS
    assert {
        BrowserRemoteAction.PLAY_PAUSE,
        BrowserRemoteAction.STOP,
        BrowserRemoteAction.PREVIOUS,
        BrowserRemoteAction.NEXT,
        BrowserRemoteAction.VOLUME_UP,
        BrowserRemoteAction.VOLUME_DOWN,
        BrowserRemoteAction.MUTE_TOGGLE,
    } == PRODUCTION_TRANSIENT_CONTROL_ACTIONS
    assert {
        classify_production_action(action) for action in BrowserRemoteAction
    } == set(ProductionActionClass)


@pytest.mark.parametrize("action", sorted(PRODUCTION_NAVIGATION_ACTIONS, key=str))
def test_authorization_allows_exactly_seven_navigation_actions(
    tmp_path: Path, action: BrowserRemoteAction
) -> None:
    env = _authority_environment(tmp_path)
    authority = env.authorization.authorize_production_navigation(
        env.principal, env.target_id, action
    )
    assert authority.inspect_scope_id == env.inspect_scope_id
    assert authority.navigate_scope_id == env.navigate_scope_id
    assert authority.native_http_acknowledgement_id is not None


@pytest.mark.parametrize(
    "action",
    sorted(
        PRODUCTION_PROHIBITED_ACTIONS | PRODUCTION_TRANSIENT_CONTROL_ACTIONS, key=str
    ),
)
def test_authorization_hard_denies_prohibited_and_future_actions(
    tmp_path: Path, action: BrowserRemoteAction
) -> None:
    env = _authority_environment(tmp_path)
    with pytest.raises(McpAuthorizationError, match="production_action_prohibited"):
        env.authorization.authorize_production_navigation(
            env.principal, env.target_id, action
        )


def test_navigation_requires_inspect_navigate_current_bridge_and_vetoes(
    tmp_path: Path,
) -> None:
    env = _authority_environment(tmp_path)
    env.administration.revoke_production_inspection_scope(env.inspect_scope_id)
    with pytest.raises(McpAuthorizationError, match="not_found_or_not_authorized"):
        env.authorization.authorize_production_navigation(
            env.principal, env.target_id, BrowserRemoteAction.HOME
        )


@pytest.mark.parametrize(
    "authority_change",
    [
        "native_generation",
        "bridge_revision",
        "reviewed_build",
        "client_preset",
        "target_preset",
        "generic_grant",
        "navigate_scope",
    ],
)
def test_navigation_rejects_stale_or_revoked_authority(
    tmp_path: Path, authority_change: str
) -> None:
    env = _authority_environment(tmp_path)
    if authority_change == "navigate_scope":
        env.administration.revoke_production_inspection_scope(env.navigate_scope_id)
    elif authority_change == "generic_grant":
        env.administration.remove_target_grant(env.client_id, env.target_id)
    elif authority_change == "target_preset":
        env.administration.assign_target_preset(
            env.target_id,
            UUID("11000000-0000-4000-8000-000000000005"),
        )
    else:
        with env.factory.begin() as session:
            if authority_change == "native_generation":
                policy = session.get(ExternalProductionPolicyRow, str(env.target_id))
                assert policy is not None
                policy.identity_generation += 1
                advance_production_policy(policy, now=datetime.now(UTC))
            elif authority_change == "bridge_revision":
                bridge = session.get(ExternalBridgePairingRow, str(env.target_id))
                assert bridge is not None
                assert bridge.production_bridge_trust_revision is not None
                bridge.production_bridge_trust_revision += 1
            elif authority_change == "reviewed_build":
                build = session.get(ReviewedBridgeBuildRow, str(BUILD_ID))
                assert build is not None
                build.state = "retired"
                build.retired_by = "local_administrator"
                build.retired_at = NOW
            elif authority_change == "client_preset":
                client = session.get(McpClientRow, str(env.client_id))
                assert client is not None
                client.preset_id = "11000000-0000-4000-8000-000000000005"
    with pytest.raises(McpAuthorizationError):
        env.authorization.authorize_production_navigation(
            env.principal, env.target_id, BrowserRemoteAction.HOME
        )

    env = _authority_environment(tmp_path / "blocked")
    with env.factory.begin() as session:
        control = session.get(ProductionMutationControlRow, 1)
        assert control is not None
        control.production_mutations_blocked = True
        control.policy_epoch += 1
    with pytest.raises(McpAuthorizationError, match="production_mutations_blocked"):
        env.authorization.authorize_production_navigation(
            env.principal, env.target_id, BrowserRemoteAction.HOME
        )

    env = _authority_environment(tmp_path / "suspended")
    with env.factory.begin() as session:
        policy = session.get(ExternalProductionPolicyRow, str(env.target_id))
        assert policy is not None
        policy.mutations_suspended = True
        policy.policy_revision += 1
    with pytest.raises(
        McpAuthorizationError, match="production_target_mutations_suspended"
    ):
        env.authorization.authorize_production_navigation(
            env.principal, env.target_id, BrowserRemoteAction.HOME
        )


def test_http_acknowledgement_is_exact_and_https_is_exempt(tmp_path: Path) -> None:
    missing = _authority_environment(tmp_path / "missing", acknowledge_http=False)
    with pytest.raises(
        McpAuthorizationError, match="transport_acknowledgement_required"
    ):
        missing.authorization.authorize_production_navigation(
            missing.principal, missing.target_id, BrowserRemoteAction.HOME
        )
    missing.acknowledgement.acknowledge(missing.target_id)
    missing.authorization.authorize_production_navigation(
        missing.principal, missing.target_id, BrowserRemoteAction.HOME
    )
    with missing.factory.begin() as session:
        policy = session.get(ExternalProductionPolicyRow, str(missing.target_id))
        assert policy is not None
        policy.policy_revision += 1
    with pytest.raises(
        McpAuthorizationError, match="transport_acknowledgement_required"
    ):
        missing.authorization.authorize_production_navigation(
            missing.principal, missing.target_id, BrowserRemoteAction.HOME
        )

    https = _authority_environment(
        tmp_path / "https",
        protocol=ExternalProtocolKind.JSONRPC_HTTPS,
        acknowledge_http=False,
    )
    authority = https.authorization.authorize_production_navigation(
        https.principal, https.target_id, BrowserRemoteAction.HOME
    )
    assert authority.native_http_acknowledgement_id is None


class _OuterLimiter:
    def require_allowed(self, _actor_key: str, _target_id: UUID) -> None:
        return


class _Remote:
    def __init__(self) -> None:
        self.calls = 0
        self.sends = 0
        self.error: str | None = None

    def invoke(
        self, target_id: UUID, action: BrowserRemoteAction, **kwargs: Any
    ) -> BrowserRemoteResult:
        self.calls += 1
        before_dispatch = cast(Any, kwargs.get("before_dispatch"))
        if before_dispatch is not None:
            before_dispatch()
        self.sends += 1
        if self.error is not None:
            raise BrowserRemoteError(self.error, self.error)
        return BrowserRemoteResult(
            invocation_id=cast(UUID, kwargs["invocation_id"]),
            managed_instance_id=target_id,
            action=action,
            result="succeeded",
            completed_at=datetime.now(UTC),
            screenshot_refresh=ScreenshotRefreshResult(result="not_requested"),
        )


def _authority() -> ProductionNavigationAuthority:
    return ProductionNavigationAuthority(
        native_identity_generation=1,
        bridge_trust_revision=1,
        target_policy_revision=1,
        mutation_control_epoch=1,
        inspect_scope_id=uuid4(),
        navigate_scope_id=uuid4(),
        client_preset_id=PRODUCTION_REMOTE_KODI_PRESET_ID,
        target_preset_id=PRODUCTION_REMOTE_KODI_PRESET_ID,
        generic_grant_created_at=NOW,
        native_http_acknowledgement_id=uuid4(),
    )


def test_production_receipt_replay_binding_audit_and_capacity(tmp_path: Path) -> None:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'receipts.db'}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    remote = _Remote()
    service = McpRemoteActionReceiptService(
        factory,
        cast(Any, remote),
        _OuterLimiter(),
        production_rate_limiter=ProductionNavigationRateLimiter(),
        max_global=1,
    )
    client_id, credential_id, target_id, request_id = (
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
    )
    authority = _authority()
    authorizations = 0

    def authorize() -> ProductionNavigationAuthority:
        nonlocal authorizations
        authorizations += 1
        return authority

    first = service.invoke_production(
        client_id=client_id,
        credential_id=credential_id,
        target_id=target_id,
        request_id=request_id,
        action=BrowserRemoteAction.HOME,
        authorize_new=authorize,
    )
    assert first.outcome == "succeeded"
    assert remote.calls == 1
    terminal_authorizations = authorizations

    def no_current_authority() -> ProductionNavigationAuthority:
        raise AssertionError("terminal replay must not reauthorize dispatch")

    replay = service.invoke_production(
        client_id=client_id,
        credential_id=uuid4(),
        target_id=target_id,
        request_id=request_id,
        action=BrowserRemoteAction.HOME,
        authorize_new=no_current_authority,
    )
    assert replay.invocation_id == first.invocation_id
    assert remote.calls == 1
    assert authorizations == terminal_authorizations
    with pytest.raises(McpRemoteActionReceiptError, match="request_id_conflict"):
        service.invoke_production(
            client_id=client_id,
            credential_id=credential_id,
            target_id=target_id,
            request_id=request_id,
            action=BrowserRemoteAction.UP,
            authorize_new=no_current_authority,
        )
    with factory() as session:
        binding = session.get(
            McpProductionRemoteReceiptBindingRow, str(first.invocation_id)
        )
        audit = session.get(McpProductionNavigationAuditRow, str(first.invocation_id))
        assert binding is not None
        assert binding.navigate_scope_id == str(authority.navigate_scope_id)
        assert audit is not None and audit.dispatch_classification == "acknowledged"
        assert audit.outcome == "succeeded"

    with pytest.raises(
        McpRemoteActionReceiptError, match="remote_action_receipt_capacity"
    ):
        service.invoke_production(
            client_id=client_id,
            credential_id=credential_id,
            target_id=target_id,
            request_id=uuid4(),
            action=BrowserRemoteAction.HOME,
            authorize_new=authorize,
        )


def test_production_retention_prunes_only_coherent_completed_evidence(
    tmp_path: Path,
) -> None:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'retention.db'}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    remote = _Remote()
    now = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    service = McpRemoteActionReceiptService(
        factory,
        cast(Any, remote),
        _OuterLimiter(),
        clock=lambda: now,
        retention=timedelta(days=30),
        production_rate_limiter=ProductionNavigationRateLimiter(
            clock=lambda: now.timestamp()
        ),
    )
    client_id, credential_id, target_id = uuid4(), uuid4(), uuid4()
    authority = _authority()
    completed_request = uuid4()
    completed = service.invoke_production(
        client_id=client_id,
        credential_id=credential_id,
        target_id=target_id,
        request_id=completed_request,
        action=BrowserRemoteAction.HOME,
        authorize_new=lambda: authority,
    )
    missing_audit = service.invoke_production(
        client_id=client_id,
        credential_id=credential_id,
        target_id=target_id,
        request_id=uuid4(),
        action=BrowserRemoteAction.HOME,
        authorize_new=lambda: authority,
    )
    missing_binding = service.invoke_production(
        client_id=client_id,
        credential_id=credential_id,
        target_id=target_id,
        request_id=uuid4(),
        action=BrowserRemoteAction.HOME,
        authorize_new=lambda: authority,
    )
    with factory.begin() as session:
        missing_audit_row = session.get(
            McpProductionNavigationAuditRow, str(missing_audit.invocation_id)
        )
        missing_binding_row = session.get(
            McpProductionRemoteReceiptBindingRow,
            str(missing_binding.invocation_id),
        )
        assert missing_audit_row is not None and missing_binding_row is not None
        session.delete(missing_audit_row)
        session.delete(missing_binding_row)
        session.add(
            McpRemoteActionReceiptRow(
                id=str(uuid4()),
                client_id=str(client_id),
                credential_id=str(credential_id),
                target_id=str(target_id),
                request_id=str(uuid4()),
                action=BrowserRemoteAction.HOME.value,
                created_at="2026-08-01T12:00:00.000000Z",
                state="pending",
                final_result=None,
                completed_at=None,
                screenshot_refresh_result=None,
                screenshot_capture_id=None,
                screenshot_captured_at=None,
            )
        )
        session.add(
            McpRemoteActionReceiptRow(
                id=str(uuid4()),
                client_id=str(client_id),
                credential_id=str(credential_id),
                target_id=str(target_id),
                request_id=str(uuid4()),
                action=BrowserRemoteAction.HOME.value,
                created_at="2026-08-01T12:00:00.000000Z",
                state="ambiguous",
                final_result="remote_action_ambiguous",
                completed_at="2026-08-01T12:00:01.000000Z",
                screenshot_refresh_result="not_requested",
                screenshot_capture_id=None,
                screenshot_captured_at=None,
            )
        )
    now += timedelta(days=30, microseconds=1)
    replacement = service.invoke_production(
        client_id=client_id,
        credential_id=credential_id,
        target_id=target_id,
        request_id=completed_request,
        action=BrowserRemoteAction.HOME,
        authorize_new=lambda: authority,
    )
    assert replacement.invocation_id != completed.invocation_id
    assert remote.sends == 4
    with factory() as session:
        assert (
            session.get(
                McpProductionRemoteReceiptBindingRow, str(completed.invocation_id)
            )
            is None
        )
        assert (
            session.get(
                McpProductionRemoteReceiptBindingRow,
                str(missing_audit.invocation_id),
            )
            is not None
        )
        assert (
            session.get(
                McpProductionNavigationAuditRow,
                str(missing_binding.invocation_id),
            )
            is not None
        )
        assert (
            session.get(McpRemoteActionReceiptRow, str(missing_audit.invocation_id))
            is not None
        )
        assert (
            session.get(McpRemoteActionReceiptRow, str(missing_binding.invocation_id))
            is not None
        )
        assert (
            session.get(McpProductionNavigationAuditRow, str(completed.invocation_id))
            is None
        )
        retained = session.scalars(
            select(McpRemoteActionReceiptRow).where(
                McpRemoteActionReceiptRow.state.in_(("pending", "ambiguous"))
            )
        ).all()
        assert {row.state for row in retained} == {"pending", "ambiguous"}


def test_ambiguous_production_receipt_is_never_redispatched(tmp_path: Path) -> None:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'ambiguous.db'}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    remote = _Remote()
    remote.error = "remote_action_ambiguous"
    service = McpRemoteActionReceiptService(factory, cast(Any, remote), _OuterLimiter())
    client_id, credential_id, target_id, request_id = (
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
    )
    authority = _authority()

    def authorize() -> ProductionNavigationAuthority:
        return authority

    with pytest.raises(McpRemoteActionReceiptError, match="remote_action_ambiguous"):
        service.invoke_production(
            client_id=client_id,
            credential_id=credential_id,
            target_id=target_id,
            request_id=request_id,
            action=BrowserRemoteAction.HOME,
            authorize_new=authorize,
        )
    assert remote.calls == 1
    with pytest.raises(McpRemoteActionReceiptError, match="remote_action_ambiguous"):
        service.invoke_production(
            client_id=client_id,
            credential_id=credential_id,
            target_id=target_id,
            request_id=request_id,
            action=BrowserRemoteAction.HOME,
            authorize_new=authorize,
        )
    assert remote.calls == 1
    with factory() as session:
        receipt = session.scalar(select(McpRemoteActionReceiptRow))
        audit = session.scalar(select(McpProductionNavigationAuditRow))
        assert receipt is not None and receipt.state == "ambiguous"
        assert audit is not None and audit.dispatch_classification == "ambiguous"
        binding = session.get(McpProductionRemoteReceiptBindingRow, receipt.id)
        assert binding is not None


def test_acknowledged_native_rejection_is_terminal_and_not_redispatched(
    tmp_path: Path,
) -> None:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'rejected.db'}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    remote = _Remote()
    remote.error = "remote_kodi_rejected"
    service = McpRemoteActionReceiptService(factory, cast(Any, remote), _OuterLimiter())
    request_id = uuid4()
    authority = _authority()
    arguments: dict[str, Any] = {
        "client_id": uuid4(),
        "credential_id": uuid4(),
        "target_id": uuid4(),
        "request_id": request_id,
        "action": BrowserRemoteAction.HOME,
        "authorize_new": lambda: authority,
    }
    for _ in range(2):
        with pytest.raises(McpRemoteActionReceiptError, match="remote_kodi_rejected"):
            service.invoke_production(**arguments)
    assert remote.calls == 1
    with factory() as session:
        audit = session.scalar(select(McpProductionNavigationAuditRow))
        assert audit is not None
        assert audit.state == "completed"
        assert audit.dispatch_classification == "acknowledged"


def test_veto_after_claim_is_terminal_without_send(tmp_path: Path) -> None:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'late-veto.db'}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    remote = _Remote()
    service = McpRemoteActionReceiptService(factory, cast(Any, remote), _OuterLimiter())
    authority = _authority()
    checks = 0

    def authorize() -> ProductionNavigationAuthority:
        nonlocal checks
        checks += 1
        if checks > 1:
            raise McpRemoteActionReceiptError("production_mutations_blocked")
        return authority

    with pytest.raises(
        McpRemoteActionReceiptError, match="production_mutations_blocked"
    ):
        service.invoke_production(
            client_id=uuid4(),
            credential_id=uuid4(),
            target_id=uuid4(),
            request_id=uuid4(),
            action=BrowserRemoteAction.HOME,
            authorize_new=authorize,
        )
    assert remote.calls == 1
    assert remote.sends == 0
    with factory() as session:
        receipt = session.scalar(select(McpRemoteActionReceiptRow))
        assert receipt is not None
        assert receipt.state == "completed"
        assert receipt.final_result == "production_mutations_blocked"


def test_veto_after_acknowledged_send_is_historical_and_never_retried(
    tmp_path: Path,
) -> None:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'post-send-veto.db'}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    remote = _Remote()
    service = McpRemoteActionReceiptService(factory, cast(Any, remote), _OuterLimiter())
    authority = _authority()
    checks = 0

    def authorize() -> ProductionNavigationAuthority:
        nonlocal checks
        checks += 1
        if checks > 2:
            raise McpRemoteActionReceiptError("production_mutations_blocked")
        return authority

    client_id, credential_id, target_id, request_id = (
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
    )
    first = service.invoke_production(
        client_id=client_id,
        credential_id=credential_id,
        target_id=target_id,
        request_id=request_id,
        action=BrowserRemoteAction.HOME,
        authorize_new=authorize,
    )
    assert first.outcome == "authority_changed_after_dispatch"

    def no_current_authority() -> ProductionNavigationAuthority:
        raise AssertionError("terminal history must not reauthorize")

    replay = service.invoke_production(
        client_id=client_id,
        credential_id=uuid4(),
        target_id=target_id,
        request_id=request_id,
        action=BrowserRemoteAction.HOME,
        authorize_new=no_current_authority,
    )
    assert replay == first
    assert remote.sends == 1
    with factory() as session:
        audit = session.scalar(select(McpProductionNavigationAuditRow))
        assert audit is not None
        assert audit.dispatch_classification == "acknowledged"
        assert audit.outcome == "authority_changed_after_dispatch"


def test_required_audit_failure_blocks_dispatch(tmp_path: Path) -> None:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'audit-failure.db'}")
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TRIGGER reject_production_audit BEFORE INSERT ON "
            "mcp_production_navigation_audits BEGIN SELECT RAISE(ABORT, "
            "'audit unavailable'); END"
        )
    factory = create_session_factory(engine)
    remote = _Remote()
    service = McpRemoteActionReceiptService(factory, cast(Any, remote), _OuterLimiter())
    with pytest.raises(McpRemoteActionReceiptError, match="remote_action_ambiguous"):
        service.invoke_production(
            client_id=uuid4(),
            credential_id=uuid4(),
            target_id=uuid4(),
            request_id=uuid4(),
            action=BrowserRemoteAction.HOME,
            authorize_new=_authority,
        )
    assert remote.calls == 0
    with factory() as session:
        assert session.scalar(select(McpRemoteActionReceiptRow)) is None


def test_navigation_rate_is_two_per_second_with_burst_three() -> None:
    now = 0.0
    limiter = ProductionNavigationRateLimiter(clock=lambda: now)
    client_id, target_id = uuid4(), uuid4()
    for _ in range(3):
        limiter.require_allowed(client_id, target_id)
    with pytest.raises(ProductionInspectionError, match="rate_limited"):
        limiter.require_allowed(client_id, target_id)
    now = 0.5
    limiter.require_allowed(client_id, target_id)
