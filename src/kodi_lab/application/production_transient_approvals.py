"""Durable one-shot Web approvals for production transient remote control."""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.mcp_administration import PRODUCTION_REMOTE_KODI_PRESET_ID
from kodi_lab.application.mcp_remote_actions import (
    REMOTE_RECEIPT_MAX_GLOBAL,
    REMOTE_RECEIPT_MAX_PER_CLIENT,
    REMOTE_RECEIPT_RETENTION,
    McpRemoteActionReceiptError,
    McpRemoteActionReceiptService,
    McpRemoteActionResult,
)
from kodi_lab.application.production_inspection import (
    ProductionInspectionError,
    evaluate_production_bridge_anchor_state,
    evaluate_production_inspection_state,
)
from kodi_lab.application.production_navigation import (
    PRODUCTION_TRANSIENT_CONTROL_ACTIONS,
    current_native_control_acknowledgement,
)
from kodi_lab.domain import ExternalProtocolKind, TargetType
from kodi_lab.kodi.remote import BrowserRemoteAction
from kodi_lab.storage.models import (
    ExternalBridgePairingRow,
    ExternalKodiTargetRow,
    ExternalProductionPolicyRow,
    McpClientRow,
    McpClientTargetGrantRow,
    McpCredentialRow,
    McpPresetPermissionRow,
    McpPresetRow,
    McpProductionRemoteReceiptBindingRow,
    McpProductionTargetScopeRow,
    McpRemoteActionReceiptRow,
    McpTargetPresetAssignmentRow,
    ProductionApprovalCreationRateRow,
    ProductionApprovalTransitionEventRow,
    ProductionMutationControlRow,
    ProductionOperationApprovalRow,
    ProductionTransientDispatchAuditRow,
    ProductionTransientDispatchRateRow,
    TargetRow,
)

APPROVAL_CANONICALIZATION_REVISION = 1
MAX_PENDING_TTL = timedelta(minutes=15)
MAX_APPROVED_DISPATCH_WINDOW = timedelta(minutes=5)
APPROVAL_CREATION_INTERVAL = timedelta(seconds=30)
TRANSIENT_DISPATCH_INTERVAL = timedelta(seconds=5)
APPROVAL_RETENTION = timedelta(days=30)
MAX_LIVE_APPROVALS_PER_CLIENT = 8
MAX_LIVE_APPROVALS_GLOBAL = 128
MAX_APPROVAL_HISTORY_PER_CLIENT = 1_000
MAX_APPROVAL_HISTORY_GLOBAL = 10_000
APPROVAL_CLEANUP_BATCH = 200


def _text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone aware")
    return (
        value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    )


def _time(value: str | None) -> datetime | None:
    return (
        None if value is None else datetime.fromisoformat(value.replace("Z", "+00:00"))
    )


def canonical_transient_payload_digest(
    target_id: UUID, action: BrowserRemoteAction
) -> str:
    if action not in PRODUCTION_TRANSIENT_CONTROL_ACTIONS:
        raise ValueError("action is not transient production control")
    encoded = json.dumps(
        {
            "action": action.value,
            "target_id": str(target_id),
            "tool": "remote_action",
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class ProductionApprovalError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class ProductionApprovalRequiredResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = 1
    result_type: str = Field(default="approval_required", pattern="^approval_required$")
    approval_id: UUID
    request_id: UUID
    target_id: UUID
    action: BrowserRemoteAction
    state: str = Field(pattern="^pending$")
    expires_at: datetime


class ProductionApprovalTerminalResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = 1
    result_type: str = Field(default="approval_terminal", pattern="^approval_terminal$")
    approval_id: UUID
    request_id: UUID
    target_id: UUID
    action: BrowserRemoteAction
    state: str = Field(pattern="^(denied|cancelled|expired)$")
    terminal_reason: str = Field(max_length=32)


class ProductionApprovalRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    approval_id: UUID
    target_id: UUID
    target_name: str = Field(max_length=256)
    client_id: UUID
    client_name: str = Field(max_length=120)
    request_id: UUID
    action: BrowserRemoteAction
    state: str
    terminal_reason: str | None
    created_at: datetime
    expires_at: datetime
    trust_summary: str = Field(max_length=128)
    global_mutations_blocked: bool
    target_mutations_suspended: bool


@dataclass(frozen=True, slots=True)
class ProductionTransientAuthority:
    native_identity_generation: int
    bridge_trust_revision: int
    target_policy_revision: int
    mutation_control_epoch: int
    client_authority_revision: int
    generic_grant_id: UUID
    generic_grant_created_at: str
    client_preset_id: UUID
    target_preset_id: UUID
    inspect_scope_id: UUID
    transient_scope_id: UUID
    native_http_acknowledgement_id: UUID | None
    reviewed_build_id: UUID


type ProductionTransientResult = (
    McpRemoteActionResult
    | ProductionApprovalRequiredResult
    | ProductionApprovalTerminalResult
)


class ProductionTransientApprovalService:
    """Approval request, Web decision, atomic receipt claim, and retention."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        remote_receipts: McpRemoteActionReceiptService,
        *,
        preflight: Callable[[UUID, BrowserRemoteAction], None],
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        pending_ttl: timedelta = MAX_PENDING_TTL,
        approved_dispatch_window: timedelta = MAX_APPROVED_DISPATCH_WINDOW,
    ) -> None:
        if pending_ttl <= timedelta(0) or pending_ttl > MAX_PENDING_TTL:
            raise ValueError("pending approval TTL exceeds policy")
        if (
            approved_dispatch_window <= timedelta(0)
            or approved_dispatch_window > MAX_APPROVED_DISPATCH_WINDOW
        ):
            raise ValueError("approved dispatch window exceeds policy")
        self._sessions = session_factory
        self._remote_receipts = remote_receipts
        self._preflight = preflight
        self._clock = clock
        self._pending_ttl = pending_ttl
        self._approved_dispatch_window = approved_dispatch_window
        self._dispatch_guard = threading.Lock()

    def invoke(
        self,
        *,
        client_id: UUID,
        credential_id: UUID,
        target_id: UUID,
        request_id: UUID,
        action: BrowserRemoteAction,
    ) -> ProductionTransientResult:
        if action not in PRODUCTION_TRANSIENT_CONTROL_ACTIONS:
            raise ProductionApprovalError("production_action_prohibited")
        digest = canonical_transient_payload_digest(target_id, action)

        receipt = self._remote_receipts.existing(client_id, request_id)
        if receipt is not None:
            try:
                return self._remote_receipts.replay(
                    receipt, target_id=target_id, action=action
                )
            except McpRemoteActionReceiptError as error:
                raise ProductionApprovalError(error.code) from error

        approval = self._approval_for_request(client_id, request_id)
        if approval is None:
            authority = self._authority(
                client_id=client_id,
                credential_id=credential_id,
                target_id=target_id,
                now=self._clock(),
            )
            return self._create(
                client_id=client_id,
                credential_id=credential_id,
                target_id=target_id,
                request_id=request_id,
                action=action,
                digest=digest,
                authority=authority,
            )

        self._require_exact(approval, target_id=target_id, action=action, digest=digest)
        approval = self._observe_expiry(approval.id)
        if approval.state == "pending":
            return self._required(approval)
        if approval.state in {"denied", "cancelled", "expired"}:
            return self._terminal(approval)
        if approval.state == "consumed":
            receipt = self._remote_receipts.existing(client_id, request_id)
            if receipt is None:
                raise ProductionApprovalError("remote_action_ambiguous")
            try:
                return self._remote_receipts.replay(
                    receipt, target_id=target_id, action=action
                )
            except McpRemoteActionReceiptError as error:
                raise ProductionApprovalError(error.code) from error
        if approval.state != "approved":
            raise ProductionApprovalError("approval_state_invalid")
        return self._consume(
            approval_id=UUID(approval.id),
            client_id=client_id,
            credential_id=credential_id,
            target_id=target_id,
            request_id=request_id,
            action=action,
            digest=digest,
        )

    def list_queue(self) -> list[ProductionApprovalRead]:
        now = self._clock()
        with self._sessions() as session:
            ids = tuple(
                session.scalars(
                    select(ProductionOperationApprovalRow.id)
                    .where(
                        ProductionOperationApprovalRow.state.in_(
                            ("pending", "approved")
                        )
                    )
                    .order_by(
                        ProductionOperationApprovalRow.created_at,
                        ProductionOperationApprovalRow.id,
                    )
                    .limit(129)
                ).all()
            )
        if len(ids) > 128:
            raise ProductionApprovalError("approval_queue_too_large")
        for approval_id in ids:
            self._observe_expiry(approval_id, now=now)
        with self._sessions() as session:
            rows = session.scalars(
                select(ProductionOperationApprovalRow)
                .where(ProductionOperationApprovalRow.id.in_(ids or ("",)))
                .order_by(
                    ProductionOperationApprovalRow.created_at,
                    ProductionOperationApprovalRow.id,
                )
            ).all()
            return [self._read(session, row) for row in rows]

    def approve(
        self,
        approval_id: UUID,
        *,
        action: BrowserRemoteAction,
        administrator_reference: str = "local_administrator",
    ) -> ProductionApprovalRead:
        administrator = self._administrator(administrator_reference)
        now = self._clock()
        failure: str | None = None
        with self._sessions.begin() as session:
            row = self._row(session, approval_id)
            self._expire_in_session(session, row, now)
            if row.action != action.value:
                failure = "approval_action_confirmation_mismatch"
            elif row.state != "pending":
                failure = "approval_not_pending"
            else:
                try:
                    authority = self._authority_in_session(
                        session,
                        client_id=UUID(row.client_id),
                        credential_id=UUID(row.credential_id),
                        target_id=UUID(row.target_id),
                        now=now,
                    )
                except ProductionApprovalError as error:
                    failure = error.code
                    self._cancel_in_session(session, row, now, error.code)
                else:
                    mismatch = self._authority_mismatch(row, authority)
                    if mismatch is not None:
                        failure = mismatch
                        self._cancel_in_session(session, row, now, mismatch)
                    else:
                        row.state = "approved"
                        row.approved_at = _text(now)
                        row.approved_dispatch_expires_at = _text(
                            now + self._approved_dispatch_window
                        )
                        row.approving_administrator = administrator
                        self._event(
                            session,
                            row,
                            event_kind="approved",
                            from_state="pending",
                            to_state="approved",
                            actor_kind="web_admin",
                            actor_reference=administrator,
                            now=now,
                        )
        if failure is not None:
            raise ProductionApprovalError(failure)
        return self.get(approval_id)

    def deny(
        self,
        approval_id: UUID,
        *,
        administrator_reference: str = "local_administrator",
    ) -> ProductionApprovalRead:
        return self._web_terminal(
            approval_id,
            allowed_states=frozenset({"pending"}),
            state="denied",
            reason="administrator_denied",
            event_kind="denied",
            administrator_reference=administrator_reference,
        )

    def cancel(
        self,
        approval_id: UUID,
        *,
        administrator_reference: str = "local_administrator",
    ) -> ProductionApprovalRead:
        return self._web_terminal(
            approval_id,
            allowed_states=frozenset({"pending", "approved"}),
            state="cancelled",
            reason="administrator_cancelled",
            event_kind="cancelled",
            administrator_reference=administrator_reference,
        )

    def get(self, approval_id: UUID) -> ProductionApprovalRead:
        row = self._observe_expiry(str(approval_id))
        with self._sessions() as session:
            current = session.get(ProductionOperationApprovalRow, row.id)
            assert current is not None
            return self._read(session, current)

    def _create(
        self,
        *,
        client_id: UUID,
        credential_id: UUID,
        target_id: UUID,
        request_id: UUID,
        action: BrowserRemoteAction,
        digest: str,
        authority: ProductionTransientAuthority,
    ) -> ProductionApprovalRequiredResult:
        now = self._clock()
        approval_id = uuid4()
        try:
            with self._sessions.begin() as session:
                existing = session.scalar(
                    select(ProductionOperationApprovalRow).where(
                        ProductionOperationApprovalRow.client_id == str(client_id),
                        ProductionOperationApprovalRow.request_id == str(request_id),
                    )
                )
                if existing is not None:
                    self._require_exact(
                        existing, target_id=target_id, action=action, digest=digest
                    )
                    session.expunge(existing)
                    return self._required(existing)
                current = self._authority_in_session(
                    session,
                    client_id=client_id,
                    credential_id=credential_id,
                    target_id=target_id,
                    now=now,
                )
                if current != authority:
                    raise ProductionApprovalError("production_authority_changed")
                self._cleanup(session, now)
                self._require_creation_capacity(session, client_id)
                rate = session.get(
                    ProductionApprovalCreationRateRow,
                    {"client_id": str(client_id), "target_id": str(target_id)},
                )
                if (
                    rate is not None
                    and now - self._required_time(rate.last_created_at)
                    < APPROVAL_CREATION_INTERVAL
                ):
                    raise ProductionApprovalError("approval_creation_rate_limited")
                if rate is None:
                    session.add(
                        ProductionApprovalCreationRateRow(
                            client_id=str(client_id),
                            target_id=str(target_id),
                            last_created_at=_text(now),
                        )
                    )
                else:
                    rate.last_created_at = _text(now)
                row = ProductionOperationApprovalRow(
                    id=str(approval_id),
                    client_id=str(client_id),
                    credential_id=str(credential_id),
                    target_id=str(target_id),
                    tool="remote_action",
                    action=action.value,
                    payload_sha256=digest,
                    canonicalization_revision=APPROVAL_CANONICALIZATION_REVISION,
                    request_id=str(request_id),
                    native_identity_generation=authority.native_identity_generation,
                    bridge_trust_revision=authority.bridge_trust_revision,
                    target_policy_revision=authority.target_policy_revision,
                    mutation_control_epoch=authority.mutation_control_epoch,
                    client_authority_revision=authority.client_authority_revision,
                    generic_grant_id=str(authority.generic_grant_id),
                    client_preset_id=str(authority.client_preset_id),
                    target_preset_id=str(authority.target_preset_id),
                    inspect_scope_id=str(authority.inspect_scope_id),
                    transient_scope_id=str(authority.transient_scope_id),
                    native_http_acknowledgement_id=(
                        None
                        if authority.native_http_acknowledgement_id is None
                        else str(authority.native_http_acknowledgement_id)
                    ),
                    reviewed_build_id=str(authority.reviewed_build_id),
                    state="pending",
                    terminal_reason=None,
                    created_at=_text(now),
                    pending_expires_at=_text(now + self._pending_ttl),
                    approved_at=None,
                    approved_dispatch_expires_at=None,
                    approving_administrator=None,
                    consumed_at=None,
                    linked_remote_receipt_id=None,
                )
                session.add(row)
                self._event(
                    session,
                    row,
                    event_kind="requested",
                    from_state=None,
                    to_state="pending",
                    actor_kind="mcp_client",
                    actor_reference=str(client_id),
                    now=now,
                )
                session.flush()
                session.expunge(row)
                return self._required(row)
        except IntegrityError:
            approval = self._approval_for_request(client_id, request_id)
            if approval is None:
                raise ProductionApprovalError("approval_request_ambiguous") from None
            self._require_exact(
                approval, target_id=target_id, action=action, digest=digest
            )
            return self._required(approval)

    def _consume(
        self,
        *,
        approval_id: UUID,
        client_id: UUID,
        credential_id: UUID,
        target_id: UUID,
        request_id: UUID,
        action: BrowserRemoteAction,
        digest: str,
    ) -> ProductionTransientResult:
        try:
            self._preflight(target_id, action)
        except Exception as error:
            code = getattr(error, "code", "target_unreachable")
            if code in {"bridge_unreachable", "target_unreachable"}:
                raise ProductionApprovalError(code) from error
            reason = (
                "capability_lost"
                if code
                in {
                    "bridge_build_not_reviewed",
                    "bridge_capability_unavailable",
                    "bridge_identity_mismatch",
                    "bridge_protocol_unsupported",
                    "capability_unavailable",
                    "identity_mismatch",
                }
                else "authority_changed"
            )
            self._invalidate(approval_id, reason)
            raise ProductionApprovalError(code) from error

        # Serialize only the durable claim window in this process. The lock is
        # released before native I/O; SQLite state provides cross-worker exclusion.
        self._dispatch_guard.acquire()
        guard_held = True
        try:
            existing = self._remote_receipts.existing(client_id, request_id)
            if existing is not None:
                try:
                    return self._remote_receipts.replay(
                        existing, target_id=target_id, action=action
                    )
                except McpRemoteActionReceiptError as error:
                    raise ProductionApprovalError(error.code) from error
            receipt_id = uuid4()
            now = self._clock()
            failure: str | None = None
            terminal: ProductionApprovalTerminalResult | None = None
            with self._sessions.begin() as session:
                row = self._row(session, approval_id)
                self._expire_in_session(session, row, now)
                self._require_exact(
                    row, target_id=target_id, action=action, digest=digest
                )
                if row.state in {"denied", "cancelled", "expired"}:
                    terminal = self._terminal(row)
                elif (
                    row.state != "approved"
                    or row.client_id != str(client_id)
                    or row.credential_id != str(credential_id)
                    or row.request_id != str(request_id)
                ):
                    failure = "approval_not_approved"
                authority: ProductionTransientAuthority | None = None
                if failure is None and terminal is None:
                    try:
                        authority = self._authority_in_session(
                            session,
                            client_id=client_id,
                            credential_id=credential_id,
                            target_id=target_id,
                            now=now,
                        )
                    except ProductionApprovalError as error:
                        failure = error.code
                        self._cancel_in_session(session, row, now, error.code)
                    else:
                        mismatch = self._authority_mismatch(row, authority)
                        if mismatch is not None:
                            failure = mismatch
                            self._cancel_in_session(session, row, now, mismatch)
                if failure is None and terminal is None:
                    assert authority is not None
                    self._require_remote_receipt_capacity(session, client_id, now)
                    # SQLite serializes this write transaction. A durable pending
                    # transient audit is therefore the cross-worker in-flight
                    # exclusion token; uncertain post-commit work remains protected.
                    in_flight_global = (
                        session.scalar(
                            select(func.count())
                            .select_from(ProductionTransientDispatchAuditRow)
                            .where(
                                ProductionTransientDispatchAuditRow.state == "pending"
                            )
                        )
                        or 0
                    )
                    in_flight_target = (
                        session.scalar(
                            select(func.count())
                            .select_from(ProductionTransientDispatchAuditRow)
                            .where(
                                ProductionTransientDispatchAuditRow.state == "pending",
                                ProductionTransientDispatchAuditRow.target_id
                                == str(target_id),
                            )
                        )
                        or 0
                    )
                    if in_flight_global or in_flight_target:
                        failure = "rate_limited"
                    rate = session.get(
                        ProductionTransientDispatchRateRow,
                        {"client_id": str(client_id), "target_id": str(target_id)},
                    )
                    if (
                        failure is None
                        and rate is not None
                        and now - self._required_time(rate.last_dispatched_at)
                        < TRANSIENT_DISPATCH_INTERVAL
                    ):
                        failure = "rate_limited"
                    if failure is None and rate is None:
                        session.add(
                            ProductionTransientDispatchRateRow(
                                client_id=str(client_id),
                                target_id=str(target_id),
                                last_dispatched_at=_text(now),
                            )
                        )
                    elif failure is None:
                        assert rate is not None
                        rate.last_dispatched_at = _text(now)
                if failure is None and terminal is None:
                    assert authority is not None
                    receipt = McpRemoteActionReceiptRow(
                        id=str(receipt_id),
                        client_id=str(client_id),
                        credential_id=str(credential_id),
                        target_id=str(target_id),
                        request_id=str(request_id),
                        action=action.value,
                        created_at=_text(now),
                        state="pending",
                        final_result=None,
                        completed_at=None,
                        screenshot_refresh_result=None,
                        screenshot_capture_id=None,
                        screenshot_captured_at=None,
                    )
                    session.add(receipt)
                    # Establish the base receipt key before inserting the binding;
                    # both writes remain inside this one SQLite transaction.
                    session.flush()
                    session.add(
                        McpProductionRemoteReceiptBindingRow(
                            receipt_id=str(receipt_id),
                            operation_class="transient_control",
                            native_identity_generation=authority.native_identity_generation,
                            bridge_trust_revision=authority.bridge_trust_revision,
                            target_policy_revision=authority.target_policy_revision,
                            mutation_control_epoch=authority.mutation_control_epoch,
                            inspect_scope_id=str(authority.inspect_scope_id),
                            navigate_scope_id=None,
                            transient_scope_id=str(authority.transient_scope_id),
                            approval_id=str(approval_id),
                            client_preset_id=str(authority.client_preset_id),
                            target_preset_id=str(authority.target_preset_id),
                            generic_grant_created_at=authority.generic_grant_created_at,
                            generic_grant_id=str(authority.generic_grant_id),
                            native_http_acknowledgement_id=(
                                None
                                if authority.native_http_acknowledgement_id is None
                                else str(authority.native_http_acknowledgement_id)
                            ),
                        )
                    )
                    session.add(
                        ProductionTransientDispatchAuditRow(
                            receipt_id=str(receipt_id),
                            approval_id=str(approval_id),
                            client_id=str(client_id),
                            credential_id=str(credential_id),
                            target_id=str(target_id),
                            request_id=str(request_id),
                            action=action.value,
                            payload_sha256=digest,
                            canonicalization_revision=APPROVAL_CANONICALIZATION_REVISION,
                            state="pending",
                            outcome="pending",
                            dispatch_classification="not_dispatched",
                            requested_at=_text(now),
                            completed_at=None,
                        )
                    )
                    row.state = "consumed"
                    row.consumed_at = _text(now)
                    row.linked_remote_receipt_id = str(receipt_id)
                    self._event(
                        session,
                        row,
                        event_kind="consumed",
                        from_state="approved",
                        to_state="consumed",
                        actor_kind="mcp_client",
                        actor_reference=str(client_id),
                        now=now,
                        linked_remote_receipt_id=receipt_id,
                    )
                    session.flush()

            self._dispatch_guard.release()
            guard_held = False
            if terminal is not None:
                return terminal
            if failure is not None:
                raise ProductionApprovalError(failure)
            assert authority is not None

            def immediate_recheck() -> None:
                current = self._authority(
                    client_id=client_id,
                    credential_id=credential_id,
                    target_id=target_id,
                    now=self._clock(),
                )
                if current != authority:
                    raise ProductionApprovalError("production_authority_changed")

            try:
                return self._remote_receipts.dispatch_claimed_production(
                    receipt_id=receipt_id,
                    client_id=client_id,
                    target_id=target_id,
                    action=action,
                    before_dispatch=immediate_recheck,
                )
            except McpRemoteActionReceiptError as error:
                raise ProductionApprovalError(error.code) from error
        finally:
            if guard_held:
                self._dispatch_guard.release()

    def _authority(
        self,
        *,
        client_id: UUID,
        credential_id: UUID,
        target_id: UUID,
        now: datetime,
    ) -> ProductionTransientAuthority:
        with self._sessions() as session:
            return self._authority_in_session(
                session,
                client_id=client_id,
                credential_id=credential_id,
                target_id=target_id,
                now=now,
            )

    @staticmethod
    def _authority_in_session(
        session: Session,
        *,
        client_id: UUID,
        credential_id: UUID,
        target_id: UUID,
        now: datetime,
    ) -> ProductionTransientAuthority:
        client = session.get(McpClientRow, str(client_id))
        credential = session.get(McpCredentialRow, str(credential_id))
        target = session.get(TargetRow, str(target_id))
        assignment = session.get(McpTargetPresetAssignmentRow, str(target_id))
        grant = session.get(
            McpClientTargetGrantRow,
            {"client_id": str(client_id), "target_id": str(target_id)},
        )
        if (
            client is None
            or not client.enabled
            or credential is None
            or credential.client_id != client.id
            or credential.revoked_at is not None
            or target is None
            or target.target_type is not TargetType.EXTERNAL_PRODUCTION
            or assignment is None
            or grant is None
        ):
            raise ProductionApprovalError("authority_changed")
        expires_at = _time(credential.expires_at)
        if expires_at is not None and expires_at <= now:
            raise ProductionApprovalError("authority_changed")
        remote_preset = session.get(McpPresetRow, str(PRODUCTION_REMOTE_KODI_PRESET_ID))
        permissions = frozenset(
            session.scalars(
                select(McpPresetPermissionRow.permission).where(
                    McpPresetPermissionRow.preset_id
                    == str(PRODUCTION_REMOTE_KODI_PRESET_ID)
                )
            ).all()
        )
        if (
            client.preset_id != str(PRODUCTION_REMOTE_KODI_PRESET_ID)
            or assignment.preset_id != str(PRODUCTION_REMOTE_KODI_PRESET_ID)
            or remote_preset is None
            or not remote_preset.builtin
            or not remote_preset.assignable
            or remote_preset.display_name != "Production Remote Kodi"
            or permissions
            != frozenset({"target.inspect", "target.visual", "target.control"})
        ):
            raise ProductionApprovalError("authority_changed")
        try:
            native = evaluate_production_inspection_state(
                session,
                target_id,
                required_capabilities=frozenset({"target_inspect", "semantic_remote"}),
            )
            bridge = evaluate_production_bridge_anchor_state(session, target_id)
        except ProductionInspectionError as error:
            reason = (
                "capability_lost"
                if error.code
                in {
                    "bridge_build_not_reviewed",
                    "bridge_reverification_required",
                    "capability_unavailable",
                }
                else "authority_changed"
            )
            raise ProductionApprovalError(reason) from error
        inspect = session.scalar(
            select(McpProductionTargetScopeRow).where(
                McpProductionTargetScopeRow.client_id == str(client_id),
                McpProductionTargetScopeRow.target_id == str(target_id),
                McpProductionTargetScopeRow.identity_generation
                == native.identity_generation,
                McpProductionTargetScopeRow.scope == "inspect",
                McpProductionTargetScopeRow.state == "active",
            )
        )
        transient = session.scalar(
            select(McpProductionTargetScopeRow).where(
                McpProductionTargetScopeRow.client_id == str(client_id),
                McpProductionTargetScopeRow.target_id == str(target_id),
                McpProductionTargetScopeRow.identity_generation
                == native.identity_generation,
                McpProductionTargetScopeRow.bridge_trust_revision
                == bridge.bridge_trust_revision,
                McpProductionTargetScopeRow.scope == "request_transient_control",
                McpProductionTargetScopeRow.state == "active",
            )
        )
        if inspect is None or transient is None:
            raise ProductionApprovalError("authority_changed")
        policy = session.get(ExternalProductionPolicyRow, str(target_id))
        mutation = session.get(ProductionMutationControlRow, 1)
        external = session.get(ExternalKodiTargetRow, str(target_id))
        pairing = session.get(ExternalBridgePairingRow, str(target_id))
        if policy is None or mutation is None or external is None or pairing is None:
            raise ProductionApprovalError("authority_changed")
        if mutation.production_mutations_blocked or policy.mutations_suspended:
            raise ProductionApprovalError("mutation_policy_changed")
        try:
            acknowledgement = current_native_control_acknowledgement(session, target_id)
        except ProductionInspectionError as error:
            raise ProductionApprovalError("authority_changed") from error
        if (
            external.protocol_kind is ExternalProtocolKind.JSONRPC_HTTP
            and acknowledgement is None
        ):
            raise ProductionApprovalError("authority_changed")
        return ProductionTransientAuthority(
            native_identity_generation=native.identity_generation,
            bridge_trust_revision=bridge.bridge_trust_revision,
            target_policy_revision=policy.policy_revision,
            mutation_control_epoch=mutation.policy_epoch,
            client_authority_revision=client.authority_revision,
            generic_grant_id=UUID(grant.grant_id),
            generic_grant_created_at=grant.created_at,
            client_preset_id=UUID(client.preset_id),
            target_preset_id=UUID(assignment.preset_id),
            inspect_scope_id=UUID(inspect.id),
            transient_scope_id=UUID(transient.id),
            native_http_acknowledgement_id=(
                None if acknowledgement is None else UUID(acknowledgement.id)
            ),
            reviewed_build_id=bridge.reviewed_build_id,
        )

    @staticmethod
    def _authority_mismatch(
        row: ProductionOperationApprovalRow,
        authority: ProductionTransientAuthority,
    ) -> str | None:
        if (
            row.target_policy_revision != authority.target_policy_revision
            or row.mutation_control_epoch != authority.mutation_control_epoch
        ):
            return "mutation_policy_changed"
        expected = (
            row.native_identity_generation,
            row.bridge_trust_revision,
            row.client_authority_revision,
            row.generic_grant_id,
            row.client_preset_id,
            row.target_preset_id,
            row.inspect_scope_id,
            row.transient_scope_id,
            row.native_http_acknowledgement_id,
            row.reviewed_build_id,
        )
        actual = (
            authority.native_identity_generation,
            authority.bridge_trust_revision,
            authority.client_authority_revision,
            str(authority.generic_grant_id),
            str(authority.client_preset_id),
            str(authority.target_preset_id),
            str(authority.inspect_scope_id),
            str(authority.transient_scope_id),
            None
            if authority.native_http_acknowledgement_id is None
            else str(authority.native_http_acknowledgement_id),
            str(authority.reviewed_build_id),
        )
        return None if expected == actual else "authority_changed"

    def _web_terminal(
        self,
        approval_id: UUID,
        *,
        allowed_states: frozenset[str],
        state: str,
        reason: str,
        event_kind: str,
        administrator_reference: str,
    ) -> ProductionApprovalRead:
        administrator = self._administrator(administrator_reference)
        now = self._clock()
        failure: str | None = None
        with self._sessions.begin() as session:
            row = self._row(session, approval_id)
            self._expire_in_session(session, row, now)
            if row.state not in allowed_states:
                failure = "approval_not_actionable"
            else:
                previous = row.state
                row.state = state
                row.terminal_reason = reason
                self._event(
                    session,
                    row,
                    event_kind=event_kind,
                    from_state=previous,
                    to_state=state,
                    actor_kind="web_admin",
                    actor_reference=administrator,
                    terminal_reason=reason,
                    now=now,
                )
        if failure is not None:
            raise ProductionApprovalError(failure)
        return self.get(approval_id)

    def _invalidate(self, approval_id: UUID, reason: str) -> None:
        now = self._clock()
        with self._sessions.begin() as session:
            row = self._row(session, approval_id)
            if row.state not in {"pending", "approved"}:
                return
            self._cancel_in_session(session, row, now, reason)

    def _cancel_in_session(
        self,
        session: Session,
        row: ProductionOperationApprovalRow,
        now: datetime,
        reason: str,
    ) -> None:
        previous = row.state
        row.state = "cancelled"
        row.terminal_reason = reason
        self._event(
            session,
            row,
            event_kind="cancelled",
            from_state=previous,
            to_state="cancelled",
            actor_kind="system",
            actor_reference="authority_evaluator",
            terminal_reason=reason,
            now=now,
        )

    def _observe_expiry(
        self, approval_id: str, *, now: datetime | None = None
    ) -> ProductionOperationApprovalRow:
        observed_at = now or self._clock()
        with self._sessions.begin() as session:
            row = session.get(ProductionOperationApprovalRow, approval_id)
            if row is None:
                raise ProductionApprovalError("approval_not_found")
            self._expire_in_session(session, row, observed_at)
            session.flush()
            session.expunge(row)
            return row

    def _expire_in_session(
        self,
        session: Session,
        row: ProductionOperationApprovalRow,
        now: datetime,
    ) -> None:
        deadline = (
            self._required_time(row.pending_expires_at)
            if row.state == "pending"
            else self._required_time(row.approved_dispatch_expires_at)
            if row.state == "approved"
            else None
        )
        if deadline is None or now < deadline:
            return
        previous = row.state
        row.state = "expired"
        row.terminal_reason = "expired"
        self._event(
            session,
            row,
            event_kind="expired",
            from_state=previous,
            to_state="expired",
            actor_kind="system",
            actor_reference="deadline_observer",
            terminal_reason="expired",
            now=now,
        )

    def _cleanup(self, session: Session, now: datetime) -> None:
        cutoff = _text(now - APPROVAL_RETENTION)
        rows = session.scalars(
            select(ProductionOperationApprovalRow)
            .where(
                ProductionOperationApprovalRow.created_at < cutoff,
                ProductionOperationApprovalRow.state.in_(
                    ("denied", "cancelled", "expired", "consumed")
                ),
            )
            .order_by(
                ProductionOperationApprovalRow.created_at,
                ProductionOperationApprovalRow.id,
            )
            .limit(APPROVAL_CLEANUP_BATCH)
        ).all()
        for row in rows:
            events = session.scalars(
                select(ProductionApprovalTransitionEventRow).where(
                    ProductionApprovalTransitionEventRow.approval_id == row.id
                )
            ).all()
            requested = sum(event.event_kind == "requested" for event in events) == 1
            terminal = sum(event.to_state == row.state for event in events) == 1
            if not requested or not terminal:
                continue
            if row.state != "consumed":
                if row.linked_remote_receipt_id is not None:
                    continue
                session.delete(row)
                session.flush()
                for event in events:
                    session.delete(event)
                continue
            if row.linked_remote_receipt_id is None:
                continue
            receipt = session.get(
                McpRemoteActionReceiptRow, row.linked_remote_receipt_id
            )
            binding = session.get(
                McpProductionRemoteReceiptBindingRow, row.linked_remote_receipt_id
            )
            audit = session.get(
                ProductionTransientDispatchAuditRow, row.linked_remote_receipt_id
            )
            if (
                receipt is None
                or binding is None
                or audit is None
                or receipt.state != "completed"
                or binding.operation_class != "transient_control"
                or binding.approval_id != row.id
                or audit.approval_id != row.id
                or audit.state != "completed"
                or audit.outcome != receipt.final_result
                or audit.completed_at != receipt.completed_at
            ):
                continue
            session.delete(row)
            session.flush()
            for event in events:
                session.delete(event)
            session.delete(binding)
            session.delete(audit)
            session.flush()
            session.delete(receipt)

    @staticmethod
    def _require_creation_capacity(session: Session, client_id: UUID) -> None:
        live_client = (
            session.scalar(
                select(func.count())
                .select_from(ProductionOperationApprovalRow)
                .where(
                    ProductionOperationApprovalRow.client_id == str(client_id),
                    ProductionOperationApprovalRow.state.in_(("pending", "approved")),
                )
            )
            or 0
        )
        live_global = (
            session.scalar(
                select(func.count())
                .select_from(ProductionOperationApprovalRow)
                .where(
                    ProductionOperationApprovalRow.state.in_(("pending", "approved"))
                )
            )
            or 0
        )
        history_client = (
            session.scalar(
                select(func.count())
                .select_from(ProductionOperationApprovalRow)
                .where(ProductionOperationApprovalRow.client_id == str(client_id))
            )
            or 0
        )
        history_global = (
            session.scalar(
                select(func.count()).select_from(ProductionOperationApprovalRow)
            )
            or 0
        )
        if (
            live_client >= MAX_LIVE_APPROVALS_PER_CLIENT
            or live_global >= MAX_LIVE_APPROVALS_GLOBAL
            or history_client >= MAX_APPROVAL_HISTORY_PER_CLIENT
            or history_global >= MAX_APPROVAL_HISTORY_GLOBAL
        ):
            raise ProductionApprovalError("approval_capacity")

    @staticmethod
    def _require_remote_receipt_capacity(
        session: Session, client_id: UUID, now: datetime
    ) -> None:
        McpRemoteActionReceiptService._cleanup_expired(
            session, _text(now - REMOTE_RECEIPT_RETENTION)
        )
        client_count = (
            session.scalar(
                select(func.count())
                .select_from(McpRemoteActionReceiptRow)
                .where(McpRemoteActionReceiptRow.client_id == str(client_id))
            )
            or 0
        )
        global_count = (
            session.scalar(select(func.count()).select_from(McpRemoteActionReceiptRow))
            or 0
        )
        if (
            client_count >= REMOTE_RECEIPT_MAX_PER_CLIENT
            or global_count >= REMOTE_RECEIPT_MAX_GLOBAL
        ):
            raise ProductionApprovalError("remote_action_receipt_capacity")

    def _approval_for_request(
        self, client_id: UUID, request_id: UUID
    ) -> ProductionOperationApprovalRow | None:
        with self._sessions() as session:
            row = session.scalar(
                select(ProductionOperationApprovalRow).where(
                    ProductionOperationApprovalRow.client_id == str(client_id),
                    ProductionOperationApprovalRow.request_id == str(request_id),
                )
            )
            if row is not None:
                session.expunge(row)
            return row

    @staticmethod
    def _require_exact(
        row: ProductionOperationApprovalRow,
        *,
        target_id: UUID,
        action: BrowserRemoteAction,
        digest: str,
    ) -> None:
        if (
            row.target_id != str(target_id)
            or row.action != action.value
            or row.payload_sha256 != digest
            or row.canonicalization_revision != APPROVAL_CANONICALIZATION_REVISION
        ):
            raise ProductionApprovalError("request_id_conflict")

    @staticmethod
    def _row(session: Session, approval_id: UUID) -> ProductionOperationApprovalRow:
        row = session.get(ProductionOperationApprovalRow, str(approval_id))
        if row is None:
            raise ProductionApprovalError("approval_not_found")
        return row

    @staticmethod
    def _event(
        session: Session,
        row: ProductionOperationApprovalRow,
        *,
        event_kind: str,
        from_state: str | None,
        to_state: str,
        actor_kind: str,
        actor_reference: str,
        now: datetime,
        terminal_reason: str | None = None,
        linked_remote_receipt_id: UUID | None = None,
    ) -> None:
        session.add(
            ProductionApprovalTransitionEventRow(
                id=str(uuid4()),
                approval_id=row.id,
                event_kind=event_kind,
                from_state=from_state,
                to_state=to_state,
                terminal_reason=terminal_reason,
                actor_kind=actor_kind,
                actor_reference=actor_reference,
                linked_remote_receipt_id=(
                    None
                    if linked_remote_receipt_id is None
                    else str(linked_remote_receipt_id)
                ),
                occurred_at=_text(now),
            )
        )

    @staticmethod
    def _required(
        row: ProductionOperationApprovalRow,
    ) -> ProductionApprovalRequiredResult:
        return ProductionApprovalRequiredResult(
            approval_id=UUID(row.id),
            request_id=UUID(row.request_id),
            target_id=UUID(row.target_id),
            action=BrowserRemoteAction(row.action),
            state="pending",
            expires_at=ProductionTransientApprovalService._required_time(
                row.pending_expires_at
            ),
        )

    @staticmethod
    def _terminal(
        row: ProductionOperationApprovalRow,
    ) -> ProductionApprovalTerminalResult:
        if row.state not in {"denied", "cancelled", "expired"}:
            raise ProductionApprovalError("approval_state_invalid")
        return ProductionApprovalTerminalResult(
            approval_id=UUID(row.id),
            request_id=UUID(row.request_id),
            target_id=UUID(row.target_id),
            action=BrowserRemoteAction(row.action),
            state=row.state,
            terminal_reason=row.terminal_reason or "authority_changed",
        )

    @staticmethod
    def _read(
        session: Session, row: ProductionOperationApprovalRow
    ) -> ProductionApprovalRead:
        target = session.get(TargetRow, row.target_id)
        client = session.get(McpClientRow, row.client_id)
        policy = session.get(ExternalProductionPolicyRow, row.target_id)
        mutation = session.get(ProductionMutationControlRow, 1)
        if target is None or client is None or policy is None or mutation is None:
            raise ProductionApprovalError("approval_history_incoherent")
        expires = (
            row.approved_dispatch_expires_at
            if row.state in {"approved", "consumed"}
            and row.approved_dispatch_expires_at is not None
            else row.pending_expires_at
        )
        return ProductionApprovalRead(
            approval_id=UUID(row.id),
            target_id=UUID(row.target_id),
            target_name=target.name,
            client_id=UUID(row.client_id),
            client_name=client.display_name,
            request_id=UUID(row.request_id),
            action=BrowserRemoteAction(row.action),
            state=row.state,
            terminal_reason=row.terminal_reason,
            created_at=ProductionTransientApprovalService._required_time(
                row.created_at
            ),
            expires_at=ProductionTransientApprovalService._required_time(expires),
            trust_summary=(
                f"native {policy.identity_generation}; bridge "
                f"{row.bridge_trust_revision}; policy {policy.policy_revision}"
            ),
            global_mutations_blocked=mutation.production_mutations_blocked,
            target_mutations_suspended=policy.mutations_suspended,
        )

    @staticmethod
    def _required_time(value: str | None) -> datetime:
        parsed = _time(value)
        if parsed is None:
            raise ProductionApprovalError("approval_history_incoherent")
        return parsed

    @staticmethod
    def _administrator(value: str) -> str:
        result = value.strip()
        if not result or len(result) > 64:
            raise ProductionApprovalError("permission_denied")
        return result
