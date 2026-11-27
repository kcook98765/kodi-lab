"""Administrator review and exact, replay-safe retirement of private feed copies."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import subprocess
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from kodi_lab.storage.inventory_snapshot import observe_filesystem, read_file

from .feed_retirement import (
    PENDING_LIMIT,
    FeedCopy,
    FeedRetirement,
    FeedState,
    PairIdentity,
    PendingCopy,
    pair_matches,
    runtime_idle,
)
from .packages import HEX
from .protocol import Document, Refusal
from .runtime import Runtime, atomic, sync_dir
from .snapshots import SnapshotQuotas, snapshot_capacity

if TYPE_CHECKING:
    from .controller import Controller

OPERATIONS = frozenset(
    {
        "storage_review",
        "storage_execute",
        "storage_policy",
        "storage_snapshot_quotas",
        "storage_set_snapshot_quotas",
    }
)
EXPIRES = 300
REVIEW_LIMIT = 4
HISTORY_LIMIT = 32
HISTORY_MAX_BYTES = 96 * 1024
TOKENS_MAX_BYTES = 2048
OBSERVATION_ERRORS = (
    OSError,
    ValueError,
    KeyError,
    TypeError,
    IndexError,
    subprocess.SubprocessError,
)
Result = Literal["removed", "skipped", "pending", "needs_review"]


class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    session: str
    nonce: str
    expires: int
    generation: str
    target: UUID
    engine: str
    publication: str
    fingerprint: str
    pair: PairIdentity
    artifact: FeedCopy


class Outcome(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    identity: str
    plan: Plan
    created: Annotated[int, Field(ge=0, le=253402300799)]
    state: Result = "pending"
    attempted: bool = False
    reason: str = "Cleanup was authorized"
    estimated_bytes: int | None = None
    available_delta: int | None = None


HISTORY: TypeAdapter[list[Outcome]] = TypeAdapter(
    Annotated[list[Outcome], Field(max_length=HISTORY_LIMIT)]
)


class StorageMaintenance:
    def __init__(self, controller: Controller) -> None:
        self.controller = controller
        self.history_file = controller.omega.root / "feed-maintenance-history.json"
        self.policy_file = controller.omega.root / "automatic-feed-cleanup.disabled"
        self.key = secrets.token_bytes(32)

    def automatic(self) -> bool:
        try:
            self.policy_file.lstat()
        except FileNotFoundError:
            return True
        return False

    def history(self) -> list[Outcome]:
        try:
            return HISTORY.validate_json(
                read_file(self.history_file, HISTORY_MAX_BYTES)
            )
        except FileNotFoundError:
            return []
        except (OSError, ValueError):
            raise Refusal("storage_maintenance_history_unavailable") from None

    def save(self, history: list[Outcome]) -> None:
        payload = HISTORY.dump_json(history)
        if len(history) > HISTORY_LIMIT or len(payload) > HISTORY_MAX_BYTES:
            raise Refusal("storage_maintenance_history_capacity")
        atomic(self.history_file, payload)

    def token(self, plan: BaseModel) -> str:
        body = base64.urlsafe_b64encode(plan.model_dump_json().encode()).decode()
        signature = hmac.digest(self.key, body.encode(), "sha256").hex()
        return body + "." + signature

    def verified_payload(self, raw: str) -> bytes:
        try:
            body, separator, signature = raw.partition(".")
            if not separator or not hmac.compare_digest(
                signature, hmac.digest(self.key, body.encode(), "sha256").hex()
            ):
                raise ValueError
            return base64.b64decode(body, altchars=b"-_", validate=True)
        except (ValueError, UnicodeError):
            raise Refusal("storage_maintenance_preview_expired") from None

    def decoded(self, raw: str, session: str) -> Plan:
        try:
            plan = Plan.model_validate_json(self.verified_payload(raw))
            if plan.session != session or time.time() >= plan.expires:
                raise ValueError
            return plan
        except (ValueError, UnicodeError):
            raise Refusal("storage_maintenance_preview_expired") from None

    def target(self, generation: str) -> tuple[Runtime, FeedRetirement]:
        if generation not in self.controller.runtimes:
            raise Refusal("managed_generation_unavailable")
        runtime = self.controller.runtimes[generation]
        return runtime, FeedRetirement(
            self.controller.omega.root, runtime.root, runtime.target
        )

    def operation_pending(self, runtime: Runtime) -> bool:
        operation = self.controller.last_operation()
        return runtime.public.session.exists() or bool(
            operation and operation["state"] == "pending"
        )

    @staticmethod
    def summarize(outcome: Outcome) -> Document:
        outcome = expired_summary(outcome, int(time.time()))
        return {
            "identity": outcome.identity,
            "generation": outcome.plan.generation,
            "artifact": outcome.plan.artifact.model_dump(exclude={"source"}),
            "time": outcome.created,
            "timestamp": datetime.fromtimestamp(outcome.created, UTC).isoformat(
                timespec="seconds"
            ),
            "actor": "Administrator",
            "state": outcome.state,
            "reason": outcome.reason,
            "estimated_bytes": outcome.estimated_bytes,
            "available_delta": outcome.available_delta,
        }

    def review(self, generation: str, session: str, page: int = 1) -> Document:
        native, book = self.target(generation)
        result: Document = {
            "generation": generation,
            "page": page,
            "automatic": self.automatic(),
            "items": [],
            "history": [self.summarize(item) for item in reversed(self.history())],
            "reason": None,
        }
        try:
            state = book.read()
            result["count"] = len(state.pending)
            if not state.current or not state.pending:
                result["reason"] = "No tracked superseded feed copies were found."
                return result
            if page > 1:
                result["previous_page"] = page - 1
            if page * REVIEW_LIMIT < len(state.pending):
                result["next_page"] = page + 1
            self.offer_review(native, book, state, session, result)
        except (Refusal, *OBSERVATION_ERRORS):
            result["reason"] = (
                "Eligibility is unavailable. The feed and its receipts were preserved."
            )
        return result

    def offer_review(
        self,
        native: Runtime,
        book: FeedRetirement,
        state: FeedState,
        session: str,
        result: Document,
    ) -> None:
        assert state.current is not None
        exclusion = None
        if state.planned:
            exclusion = "A feed publication needs reconciliation."
        elif book.hold.exists() or book.hold.is_symlink():
            exclusion = "A recovery hold protects this feed."
        elif self.operation_pending(native):
            exclusion = "A native or public operation needs to finish."
        elif not state.engine or not runtime_idle(native, state.engine):
            exclusion = "Kodi is active or its Engine could not be verified."
        catalog = book.approved_catalog() if exclusion is None else set()
        offset = (result["page"] - 1) * REVIEW_LIMIT
        for pending in state.pending[offset : offset + REVIEW_LIMIT]:
            item = pending.artifact
            row: Document = {
                "artifact": item.model_dump(exclude={"source"}),
                "token": None,
                "reason": exclusion
                or "The retained archive or publication could not be verified.",
                "estimated_bytes": None,
            }
            if item.digest in catalog:
                row["reason"] = "The canonical dependency catalog protects this digest."
            elif exclusion is None:
                proof = book.prove(state, pending, durable=False)
                if proof is not None and state.engine:
                    plan = Plan(
                        session=session,
                        nonce=secrets.token_hex(16),
                        expires=int(time.time()) + EXPIRES,
                        generation=native.generation,
                        target=native.target,
                        engine=state.engine,
                        publication=state.current.index,
                        fingerprint=proof_digest(proof.identities),
                        pair=proof.pair,
                        artifact=item,
                    )
                    row.update(
                        token=self.token(plan),
                        expires=plan.expires,
                        reason=(
                            "The current index does not use this path. "
                            "The approved ZIP remains available."
                        ),
                        estimated_bytes=estimated_size(proof.target, proof.sidecar),
                    )
            result["items"].append(row)

    def policy(self, enabled: bool) -> Document:
        with self.controller.omega.exclusive():
            if enabled:
                self.policy_file.unlink(missing_ok=True)
                sync_dir(self.policy_file.parent)
            else:
                atomic(self.policy_file, b"automatic feed cleanup paused\n")
        return {"automatic": self.automatic()}

    def reserve(self, history: list[Outcome], outcome: Outcome) -> list[Outcome]:
        now = int(time.time())
        history[:] = [expired_summary(item, now) for item in history]
        if any(
            item.state == "pending"
            and item.plan.generation == outcome.plan.generation
            and item.plan.artifact == outcome.plan.artifact
            for item in history
        ):
            raise Refusal("storage_maintenance_already_pending")
        if len(history) == HISTORY_LIMIT:
            expired = next(
                (
                    index
                    for index, entry in enumerate(history)
                    if entry.state != "pending" and entry.plan.expires <= now
                ),
                None,
            )
            if expired is None:
                raise Refusal("storage_maintenance_history_capacity")
            history.pop(expired)
        history.append(outcome)
        self.save(history)
        return history

    def execute(self, raw: str, session: str, generation: str) -> Document:
        identity = hashlib.sha256(raw.encode()).hexdigest()
        with self.controller.omega.exclusive():
            history = self.history()
            previous = next(
                (entry for entry in history if entry.identity == identity), None
            )
            if previous is not None:
                if (
                    previous.plan.session != session
                    or previous.plan.generation != generation
                ):
                    raise Refusal("storage_maintenance_preview_expired")
                if previous.state != "pending" or previous.plan.expires <= time.time():
                    return self.summarize(previous)
                outcome = previous
            else:
                plan = self.decoded(raw, session)
                if plan.generation != generation:
                    raise Refusal("storage_maintenance_preview_expired")
                outcome = Outcome(
                    identity=identity, plan=plan, created=int(time.time())
                )
                self.reserve(history, outcome)
        return self.summarize(self.finish(outcome))

    def finish(self, outcome: Outcome) -> Outcome:
        plan = outcome.plan
        native, book = self.target(plan.generation)
        try:
            state = book.read()
            pending = only_exact_pending(state, plan)
            if pending is None or native.target != plan.target:
                return self.store(
                    outcome, "skipped", "The publication or candidate changed."
                )
            proof = book.prove(state, pending)
            if proof is None:
                return self.store(
                    outcome,
                    "needs_review",
                    "The archive, current publication or protection changed.",
                )
            if proof_digest(proof.identities) != plan.fingerprint and not (
                outcome.attempted
                and pending.phase == "removing"
                and pair_matches(plan.pair, proof.pair)
            ):
                return self.store(
                    outcome, "skipped", "The reviewed file identity changed."
                )
            with self.controller.omega.exclusive():
                history = self.history()
                index = next(
                    index
                    for index, entry in enumerate(history)
                    if entry.identity == outcome.identity
                )
                latest = history[index]
                if latest.state != "pending":
                    return latest
                if latest.plan.expires <= time.time():
                    expired = expired_summary(latest, int(time.time()))
                    return self.save_result(
                        history, latest, expired.state, expired.reason
                    )
                if self.operation_pending(native):
                    return latest.model_copy(
                        update={"reason": "A native or public operation is pending."}
                    )
                if not runtime_idle(native, plan.engine):
                    return latest.model_copy(
                        update={
                            "reason": "Kodi is active or the Engine was unavailable."
                        }
                    )
                # A failed authorization rename may have been visible. Require
                # its durability before any exact resource removal, including retries.
                estimate = (
                    latest.estimated_bytes
                    if latest.attempted
                    else estimated_size(proof.target, proof.sidecar)
                )
                if not latest.attempted:
                    latest = latest.model_copy(
                        update={"attempted": True, "estimated_bytes": estimate}
                    )
                    history[index] = latest
                    self.save(history)
                else:
                    sync_dir(self.history_file.parent)
                before = observe_filesystem(native.root, "Managed state")
                completed: Outcome | None = None

                def record_outcome() -> None:
                    nonlocal completed
                    after = observe_filesystem(native.root, "Managed state")
                    completed = self.save_result(
                        history,
                        latest,
                        "removed",
                        "The superseded feed copy and its checksum retired.",
                        estimated_bytes=estimate,
                        available_delta=net_delta(before, after)
                        if pending.phase == "pending"
                        else None,
                    )

                try:
                    retired = book.retire(
                        state, pending, proof, record_outcome=record_outcome
                    )
                except OSError:
                    if completed is not None:
                        return completed
                    raise
                if not retired:
                    return self.save_result(
                        history,
                        latest,
                        "needs_review",
                        "The archive or a protection changed.",
                    )
                assert completed is not None
                return completed
        except (Refusal, *OBSERVATION_ERRORS):
            return outcome.model_copy(
                update={
                    "reason": "Cleanup could not be confirmed. Retry the same review."
                }
            )

    def reserved(self, generation: str, artifact: FeedCopy) -> bool:
        return any(
            item.state == "pending"
            and item.plan.generation == generation
            and item.plan.artifact == artifact
            and item.plan.expires > time.time()
            for item in self.history()
        )

    def store(self, outcome: Outcome, state: Result, reason: str) -> Outcome:
        with self.controller.omega.exclusive():
            return self.save_result(self.history(), outcome, state, reason)

    def save_result(
        self,
        history: list[Outcome],
        outcome: Outcome,
        state: Result,
        reason: str,
        **observations: int | None,
    ) -> Outcome:
        index = next(
            index
            for index, entry in enumerate(history)
            if entry.identity == outcome.identity
        )
        if history[index].state != "pending":
            return history[index]
        result = outcome.model_copy(
            update={"state": state, "reason": reason, **observations}
        )
        history[index] = result
        self.save(history)
        return result

    def snapshot_quotas(self) -> Document:
        with self.controller.omega.exclusive():
            quotas = SnapshotQuotas.read(self.controller.snapshot_quotas_file)
            used = len(self.controller.actor_snapshots("admin").list())
            return {
                "administrator": snapshot_capacity(used, quotas.administrator),
                "agent_default": quotas.agent_default,
            }

    def set_snapshot_quotas(self, administrator: str, agent_default: str) -> Document:
        quotas = SnapshotQuotas.from_arguments(administrator, agent_default)
        with self.controller.omega.exclusive():
            try:
                atomic(
                    self.controller.snapshot_quotas_file,
                    quotas.model_dump_json().encode(),
                )
            except OSError:
                raise Refusal("operation_outcome_unconfirmed") from None
        return quotas.model_dump()

    def request(self, name: str, args: object) -> Document:
        if not isinstance(args, dict) or not all(
            isinstance(value, str) for value in args.values()
        ):
            raise Refusal("invalid_storage_maintenance_request")
        if (
            name == "storage_policy"
            and set(args) == {"enabled"}
            and args["enabled"] in {"true", "false"}
        ):
            return self.policy(args["enabled"] == "true")
        if name == "storage_snapshot_quotas" and not args:
            return self.snapshot_quotas()
        if name == "storage_set_snapshot_quotas" and set(args) == {
            "administrator",
            "agent_default",
        }:
            return self.set_snapshot_quotas(
                args["administrator"], args["agent_default"]
            )
        session = args.get("session", "")
        if not HEX.fullmatch(session):
            raise Refusal("invalid_storage_maintenance_request")
        if name == "storage_review" and set(args) == {"session", "generation", "page"}:
            raw_page = args["page"]
            if not raw_page.isascii() or not raw_page.isdecimal() or len(raw_page) > 2:
                raise Refusal("invalid_storage_maintenance_request")
            page = int(raw_page)
            if not 1 <= page <= (PENDING_LIMIT + REVIEW_LIMIT - 1) // REVIEW_LIMIT:
                raise Refusal("invalid_storage_maintenance_request")
            return self.review(args["generation"], session, page)
        if (
            name == "storage_execute"
            and set(args) == {"session", "token", "generation"}
            and 1 <= len(args["token"]) <= TOKENS_MAX_BYTES
        ):
            return self.execute(args["token"], session, args["generation"])
        raise Refusal("invalid_storage_maintenance_request")


def expired_summary(item: Outcome, now: int) -> Outcome:
    if item.state == "pending" and item.plan.expires <= now:
        return item.model_copy(
            update={
                "state": "needs_review",
                "reason": "Authorization expired. Review the remaining files again.",
            }
        )
    return item


def only_exact_pending(state: FeedState, plan: Plan) -> PendingCopy | None:
    if (
        state.planned
        or not state.current
        or state.current.index != plan.publication
        or state.engine != plan.engine
    ):
        return None
    return next(
        (item for item in state.pending if item.artifact == plan.artifact), None
    )


def proof_digest(observations: object) -> str:
    return hashlib.sha256(repr(observations).encode()).hexdigest()


def estimated_size(*paths: Path) -> int | None:
    try:
        return sum(path.stat(follow_symlinks=False).st_size for path in paths)
    except OSError:
        return None


def net_delta(before: Mapping[str, object], after: Mapping[str, object]) -> int | None:
    old, new = before.get("available"), after.get("available")
    if (
        isinstance(old, int)
        and isinstance(new, int)
        and before.get("device") == after.get("device")
        and before.get("capacity") == after.get("capacity")
    ):
        return new - old
    return None
