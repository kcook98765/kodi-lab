"""Durable replay barrier around the existing semantic remote service."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.browser_remote import (
    BrowserRemoteError,
    BrowserRemoteResult,
    RemoteActorContext,
    ScreenshotRefreshResult,
)
from kodi_lab.application.production_inspection import ProductionInspectionError
from kodi_lab.application.production_navigation import (
    ProductionNavigationAuthority,
    ProductionNavigationRateLimiter,
)
from kodi_lab.kodi.remote import BrowserRemoteAction
from kodi_lab.storage.models import (
    McpProductionNavigationAuditRow,
    McpProductionRemoteReceiptBindingRow,
    McpRemoteActionReceiptRow,
    ProductionTransientDispatchAuditRow,
)

REMOTE_RECEIPT_RETENTION = timedelta(days=30)
REMOTE_RECEIPT_MAX_PER_CLIENT = 10_000
REMOTE_RECEIPT_MAX_GLOBAL = 100_000
REMOTE_RECEIPT_CLEANUP_BATCH = 500


def _to_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone aware")
    return (
        value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    )


def _from_text(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class McpRemoteActionReceiptError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class McpRemoteActionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = 1
    request_id: UUID
    invocation_id: UUID
    target_id: UUID
    action: BrowserRemoteAction
    outcome: str = Field(max_length=64)
    created_at: datetime
    completed_at: datetime
    screenshot_refresh: ScreenshotRefreshResult


@dataclass(frozen=True, slots=True)
class _Claim:
    row: McpRemoteActionReceiptRow
    created: bool


class _RemoteService(Protocol):
    def invoke(
        self,
        target_id: UUID,
        action: BrowserRemoteAction,
        *,
        actor: RemoteActorContext | None = None,
        invocation_id: UUID | None = None,
        before_dispatch: Callable[[], None] | None = None,
    ) -> BrowserRemoteResult: ...


class _RateLimiter(Protocol):
    def require_allowed(self, actor_key: str, target_id: UUID) -> None: ...


class McpRemoteActionReceiptService:
    """Commit a replay barrier before one invocation of BrowserRemoteService."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        remote: _RemoteService,
        rate_limiter: _RateLimiter,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        retention: timedelta = REMOTE_RECEIPT_RETENTION,
        max_per_client: int = REMOTE_RECEIPT_MAX_PER_CLIENT,
        max_global: int = REMOTE_RECEIPT_MAX_GLOBAL,
        production_rate_limiter: ProductionNavigationRateLimiter | None = None,
    ) -> None:
        if retention <= timedelta(0) or max_per_client < 1 or max_global < 1:
            raise ValueError("remote receipt bounds must be positive")
        self._sessions = session_factory
        self._remote = remote
        self._rate_limiter = rate_limiter
        self._clock = clock
        self._retention = retention
        self._max_per_client = max_per_client
        self._max_global = max_global
        self._production_rate_limiter = (
            production_rate_limiter or ProductionNavigationRateLimiter()
        )

    def invoke(
        self,
        *,
        client_id: UUID,
        credential_id: UUID,
        target_id: UUID,
        request_id: UUID,
        action: BrowserRemoteAction,
        before_dispatch: Callable[[], None] | None = None,
    ) -> McpRemoteActionResult:
        claim = self._claim(
            client_id=client_id,
            credential_id=credential_id,
            target_id=target_id,
            request_id=request_id,
            action=action,
        )
        if not claim.created:
            return self._replay(claim.row, target_id=target_id, action=action)

        actor = RemoteActorContext.mcp_client(client_id)
        try:
            self._rate_limiter.require_allowed(actor.rate_key, target_id)
        except BrowserRemoteError as error:
            code = "rate_limited" if error.code == "visual_rate_limited" else error.code
            self._finalize_error(claim.row.id, code, state="completed")
            raise McpRemoteActionReceiptError(code) from error

        try:
            result = self._remote.invoke(
                target_id,
                action,
                actor=actor,
                invocation_id=UUID(claim.row.id),
                before_dispatch=before_dispatch,
            )
        except BrowserRemoteError as error:
            state = (
                "ambiguous" if error.code == "remote_action_ambiguous" else "completed"
            )
            try:
                self._finalize_error(claim.row.id, error.code, state=state)
            except (SQLAlchemyError, RuntimeError):
                raise McpRemoteActionReceiptError("remote_action_ambiguous") from error
            raise McpRemoteActionReceiptError(error.code) from error
        except Exception as error:
            # The durable pending row is intentionally left unresolved. It is the
            # replay barrier when dispatch completion cannot be classified safely.
            raise McpRemoteActionReceiptError("remote_action_ambiguous") from error

        try:
            row = self._finalize_success(claim.row.id, result)
        except (SQLAlchemyError, RuntimeError) as error:
            raise McpRemoteActionReceiptError("remote_action_ambiguous") from error
        return self._result(row)

    def invoke_production(
        self,
        *,
        client_id: UUID,
        credential_id: UUID,
        target_id: UUID,
        request_id: UUID,
        action: BrowserRemoteAction,
        authorize_new: Callable[[], ProductionNavigationAuthority],
    ) -> McpRemoteActionResult:
        """Replay terminal history first; authorize and bind only genuinely new work."""

        existing = self._existing(client_id, request_id)
        cutoff = _to_text(self._clock() - self._retention)
        if existing is not None and (
            existing.state != "completed" or existing.created_at >= cutoff
        ):
            return self._replay(existing, target_id=target_id, action=action)
        authority = authorize_new()
        claim = self._claim(
            client_id=client_id,
            credential_id=credential_id,
            target_id=target_id,
            request_id=request_id,
            action=action,
            production_authority=authority,
        )
        if not claim.created:
            return self._replay(claim.row, target_id=target_id, action=action)
        try:
            self._production_rate_limiter.require_allowed(client_id, target_id)
            self._rate_limiter.require_allowed(f"mcp:{client_id}", target_id)
        except (ProductionInspectionError, BrowserRemoteError) as error:
            code = "rate_limited"
            self._finalize_error(claim.row.id, code, state="completed")
            raise McpRemoteActionReceiptError(code) from error

        def reauthorize() -> None:
            current = authorize_new()
            if current != authority:
                raise McpRemoteActionReceiptError("production_authority_changed")

        actor = RemoteActorContext.mcp_client(client_id)
        try:
            result = self._remote.invoke(
                target_id,
                action,
                actor=actor,
                invocation_id=UUID(claim.row.id),
                before_dispatch=reauthorize,
            )
        except BrowserRemoteError as error:
            state = (
                "ambiguous" if error.code == "remote_action_ambiguous" else "completed"
            )
            try:
                self._finalize_error(claim.row.id, error.code, state=state)
            except (SQLAlchemyError, RuntimeError):
                raise McpRemoteActionReceiptError("remote_action_ambiguous") from error
            raise McpRemoteActionReceiptError(error.code) from error
        except McpRemoteActionReceiptError as error:
            self._finalize_error(claim.row.id, error.code, state="completed")
            raise
        except Exception as error:
            raise McpRemoteActionReceiptError("remote_action_ambiguous") from error

        try:
            try:
                reauthorize()
            except Exception:
                result = result.model_copy(
                    update={"result": "authority_changed_after_dispatch"}
                )
            row = self._finalize_success(claim.row.id, result)
        except (SQLAlchemyError, RuntimeError) as error:
            raise McpRemoteActionReceiptError("remote_action_ambiguous") from error
        return self._result(row)

    def _existing(
        self, client_id: UUID, request_id: UUID
    ) -> McpRemoteActionReceiptRow | None:
        with self._sessions() as session:
            row = session.scalar(
                select(McpRemoteActionReceiptRow).where(
                    McpRemoteActionReceiptRow.client_id == str(client_id),
                    McpRemoteActionReceiptRow.request_id == str(request_id),
                )
            )
            if row is not None:
                session.expunge(row)
            return row

    def existing(
        self, client_id: UUID, request_id: UUID
    ) -> McpRemoteActionReceiptRow | None:
        """Read one durable replay identity for approval-first orchestration."""

        return self._existing(client_id, request_id)

    def replay(
        self,
        row: McpRemoteActionReceiptRow,
        *,
        target_id: UUID,
        action: BrowserRemoteAction,
    ) -> McpRemoteActionResult:
        return self._replay(row, target_id=target_id, action=action)

    def dispatch_claimed_production(
        self,
        *,
        receipt_id: UUID,
        client_id: UUID,
        target_id: UUID,
        action: BrowserRemoteAction,
        before_dispatch: Callable[[], None],
    ) -> McpRemoteActionResult:
        """Dispatch one already committed production receipt exactly once."""

        try:
            self._rate_limiter.require_allowed(f"mcp:{client_id}", target_id)
        except BrowserRemoteError as error:
            code = "rate_limited" if error.code == "visual_rate_limited" else error.code
            self._finalize_error(str(receipt_id), code, state="completed")
            raise McpRemoteActionReceiptError(code) from error
        actor = RemoteActorContext.mcp_client(client_id)
        try:
            result = self._remote.invoke(
                target_id,
                action,
                actor=actor,
                invocation_id=receipt_id,
                before_dispatch=before_dispatch,
            )
        except BrowserRemoteError as error:
            state = (
                "ambiguous" if error.code == "remote_action_ambiguous" else "completed"
            )
            try:
                self._finalize_error(str(receipt_id), error.code, state=state)
            except (SQLAlchemyError, RuntimeError):
                raise McpRemoteActionReceiptError("remote_action_ambiguous") from error
            raise McpRemoteActionReceiptError(error.code) from error
        except Exception as error:
            raise McpRemoteActionReceiptError("remote_action_ambiguous") from error
        try:
            row = self._finalize_success(str(receipt_id), result)
        except (SQLAlchemyError, RuntimeError) as error:
            raise McpRemoteActionReceiptError("remote_action_ambiguous") from error
        return self._result(row)

    def _claim(
        self,
        *,
        client_id: UUID,
        credential_id: UUID,
        target_id: UUID,
        request_id: UUID,
        action: BrowserRemoteAction,
        production_authority: ProductionNavigationAuthority | None = None,
    ) -> _Claim:
        now = self._clock()
        cutoff = _to_text(now - self._retention)
        invocation_id = uuid4()
        try:
            with self._sessions.begin() as session:
                self._cleanup_expired(session, cutoff)
                existing = session.scalar(
                    select(McpRemoteActionReceiptRow).where(
                        McpRemoteActionReceiptRow.client_id == str(client_id),
                        McpRemoteActionReceiptRow.request_id == str(request_id),
                    )
                )
                if existing is not None:
                    session.expunge(existing)
                    return _Claim(existing, False)
                client_count = session.scalar(
                    select(func.count())
                    .select_from(McpRemoteActionReceiptRow)
                    .where(McpRemoteActionReceiptRow.client_id == str(client_id))
                )
                global_count = session.scalar(
                    select(func.count()).select_from(McpRemoteActionReceiptRow)
                )
                if (client_count or 0) >= self._max_per_client or (
                    global_count or 0
                ) >= self._max_global:
                    raise McpRemoteActionReceiptError("remote_action_receipt_capacity")
                row = McpRemoteActionReceiptRow(
                    id=str(invocation_id),
                    client_id=str(client_id),
                    credential_id=str(credential_id),
                    target_id=str(target_id),
                    request_id=str(request_id),
                    action=action.value,
                    created_at=_to_text(now),
                    state="pending",
                    final_result=None,
                    completed_at=None,
                    screenshot_refresh_result=None,
                    screenshot_capture_id=None,
                    screenshot_captured_at=None,
                )
                session.add(row)
                session.flush()
                if production_authority is not None:
                    session.add(
                        McpProductionRemoteReceiptBindingRow(
                            receipt_id=row.id,
                            operation_class="navigation",
                            native_identity_generation=(
                                production_authority.native_identity_generation
                            ),
                            bridge_trust_revision=(
                                production_authority.bridge_trust_revision
                            ),
                            target_policy_revision=(
                                production_authority.target_policy_revision
                            ),
                            mutation_control_epoch=(
                                production_authority.mutation_control_epoch
                            ),
                            inspect_scope_id=str(production_authority.inspect_scope_id),
                            navigate_scope_id=str(
                                production_authority.navigate_scope_id
                            ),
                            transient_scope_id=None,
                            approval_id=None,
                            client_preset_id=str(production_authority.client_preset_id),
                            target_preset_id=str(production_authority.target_preset_id),
                            generic_grant_created_at=(
                                production_authority.generic_grant_created_at
                            ),
                            generic_grant_id=str(production_authority.generic_grant_id),
                            native_http_acknowledgement_id=(
                                None
                                if production_authority.native_http_acknowledgement_id
                                is None
                                else str(
                                    production_authority.native_http_acknowledgement_id
                                )
                            ),
                        )
                    )
                    session.add(
                        McpProductionNavigationAuditRow(
                            receipt_id=row.id,
                            client_id=str(client_id),
                            credential_id=str(credential_id),
                            target_id=str(target_id),
                            request_id=str(request_id),
                            action=action.value,
                            operation_class="navigation",
                            native_identity_generation=(
                                production_authority.native_identity_generation
                            ),
                            bridge_trust_revision=(
                                production_authority.bridge_trust_revision
                            ),
                            target_policy_revision=(
                                production_authority.target_policy_revision
                            ),
                            mutation_control_epoch=(
                                production_authority.mutation_control_epoch
                            ),
                            inspect_scope_id=str(production_authority.inspect_scope_id),
                            navigate_scope_id=str(
                                production_authority.navigate_scope_id
                            ),
                            state="pending",
                            outcome="pending",
                            dispatch_classification="not_dispatched",
                            requested_at=_to_text(now),
                            completed_at=None,
                        )
                    )
                    session.flush()
                session.expunge(row)
                return _Claim(row, True)
        except IntegrityError:
            with self._sessions() as session:
                existing = session.scalar(
                    select(McpRemoteActionReceiptRow).where(
                        McpRemoteActionReceiptRow.client_id == str(client_id),
                        McpRemoteActionReceiptRow.request_id == str(request_id),
                    )
                )
                if existing is None:
                    raise McpRemoteActionReceiptError(
                        "remote_action_ambiguous"
                    ) from None
                session.expunge(existing)
                return _Claim(existing, False)

    @staticmethod
    def _cleanup_expired(session: Session, cutoff: str) -> None:
        """Delete only coherent completed replay evidence after its retention."""

        production_ids = tuple(
            session.scalars(
                select(McpRemoteActionReceiptRow.id)
                .join(
                    McpProductionRemoteReceiptBindingRow,
                    McpProductionRemoteReceiptBindingRow.receipt_id
                    == McpRemoteActionReceiptRow.id,
                )
                .join(
                    McpProductionNavigationAuditRow,
                    McpProductionNavigationAuditRow.receipt_id
                    == McpRemoteActionReceiptRow.id,
                )
                .where(
                    McpRemoteActionReceiptRow.created_at < cutoff,
                    McpRemoteActionReceiptRow.state == "completed",
                    McpRemoteActionReceiptRow.final_result.is_not(None),
                    McpRemoteActionReceiptRow.completed_at.is_not(None),
                    McpRemoteActionReceiptRow.screenshot_refresh_result.is_not(None),
                    McpProductionNavigationAuditRow.client_id
                    == McpRemoteActionReceiptRow.client_id,
                    McpProductionNavigationAuditRow.credential_id
                    == McpRemoteActionReceiptRow.credential_id,
                    McpProductionNavigationAuditRow.target_id
                    == McpRemoteActionReceiptRow.target_id,
                    McpProductionNavigationAuditRow.request_id
                    == McpRemoteActionReceiptRow.request_id,
                    McpProductionNavigationAuditRow.action
                    == McpRemoteActionReceiptRow.action,
                    McpProductionNavigationAuditRow.operation_class
                    == McpProductionRemoteReceiptBindingRow.operation_class,
                    McpProductionNavigationAuditRow.native_identity_generation
                    == McpProductionRemoteReceiptBindingRow.native_identity_generation,
                    McpProductionNavigationAuditRow.bridge_trust_revision
                    == McpProductionRemoteReceiptBindingRow.bridge_trust_revision,
                    McpProductionNavigationAuditRow.target_policy_revision
                    == McpProductionRemoteReceiptBindingRow.target_policy_revision,
                    McpProductionNavigationAuditRow.mutation_control_epoch
                    == McpProductionRemoteReceiptBindingRow.mutation_control_epoch,
                    McpProductionNavigationAuditRow.inspect_scope_id
                    == McpProductionRemoteReceiptBindingRow.inspect_scope_id,
                    McpProductionNavigationAuditRow.navigate_scope_id
                    == McpProductionRemoteReceiptBindingRow.navigate_scope_id,
                    McpProductionNavigationAuditRow.state == "completed",
                    McpProductionNavigationAuditRow.outcome
                    == McpRemoteActionReceiptRow.final_result,
                    McpProductionNavigationAuditRow.requested_at
                    == McpRemoteActionReceiptRow.created_at,
                    McpProductionNavigationAuditRow.completed_at
                    == McpRemoteActionReceiptRow.completed_at,
                    or_(
                        and_(
                            McpRemoteActionReceiptRow.final_result.in_(
                                (
                                    "succeeded",
                                    "authority_changed_after_dispatch",
                                    "remote_kodi_rejected",
                                )
                            ),
                            McpProductionNavigationAuditRow.dispatch_classification
                            == "acknowledged",
                        ),
                        and_(
                            ~McpRemoteActionReceiptRow.final_result.in_(
                                (
                                    "succeeded",
                                    "authority_changed_after_dispatch",
                                    "remote_kodi_rejected",
                                )
                            ),
                            McpProductionNavigationAuditRow.dispatch_classification
                            == "not_dispatched",
                        ),
                    ),
                )
                .order_by(
                    McpRemoteActionReceiptRow.created_at,
                    McpRemoteActionReceiptRow.id,
                )
                .limit(REMOTE_RECEIPT_CLEANUP_BATCH)
            ).all()
        )
        if production_ids:
            # The migration triggers require this order. The surrounding claim
            # transaction makes removal of binding, audit and replay key atomic.
            session.execute(
                delete(McpProductionRemoteReceiptBindingRow).where(
                    McpProductionRemoteReceiptBindingRow.receipt_id.in_(production_ids)
                )
            )
            session.execute(
                delete(McpProductionNavigationAuditRow).where(
                    McpProductionNavigationAuditRow.receipt_id.in_(production_ids)
                )
            )
            session.execute(
                delete(McpRemoteActionReceiptRow).where(
                    McpRemoteActionReceiptRow.id.in_(production_ids)
                )
            )

        session.execute(
            delete(McpRemoteActionReceiptRow).where(
                McpRemoteActionReceiptRow.created_at < cutoff,
                McpRemoteActionReceiptRow.state == "completed",
                ~select(McpProductionRemoteReceiptBindingRow.receipt_id)
                .where(
                    McpProductionRemoteReceiptBindingRow.receipt_id
                    == McpRemoteActionReceiptRow.id
                )
                .exists(),
                ~select(McpProductionNavigationAuditRow.receipt_id)
                .where(
                    McpProductionNavigationAuditRow.receipt_id
                    == McpRemoteActionReceiptRow.id
                )
                .exists(),
            )
        )

    def _replay(
        self,
        row: McpRemoteActionReceiptRow,
        *,
        target_id: UUID,
        action: BrowserRemoteAction,
    ) -> McpRemoteActionResult:
        if row.target_id != str(target_id) or row.action != action.value:
            raise McpRemoteActionReceiptError("request_id_conflict")
        if row.state != "completed":
            raise McpRemoteActionReceiptError("remote_action_ambiguous")
        if row.final_result not in {"succeeded", "authority_changed_after_dispatch"}:
            raise McpRemoteActionReceiptError(
                row.final_result or "remote_action_ambiguous"
            )
        return self._result(row)

    def _finalize_success(
        self, receipt_id: str, result: BrowserRemoteResult
    ) -> McpRemoteActionReceiptRow:
        completed_at = result.completed_at
        refresh = result.screenshot_refresh
        with self._sessions.begin() as session:
            row = session.get(McpRemoteActionReceiptRow, receipt_id)
            if row is None or row.state != "pending":
                raise RuntimeError("remote action receipt lost pending ownership")
            row.state = "completed"
            row.final_result = result.result
            row.completed_at = _to_text(completed_at)
            row.screenshot_refresh_result = refresh.result
            row.screenshot_capture_id = (
                None if refresh.capture_id is None else str(refresh.capture_id)
            )
            row.screenshot_captured_at = (
                None if refresh.captured_at is None else _to_text(refresh.captured_at)
            )
            audit = session.get(McpProductionNavigationAuditRow, receipt_id)
            if audit is not None:
                audit.state = "completed"
                audit.outcome = result.result
                audit.dispatch_classification = "acknowledged"
                audit.completed_at = _to_text(completed_at)
            transient_audit = session.get(
                ProductionTransientDispatchAuditRow, receipt_id
            )
            if transient_audit is not None:
                transient_audit.state = "completed"
                transient_audit.outcome = result.result
                transient_audit.dispatch_classification = "acknowledged"
                transient_audit.completed_at = _to_text(completed_at)
            session.flush()
            session.expunge(row)
            return row

    def _finalize_error(self, receipt_id: str, code: str, *, state: str) -> None:
        now = self._clock()
        with self._sessions.begin() as session:
            row = session.get(McpRemoteActionReceiptRow, receipt_id)
            if row is None or row.state != "pending":
                raise RuntimeError("remote action receipt lost pending ownership")
            row.state = state
            row.final_result = code
            row.completed_at = _to_text(now)
            row.screenshot_refresh_result = "not_requested"
            audit = session.get(McpProductionNavigationAuditRow, receipt_id)
            if audit is not None:
                audit.state = state
                audit.outcome = code
                audit.dispatch_classification = (
                    "ambiguous"
                    if state == "ambiguous"
                    else "acknowledged"
                    if code == "remote_kodi_rejected"
                    else "not_dispatched"
                )
                audit.completed_at = _to_text(now)
            transient_audit = session.get(
                ProductionTransientDispatchAuditRow, receipt_id
            )
            if transient_audit is not None:
                transient_audit.state = state
                transient_audit.outcome = code
                transient_audit.dispatch_classification = (
                    "ambiguous"
                    if state == "ambiguous"
                    else "acknowledged"
                    if code == "remote_kodi_rejected"
                    else "not_dispatched"
                )
                transient_audit.completed_at = _to_text(now)

    @staticmethod
    def _result(row: McpRemoteActionReceiptRow) -> McpRemoteActionResult:
        if (
            row.final_result is None
            or row.completed_at is None
            or row.screenshot_refresh_result is None
        ):
            raise McpRemoteActionReceiptError("remote_action_ambiguous")
        return McpRemoteActionResult(
            request_id=UUID(row.request_id),
            invocation_id=UUID(row.id),
            target_id=UUID(row.target_id),
            action=BrowserRemoteAction(row.action),
            outcome=row.final_result,
            created_at=_from_text(row.created_at),
            completed_at=_from_text(row.completed_at),
            screenshot_refresh=ScreenshotRefreshResult(
                result=row.screenshot_refresh_result,
                capture_id=(
                    None
                    if row.screenshot_capture_id is None
                    else UUID(row.screenshot_capture_id)
                ),
                captured_at=(
                    None
                    if row.screenshot_captured_at is None
                    else _from_text(row.screenshot_captured_at)
                ),
            ),
        )
