from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Never
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from kodi_lab.application.mcp_administration import (
    FULL_LAB_CONTROL_PRESET_ID,
    PRODUCTION_REMOTE_KODI_PRESET_ID,
)
from kodi_lab.application.mcp_remote_actions import (
    McpRemoteActionReceiptService,
    McpRemoteActionResult,
)
from kodi_lab.application.production_inspection import PRODUCTION_VISUAL_PRESET_ID
from kodi_lab.application.production_transient_approvals import (
    ProductionApprovalError,
    ProductionApprovalRequiredResult,
    ProductionApprovalTerminalResult,
    ProductionTransientApprovalService,
    ProductionTransientResult,
)
from kodi_lab.domain import ExternalAdministrativeState
from kodi_lab.kodi.remote import BrowserRemoteAction
from kodi_lab.storage.models import (
    ExternalBridgePairingRow,
    ExternalKodiTargetRow,
    ExternalProductionPolicyRow,
    McpCredentialRow,
    McpProductionRemoteReceiptBindingRow,
    McpRemoteActionReceiptRow,
    ProductionApprovalTransitionEventRow,
    ProductionMutationControlRow,
    ProductionOperationApprovalRow,
    ProductionTransientDispatchAuditRow,
    ReviewedBridgeBuildRow,
)
from tests.application.test_production_navigation import (
    BUILD_ID,
    _authority_environment,
    _AuthorityEnvironment,
    _OuterLimiter,
    _Remote,
)


@dataclass
class _Clock:
    now: datetime = datetime(2026, 9, 21, 12, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


@dataclass
class _Preflight:
    error: str | None = None
    calls: int = 0

    def __call__(self, _target_id: UUID, _action: BrowserRemoteAction) -> None:
        self.calls += 1
        if self.error is not None:
            raise ProductionApprovalError(self.error)


def _service(
    tmp_path: Path,
) -> tuple[
    _AuthorityEnvironment,
    UUID,
    _Clock,
    _Remote,
    _Preflight,
    ProductionTransientApprovalService,
]:
    env = _authority_environment(tmp_path)
    transient = env.administration.grant_production_transient_control_scope(
        env.client_id,
        env.target_id,
        identity_generation=1,
        bridge_trust_revision=1,
    )
    clock = _Clock()
    remote = _Remote()
    receipts = McpRemoteActionReceiptService(
        env.factory,
        remote,
        _OuterLimiter(),
        clock=clock,
    )
    preflight = _Preflight()
    service = ProductionTransientApprovalService(
        env.factory,
        receipts,
        preflight=preflight,
        clock=clock,
    )
    return env, transient.scope_id, clock, remote, preflight, service


def _request(
    service: ProductionTransientApprovalService,
    env: _AuthorityEnvironment,
    request_id: UUID,
) -> ProductionTransientResult:
    return service.invoke(
        client_id=env.client_id,
        credential_id=env.principal.credential_id,
        target_id=env.target_id,
        request_id=request_id,
        action=BrowserRemoteAction.PLAY_PAUSE,
    )


@pytest.mark.parametrize(
    "action",
    [
        BrowserRemoteAction.PLAY_PAUSE,
        BrowserRemoteAction.STOP,
        BrowserRemoteAction.PREVIOUS,
        BrowserRemoteAction.NEXT,
        BrowserRemoteAction.VOLUME_UP,
        BrowserRemoteAction.VOLUME_DOWN,
        BrowserRemoteAction.MUTE_TOGGLE,
    ],
)
def test_each_transient_action_requires_approval_without_dispatch(
    tmp_path: Path, action: BrowserRemoteAction
) -> None:
    env, _scope, _clock, remote, preflight, service = _service(tmp_path)
    result = service.invoke(
        client_id=env.client_id,
        credential_id=env.principal.credential_id,
        target_id=env.target_id,
        request_id=uuid4(),
        action=action,
    )
    assert isinstance(result, ProductionApprovalRequiredResult)
    assert result.action is action
    assert remote.calls == 0
    assert preflight.calls == 0


def test_pending_request_is_durable_idempotent_and_creates_no_receipt(
    tmp_path: Path,
) -> None:
    env, _scope, clock, remote, preflight, service = _service(tmp_path)
    request_id = uuid4()

    first = _request(service, env, request_id)
    duplicate = _request(service, env, request_id)

    assert isinstance(first, ProductionApprovalRequiredResult)
    assert duplicate == first
    assert first.expires_at == clock.now + timedelta(minutes=15)
    assert remote.calls == 0
    assert preflight.calls == 0
    with env.factory() as session:
        assert (
            session.scalar(
                select(func.count()).select_from(ProductionOperationApprovalRow)
            )
            == 1
        )
        assert (
            session.scalar(select(func.count()).select_from(McpRemoteActionReceiptRow))
            == 0
        )
        assert (
            session.scalar(
                select(func.count()).select_from(ProductionApprovalTransitionEventRow)
            )
            == 1
        )

    with pytest.raises(ProductionApprovalError, match="request_id_conflict"):
        service.invoke(
            client_id=env.client_id,
            credential_id=env.principal.credential_id,
            target_id=env.target_id,
            request_id=request_id,
            action=BrowserRemoteAction.STOP,
        )


def test_web_approval_consumes_once_with_atomic_receipt_binding_and_audit(
    tmp_path: Path,
) -> None:
    env, transient_scope_id, _clock, remote, preflight, service = _service(tmp_path)
    request_id = uuid4()
    pending = _request(service, env, request_id)
    assert isinstance(pending, ProductionApprovalRequiredResult)

    approved = service.approve(
        pending.approval_id,
        action=BrowserRemoteAction.PLAY_PAUSE,
    )
    assert approved.state == "approved"
    result = _request(service, env, request_id)

    assert isinstance(result, McpRemoteActionResult)
    assert result.outcome == "succeeded"
    assert remote.sends == 1
    assert preflight.calls == 1
    with env.factory() as session:
        approval = session.get(ProductionOperationApprovalRow, str(pending.approval_id))
        assert approval is not None
        assert approval.state == "consumed"
        assert approval.linked_remote_receipt_id is not None
        receipt = session.get(
            McpRemoteActionReceiptRow, approval.linked_remote_receipt_id
        )
        binding = session.get(
            McpProductionRemoteReceiptBindingRow, approval.linked_remote_receipt_id
        )
        audit = session.get(
            ProductionTransientDispatchAuditRow, approval.linked_remote_receipt_id
        )
        assert receipt is not None and receipt.state == "completed"
        assert binding is not None
        assert binding.operation_class == "transient_control"
        assert binding.approval_id == approval.id
        assert binding.navigate_scope_id is None
        assert binding.transient_scope_id == str(transient_scope_id)
        assert audit is not None and audit.state == "completed"
        assert audit.outcome == "succeeded"

    replay = _request(service, env, request_id)
    assert replay == result
    assert remote.sends == 1
    assert preflight.calls == 1


def test_denial_expiry_and_temporary_reachability_are_terminally_distinct(
    tmp_path: Path,
) -> None:
    env, _scope, clock, remote, preflight, service = _service(tmp_path)
    denied_request = uuid4()
    denied = _request(service, env, denied_request)
    assert isinstance(denied, ProductionApprovalRequiredResult)
    with pytest.raises(
        ProductionApprovalError, match="approval_action_confirmation_mismatch"
    ):
        service.approve(denied.approval_id, action=BrowserRemoteAction.STOP)
    with env.factory.begin() as session:
        mutation = session.get(ProductionMutationControlRow, 1)
        assert mutation is not None
        mutation.production_mutations_blocked = True
        mutation.policy_epoch += 1
    service.deny(denied.approval_id)
    terminal = _request(service, env, denied_request)
    assert isinstance(terminal, ProductionApprovalTerminalResult)
    assert terminal.state == "denied"

    with env.factory.begin() as session:
        mutation = session.get(ProductionMutationControlRow, 1)
        assert mutation is not None
        mutation.production_mutations_blocked = False
        mutation.policy_epoch += 1

    clock.advance(timedelta(seconds=31))
    retryable_request = uuid4()
    retryable = _request(service, env, retryable_request)
    assert isinstance(retryable, ProductionApprovalRequiredResult)
    service.approve(retryable.approval_id, action=BrowserRemoteAction.PLAY_PAUSE)
    approved_expiry = service.get(retryable.approval_id).expires_at
    preflight.error = "target_unreachable"
    with pytest.raises(ProductionApprovalError, match="target_unreachable"):
        _request(service, env, retryable_request)
    assert service.get(retryable.approval_id).state == "approved"
    assert service.get(retryable.approval_id).expires_at == approved_expiry
    assert remote.sends == 0

    preflight.error = None
    clock.advance(timedelta(minutes=5))
    expired = _request(service, env, retryable_request)
    assert isinstance(expired, ProductionApprovalTerminalResult)
    assert expired.state == "expired"
    assert remote.sends == 0


def test_web_terminal_observation_persists_expiry_before_rejection(
    tmp_path: Path,
) -> None:
    env, _scope, clock, _remote, _preflight, service = _service(tmp_path)
    pending = _request(service, env, uuid4())
    assert isinstance(pending, ProductionApprovalRequiredResult)
    clock.advance(timedelta(minutes=15))
    with pytest.raises(ProductionApprovalError, match="approval_not_actionable"):
        service.cancel(pending.approval_id)
    current = service.get(pending.approval_id)
    assert current.state == "expired"
    assert current.terminal_reason == "expired"


def test_authority_resurrection_and_mutation_epoch_cannot_revive_approval(
    tmp_path: Path,
) -> None:
    env, _scope, clock, remote, _preflight, service = _service(tmp_path)
    request_id = uuid4()
    pending = _request(service, env, request_id)
    assert isinstance(pending, ProductionApprovalRequiredResult)
    service.approve(pending.approval_id, action=BrowserRemoteAction.PLAY_PAUSE)

    env.administration.remove_target_grant(env.client_id, env.target_id)
    env.administration.grant_target(env.client_id, env.target_id)
    with pytest.raises(ProductionApprovalError, match="authority_changed"):
        _request(service, env, request_id)
    assert service.get(pending.approval_id).state == "cancelled"
    assert remote.sends == 0

    clock.advance(timedelta(seconds=31))
    fresh_request = uuid4()
    fresh = _request(service, env, fresh_request)
    assert isinstance(fresh, ProductionApprovalRequiredResult)
    service.approve(fresh.approval_id, action=BrowserRemoteAction.PLAY_PAUSE)
    with env.factory.begin() as session:
        mutation = session.get(ProductionMutationControlRow, 1)
        assert mutation is not None
        mutation.production_mutations_blocked = True
        mutation.policy_epoch += 1
        mutation.production_mutations_blocked = False
        mutation.policy_epoch += 1
    with pytest.raises(ProductionApprovalError, match="mutation_policy_changed"):
        _request(service, env, fresh_request)
    assert service.get(fresh.approval_id).state == "cancelled"
    assert remote.sends == 0


@pytest.mark.parametrize(
    "change",
    [
        "credential",
        "client_enablement",
        "client_preset",
        "target_preset",
        "inspect_scope",
        "transient_scope",
        "native_generation",
        "bridge_revision",
        "reviewed_build",
        "capability",
        "http_acknowledgement",
        "target_enablement",
        "target_suspension",
    ],
)
def test_exact_authority_changes_leave_old_approval_unusable(
    tmp_path: Path, change: str
) -> None:
    env, transient_scope_id, _clock, remote, _preflight, service = _service(tmp_path)
    pending = _request(service, env, uuid4())
    assert isinstance(pending, ProductionApprovalRequiredResult)
    service.approve(pending.approval_id, action=BrowserRemoteAction.PLAY_PAUSE)

    if change == "credential":
        env.administration.rotate_credential(env.client_id, env.principal.credential_id)
    elif change == "client_enablement":
        env.administration.set_client_enabled(env.client_id, False)
        env.administration.set_client_enabled(env.client_id, True)
    elif change == "client_preset":
        env.administration.assign_client_preset(
            env.client_id, FULL_LAB_CONTROL_PRESET_ID
        )
        env.administration.assign_client_preset(
            env.client_id, PRODUCTION_REMOTE_KODI_PRESET_ID
        )
    elif change == "target_preset":
        env.administration.assign_target_preset(
            env.target_id, PRODUCTION_VISUAL_PRESET_ID
        )
        env.administration.assign_target_preset(
            env.target_id, PRODUCTION_REMOTE_KODI_PRESET_ID
        )
    elif change == "inspect_scope":
        env.administration.revoke_production_inspection_scope(env.inspect_scope_id)
        env.administration.grant_production_inspection_scope(
            env.client_id, env.target_id, identity_generation=1
        )
        env.administration.grant_production_transient_control_scope(
            env.client_id,
            env.target_id,
            identity_generation=1,
            bridge_trust_revision=1,
        )
    elif change == "transient_scope":
        env.administration.revoke_production_inspection_scope(transient_scope_id)
        env.administration.grant_production_transient_control_scope(
            env.client_id,
            env.target_id,
            identity_generation=1,
            bridge_trust_revision=1,
        )
    else:
        with env.factory.begin() as session:
            if change == "native_generation":
                policy = session.get(ExternalProductionPolicyRow, str(env.target_id))
                assert policy is not None
                policy.identity_generation += 1
                policy.policy_revision += 1
            elif change == "bridge_revision":
                pairing = session.get(ExternalBridgePairingRow, str(env.target_id))
                assert pairing is not None
                assert pairing.production_bridge_trust_revision is not None
                pairing.production_bridge_trust_revision += 1
            elif change == "reviewed_build":
                build = session.get(ReviewedBridgeBuildRow, str(BUILD_ID))
                assert build is not None
                build.state = "retired"
                build.retired_by = "local_administrator"
                build.retired_at = "2026-09-21T12:01:00.000000Z"
            elif change == "capability":
                external = session.get(ExternalKodiTargetRow, str(env.target_id))
                assert external is not None
                external.verified_capabilities_json = '["target_inspect"]'
            elif change == "http_acknowledgement":
                policy = session.get(ExternalProductionPolicyRow, str(env.target_id))
                assert policy is not None
                policy.policy_revision += 1
            elif change == "target_enablement":
                external = session.get(ExternalKodiTargetRow, str(env.target_id))
                policy = session.get(ExternalProductionPolicyRow, str(env.target_id))
                assert external is not None and policy is not None
                external.administrative_state = ExternalAdministrativeState.DISABLED
                external.administrative_state = ExternalAdministrativeState.ENABLED
                policy.policy_revision += 2
            elif change == "target_suspension":
                policy = session.get(ExternalProductionPolicyRow, str(env.target_id))
                assert policy is not None
                policy.mutations_suspended = True
                policy.mutations_suspended = False
                policy.policy_revision += 2
        if change == "http_acknowledgement":
            env.acknowledgement.acknowledge(env.target_id)

    with pytest.raises(ProductionApprovalError):
        _request(service, env, pending.request_id)
    assert service.get(pending.approval_id).state == "cancelled"
    assert remote.sends == 0


def test_new_approval_admission_is_rate_limited_but_duplicate_bypasses(
    tmp_path: Path,
) -> None:
    env, _scope, _clock, _remote, _preflight, service = _service(tmp_path)
    first_request = uuid4()
    first = _request(service, env, first_request)
    assert _request(service, env, first_request) == first
    with pytest.raises(ProductionApprovalError, match="approval_creation_rate_limited"):
        _request(service, env, uuid4())


def test_live_capacity_and_durable_dispatch_interval_fail_immediately(
    tmp_path: Path,
) -> None:
    env, _scope, clock, remote, _preflight, service = _service(tmp_path)
    with env.factory.begin() as session:
        credential = session.get(McpCredentialRow, str(env.principal.credential_id))
        assert credential is not None
        credential.expires_at = None

    pending = []
    for _index in range(8):
        result = _request(service, env, uuid4())
        assert isinstance(result, ProductionApprovalRequiredResult)
        pending.append(result)
        clock.advance(timedelta(seconds=31))
    with pytest.raises(ProductionApprovalError, match="approval_capacity"):
        _request(service, env, uuid4())

    for approval in pending[2:]:
        service.cancel(approval.approval_id)
    service.approve(pending[0].approval_id, action=BrowserRemoteAction.PLAY_PAUSE)
    service.approve(pending[1].approval_id, action=BrowserRemoteAction.PLAY_PAUSE)
    first = _request(service, env, pending[0].request_id)
    assert isinstance(first, McpRemoteActionResult)
    assert first.outcome == "succeeded"
    with pytest.raises(ProductionApprovalError, match="rate_limited"):
        _request(service, env, pending[1].request_id)
    assert service.get(pending[1].approval_id).state == "approved"
    assert remote.sends == 1


def test_concurrent_exact_consumption_has_one_claim_and_one_dispatch(
    tmp_path: Path,
) -> None:
    env, _scope, _clock, remote, _preflight, service = _service(tmp_path)
    request_id = uuid4()
    pending = _request(service, env, request_id)
    assert isinstance(pending, ProductionApprovalRequiredResult)
    service.approve(pending.approval_id, action=BrowserRemoteAction.PLAY_PAUSE)

    def invoke() -> str:
        try:
            result = _request(service, env, request_id)
            assert isinstance(result, McpRemoteActionResult)
            return result.outcome
        except ProductionApprovalError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = sorted(executor.map(lambda _index: invoke(), range(2)))

    assert outcomes == ["remote_action_ambiguous", "succeeded"]
    assert remote.sends == 1
    replay = _request(service, env, request_id)
    assert isinstance(replay, McpRemoteActionResult)
    assert replay.outcome == "succeeded"
    assert remote.sends == 1


def test_atomic_claim_rollback_leaves_approval_retryable_and_no_receipt(
    tmp_path: Path,
) -> None:
    env, _scope, _clock, remote, _preflight, service = _service(tmp_path)
    request_id = uuid4()
    pending = _request(service, env, request_id)
    assert isinstance(pending, ProductionApprovalRequiredResult)
    service.approve(pending.approval_id, action=BrowserRemoteAction.PLAY_PAUSE)
    with env.factory.begin() as session:
        session.execute(
            text(
                "CREATE TRIGGER fail_transient_audit BEFORE INSERT ON "
                "production_transient_dispatch_audits BEGIN "
                "SELECT RAISE(ABORT, 'required transient audit failed'); END"
            )
        )
    with pytest.raises(IntegrityError, match="required transient audit failed"):
        _request(service, env, request_id)
    with env.factory() as session:
        approval = session.get(ProductionOperationApprovalRow, str(pending.approval_id))
        assert approval is not None and approval.state == "approved"
        assert (
            session.scalar(select(func.count()).select_from(McpRemoteActionReceiptRow))
            == 0
        )
    assert remote.sends == 0

    with env.factory.begin() as session:
        session.execute(text("DROP TRIGGER fail_transient_audit"))
    result = _request(service, env, request_id)
    assert isinstance(result, McpRemoteActionResult)
    assert result.outcome == "succeeded"
    assert remote.sends == 1


def test_post_commit_crash_leaves_protected_receipt_and_never_redispatches(
    tmp_path: Path,
) -> None:
    env = _authority_environment(tmp_path)
    env.administration.grant_production_transient_control_scope(
        env.client_id,
        env.target_id,
        identity_generation=1,
        bridge_trust_revision=1,
    )
    clock = _Clock()

    class CrashRemote(_Remote):
        def invoke(
            self, target_id: UUID, action: BrowserRemoteAction, **kwargs: Any
        ) -> Never:
            self.calls += 1
            raise RuntimeError("simulated process loss before native send")

    remote = CrashRemote()
    receipts = McpRemoteActionReceiptService(
        env.factory, remote, _OuterLimiter(), clock=clock
    )
    service = ProductionTransientApprovalService(
        env.factory, receipts, preflight=_Preflight(), clock=clock
    )
    request_id = uuid4()
    pending = _request(service, env, request_id)
    assert isinstance(pending, ProductionApprovalRequiredResult)
    service.approve(pending.approval_id, action=BrowserRemoteAction.PLAY_PAUSE)

    with pytest.raises(ProductionApprovalError, match="remote_action_ambiguous"):
        _request(service, env, request_id)
    with env.factory() as session:
        approval = session.get(ProductionOperationApprovalRow, str(pending.approval_id))
        assert approval is not None and approval.state == "consumed"
        receipt = session.get(
            McpRemoteActionReceiptRow, approval.linked_remote_receipt_id
        )
        assert receipt is not None and receipt.state == "pending"
    with pytest.raises(ProductionApprovalError, match="remote_action_ambiguous"):
        _request(service, env, request_id)
    assert remote.calls == 1


def test_retention_deletes_only_coherent_terminal_bundles_atomically(
    tmp_path: Path,
) -> None:
    env, _scope, clock, _remote, _preflight, service = _service(tmp_path)
    with env.factory.begin() as session:
        credential = session.get(McpCredentialRow, str(env.principal.credential_id))
        assert credential is not None
        credential.expires_at = None
    denied_request = uuid4()
    denied = _request(service, env, denied_request)
    assert isinstance(denied, ProductionApprovalRequiredResult)
    service.deny(denied.approval_id)

    clock.advance(timedelta(days=31))
    completed_request = uuid4()
    completed = _request(service, env, completed_request)
    assert isinstance(completed, ProductionApprovalRequiredResult)
    service.approve(completed.approval_id, action=BrowserRemoteAction.PLAY_PAUSE)
    result = _request(service, env, completed_request)
    assert isinstance(result, McpRemoteActionResult)
    receipt_id = result.invocation_id

    clock.advance(timedelta(days=31))
    assert isinstance(_request(service, env, uuid4()), ProductionApprovalRequiredResult)
    with env.factory() as session:
        assert (
            session.get(ProductionOperationApprovalRow, str(denied.approval_id)) is None
        )
        assert (
            session.get(ProductionOperationApprovalRow, str(completed.approval_id))
            is None
        )
        assert session.get(McpRemoteActionReceiptRow, str(receipt_id)) is None
        assert (
            session.get(McpProductionRemoteReceiptBindingRow, str(receipt_id)) is None
        )
        assert session.get(ProductionTransientDispatchAuditRow, str(receipt_id)) is None
        assert (
            session.scalar(
                select(func.count())
                .select_from(ProductionApprovalTransitionEventRow)
                .where(
                    ProductionApprovalTransitionEventRow.approval_id.in_(
                        (str(denied.approval_id), str(completed.approval_id))
                    )
                )
            )
            == 0
        )


def test_retention_protects_unobserved_pending_and_consumed_pending_receipt(
    tmp_path: Path,
) -> None:
    env = _authority_environment(tmp_path)
    env.administration.grant_production_transient_control_scope(
        env.client_id,
        env.target_id,
        identity_generation=1,
        bridge_trust_revision=1,
    )
    clock = _Clock()

    class CrashRemote(_Remote):
        def invoke(
            self, target_id: UUID, action: BrowserRemoteAction, **kwargs: Any
        ) -> Never:
            raise RuntimeError("simulated post-commit loss")

    receipts = McpRemoteActionReceiptService(
        env.factory, CrashRemote(), _OuterLimiter(), clock=clock
    )
    service = ProductionTransientApprovalService(
        env.factory, receipts, preflight=_Preflight(), clock=clock
    )
    with env.factory.begin() as session:
        credential = session.get(McpCredentialRow, str(env.principal.credential_id))
        assert credential is not None
        credential.expires_at = None
    untouched = _request(service, env, uuid4())
    assert isinstance(untouched, ProductionApprovalRequiredResult)
    clock.advance(timedelta(seconds=31))
    consumed = _request(service, env, uuid4())
    assert isinstance(consumed, ProductionApprovalRequiredResult)
    service.approve(consumed.approval_id, action=BrowserRemoteAction.PLAY_PAUSE)
    with pytest.raises(ProductionApprovalError, match="remote_action_ambiguous"):
        _request(service, env, consumed.request_id)

    clock.advance(timedelta(days=31))
    assert isinstance(_request(service, env, uuid4()), ProductionApprovalRequiredResult)
    with env.factory() as session:
        pending_row = session.get(
            ProductionOperationApprovalRow, str(untouched.approval_id)
        )
        consumed_row = session.get(
            ProductionOperationApprovalRow, str(consumed.approval_id)
        )
        assert pending_row is not None and pending_row.state == "pending"
        assert consumed_row is not None and consumed_row.state == "consumed"
        receipt = session.get(
            McpRemoteActionReceiptRow, consumed_row.linked_remote_receipt_id
        )
        assert receipt is not None and receipt.state == "pending"
