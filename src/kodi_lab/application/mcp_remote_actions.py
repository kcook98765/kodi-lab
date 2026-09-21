"""Durable replay barrier around the existing semantic remote service."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.browser_remote import (
    BrowserRemoteError,
    BrowserRemoteResult,
    RemoteActorContext,
    ScreenshotRefreshResult,
)
from kodi_lab.kodi.remote import BrowserRemoteAction
from kodi_lab.storage.models import McpRemoteActionReceiptRow

REMOTE_RECEIPT_RETENTION = timedelta(days=30)
REMOTE_RECEIPT_MAX_PER_CLIENT = 10_000
REMOTE_RECEIPT_MAX_GLOBAL = 100_000


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

    def _claim(
        self,
        *,
        client_id: UUID,
        credential_id: UUID,
        target_id: UUID,
        request_id: UUID,
        action: BrowserRemoteAction,
    ) -> _Claim:
        now = self._clock()
        cutoff = _to_text(now - self._retention)
        invocation_id = uuid4()
        try:
            with self._sessions.begin() as session:
                session.execute(
                    delete(McpRemoteActionReceiptRow).where(
                        McpRemoteActionReceiptRow.created_at < cutoff
                    )
                )
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
        if row.final_result != "succeeded":
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
            row.final_result = "succeeded"
            row.completed_at = _to_text(completed_at)
            row.screenshot_refresh_result = refresh.result
            row.screenshot_capture_id = (
                None if refresh.capture_id is None else str(refresh.capture_id)
            )
            row.screenshot_captured_at = (
                None if refresh.captured_at is None else _to_text(refresh.captured_at)
            )
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
