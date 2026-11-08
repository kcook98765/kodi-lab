"""Thin administrator adapter to the shared version and clean Reset coordinators."""

import hashlib
import json
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime, timedelta
from typing import Any, Literal, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from kodi_lab.application.managed_clean_reset import (
    ManagedCleanResetService,
    ManagedResetInput,
)
from kodi_lab.application.managed_versions import (
    ManagedVersionError,
    ManagedVersionLifecycleService,
    Version,
)
from kodi_lab.domain import TargetType
from kodi_lab.storage.development_sessions import active_session
from kodi_lab.storage.managed_activity import read_resource_policy, timestamp
from kodi_lab.storage.managed_lifecycle_owner import WEB_ADMIN_OWNER
from kodi_lab.storage.managed_version_slots import managed_version_slots
from kodi_lab.storage.models import (
    ManagedCleanResetRow,
    ManagedInstanceMutationLeaseRow,
    ManagedInstanceRow,
    ManagedInstanceUseRow,
    ManagedProvisionReservationRow,
    ManagedStartReservationRow,
    ManagedVersionOperationRow,
    ManagedWebIntentRow,
    RuntimeContainerArtifactRow,
    TargetRow,
)

Action = Literal["start", "stop", "reset"]


class WebSlotRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: UUID = Field(strict=False)
    expected_snapshot: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirm_reset: bool = False


_MESSAGES = {
    "not_authorized": "This version's saved registration needs review in Advanced.",
    "managed_reset_pending": (
        "A clean Reset is in progress. Check its saved request or review recovery."
    ),
    "managed_version_operation_pending": (
        "A saved lifecycle request is in progress. Check its original receipt."
    ),
    "replacement_failed": (
        "Clean profile preparation failed. Review the original operation and "
        "confirmed retirement before preparing another profile."
    ),
    "approved_cached_runtime_required": (
        "Install and approve the Kodi files before starting."
    ),
    "other_version_running": (
        "Stop the running version first. Its profile will be kept."
    ),
    "active_viewer_or_input": "Close the screen and finish any control session first.",
    "playback_active": "Stop playback before stopping or resetting Kodi.",
    "playback_unknown": (
        "Playback could not be checked. Review Kodi's connection before acting."
    ),
    "keep_awake": (
        "Another user's keep-awake hold protects Kodi. Wait for its "
        "owner to release it."
    ),
    "other_client_active": (
        "Another agent or user is using Kodi. Wait for them to finish."
    ),
    "stale_browser": (
        "Kodi changed since this page was checked. Refresh and review "
        "the current state before acting."
    ),
    "request_conflict": (
        "This saved request has different details. Review its original receipt."
    ),
    "reset_confirmation_required": (
        "Confirm that Kodi settings, add-ons and profile data will be discarded."
    ),
    "target_busy": (
        "Another request is active. Check the saved request before continuing."
    ),
    "rate_limited": "Wait a few seconds, then check or resume the original request.",
}


def friendly(code: str) -> str:
    return _MESSAGES.get(
        code,
        "Kodi needs review. Open recovery and inspect the original "
        "operation before continuing.",
    )


class ManagedSlotsWeb:
    def __init__(
        self,
        versions: ManagedVersionLifecycleService,
        screen_supported: Callable[[UUID], bool],
    ) -> None:
        self.v, self.reset = versions, ManagedCleanResetService(versions)
        self.sessions = versions._sessions
        self.screen_supported = screen_supported

    def authorize(
        self, session: Session, target: UUID, admin: Callable[[Session], None]
    ) -> None:
        admin(session)
        slot = next(
            (s for s in managed_version_slots() if s.canonical_target_id == target),
            None,
        )
        if slot is None:
            raise PermissionError("Administrator version scope is invalid")
        current = session.get(TargetRow, str(target))
        if current is not None and (
            current.target_type is not TargetType.MANAGED
            or current.kodi_version != slot.kodi_version
        ):
            raise ManagedVersionError("canonical_slot_requires_review")

    def fingerprint(self, session: Session, version: Version) -> str:
        slot = self.v.slot(version)
        target = str(slot.canonical_target_id)
        instance = session.get(ManagedInstanceRow, target)
        artifact = (
            session.get(RuntimeContainerArtifactRow, instance.runtime_revision_id)
            if instance
            else None
        )
        current = active_session(session)
        records = self.v._records(session, slot).containers
        value = {
            "version": version,
            "target": target,
            "pin": instance.runtime_revision_id if instance else None,
            "epoch": instance.runtime_state_revision if instance else None,
            "image": artifact.image_id if artifact else None,
            "profile": sorted(
                (r.container_id, r.runtime_revision_id, r.image_id, r.runtime_state)
                for r in records
                if r.managed_instance_id == target
            ),
            "selection": (current.generation, current.container_id)
            if current and current.target_id == target
            else None,
            "leases": [
                getattr(session.get(model, target), "operation_id", None)
                for model in (
                    ManagedInstanceMutationLeaseRow,
                    ManagedProvisionReservationRow,
                    ManagedStartReservationRow,
                )
            ],
        }
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()

    def _parent(self, session: Session, request: UUID, action: Action) -> Any:
        model = (
            ManagedCleanResetRow if action == "reset" else ManagedVersionOperationRow
        )
        return session.scalar(
            select(model).where(
                model.client_id == str(WEB_ADMIN_OWNER),
                model.request_id == str(request),
            )
        )

    def _authority(
        self,
        session: Session,
        target: UUID,
        request: UUID,
        admin: Callable[[Session], None],
    ) -> None:
        self.authorize(session, target, admin)
        intent = session.get(ManagedWebIntentRow, str(request))
        if (
            intent is None
            or self.v.slot(intent.generation).canonical_target_id != target
        ):
            raise ManagedVersionError("request_conflict")
        if self._parent(session, request, cast(Action, intent.action)) is None and (
            self.fingerprint(session, cast(Version, intent.generation))
            != intent.snapshot_sha256
        ):
            raise ManagedVersionError("stale_browser")

    def invoke(
        self,
        version: Version,
        action: Action,
        operation: WebSlotRequest,
        admin: Callable[[Session], None],
    ) -> dict[str, Any]:
        with self.sessions.begin() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            self.authorize(session, self.v.slot(version).canonical_target_id, admin)
            prior = session.get(ManagedWebIntentRow, str(operation.request_id))
            binding = (
                version,
                action,
                operation.expected_snapshot,
                operation.confirm_reset,
            )
            if action == "reset" and operation.confirm_reset is not True:
                raise ManagedVersionError("reset_confirmation_required")
            if prior is not None:
                if (
                    prior.generation,
                    prior.action,
                    prior.snapshot_sha256,
                    prior.reset_confirmed,
                ) != binding:
                    raise ManagedVersionError("request_conflict")
            else:
                if (
                    session.scalar(
                        select(func.count()).select_from(ManagedWebIntentRow)
                    )
                    or 0
                ) >= 4096:
                    raise ManagedVersionError("managed_version_receipt_quota")
                if self.fingerprint(session, version) != operation.expected_snapshot:
                    raise ManagedVersionError("stale_browser")
                session.add(
                    ManagedWebIntentRow(
                        request_id=str(operation.request_id),
                        generation=version,
                        action=action,
                        snapshot_sha256=operation.expected_snapshot,
                        reset_confirmed=operation.confirm_reset,
                        created_at=timestamp(self.v._clock()),
                    )
                )

        def authorize(session: Session, target: UUID) -> None:
            self._authority(session, target, operation.request_id, admin)

        if action == "reset":
            self.reset.invoke(
                WEB_ADMIN_OWNER,
                ManagedResetInput(
                    version=version, request_id=operation.request_id, restart=False
                ),
                authorize,
            )
        else:
            self.v.invoke(
                client_id=WEB_ADMIN_OWNER,
                request_id=operation.request_id,
                version=version,
                action=action,
                authorize=authorize,
            )
        return self.receipt(operation.request_id, admin)

    def receipt(
        self, request: UUID, admin: Callable[[Session], None]
    ) -> dict[str, Any]:
        with self.sessions() as session:
            admin(session)
            intent = session.get(ManagedWebIntentRow, str(request))
            if intent is None:
                raise ManagedVersionError("request_not_found")
            parent = self._parent(session, request, cast(Action, intent.action))
            phase = parent.phase if parent else "not_dispatched"
            code = parent.code if parent else "accepted"
            version, action = intent.generation, intent.action
            registered = (
                session.get(
                    ManagedInstanceRow, str(self.v.slot(version).canonical_target_id)
                )
                is not None
            )
            binding = dict(
                request_id=str(request),
                expected_snapshot=intent.snapshot_sha256,
                confirm_reset=intent.reset_confirmed,
            )
        # Reading receipts never invokes or advances a child operation.
        evidence = None
        if parent is not None:

            def authority(session: Session, target: UUID) -> None:
                self.authorize(session, target, admin)

            evidence = (
                self.reset._result(parent.id, authority, True)
                if action == "reset"
                else self.v._result(parent.id, WEB_ADMIN_OWNER, authority, True)
            ).model_dump(mode="json")
        if parent is None:
            with self.sessions() as session:
                try:
                    if (
                        self.fingerprint(session, cast(Version, version))
                        != binding["expected_snapshot"]
                    ):
                        phase, code = "blocked", "stale_browser"
                except Exception:
                    phase, code = "blocked", "stale_browser"
        done = phase in {"complete", "blocked"}
        message = (
            "Reset completed and prepared a stopped profile. Check current status "
            "below; development setup needs a fresh review."
            if phase == "complete" and action == "reset"
            else "Request completed. Check Kodi's current status below."
            if phase == "complete"
            else friendly(code)
            if phase in {"blocked", "uncertain"}
            else (
                "The original request is saved. Resume it to continue or "
                "check its original operation."
            )
        )
        return dict(
            version=version,
            action=action,
            phase=phase,
            message=message,
            done=done,
            can_resume=not done and phase != "uncertain",
            request=binding,
            recovery_url=f"/instances/{self.v.slot(version).canonical_target_id}/history"
            if registered
            else "/history/instances",
            operation_id=parent.id if parent else None,
            code=code,
            technical=evidence,
        )

    def _protection(self, session: Session, target: UUID) -> str | None:
        try:
            instance = session.get(ManagedInstanceRow, str(target))
            if instance is None:
                return None
            fake = ManagedVersionOperationRow(target_id=str(target), action="stop")
            self.v._idle(session, fake)
            now = self.v._clock()
            minutes = read_resource_policy(session).idle_minutes or 60
            for use in session.scalars(
                select(ManagedInstanceUseRow).where(
                    ManagedInstanceUseRow.managed_instance_id == str(target)
                )
            ):
                if use.actor_key == "web":
                    continue
                if use.hold_until and use.hold_until > timestamp(now):
                    return friendly("keep_awake")
                if (
                    use.released_at is None or use.last_activity_at > use.released_at
                ) and datetime.fromisoformat(
                    use.last_activity_at.replace("Z", "+00:00")
                ) + timedelta(minutes=minutes) > now:
                    return friendly("other_client_active")
        except Exception as error:
            return friendly(self.v._code(error))
        return None

    def status(self, admin: Callable[[Session], None]) -> dict[str, Any]:
        def status_authority(session: Session, target: UUID) -> None:
            try:
                self.authorize(session, target, admin)
            except ManagedVersionError as error:
                raise PermissionError(error.code) from error

        values = self.v.status(WEB_ADMIN_OWNER, status_authority)
        slots = []
        running = [
            v.version.title()
            for v in values.versions
            if v.state in {"running", "starting"}
        ]
        # Native readiness can fail while a container is still running. Keep
        # that version visible as the capacity owner; never suggest a switch.
        with suppress(Exception):
            names = {
                str(slot.canonical_target_id): slot.generation.title()
                for slot in managed_version_slots()
            }
            running = sorted(
                set(running)
                | {
                    names[record.managed_instance_id]
                    for record in self.v._inventory().containers
                    if record.runtime_state == "running"
                    and record.managed_instance_id in names
                }
            )
        for value in values.versions:
            version = cast(Version, value.version)
            target = self.v.slot(version).canonical_target_id
            with self.sessions() as session:
                admin(session)
                instance = session.get(ManagedInstanceRow, str(target))
                try:
                    snapshot = self.fingerprint(session, version)
                    protection = self._protection(session, target)
                except Exception:
                    snapshot, protection = (
                        None,
                        "The profile identity needs review in Advanced.",
                    )
                latest = session.scalar(
                    select(ManagedCleanResetRow)
                    .where(
                        ManagedCleanResetRow.target_id == str(target),
                        ManagedCleanResetRow.phase == "complete",
                    )
                    .order_by(ManagedCleanResetRow.created_at.desc())
                    .limit(1)
                )
                reset_note = False
                if latest and instance:
                    # A blocked identity takes precedence over the clean note.
                    with suppress(Exception):
                        reset_note = any(
                            record.container_id == latest.new_container_id
                            for record in self.v._records(
                                session, self.v.slot(version)
                            ).containers
                            if record.managed_instance_id == str(target)
                        )
                intent = (
                    session.get(ManagedWebIntentRow, str(value.request_id))
                    if value.request_id
                    else None
                )
            cached = self.v._installer.observe_create(version)
            cache_ok = cached.status == "ready" and not cached.busy
            other = next((name for name in running if name.lower() != version), None)
            ready = value.native_ready and value.state == "running"
            reason = protection or (
                friendly(value.code) if value.state == "blocked" else None
            )
            start_reason = (
                ("Kodi is already running." if ready else None)
                or (
                    "Wait for the current request to finish."
                    if value.state == "starting"
                    else None
                )
                or reason
                or (
                    f"Review {other}'s recovery before starting another version."
                    if other
                    and any(
                        v.version == other.lower() and v.state == "blocked"
                        for v in values.versions
                    )
                    else f"Stop {other} first. Its profile will be kept."
                    if other
                    else None
                )
                or (
                    None
                    if cache_ok or value.state == "stopped"
                    else friendly("approved_cached_runtime_required")
                )
            )
            stop_reason = reason or (
                None if ready else "Wait for Kodi to be ready before stopping safely."
            )
            reset_reason = (
                reason
                or (None if cache_ok else friendly("approved_cached_runtime_required"))
                or (
                    "Wait for Kodi to be ready before resetting."
                    if value.state == "starting"
                    else None
                )
            )
            screen_ok = ready and self.screen_supported(target)
            slots.append(
                dict(
                    version=version,
                    name=version.title() + (" (preview)" if version == "piers" else ""),
                    release=value.kodi_release,
                    state=value.state,
                    state_label={
                        "empty": "Not set up",
                        "stopped": "Stopped",
                        "starting": "Starting",
                        "running": "Running",
                        "blocked": "Needs attention",
                    }[value.state],
                    preparation="Profile prepared"
                    if instance and value.state != "empty"
                    else "Kodi files available"
                    if cache_ok
                    else "Installation needed",
                    native="Ready"
                    if ready
                    else "Checking Kodi"
                    if value.state == "starting"
                    else "Not running"
                    if value.state in {"empty", "stopped"}
                    else "Needs review",
                    message=reason
                    or (
                        "Kodi is running and ready to use."
                        if ready
                        else "Kodi is starting. Wait for the current request to finish."
                        if value.state == "starting"
                        else (
                            "Start Kodi uses this version's saved settings and add-ons."
                        )
                        if value.state == "stopped"
                        else (
                            "Kodi files are installed. Start Kodi prepares "
                            "and starts this version."
                        )
                        if cache_ok
                        else (
                            "Use Create Managed Kodi to install this version "
                            "before starting."
                        )
                    ),
                    snapshot=snapshot,
                    start=not start_reason and value.state in {"empty", "stopped"},
                    start_reason=start_reason,
                    stop=ready and not stop_reason,
                    stop_reason=stop_reason,
                    reset=not reset_reason
                    and value.state in {"empty", "stopped", "running"},
                    reset_reason=reset_reason,
                    screen_url=f"/instances/{target}/novnc-phase1"
                    if screen_ok
                    else None,
                    screen_reason="Start Kodi and wait for Ready to open its screen."
                    if not ready
                    else "Screen support needs review in Advanced.",
                    install_url=f"/create-kodi?generation={version}",
                    development_url=f"/managed-kodi/{version}/development",
                    selection_url=f"/development-session/retained?target_id={target}",
                    resource_url=f"/managed-resources#instance-{target}",
                    recovery_url=f"/instances/{target}/history"
                    if instance
                    else "/managed-instances"
                    if value.state == "blocked"
                    else "/runtimes",
                    advanced_url=f"/instances/{target}" if instance else "/runtimes",
                    reset_note=reset_note,
                    needs_review=bool(reason),
                    pending_request=str(value.request_id) if intent else None,
                    technical=value.model_dump(mode="json"),
                )
            )
        return dict(slots=slots, running=running)
