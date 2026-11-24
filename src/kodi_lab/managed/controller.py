"""Closed managed operation surface shared with the experimental MCP transport."""

from __future__ import annotations

import base64
import binascii
import json
import re
import shutil
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4
from xml.etree import ElementTree as ET

from kodi_lab.kodi.remote import (
    NAVIGATION_ACTIONS,
    BrowserRemoteAction,
    fixed_request_mapping,
)

from . import RELEASES, reviewed_image_file
from .packages import Package, Packages, addon_id, manifest, sha, workspace_files
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
from .runtime import ESTUARY, REPO, Runtime, atomic
from .snapshots import Snapshots


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
                )
        self.input_attempt: UUID | None = None
        self.input_revoked = False
        self.record = self.runtime.root / "operation.json"
        self.current: Document | None = None
        self.interrupt = interrupt or (lambda _event: None)
        self.actor = "admin"
        self.lease_path = self.runtime.root / "active-owner"
        self.agents = self.runtime.root / "agents"
        self.agents.mkdir(mode=0o700, exist_ok=True)
        self.actions: dict[str, Callable[..., object]] = {
            "managed_prepare": lambda: self.runtime.prepare(),
            "managed_start": lambda: self.runtime.start(),
            "managed_stop": lambda: self.runtime.stop(),
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
        self.claim("admin")
        self.input_attempt = attempt
        self.input_revoked = False

    def finish_input(self, attempt: UUID | None, terminated: bool) -> None:
        if not terminated or attempt is None:
            return
        with self.omega.exclusive():
            if self.input_attempt == attempt:
                if self.owner() == "admin":
                    self.lease_path.unlink(missing_ok=True)
                self.input_attempt = None
                self.input_revoked = False

    def authorize_profile(
        self, name: str, actor: str, pending: Document | None
    ) -> Document | None:
        if name in ADMIN and actor != "admin":
            raise Refusal("managed_admin_required")
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
        if owner and owner != actor and (actor != "admin" or name not in READ):
            raise Refusal("managed_owned")
        if name == "managed_release":
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
        return (
            cast(Document, json.loads(self.record.read_text()))
            if self.record.exists()
            else None
        )

    def checkpoint(self, event: str, **data: object) -> None:
        if self.current is not None:
            self.current.update(event=event, **data)
            atomic(self.record, json.dumps(self.current).encode())
            self.interrupt(event)

    def perform(self, name: str, args: dict[str, str], *, actor: str) -> OperationReply:
        started = time.monotonic()
        actor_key(actor)
        with self.omega.exclusive():
            _, args = validate(name, args)
            arguments = dict(args)
            generation = arguments.pop("generation", "omega")
            runtime = self.runtimes.get(generation)
            if runtime is None:
                raise Refusal("managed_generation_unavailable")
            self.runtime = runtime
            self.actor = actor
            try:
                result = self.perform_locked(name, arguments)
            finally:
                self.actor = "admin"
                self.runtime = self.omega
        return {
            "result": result,
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }

    def perform_locked(self, name: str, args: dict[str, str]) -> object:
        previous = self.last_operation()
        pending = previous if previous and previous["state"] == "pending" else None
        lease_result = self.authorize_profile(name, self.actor, pending)
        if lease_result is not None:
            return lease_result
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
            return self.runtime.stop()
        if pending:
            if not (
                pending.get("recovery_error")
                and name in {"managed_reset", "snapshot_load"}
            ):
                raise Refusal("managed_recovery_required")
            self.runtime.remove_exited()
        self.current = {
            "id": uuid4().hex,
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
        try:
            self.checkpoint("begin")
            result = self.actions[name](**args)
            if not (self.current.get("native_pending") or self.current.get("skin")):
                self.checkpoint("reply", state="complete", result=result)
            return result
        except Refusal as exc:
            if self.current["event"] == "begin":
                self.checkpoint("refused", state="complete", error=str(exc))
            raise
        finally:
            self.current = None

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
        status["generation"] = self.runtime.generation
        status["available_generations"] = list(RELEASES)
        status["owner"] = self.owner()
        status["view_control"] = self.input_attempt is not None
        status["view_target"] = str(self.runtime.target)
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
            atomic(self.record, json.dumps(record).encode())
            raise
        # No native install has been replayed or marked successful here.
        report = {
            "state": "recovered",
            "interrupted": record["name"],
            "completion": "unconfirmed",
            "native_verification_required": bool(record.get("native_pending")),
        }
        atomic(
            self.record,
            json.dumps(
                {
                    "id": record["id"],
                    "name": record["name"],
                    "generation": self.runtime.generation,
                    "actor": record.get("actor", "admin"),
                    "subject": record["subject"],
                    "event": "recovered",
                    "state": "complete",
                    "result": report,
                }
            ).encode(),
        )
        return report

    def reconcile(self, record: Document) -> None:
        native = self.runtime
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
            native.stop()
        elif "archive" in record:
            self.actor_snapshots(record.get("actor", "admin")).discard_incomplete(
                record
            )
        elif "slot" in record:
            active = native.read_pointer() if native.pointer.exists() else None
            if active is not None and active == record["slot"]:
                native.require_volume(active)
                if record.get("prior_slot"):
                    native.retire(record["prior_slot"])
            else:
                native.retire(record["slot"])
        elif "deploy" in record:
            self.recover_deploy(record["deploy"])

    def import_zip(self, encoded: str, digest: str) -> Document:
        if len(encoded) > 740_000:
            raise Refusal("inline_package_limit")
        try:
            data = base64.b64decode(encoded, validate=True)
        except binascii.Error as error:
            raise Refusal("bad_package_encoding") from error
        if sha(data) != digest:
            raise Refusal("package_digest_mismatch")
        pkg = self.packages.approve(data)
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
                )
            )
        ]

    def installed(self, addon: str) -> Document | None:
        return next((x for x in self.inventory() if x["addonid"] == addon), None)

    def verify(self, pkg: Package) -> Document:
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
        if surplus:
            # Generated files are distinguished from the caller-approved package bytes.
            current["generated_paths"] = sorted(surplus)
        (database,) = (self.runtime.profile() / "userdata/Database").glob("Addons*.db")
        if database.is_symlink() or not database.is_file():
            raise Refusal("native_database_unavailable")
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=1) as db:
            origin = db.execute(
                "SELECT origin, enabled FROM installed WHERE addonID=?", (pkg.id,)
            ).fetchone()
        if origin != (REPO, 1):
            raise Refusal("native_origin_mismatch")
        return {
            "addon": pkg.id,
            "version": pkg.version,
            "digest": pkg.digest,
            "origin": REPO,
            "enabled": True,
            "files_verified": len(pkg.members),
        }

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
        checksum, catalog = self.packages.publish(pkg)
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
        if response.get("error") or response.get("restore_error"):
            if response.get("error") in {
                "missing_dependency",
                "unexpected_window_before_install",
            } and not response.get("restore_error"):
                self.checkpoint("native_refused", native_pending=False)
            return {"state": "failed", "native": response}
        record = self.verify(pkg)
        deps = self.dependencies(pkg, catalog)
        if response.get("prior_skin") != response.get("final_skin"):
            raise Refusal("skin_restore_unconfirmed")
        self.checkpoint("native_verified", native_pending=False)
        return {
            "state": "verified",
            "package": record,
            "dependencies": deps,
            "native": response,
        }

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

    def deploy(self, addon: str) -> Document:
        addon_id(addon)
        self.runtime.require_stopped()
        src = self.packages.workspaces / addon
        files = workspace_files(src)
        dst = self.runtime.profile() / "addons" / addon
        if not dst.is_dir() or dst.is_symlink():
            raise Refusal("destination_not_installed")
        workspace_files(dst)
        candidate, _ = manifest((src / "addon.xml").read_bytes())
        current, _ = manifest((dst / "addon.xml").read_bytes())
        if (candidate.attrib["id"], candidate.attrib["version"]) != (
            addon,
            current.attrib["version"],
        ):
            raise Refusal("workspace_identity")
        for tag in ("requires", "extension"):
            if [ET_bytes(x) for x in candidate.findall(tag)] != [
                ET_bytes(x) for x in current.findall(tag)
            ]:
                raise Refusal("native_install_required_for_manifest_change")
        stage = dst.with_name(".stage-" + uuid4().hex)
        backup = dst.with_name(".prior-" + uuid4().hex)
        self.checkpoint(
            "deploy_staging",
            deploy={
                "addon": addon,
                "slot": self.runtime.read_pointer(),
                "stage": stage.name,
                "backup": backup.name,
            },
        )
        stage.mkdir()
        try:
            hashes = {}
            for name, path in files:
                dest = stage / name
                dest.parent.mkdir(exist_ok=True, parents=True)
                data = path.read_bytes()
                hashes[name] = sha(data)
                dest.write_bytes(data)
            assert hashes == {
                name: sha(path.read_bytes()) for name, path in workspace_files(stage)
            }
            dst.rename(backup)
            try:
                stage.rename(dst)
            except Exception:
                backup.rename(dst)
                raise
        finally:
            if stage.exists():
                shutil.rmtree(stage)
        shutil.rmtree(backup)
        return {
            "state": "applied",
            "files_verified": len(files),
            "version": candidate.attrib["version"],
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
                shutil.rmtree(backup)
        if stage.exists():
            shutil.rmtree(stage)


def ET_bytes(value: ET.Element) -> bytes:
    return ET.tostring(value)
