"""Closed managed operation surface shared with the experimental MCP transport."""

from __future__ import annotations

import base64
import binascii
import json
import logging
import re
import shutil
import sqlite3
import stat
import subprocess
import time
from bisect import bisect_right
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import TypedDict, cast
from uuid import UUID, uuid4
from xml.etree import ElementTree as ET

from kodi_lab.kodi.remote import (
    NAVIGATION_ACTIONS,
    BrowserRemoteAction,
    fixed_request_mapping,
)

from . import RELEASES, reviewed_image_file
from .feed_retirement import FeedRetirement, observed_engine, runtime_idle
from .packages import Package, Packages, addon_id, manifest, sha, workspace_files
from .prepare import (
    ABANDONED_SCRATCH_GRACE,
    PREPARE_CLEANUP_BATCH,
    PREPARE_CLEANUP_CAPACITY,
    PREPARE_CLEANUP_SECONDS,
    PrepareResources,
    valid_prepare_receipt,
)
from .protocol import (
    ADMIN,
    PRIVATE,
    READ,
    Document,
    OperationReply,
    Refusal,
    actor_key,
    validate,
)
from .public_repository import REPOSITORY, Approval, approval
from .runtime import ESTUARY, REPO, Runtime, atomic, sync_dir
from .runtime_retirement import RuntimeRetirement
from .snapshots import Snapshots
from .source_provenance import (
    SourceFiles,
    read_sources,
    validate_sources,
    write_sources,
)
from .storage_maintenance import StorageMaintenance


class GeneratedInclude(TypedDict):
    digest: str
    mode: int
    bytes: int


class Controller:
    def __init__(
        self,
        root: Path,
        *,
        daemon_root: Path | None = None,
        reviewed_image: Path | None = None,
        handoff_root: Path | None = None,
        interrupt: Callable[[str], None] | None = None,
    ) -> None:
        self.runtime = Runtime(
            root,
            self.checkpoint,
            daemon_root=daemon_root,
            reviewed_image=reviewed_image,
        )
        self.omega = self.runtime
        self.storage_maintenance = StorageMaintenance(self)
        self.runtime_retirement = RuntimeRetirement(self)
        self.runtimes = {"omega": self.omega}
        if handoff_root is not None:
            assert daemon_root is not None
            for generation in RELEASES.keys() - {"omega"}:
                path = root / generation
                path.mkdir(mode=0o700, exist_ok=True)
                self.runtimes[generation] = Runtime(
                    path,
                    self.checkpoint,
                    daemon_root=daemon_root / generation,
                    reviewed_image=handoff_root / reviewed_image_file(generation),
                    generation=generation,
                    network_policy=self.omega.network_policy,
                )
        self.input_attempt: UUID | None = None
        self.input_prior_lease: bytes | None = None
        self.input_revoked = False
        self.record = self.runtime.root / "operation.json"
        self.current: Document | None = None
        self._prepare_cleanup_cursor = ""
        self._feed_cleanup_cursor: dict[str, str] = {}
        self._feed_generation_cursor = 0
        self._feed_engine_bound = daemon_root is not None
        self._prepare_warning_at: dict[str, float] = {}
        self.lifecycle_status: Document | None = None
        self.interrupt = interrupt or (lambda _event: None)
        self.actor = "admin"
        self.lease_path = self.runtime.root / "active-owner"
        self.agents = self.runtime.root / "agents"
        self.agents.mkdir(mode=0o700, exist_ok=True)
        self.actions: dict[str, Callable[..., object]] = {
            "managed_prepare": self.prepare_runtime,
            "managed_start": self.start_runtime,
            "managed_stop": self.stop_runtime,
            "managed_public_repository_status": self.public_status,
            "managed_public_repository_select": self.select_public,
            "managed_public_catalog": self.public_catalog,
            "managed_public_install": self.public_install,
            "managed_reset": lambda: self.runtime.reset(),
            "managed_status": self.status,
            "managed_recover": self.recover,
            "snapshot_list": lambda **args: self.snapshots.list(**args),
            "snapshot_save": lambda **args: self.snapshots.save(**args),
            "snapshot_load": lambda **args: self.snapshots.load(**args),
            "snapshot_delete": lambda **args: self.snapshots.delete(**args),
            "workspace_open": lambda **args: self.packages.open_workspace(**args),
            "workspace_tree": lambda **args: self.packages.tree(**args),
            "workspace_read": lambda **args: self.packages.read(**args),
            "workspace_edit": lambda **args: self.packages.edit(**args),
            "workspace_package": lambda **args: self.packages.workspace_package(**args),
            "addon_install": self.install,
            "workspace_deploy": self.deploy,
            "package_import": self.import_zip,
            "addon_launch": self.launch,
            "addon_setting": self.setting,
            "addon_inventory": self.inventory,
            "managed_gui": self.gui,
            "managed_screen": lambda: self.runtime.screen(),
            "managed_logs": lambda: self.runtime.logs(),
            "managed_navigate": self.navigate,
            "managed_skin": self.skin,
        }

    def actor_directory(self, actor: str) -> Path:
        if actor == "admin":
            return self.omega.root
        root = self.agents / actor_key(actor)
        root.mkdir(mode=0o700, exist_ok=True)
        for name in ("snapshots", "approved", "workspaces"):
            (root / name).mkdir(mode=0o700, exist_ok=True)
        return root

    @property
    def packages(self) -> Packages:
        return Packages(
            self.actor_directory(self.actor),
            self.runtime.feed,
            catalog_root=self.omega.root,
        )

    @property
    def snapshots(self) -> Snapshots:
        return self.actor_snapshots(self.actor)

    def actor_snapshots(self, actor: str) -> Snapshots:
        return Snapshots(
            self.runtime,
            self.checkpoint,
            directory=self.actor_directory(actor) / "snapshots",
        )

    def owner(self) -> str | None:
        if not self.lease_path.exists():
            return None
        lease = json.loads(self.lease_path.read_text())
        return actor_key(lease["owner"]) if lease["expires"] > time.time() else None

    def claim(self, actor: str) -> Document:
        atomic(
            self.lease_path,
            json.dumps(
                {
                    "owner": actor,
                    "expires": time.time() + 900,
                }
            ).encode(),
        )
        return {"owner": actor, "idle_timeout_seconds": 900}

    def begin_input(self, attempt: UUID) -> None:
        if self.input_attempt is not None:
            raise Refusal("managed_input_in_use")
        record = self.last_operation()
        if record and record["state"] == "pending":
            raise Refusal("managed_recovery_required")
        # The administrator lease fences a surviving helper on a broker
        # restart. Keep the live agent lease available for verified handback.
        previous = self.owner()
        prior_lease = (
            self.lease_path.read_bytes() if previous not in (None, "admin") else None
        )
        self.claim("admin")
        self.input_prior_lease = prior_lease
        self.input_attempt = attempt
        self.input_revoked = False

    def finish_input(self, attempt: UUID | None, terminated: bool) -> None:
        if not terminated or attempt is None:
            return
        with self.omega.exclusive():
            if self.input_attempt == attempt:
                if self.owner() == "admin":
                    if self.input_prior_lease is None:
                        self.lease_path.unlink(missing_ok=True)
                    else:
                        atomic(self.lease_path, self.input_prior_lease)
                self.input_prior_lease = None
                self.input_attempt = None
                self.input_revoked = False

    def authorize_profile(
        self, name: str, actor: str, pending: Document | None
    ) -> Document | None:
        if self.input_attempt is not None and name not in READ | PRIVATE:
            if actor == "admin" and name == "managed_release":
                self.input_revoked = True
                return {"owner": "admin", "release_pending": True}
            if actor == "admin" and name == "managed_stop":
                self.input_revoked = True
            else:
                raise Refusal("managed_input_in_use")
        if name == "managed_take_control":
            return self.claim(actor)
        if name in PRIVATE or name == "managed_status":
            return None

        owner = self.owner()
        if name in READ and self.input_prior_lease is not None:
            prior = json.loads(self.input_prior_lease)
            if prior["owner"] == actor and prior["expires"] > time.time():
                return None
        public_stop = (
            actor == "admin"
            and name == "managed_stop"
            and self.runtime.public.session.exists()
        )
        if (
            owner
            and owner != actor
            and (actor != "admin" or name not in READ)
            and not public_stop
        ):
            raise Refusal("managed_owned")
        if name == "managed_release":
            if self.omega.public.session.exists():
                raise Refusal("stop_public_test_first")
            if owner == actor:
                self.lease_path.unlink(missing_ok=True)
            return {"owner": None}
        if name == "managed_acquire":
            if pending:
                raise Refusal("managed_recovery_required")
            return self.claim(actor)
        if name not in READ:
            if actor != "admin" and owner != actor:
                raise Refusal("managed_acquire_required")
            self.claim(actor)
        elif actor != "admin" and owner != actor:
            raise Refusal("managed_acquire_required")
        return None

    def last_operation(self) -> Document | None:
        try:
            record = json.loads(self.record.read_text())
        except FileNotFoundError:
            return None
        except (ValueError, RecursionError):
            raise Refusal("managed_operation_journal_invalid") from None
        if not isinstance(record, dict) or record.get("state") not in (
            "pending",
            "complete",
        ):
            raise Refusal("managed_operation_journal_invalid")
        return cast(Document, record)

    def checkpoint(self, event: str, **data: object) -> None:
        if self.current is not None:
            self.current.update(event=event, **data)
            self.write_operation(self.current)
            self.interrupt(event)

    def write_operation(self, record: Document) -> None:
        atomic(self.record, json.dumps(record).encode())

    def warn_prepare_cleanup(self, reason: str) -> None:
        now = time.monotonic()
        if now >= self._prepare_warning_at.get(reason, now):
            logging.warning("Managed Prepare cleanup pending (%s)", reason)
            self._prepare_warning_at[reason] = now + 60

    def update_prepare_cleanup(
        self, operation: str, expected: Document, updated: Document
    ) -> None:
        # Merge only this receipt into the latest operation. The lifecycle lock
        # protects journal writes, while resource I/O stays in the cleanup worker.
        with self.omega.exclusive():
            current = self.last_operation()
            if not current or (
                current["state"] == "pending" and current.get("id") == operation
            ):
                raise Refusal("prepare_cleanup_receipt_changed")
            obligations = current.get("prepare_cleanup")
            if (
                not isinstance(obligations, dict)
                or obligations.get(operation) != expected
            ):
                raise Refusal("prepare_cleanup_receipt_changed")
            if {"scratch", "engine"} & updated.keys():
                obligations[operation] = updated
            else:
                del obligations[operation]
                if not obligations:
                    del current["prepare_cleanup"]
            self.write_operation(current)

    def retry_prepare_cleanup(self, record: Document) -> None:
        if "prepare_cleanup" not in record:
            return
        obligations = record["prepare_cleanup"]
        if not isinstance(obligations, dict):
            self.warn_prepare_cleanup("prepare_cleanup_collection_invalid")
            return
        deadline = time.monotonic() + PREPARE_CLEANUP_SECONDS
        keys = sorted(obligations)
        start = bisect_right(keys, self._prepare_cleanup_cursor)
        batch = (keys[start:] + keys[:start])[:PREPARE_CLEANUP_BATCH]
        if not batch:
            return
        # A journal rename may be visible after its directory fsync failed.
        sync_dir(self.record.parent)
        for operation in batch:
            if time.monotonic() >= deadline:
                break
            self._prepare_cleanup_cursor = operation
            receipt = obligations[operation]
            if (
                record["state"] == "pending" and operation == record.get("id")
            ) or not valid_prepare_receipt(operation, receipt, terminal=True):
                self.warn_prepare_cleanup("prepare_cleanup_receipt_invalid")
                continue
            runtime = self.runtimes.get(receipt["generation"])
            if runtime is None:
                self.warn_prepare_cleanup("prepare_cleanup_generation_unsupported")
                continue
            try:
                resources = PrepareResources(runtime, operation, receipt)
            except Refusal:
                self.warn_prepare_cleanup("prepare_resource_identity")
                continue
            self.retire_prepare_resources(resources, deadline)

    def retire_prepare_resources(
        self, resources: PrepareResources, deadline: float
    ) -> None:
        receipt = resources.receipt
        expected = receipt.copy()

        def persist() -> None:
            self.update_prepare_cleanup(resources.operation, expected, receipt)
            expected.update(receipt)

        # Let bounded Engine cleanup proceed even if scratch is expensive.
        for key in ("engine", "scratch"):
            if key not in receipt or (
                key == "scratch" and time.time() < receipt["scratch_after"]
            ):
                continue
            if time.monotonic() >= deadline:
                return
            try:
                if key == "engine":
                    resources.retire_inspection(persist, deadline=deadline)
                else:
                    resources.retire_scratch()
                del receipt[key]
                if key == "engine":
                    receipt.pop("container", None)
                else:
                    receipt.pop("scratch_allocation", None)
                self.update_prepare_cleanup(resources.operation, expected, receipt)
                expected = receipt.copy()
            except (Refusal, OSError) as error:
                if str(error) == "managed_busy":
                    return
                self.warn_prepare_cleanup(
                    str(error) if isinstance(error, Refusal) else key
                )
                # A failed update can have reached the journal. Reload before
                # attempting further writes for this receipt.
                if receipt != expected:
                    return

    def require_prepare_recovery_receipt(self, record: Document) -> None:
        if (
            record.get("name") != "managed_prepare"
            or not valid_prepare_receipt(
                record.get("id"), record["prepare"], terminal=False
            )
            or record["prepare"]["target"] != str(self.runtime.target)
        ):
            raise Refusal("prepare_recovery_receipt_invalid")
        obligations = record.get("prepare_cleanup", {})
        if not isinstance(obligations, dict) or record["id"] in obligations:
            raise Refusal("prepare_cleanup_collection_invalid")
        sync_dir(self.record.parent)

    def finish_prepare(
        self, record: Document, *, abandoned: bool = False, **outcome: object
    ) -> None:
        receipt = record.pop("prepare")
        if {"scratch", "engine"} & receipt.keys():
            receipt.update(
                generation=self.runtime.generation,
                scratch_after=time.time() + ABANDONED_SCRATCH_GRACE if abandoned else 0,
            )
            record.setdefault("prepare_cleanup", {})[record["id"]] = receipt
        record.pop("recovery_error", None)
        record.update(state="complete", **outcome)
        # Persist the outcome and obligations together before best-effort retirement.
        self.write_operation(record)

    def resume_prepare_cleanup(self) -> None:
        """Collect terminal obligations without holding the lifecycle lock over I/O."""
        try:
            record = self.last_operation()
            if record:
                self.retry_prepare_cleanup(record)
        except Refusal as error:
            self.warn_prepare_cleanup(str(error))
        except OSError:
            self.warn_prepare_cleanup("prepare_cleanup_journal_io")

    def resume_feed_cleanup(self) -> None:
        """Offer at most one stopped generation's redundant feed copy per pass."""
        runtimes = tuple(self.runtimes.values())
        native = runtimes[self._feed_generation_cursor]
        self._feed_generation_cursor = (self._feed_generation_cursor + 1) % len(
            runtimes
        )
        book = FeedRetirement(self.omega.root, native.root, native.target)
        try:
            state = book.read()
            pending_operation = self.last_operation()
            if (pending_operation and pending_operation["state"] == "pending") or (
                native.public.session.exists()
            ):
                return
            if state.planned is not None:
                planned = book.planned_proof(state)
                if planned is not None:
                    with self.omega.exclusive():
                        latest = self.last_operation()
                        if book.read() == state and not (
                            latest and latest["state"] == "pending"
                        ):
                            book.reconcile(state, planned)
                return
            if not state.pending or not self.storage_maintenance.automatic():
                return
            items = sorted(state.pending, key=lambda item: item.artifact.name)
            cursor = self._feed_cleanup_cursor.get(native.generation, "")
            index = bisect_right([item.artifact.name for item in items], cursor)
            pending = items[index % len(items)]
            self._feed_cleanup_cursor[native.generation] = pending.artifact.name
            proof = book.prove(state, pending)
            if proof is None:
                return
            with self.omega.exclusive():
                latest = self.last_operation()
                if (
                    latest and latest["state"] == "pending"
                ) or native.public.session.exists():
                    return
                if (
                    self.storage_maintenance.automatic()
                    and not self.storage_maintenance.reserved(
                        native.generation, pending.artifact
                    )
                    and runtime_idle(native, state.engine)
                ):
                    book.retire(state, pending, proof)
        except (
            Refusal,
            OSError,
            ValueError,
            TypeError,
            KeyError,
            IndexError,
            subprocess.SubprocessError,
        ):
            return

    def resume_interrupted_prepare(self) -> None:
        """Recover the active Prepare independently of terminal obligations."""
        try:
            with self.omega.exclusive():
                record = self.last_operation()
                if (
                    not record
                    or record["state"] != "pending"
                    or record.get("name") != "managed_prepare"
                ):
                    return
                generation = record.get("generation", "omega")
                if not isinstance(generation, str) or generation not in self.runtimes:
                    self.warn_prepare_cleanup("prepare_recovery_generation_unsupported")
                    return
                self.runtime = self.runtimes[generation]
                try:
                    self.recover()
                finally:
                    self.runtime = self.omega
        except (Refusal, OSError, subprocess.SubprocessError):
            self.warn_prepare_cleanup("prepare_recovery_pending")

    def perform(self, name: str, args: dict[str, str], *, actor: str) -> OperationReply:
        started = time.monotonic()
        actor_key(actor)
        _, args = validate(name, args)
        arguments = dict(args)
        generation = arguments.pop("generation", "omega")
        runtime = self.runtimes.get(generation)
        if runtime is None:
            raise Refusal("managed_generation_unavailable")
        try:
            with self.omega.exclusive():
                self.runtime = runtime
                self.actor = actor
                try:
                    result = self.perform_locked(name, arguments)
                finally:
                    self.actor = "admin"
                    self.runtime = self.omega
        except Refusal as error:
            transition = self.lifecycle_status
            if (
                str(error) != "managed_busy"
                or name != "managed_status"
                or actor != "admin"
                or transition is None
                or transition["generation"] != generation
            ):
                raise
            result = dict(transition)
        return {
            "result": result,
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }

    def perform_locked(self, name: str, args: dict[str, str]) -> object:
        if name in ADMIN and self.actor != "admin":
            raise Refusal("managed_admin_required")
        if name == "managed_network_access":
            enabled = args["enabled"] == "true"
            atomic(self.omega.network_policy, b"true\n" if enabled else b"false\n")
            return {"network_access_configured": enabled}
        if name in {
            "managed_prepare",
            "managed_start",
            "managed_reset",
            "snapshot_load",
            "managed_public_repository_select",
            "workspace_deploy",
        }:
            self.runtime.require_not_retired()
        previous = self.last_operation()
        pending = previous if previous and previous["state"] == "pending" else None
        lease_result = self.authorize_profile(name, self.actor, pending)
        if lease_result is not None:
            return lease_result
        if self.omega.public.session.exists():
            if self.runtime is not self.omega and name in {
                "managed_prepare",
                "managed_start",
                "managed_reset",
                "snapshot_load",
            }:
                raise Refusal("stop_public_test_first")
            if self.runtime is self.omega and name in {
                "snapshot_save",
                "snapshot_load",
                "managed_reset",
                "managed_release",
            }:
                raise Refusal("stop_public_test_first")
            if self.runtime is self.omega and pending and name == "managed_stop":
                return self.stop_pending_public(pending)
        if name in READ or name in PRIVATE or name == "managed_recover":
            result = self.actions[name](**args)
            if isinstance(result, Package):
                return {
                    "digest": result.digest,
                    "addon": result.id,
                    "version": result.version,
                }
            return result
        if pending and pending.get("generation", "omega") != self.runtime.generation:
            raise Refusal("managed_recovery_required")
        if pending and pending.get("recovery_error") and name == "managed_stop":
            if self.runtime.pointer.exists():
                self.require_reconciled_volume(pending, self.runtime.read_pointer())
            return self.runtime.stop()
        if pending and not (
            "prepare" not in pending
            and pending.get("recovery_error")
            and name in {"managed_reset", "snapshot_load"}
        ):
            raise Refusal("managed_recovery_required")
        operation = uuid4().hex
        obligations = previous.get("prepare_cleanup") if previous else None
        if isinstance(obligations, dict) and operation in obligations:
            raise Refusal("managed_operation_identity_collision")
        self.current = {
            "id": operation,
            "name": name,
            "generation": self.runtime.generation,
            "actor": self.actor,
            "state": "pending",
            "subject": {
                key: value
                for key, value in args.items()
                if key in {"addon", "version", "digest", "name"}
            },
        }
        if previous and "prepare_cleanup" in previous:
            self.current["prepare_cleanup"] = previous["prepare_cleanup"]
        try:
            phase = {"managed_start": "starting", "managed_stop": "stopping"}.get(name)
            if phase is not None:
                # Publish a single read-only snapshot while this lifecycle action
                # holds the lock. Never use it to authorize a view or mutation.
                self.lifecycle_status = {
                    "state": phase,
                    "generation": self.runtime.generation,
                    "owner": self.owner(),
                    "view_control": self.input_attempt is not None,
                    "view_target": str(self.runtime.target),
                }
            self.checkpoint("begin")
            try:
                result = self.actions[name](**args)
            except Exception as error:
                if "prepare" not in self.current:
                    if isinstance(error, Refusal) and self.current["event"] == "begin":
                        self.checkpoint("refused", state="complete", error=str(error))
                    raise
                # A failed checkpoint can leave a newer receipt only in memory.
                # Act on the persisted record, then require its durability barrier.
                persisted = self.last_operation()
                if (
                    not persisted
                    or persisted.get("id") != self.current["id"]
                    or "prepare" not in persisted
                ):
                    raise
                self.current = persisted
                self.require_prepare_recovery_receipt(persisted)
                if not self.reconcile_profile(persisted):
                    self.finish_prepare(
                        persisted,
                        event="prepare_failed",
                        error=str(error)
                        if isinstance(error, Refusal)
                        else "operation_outcome_unconfirmed",
                    )
                    raise
                result = {"state": "prepared", "profile": self.runtime.generation}
            if "prepare" in self.current:
                self.finish_prepare(self.current, event="reply", result=result)
            elif not (self.current.get("native_pending") or self.current.get("skin")):
                self.checkpoint("reply", state="complete", result=result)
            return result
        finally:
            self.lifecycle_status = None
            self.current = None

    def public_approval(self) -> Approval:
        if self.runtime.generation != "omega":
            raise Refusal("public_test_requires_omega")
        selected = approval(self.runtime.root)
        if selected is None:
            raise Refusal("public_repository_not_approved")
        return selected

    def public_status(self) -> Document:
        selected = (
            approval(self.runtime.root) if self.runtime.generation == "omega" else None
        )
        return {
            "approval": selected.info() if selected else None,
            "active": self.runtime.public.session.exists(),
        }

    def select_public(self, repository: str) -> Document:
        selected = self.public_approval()
        if repository != selected.repository.id:
            raise Refusal("public_repository_not_approved")
        self.runtime.require_stopped()
        # A repository test is opt-in on an unprepared, disposable profile.
        # Never replace or attach public egress to a retained working slot.
        if self.runtime.pointer.exists() or self.runtime.public.session.exists():
            raise Refusal("public_test_requires_unprepared_profile")
        if any(item.pointer.exists() for item in self.runtimes.values()):
            raise Refusal("public_test_requires_disposable_site")
        self.checkpoint("public_selection_pending")
        atomic(self.runtime.public.session, str(time.time() + 900).encode())
        return self.public_status()

    def prepare_runtime(self) -> Document:
        if self.runtime.public.session.exists():
            self.runtime.public.require_unexpired()
        assert self.current is not None
        if not self.runtime.pointer.exists():
            obligations = self.current.get("prepare_cleanup", {})
            if not isinstance(obligations, dict):
                raise Refusal("prepare_cleanup_collection_invalid")
            if len(obligations) >= PREPARE_CLEANUP_CAPACITY:
                raise Refusal("prepare_cleanup_capacity")
        return self.runtime.prepare(self.current["id"])

    def start_runtime(self) -> Document:
        try:
            if self.runtime.public.session.exists():
                self.public_approval()
            return self.runtime.start()
        except Exception:
            if self.runtime.public.session.exists():
                self.runtime.public.remove()
            raise

    def stop_pending_public(self, record: Document) -> Document:
        try:
            if self.runtime.pointer.exists():
                self.require_reconciled_volume(record, self.runtime.read_pointer())
        except Refusal:
            self.runtime.public.remove()
            raise
        return self.stop_runtime()

    def stop_runtime(self) -> Document:
        try:
            return self.runtime.stop()
        finally:
            if self.runtime.public.session.exists():
                self.runtime.public.remove()

    def active_public(self) -> Approval:
        selected = self.public_approval()
        if not self.runtime.public.session.exists():
            raise Refusal("public_test_not_selected")
        self.runtime.public.require_unexpired()
        if not self.runtime.ready():
            raise Refusal("bridge_not_ready")
        return selected

    def public_catalog(self) -> Document:
        selected = self.active_public()
        try:
            self.verify(selected.repository, origin=REPOSITORY, exact=True)
            self.checkpoint("public_catalog_dispatch", native_pending=True)
            result = self.runtime.signed(
                "managed_v2_public_catalog", {"checksum": selected.checksum}
            )
            if result.get("state") != "verified":
                raise Refusal(str(result.get("error", "public_catalog_not_confirmed")))
        except Exception:
            self.stop_runtime()
            raise
        self.checkpoint("public_catalog_verified", native_pending=False)
        return {"state": "verified", **selected.info()}

    def public_install(
        self, addon: str, version: str, digest: str, allow_services: str = "false"
    ) -> Document:
        selected = self.active_public()
        bootstrap = addon == REPOSITORY
        package = selected.repository if bootstrap else selected.target
        if (addon, version, digest) != (package.id, package.version, package.digest):
            raise Refusal("public_package_not_approved")
        if allow_services not in {"true", "false"} or (
            allow_services != "true"
            and package.node.find("extension[@point='xbmc.service']") is not None
        ):
            raise Refusal("service_activation_consent_required")
        if bootstrap:
            checksum, _ = self.packages.publish(package)
        else:
            if self.installed(addon) is None:
                raise Refusal("public_test_requires_installed_base")
            checksum = selected.checksum
        intent: dict[str, object] = {
            "addon": package.id,
            "version": package.version,
            "name": package.node.attrib["name"],
            "category": package.category,
            "replace": False,
            "files": package.members,
            "catalog": checksum,
            "dependencies": [],
        }
        try:
            if not bootstrap:
                self.verify(selected.repository, origin=REPOSITORY, exact=True)
                # The first workflow uses prerequisites already installed from
                # the existing private catalog.
                self.dependencies(package, self.packages.catalog())
                intent["repository"] = REPOSITORY
            previous_addons = self.addon_versions()
            response = self.install_on_estuary(intent)
            if response.get("error") or response.get("restore_error"):
                raise Refusal(str(response.get("error") or response["restore_error"]))
            # Kodi records a repository add-on as its own origin even when
            # the ZIP was installed from another native repository.
            verified = self.verify(package, origin=REPOSITORY, exact=True)
            self.verify_public_archive(package)
            previous_addons[package.id] = package.version
            if self.addon_versions() != previous_addons:
                raise Refusal("public_unexpected_addon_change")
            dependencies = (
                [] if bootstrap else self.dependencies(package, self.packages.catalog())
            )
            if response.get("prior_skin") != response.get("final_skin"):
                raise Refusal("skin_restore_unconfirmed")
            if not bootstrap:
                self.record_source(package.id, package.members)
        except Exception:
            self.stop_runtime()
            raise
        self.checkpoint("native_verified", native_pending=False)
        return {
            "state": "verified",
            "package": verified,
            "native": response,
            "dependencies": dependencies,
        }

    def status(self) -> Document:
        obj = self.runtime.inspect()
        status = (self.runtime.ready() if obj else None) or {
            "state": (
                "running_unconfirmed"
                if obj and obj["State"]["Running"]
                else "exited_unconfirmed"
                if obj
                else "stopped"
                if self.runtime.pointer.exists()
                else "unprepared"
            ),
        }
        previous = self.last_operation()
        if self.runtime.retired():
            status["retired"] = True
            if obj is None:
                status["state"] = "retired"
        status["generation"] = self.runtime.generation
        if self.runtime.generation == "matrix" and self.runtime.pointer.exists():
            try:
                major, minor = self.runtime.expected_release()
            except (Refusal, subprocess.CalledProcessError):
                pass
            else:
                status["profile_version"] = f"{major}.{minor}"
        status["available_generations"] = list(RELEASES)
        status["owner"] = self.owner()
        status["view_control"] = self.input_attempt is not None
        status["view_target"] = str(self.runtime.target)
        if self.actor == "admin":
            try:
                status["network_access_configured"] = self.omega.network_access()
            except Refusal:
                status["network_access_configured"] = None
            status["network_access_effective"] = (
                self.runtime.effective_network(obj)
                if obj and obj["State"]["Running"]
                else "stopped"
            )
        if self.runtime.public.session.exists():
            status["public_repository_test"] = True
        if previous and (self.actor == "admin" or previous.get("actor") == self.actor):
            status["operation"] = {
                key: previous[key]
                for key in (
                    "id",
                    "name",
                    "generation",
                    "state",
                    "subject",
                    "event",
                    "result",
                    "error",
                    "recovery_error",
                )
                if key in previous
            }
        return status

    def recover(self) -> Document:
        record = self.last_operation()
        if not record or record["state"] != "pending":
            return {"state": "no_recovery_required"}
        if record.get("generation", "omega") != self.runtime.generation:
            raise Refusal("managed_recovery_generation_mismatch")
        try:
            self.reconcile(record)
        except Refusal as exc:
            record["recovery_error"] = str(exc)
            self.write_operation(record)
            raise
        # No native install has been replayed or marked successful here.
        report = {
            "state": "recovered",
            "interrupted": record["name"],
            "completion": "unconfirmed",
            "native_verification_required": bool(record.get("native_pending")),
        }
        if "prepare" in record:
            committed = (
                self.runtime.pointer.exists()
                and self.runtime.read_pointer() == record.get("slot")
            )
            if committed:
                report["completion"] = "committed"
            self.finish_prepare(
                record, abandoned=not committed, event="recovered", result=report
            )
        else:
            record = {
                key: record[key]
                for key in ("id", "name", "actor", "subject", "prepare_cleanup")
                if key in record
            }
            record.setdefault("actor", "admin")
            record.update(
                generation=self.runtime.generation,
                event="recovered",
                state="complete",
                result=report,
            )
            self.write_operation(record)
        return report

    def reconcile(self, record: Document) -> None:
        native = self.runtime
        if "prepare" in record:
            self.require_prepare_recovery_receipt(record)
            if native.public.session.exists():
                self.stop_pending_public(record)
            self.reconcile_profile(record)
            return
        if native.public.session.exists():
            self.stop_runtime()
            if record.get("skin"):
                # Keep the recovered settings on the stopped disposable profile.
                # Restarting during recovery would restore network access.
                skin = record["skin"]
                if native.read_pointer() != skin["slot"]:
                    raise Refusal("skin_recovery_profile_mismatch")
                native.write_skin(skin["addon"], saved=skin["settings"])
                return
            if record["name"] != "managed_prepare" and "deploy" not in record:
                return
        if record.get("skin"):
            skin = record["skin"]
            if native.read_pointer() != skin["slot"]:
                raise Refusal("skin_recovery_profile_mismatch")
            native.stop()
            native.write_skin(skin["addon"], saved=skin["settings"])
            native.start_with_skin(skin["addon"])
        elif record["name"] == "managed_start":
            if not native.ready():
                native.stop()
        elif (
            record["name"] == "managed_stop"
            or record.get("native_pending")
            or record["event"] in {"runtime_quit_requested", "runtime_removed"}
        ):
            self.stop_runtime()
        elif "archive" in record:
            self.actor_snapshots(record.get("actor", "admin")).discard_incomplete(
                record
            )
        elif "slot" in record:
            self.reconcile_profile(record)
        elif "deploy" in record:
            self.recover_deploy(record["deploy"])

    def require_reconciled_volume(self, record: Document, slot: str) -> None:
        if slot == record.get("slot") and "slot_inode" in record:
            self.runtime.require_staged_volume(
                slot, record["slot_inode"], record.get("slot_allocation")
            )

    def reconcile_profile(self, record: Document) -> bool:
        slot = record.get("slot")
        if slot is None:
            return False
        native = self.runtime
        active = native.read_pointer() if native.pointer.exists() else None
        if active is not None and active == slot:
            if "slot_inode" in record:
                self.require_reconciled_volume(record, active)
            else:
                native.require_volume(active)
            # A visible rename can precede a failed directory sync. Keep the
            # prior profile until the selected pointer is durable.
            sync_dir(native.root)
            if "prepare" in record:
                receipt = record["prepare"]
                if native.image != receipt["image"]:
                    raise Refusal("prepare_profile_image_identity")
                PrepareResources(native, record["id"], receipt).publish_baseline()
            elif record.get("prior_slot"):
                native.retire(record["prior_slot"])
            return True
        # The allocation checkpoint must have survived before retirement.
        sync_dir(self.record.parent)
        native.retire_staged(
            slot, record.get("slot_inode"), record.get("slot_allocation")
        )
        return False

    def import_zip(self, encoded: str, digest: str) -> Document:
        if len(encoded) > 740_000:
            raise Refusal("inline_package_limit")
        try:
            data = base64.b64decode(encoded, validate=True)
        except binascii.Error as error:
            raise Refusal("bad_package_encoding") from error
        return self.admit_package(self.packages, data, digest)

    def upload_package(self, data: bytes, digest: str, *, actor: str) -> OperationReply:
        started = time.monotonic()
        with self.omega.exclusive():
            packages = Packages(self.actor_directory(actor), self.omega.feed)
            result = self.admit_package(packages, data, digest)
        return {
            "result": result,
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }

    @staticmethod
    def admit_package(packages: Packages, data: bytes, digest: str) -> Document:
        if sha(data) != digest:
            raise Refusal("package_digest_mismatch")
        pkg = packages.approve(data)
        return {"digest": pkg.digest, "addon": pkg.id, "version": pkg.version}

    def inventory(self) -> list[Document]:
        result: Document = self.runtime.rpc(
            "Addons.GetAddons", properties=["enabled", "version", "name"]
        )
        return [
            row
            for row in result["addons"]
            if row["addonid"].startswith(
                (
                    "script.",
                    "plugin.",
                    "resource.",
                    "skin.",
                    "service.",
                    REPO,
                    REPOSITORY,
                )
            )
        ]

    def installed(self, addon: str) -> Document | None:
        return next((x for x in self.inventory() if x["addonid"] == addon), None)

    def addon_versions(self) -> dict[str, str]:
        result: Document = self.runtime.rpc("Addons.GetAddons", properties=["version"])
        return {row["addonid"]: row["version"] for row in result["addons"]}

    def verify(
        self, pkg: Package, *, origin: str = REPO, exact: bool = False
    ) -> Document:
        current = self.installed(pkg.id)
        if not current or not current["enabled"] or current["version"] != pkg.version:
            raise Refusal("native_install_state_mismatch")
        root = self.runtime.profile() / "addons" / pkg.id
        tree = workspace_files(root)
        files = {
            name: sha(path.read_bytes())
            for name, path in tree
            if not name.endswith(".pyc")
        }
        if any(files.get(name) != content for name, content in pkg.members.items()):
            raise Refusal("installed_digest_mismatch")
        surplus = set(files) - pkg.members.keys()
        if exact and surplus:
            raise Refusal("installed_digest_mismatch")
        if surplus:
            # Generated files are distinguished from the caller-approved package bytes.
            current["generated_paths"] = sorted(surplus)
        self.verify_origin(pkg.id, expected=origin)
        return {
            "addon": pkg.id,
            "version": pkg.version,
            "digest": pkg.digest,
            "origin": origin,
            "enabled": True,
            "files_verified": len(pkg.members),
        }

    def verify_public_archive(self, package: Package) -> None:
        profile = self.runtime.profile()
        addons = profile / "addons"
        cache = addons / "packages"
        archive = cache / f"{package.id}-{package.version}.zip"
        if (
            any(path.is_symlink() for path in (addons, cache, archive))
            or not archive.is_file()
            or archive.stat().st_size != len(package.data)
            or sha(archive.read_bytes()) != package.digest
        ):
            raise Refusal("native_public_archive_mismatch")

    def verify_origin(self, addon: str, *, expected: str = REPO) -> None:
        userdata = self.runtime.profile() / "userdata"
        folder = userdata / "Database"
        if userdata.is_symlink() or folder.is_symlink():
            raise Refusal("native_database_unavailable")
        databases = list(
            (self.runtime.profile() / "userdata/Database").glob("Addons*.db")
        )
        if len(databases) != 1:
            raise Refusal("native_database_unavailable")
        (database,) = databases
        if database.is_symlink() or not database.is_file():
            raise Refusal("native_database_unavailable")
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=1) as db:
            origin = db.execute(
                "SELECT origin, enabled FROM installed WHERE addonID=?", (addon,)
            ).fetchone()
        if origin != (expected, 1):
            raise Refusal("native_origin_mismatch")

    def dependencies(
        self, package: Package, catalog: dict[str, Package]
    ) -> list[Document]:
        records = {}
        # Dependencies supplied by the pinned runtime's system manifest are
        # included in the immutable image rather than the curated ZIP feed.
        system = {
            entry.text
            for entry in ET.parse(self.runtime.material / "manifest.xml")
            .getroot()
            .findall("addon")
            if entry.get("optional") != "true"
        }
        seen = {package.id}

        def scan(parent: Package) -> None:
            for child in parent.node.findall("requires/import"):
                name = child.attrib["addon"]
                if name in seen or name in system:
                    continue
                seen.add(name)
                if not self.installed(name) and child.get("optional") == "true":
                    continue
                if name not in catalog:
                    raise Refusal("dependency_outside_approved_catalog")
                records[name] = self.verify(catalog[name])
                scan(catalog[name])

        scan(package)
        return list(records.values())

    def install(
        self, addon: str, version: str, digest: str, allow_services: str = "false"
    ) -> Document:
        addon_id(addon)
        pkg = self.packages.get(digest)
        if pkg.id != addon or pkg.version != version:
            raise Refusal("package_identity_mismatch")
        if pkg.category not in (
            "xbmc.python.script",
            "xbmc.python.pluginsource",
            "xbmc.gui.skin",
        ):
            raise Refusal("unsupported_root_extension")
        if not self.runtime.ready():
            raise Refusal("bridge_not_ready")
        old = self.installed(addon)
        if allow_services not in {"true", "false"}:
            raise Refusal("invalid_service_consent")
        catalog = self.packages.catalog()
        if allow_services == "false" and any(
            item.node.find("extension[@point='xbmc.service']") is not None
            for item in [pkg, *catalog.values()]
        ):
            raise Refusal("service_activation_consent_required")
        retirement = None
        if self._feed_engine_bound and self.runtime.feed == self.runtime.private_feed:
            try:
                engine = observed_engine(time.monotonic() + 0.5)
            except (Refusal, OSError, subprocess.SubprocessError):
                pass
            else:
                retirement = FeedRetirement(
                    self.omega.root, self.runtime.root, self.runtime.target, engine
                )
        checksum, catalog = self.packages.publish(
            pkg, retirement=retirement, owner=self.actor
        )
        allowed = {dep.node.attrib["name"] for dep in catalog.values()}
        allowed.update(
            dep.attrib["addon"]
            for p in catalog.values()
            for dep in p.node.findall("requires/import")
        )
        intent: dict[str, object] = {
            "addon": addon,
            "version": version,
            "category": pkg.category,
            "name": pkg.node.attrib["name"],
            "replace": bool(old and old["version"] == version),
            "files": pkg.members,
            "dependencies": sorted(allowed),
            "catalog": checksum,
        }
        response = self.install_on_estuary(intent)
        interrupted_by_ok = (
            old is None
            and response.get("prior_skin") == response.get("final_skin") == ESTUARY
            and response.get("error") == "unexpected_window_during_install:12002"
            and not response.get("restore_error")
        )
        if (
            response.get("error") or response.get("restore_error")
        ) and not interrupted_by_ok:
            if response.get("error") in {
                "missing_dependency",
                "unexpected_window_before_install",
            } and not response.get("restore_error"):
                self.checkpoint("native_refused", native_pending=False)
            return {"state": "failed", "native": response}
        try:
            record = self.verify(pkg)
            deps = self.dependencies(pkg, catalog)
        except Refusal as error:
            if interrupted_by_ok:
                return {
                    "state": "failed",
                    "verification_error": str(error),
                    "native": response,
                }
            raise
        if response.get("prior_skin") != response.get("final_skin"):
            raise Refusal("skin_restore_unconfirmed")
        self.record_source(pkg.id, pkg.members)
        self.checkpoint("native_verified", native_pending=False)
        result: Document = {
            "state": "verified",
            "package": record,
            "dependencies": deps,
            "native": response,
        }
        if interrupted_by_ok:
            # The native installer no longer handles input. Only verified
            # installs release the pending fence for explicit operator input.
            result["interaction"] = {
                "state": "dialog_observed",
                "window_id": 12002,
                "next_action": "inspect_native_gui",
            }
        return result

    def install_on_estuary(self, intent: dict[str, object]) -> Document:
        native = self.runtime
        prior = native.active_skin()
        if prior == ESTUARY and not intent["replace"]:
            reply = self.dispatch_native(intent)
            return {"prior_skin": prior, "final_skin": native.active_skin(), **reply}

        gui = native.rpc("GUI.GetProperties", properties=["currentwindow"])
        if gui["currentwindow"]["id"] not in (10000, 10040):
            return {
                "error": "unexpected_window_before_install",
                "prior_skin": prior,
                "final_skin": prior,
            }

        if prior == ESTUARY:
            # Kodi's in-process ZIP directory cache keys by path and whole-second
            # mtime. The native installer reuses the path for an equal version.
            # A clean restart discards that cache without changing the profile.
            native.stop()
            native.start_with_skin(ESTUARY)
            reply = self.dispatch_native(intent)
            return {
                "prior_skin": prior,
                "final_skin": native.active_skin(),
                **reply,
                "actions": ["restart_equal_version", *reply.get("actions", [])],
            }

        actions = []
        saved = None
        result: Document = {}
        try:
            native.stop()
            _, tree = native.skin_settings()
            saved = [
                ET_bytes(entry).decode()
                for entry in tree.getroot()
                if entry.get("id", "").startswith("lookandfeel.")
            ]
            self.checkpoint(
                "skin_saved",
                skin={"addon": prior, "slot": native.read_pointer(), "settings": saved},
            )
            native.write_skin(ESTUARY)
            native.start_with_skin(ESTUARY)
            actions.append("stop_edit_start:" + ESTUARY)
            self.checkpoint("estuary_active")
            result = self.dispatch_native(intent)
            actions.extend(result.pop("actions", []))
        except Exception as exc:
            result["error"] = (
                str(exc)
                if isinstance(exc, Refusal)
                else "native_install_outcome_unconfirmed"
            )
        if saved is not None:
            try:
                native.stop()
                native.write_skin(prior, saved=saved)
                native.start_with_skin(prior)
                actions.append("stop_edit_start:" + prior)
                self.checkpoint("skin_restored", skin=None)
            except Exception as exc:
                result["restore_error"] = (
                    str(exc) if isinstance(exc, Refusal) else "skin_restore_unconfirmed"
                )
        else:
            result["restore_error"] = "prior_skin_state_unconfirmed"
        try:
            skin: str | None = native.active_skin()
        except Exception:
            skin = None
        return {"prior_skin": prior, "final_skin": skin, "actions": actions, **result}

    def dispatch_native(self, intent: dict[str, object]) -> Document:
        self.checkpoint("native_dispatch", native_pending=True)
        result = self.runtime.signed("managed_v2_install", intent)
        self.checkpoint("native_reply")
        return result

    def gui(self) -> Document:
        result: Document = self.runtime.rpc(
            "GUI.GetProperties", properties=["currentwindow", "currentcontrol", "skin"]
        )
        return result

    def navigate(self, action: str) -> Document:
        try:
            remote = BrowserRemoteAction(action)
        except ValueError as exc:
            raise Refusal("invalid_navigation_action") from exc
        if remote not in NAVIGATION_ACTIONS:
            raise Refusal("invalid_navigation_action")
        method, params = fixed_request_mapping()[remote]
        self.runtime.rpc(method, **(params if isinstance(params, dict) else {}))
        return {"state": "dispatched"}

    def launch(self, addon: str) -> Document:
        addon_id(addon)
        row = self.installed(addon)
        if (
            not row
            or not row["enabled"]
            or row["type"] not in {"xbmc.python.script", "xbmc.python.pluginsource"}
        ):
            raise Refusal("program_not_enabled")
        self.runtime.rpc("Addons.ExecuteAddon", addonid=addon, wait=False)
        return {"state": "dispatched", "addon": addon, "version": row["version"]}

    def setting(self, addon: str, key: str, value: str) -> Document:
        addon_id(addon)
        if not self.installed(addon):
            raise Refusal("addon_not_installed")
        return self.runtime.signed(
            "managed_v2_setting", {"addon": addon, "key": key, "value": value}
        )

    def skin(self, addon: str) -> Document:
        addon_id(addon)
        row = self.installed(addon)
        if (
            not addon.startswith("skin.")
            or not row
            or row["type"] != "xbmc.gui.skin"
            or not row["enabled"]
        ):
            raise Refusal("skin_not_enabled")
        prior = self.runtime.active_skin()
        self.runtime.stop()
        _, tree = self.runtime.skin_settings()
        self.checkpoint(
            "skin_saved",
            skin={
                "addon": prior,
                "slot": self.runtime.read_pointer(),
                "settings": [
                    ET_bytes(entry).decode()
                    for entry in tree.getroot()
                    if entry.get("id", "").startswith("lookandfeel.")
                ],
            },
        )
        self.runtime.write_skin(addon)
        result = self.runtime.start_with_skin(addon)
        self.checkpoint("skin_changed", skin=None)
        return {"skin": result, "actions": ["stop_edit_start:" + addon]}

    def record_source(self, addon: str, files: SourceFiles) -> None:
        slot = self.runtime.read_pointer()
        root = self.runtime.root
        sources = read_sources(root, slot)
        sources[addon] = files
        write_sources(root, slot, sources)

    def deploy(
        self, addon: str, baseline: str | None = None, review: str | None = None
    ) -> Document:
        addon_id(addon)
        self.runtime.require_stopped()
        slot = self.runtime.read_pointer()
        src = self.packages.workspaces / addon
        files = dict(workspace_files(src))
        addons = self.runtime.profile() / "addons"
        dst = addons / addon
        if addons.is_symlink() or not dst.is_dir() or dst.is_symlink():
            raise Refusal("destination_not_installed")
        installed = dict(workspace_files(dst))
        candidate, category = manifest((src / "addon.xml").read_bytes())
        current, _ = manifest((dst / "addon.xml").read_bytes())
        if current.attrib["id"] != addon:
            raise Refusal("workspace_identity")
        version = candidate.attrib["version"]
        if (candidate.attrib["id"], version) != (addon, current.attrib["version"]):
            raise Refusal("workspace_identity")
        for tag in ("requires", "extension"):
            if [ET_bytes(x) for x in candidate.findall(tag)] != [
                ET_bytes(x) for x in current.findall(tag)
            ]:
                raise Refusal("native_install_required_for_manifest_change")

        previous = read_sources(self.runtime.root, slot).get(addon)
        if previous is None:
            if baseline is None:
                raise Refusal("source_provenance_required")
            package = self.packages.get(baseline)
            if (package.id, package.version) != (addon, version):
                raise Refusal("source_provenance_identity")
            self.verify_origin(addon)
            previous = package.members
        elif baseline is not None:
            package = self.packages.get(baseline)
            if (package.id, package.version, package.members) != (
                addon,
                version,
                previous,
            ):
                raise Refusal("source_provenance_identity")

        installed_hashes = {
            name: sha(path.read_bytes()) for name, path in installed.items()
        }
        if any(
            installed_hashes.get(name) != digest for name, digest in previous.items()
        ):
            raise Refusal("installed_source_changed")
        new_source = {name: sha(path.read_bytes()) for name, path in files.items()}
        additions = sorted(installed.keys() - previous.keys())
        preserved: dict[str, GeneratedInclude] = {}
        file_dirs = {
            p.as_posix() for name in files for p in PurePosixPath(name).parents
        }
        for name in additions:
            if (
                name in files
                or name in file_dirs
                or any(p.as_posix() in files for p in PurePosixPath(name).parents)
            ):
                raise Refusal("generated_source_conflict")
            path = installed[name]
            info = path.stat()
            mode = stat.S_IMODE(info.st_mode)
            if (
                category != "xbmc.gui.skin"
                or path.suffix != ".xml"
                or info.st_nlink != 1
                or mode not in (0o600, 0o644)
                or info.st_size > 1024**2
            ):
                raise Refusal("unexpected_runtime_addition")
            try:
                if ET.parse(path).getroot().tag != "includes":
                    raise Refusal("unexpected_runtime_addition")
            except ET.ParseError as error:
                raise Refusal("unexpected_runtime_addition") from error
            preserved[name] = {
                "digest": installed_hashes[name],
                "mode": mode,
                "bytes": info.st_size,
            }
        if len(preserved) > 32:
            raise Refusal("generated_review_limit")
        if preserved or review is not None:
            token = sha(
                json.dumps(
                    [
                        str(self.runtime.target),
                        slot,
                        self.actor,
                        addon,
                        previous,
                        new_source,
                        preserved,
                    ],
                    sort_keys=True,
                ).encode()
            )
            if review is None:
                return {
                    "state": "review_required",
                    "review": token,
                    "generated": preserved,
                }
            if not preserved or review != token:
                raise Refusal("generated_review_changed")

        stage = dst.with_name(".stage-" + uuid4().hex)
        backup = dst.with_name(".prior-" + uuid4().hex)
        self.checkpoint(
            "deploy_staging",
            deploy={
                "addon": addon,
                "slot": slot,
                "stage": stage.name,
                "backup": backup.name,
                "source": new_source,
                "generated": {name: installed_hashes[name] for name in preserved},
            },
        )
        stage.mkdir()
        try:
            for name, path in files.items():
                dest = stage / name
                dest.parent.mkdir(exist_ok=True, parents=True)
                dest.write_bytes(path.read_bytes())
            for name in preserved:
                dest = stage / name
                dest.parent.mkdir(exist_ok=True, parents=True)
                shutil.copyfile(installed[name], dest)
                dest.chmod(preserved[name]["mode"])
            if new_source | {name: installed_hashes[name] for name in preserved} != {
                name: sha(path.read_bytes()) for name, path in workspace_files(stage)
            }:
                raise Refusal("deployed_tree_mismatch")
            dst.rename(backup)
            try:
                stage.rename(dst)
            except Exception:
                backup.rename(dst)
                raise
            self.record_source(addon, new_source)
        finally:
            if stage.exists():
                shutil.rmtree(stage)
        shutil.rmtree(backup)
        return {
            "state": "applied",
            "files_verified": len(files) + len(preserved),
            "source_files": len(files),
            "generated_preserved": sorted(preserved),
            "version": version,
        }

    def recover_deploy(self, deploy: Document) -> None:
        self.runtime.require_stopped()
        if (
            deploy["slot"] != self.runtime.read_pointer()
            or not re.fullmatch(r"\.stage-[0-9a-f]{32}", deploy["stage"])
            or not re.fullmatch(r"\.prior-[0-9a-f]{32}", deploy["backup"])
        ):
            raise Refusal("deploy_recovery_identity")
        addons = self.runtime.profile() / "addons"
        dst = addons / addon_id(deploy["addon"])
        stage, backup = (addons / deploy[key] for key in ("stage", "backup"))
        if backup.exists():
            if not dst.exists():
                backup.rename(dst)
            else:
                if "source" in deploy:
                    addon = addon_id(deploy["addon"])
                    source = validate_sources({addon: deploy["source"]})[addon]
                    expected = source | deploy["generated"]
                    if expected != {
                        name: sha(path.read_bytes())
                        for name, path in workspace_files(dst)
                    }:
                        raise Refusal("deployed_tree_mismatch")
                    self.record_source(addon, source)
                shutil.rmtree(backup)
        if stage.exists():
            shutil.rmtree(stage)


def ET_bytes(value: ET.Element) -> bytes:
    return ET.tostring(value)
