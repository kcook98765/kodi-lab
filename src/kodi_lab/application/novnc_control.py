"""Human Web-only durable worker and input ownership.

No MCP/VNC facility is exported. Existing remote dispatchers and mutation leases
participate through database exclusion triggers. Expiry revokes permission but
never releases a claimed ownership fence until the broker proves worker death.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import delete, select, text, update
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.broker.novnc import ViewBinding
from kodi_lab.storage.models import (
    ManagedInstanceMutationLeaseRow,
    ManagedRemoteGuardRow,
    NovncSessionRow,
)

MAX_SESSION_SECONDS = 600


class HumanControlBusyError(ValueError):
    """Early refusal leaves the existing view-only attachment untouched."""


def timestamp(value: datetime | None = None) -> str:
    return (value or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def binding_json(binding: ViewBinding) -> str:
    return json.dumps(
        [binding.container, binding.image, binding.started, binding.identity],
        separators=(",", ":"),
    )


@dataclass(frozen=True)
class ControllerStatus:
    attempt: UUID
    mode: str
    state: str
    deadline: str


class HumanViewerOwnership:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    def require_control_available(self, instance: UUID) -> None:
        # Advisory early refusal preserves view-only on a busy target. Existing
        # database triggers remain the atomic arbiter against a concurrent claim.
        with self.sessions() as db:
            from kodi_lab.application.novnc import require_resolved_view_work

            require_resolved_view_work(db, instance, self.sessions)
            if (
                db.get(ManagedInstanceMutationLeaseRow, str(instance)) is not None
                or db.get(ManagedRemoteGuardRow, str(instance)) is not None
            ):
                raise HumanControlBusyError("agent operation active")

    def create(
        self,
        instance: UUID,
        attempt: UUID,
        owner: str,
        binding: ViewBinding,
        mode: str,
        deadline: str | None = None,
    ) -> str:
        now = datetime.now(UTC)
        absolute = deadline or timestamp(now + timedelta(seconds=MAX_SESSION_SECONDS))
        if absolute <= timestamp(now) or mode not in ("view-only", "interactive"):
            raise ValueError("viewer lifetime expired")
        with self.sessions.begin() as db:
            from kodi_lab.application.novnc import require_resolved_view_work

            db.execute(text("BEGIN IMMEDIATE"))
            require_resolved_view_work(db, instance, self.sessions)
            # Serialize admission against Stop's existing lifecycle claim. Even
            # view-only authority resolved before that claim must not create a
            # successor after the pre-dispatch worker-release proof.
            lease = db.get(ManagedInstanceMutationLeaseRow, str(instance))
            if lease is not None and lease.operation_kind.value == "lifecycle":
                raise ValueError("view lifecycle operation conflict")
            db.add(
                NovncSessionRow(
                    instance_id=str(instance),
                    attempt=str(attempt),
                    owner=owner,
                    mode=mode,
                    state="pending",
                    identity_json=binding_json(binding),
                    expires_at=min(timestamp(now + timedelta(seconds=10)), absolute),
                    absolute_expires_at=absolute,
                )
            )
        return absolute

    def claim(
        self,
        instance: UUID,
        attempt: UUID,
        owner: str,
        binding: ViewBinding,
        mode: str,
        broker: str,
    ) -> None:
        with self.sessions.begin() as db:
            result = db.execute(
                update(NovncSessionRow)
                .where(
                    NovncSessionRow.instance_id == str(instance),
                    NovncSessionRow.attempt == str(attempt),
                    NovncSessionRow.owner == owner,
                    NovncSessionRow.mode == mode,
                    NovncSessionRow.identity_json == binding_json(binding),
                    NovncSessionRow.state == "pending",
                    NovncSessionRow.broker_id.is_(None),
                    NovncSessionRow.expires_at > timestamp(),
                )
                .values(state="active", broker_id=broker)
            )
            if int(getattr(result, "rowcount", 0)) != 1:
                raise ValueError("human viewer ownership unavailable")

    def started(self, instance: UUID, attempt: UUID, broker: str, exec_id: str) -> None:
        with self.sessions.begin() as db:
            result = db.execute(
                update(NovncSessionRow)
                .where(
                    NovncSessionRow.instance_id == str(instance),
                    NovncSessionRow.attempt == str(attempt),
                    NovncSessionRow.broker_id == broker,
                    NovncSessionRow.state == "active",
                )
                .values(exec_id=exec_id)
            )
            if int(getattr(result, "rowcount", 0)) != 1:
                raise ValueError("worker ownership revoked")

    def valid(self, instance: UUID, attempt: UUID, broker: str) -> bool:
        with self.sessions() as db:
            row = db.get(NovncSessionRow, str(instance))
            return bool(
                row
                and row.attempt == str(attempt)
                and row.broker_id == broker
                and row.state == "active"
                and timestamp() < row.expires_at <= row.absolute_expires_at
            )

    def renew(self, instance: UUID, attempt: UUID, owner: str) -> bool:
        now = datetime.now(UTC)
        with self.sessions.begin() as db:
            row = db.get(NovncSessionRow, str(instance))
            if (
                not row
                or row.attempt != str(attempt)
                or row.owner != owner
                or row.state != "active"
                or row.expires_at <= timestamp(now)
                or row.absolute_expires_at <= timestamp(now)
            ):
                return False
            row.expires_at = min(
                timestamp(now + timedelta(seconds=2)), row.absolute_expires_at
            )
            return True

    def revoke(
        self, instance: UUID, attempt: UUID | None = None, owner: str | None = None
    ) -> None:
        with self.sessions.begin() as db:
            criteria = [NovncSessionRow.instance_id == str(instance)]
            if attempt is not None:
                criteria.append(NovncSessionRow.attempt == str(attempt))
            if owner is not None:
                criteria.append(NovncSessionRow.owner == owner)
            # Conditional SQL, never an ORM delete based on a stale read.
            db.execute(
                delete(NovncSessionRow).where(
                    *criteria, NovncSessionRow.broker_id.is_(None)
                )
            )
            db.execute(
                update(NovncSessionRow)
                .where(*criteria, NovncSessionRow.broker_id.is_not(None))
                .values(state="revoking")
            )

    def expire_pending(self) -> None:
        with self.sessions.begin() as db:
            db.execute(
                delete(NovncSessionRow).where(
                    NovncSessionRow.broker_id.is_(None),
                    NovncSessionRow.expires_at <= timestamp(),
                )
            )

    def finish(self, instance: UUID, attempt: UUID, broker: str, proven: bool) -> None:
        with self.sessions.begin() as db:
            criteria = (
                NovncSessionRow.instance_id == str(instance),
                NovncSessionRow.attempt == str(attempt),
                NovncSessionRow.broker_id == broker,
            )
            if proven:
                db.execute(delete(NovncSessionRow).where(*criteria))
            else:
                db.execute(
                    update(NovncSessionRow).where(*criteria).values(state="blocked")
                )

    def retire_stopped(self, instance: UUID, container: str) -> None:
        # Only an exact broker stopped-container receipt calls this. The native
        # display/input incarnation is dead; no viewer service is required.
        with self.sessions.begin() as db:
            row = db.get(NovncSessionRow, str(instance))
            if row is not None and json.loads(row.identity_json)[0] == container:
                db.execute(
                    delete(NovncSessionRow).where(
                        NovncSessionRow.instance_id == str(instance),
                        NovncSessionRow.attempt == row.attempt,
                        NovncSessionRow.identity_json == row.identity_json,
                    )
                )

    def status(self, instance: UUID) -> ControllerStatus | None:
        with self.sessions() as db:
            row = db.get(NovncSessionRow, str(instance))
            return (
                None
                if row is None
                else ControllerStatus(
                    UUID(row.attempt), row.mode, row.state, row.absolute_expires_at
                )
            )

    def wait_released(self, instance: UUID, timeout: float = 7) -> None:
        deadline = time.monotonic() + timeout
        while self.status(instance) is not None:
            if time.monotonic() >= deadline:
                raise ValueError("worker termination remains unproven")
            time.sleep(0.05)

    def recover(self, broker: str, proof: object) -> None:
        """Called once before display admission; proof must fence the old native.

        The callable checks the exact stored container, never the current target's
        replacement. Unproven rows remain blocked, including after lease expiry.
        """
        from collections.abc import Callable
        from typing import cast

        check = cast(Callable[[str, str | None, bool], bool], proof)
        with self.sessions() as db:
            rows = list(db.scalars(select(NovncSessionRow)))
        for row in rows:
            if row.broker_id is None:
                if row.expires_at <= timestamp():
                    self.revoke(UUID(row.instance_id), UUID(row.attempt))
                continue
            if row.broker_id == broker:
                continue
            binding = json.loads(row.identity_json)
            # Old helper expiry is independent of this broker. Do not renew it.
            deadline = time.monotonic() + 6
            proven = check(binding[0], row.exec_id, row.mode == "interactive")
            while not proven and time.monotonic() < deadline:
                time.sleep(0.1)
                proven = check(binding[0], row.exec_id, row.mode == "interactive")
            self.finish(UUID(row.instance_id), UUID(row.attempt), row.broker_id, proven)
