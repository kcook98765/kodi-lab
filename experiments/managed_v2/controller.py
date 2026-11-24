"""Closed managed operation surface shared with the experimental MCP transport."""

from __future__ import annotations

import base64
import binascii
import json
import re
import shutil
import sqlite3
import time
from uuid import uuid4

from .packages import Packages, addon_id, manifest, sha, workspace_files
from .protocol import READ, Refusal, validate
from .runtime import ESTUARY, REPO, Runtime, atomic
from .snapshots import Snapshots


class Controller:
    def __init__(self, root, *, interrupt=None):
        self.runtime = Runtime(root, self.checkpoint)
        self.record = self.runtime.root / "operation.json"
        self.current = None
        self.interrupt = interrupt or (lambda _event: None)
        self.packages = Packages(self.runtime.root, self.runtime.feed)
        self.snapshots = Snapshots(self.runtime, self.checkpoint)
        self.actions = {
            "managed_prepare": self.runtime.prepare,
            "managed_start": self.runtime.start,
            "managed_stop": self.runtime.stop,
            "managed_reset": self.runtime.reset,
            "managed_status": self.status,
            "managed_recover": self.recover,
            "snapshot_list": self.snapshots.list,
            "snapshot_save": self.snapshots.save,
            "snapshot_load": self.snapshots.load,
            "snapshot_delete": self.snapshots.delete,
            "workspace_package": self.packages.workspace_package,
            "addon_install": self.install,
            "workspace_deploy": self.deploy,
            "package_import": self.import_zip,
            "addon_launch": self.launch,
            "addon_setting": self.setting,
            "addon_inventory": self.inventory,
            "managed_skin": self.skin,
        }

    def last_operation(self):
        return json.loads(self.record.read_text()) if self.record.exists() else None

    def checkpoint(self, event, **data):
        if self.current is not None:
            self.current.update(event=event, **data)
            atomic(self.record, json.dumps(self.current).encode())
            self.interrupt(event)

    def perform(self, name, args, *, write):
        started = time.monotonic()
        if name not in READ and not write:
            raise Refusal("managed_write_denied")
        with self.runtime.exclusive():
            validate(name, args)
            previous = self.last_operation()
            pending = previous if previous and previous["state"] == "pending" else None
            if name in READ or name == "managed_recover":
                result = self.actions[name](**args)
            elif pending and pending.get("recovery_error") and name == "managed_stop":
                # A failed skin recovery can leave the fallback booted.
                result = self.runtime.stop()
            else:
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
                    if name == "workspace_package":
                        result = {
                            "digest": result.digest,
                            "addon": result.id,
                            "version": result.version,
                        }
                    if not (
                        self.current.get("native_pending") or self.current.get("skin")
                    ):
                        self.checkpoint("reply", state="complete", result=result)
                except Refusal as exc:
                    if self.current["event"] == "begin":
                        self.checkpoint("refused", state="complete", error=str(exc))
                    raise
                finally:
                    self.current = None
        return {
            "result": result,
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }

    def status(self):
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
        if previous:
            status["operation"] = {
                key: previous[key]
                for key in (
                    "id",
                    "name",
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

    def recover(self):
        record = self.last_operation()
        if not record or record["state"] != "pending":
            return {"state": "no_recovery_required"}
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
                    "subject": record["subject"],
                    "event": "recovered",
                    "state": "complete",
                    "result": report,
                }
            ).encode(),
        )
        return report

    def reconcile(self, record):
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
            self.snapshots.discard_incomplete(record)
        elif "slot" in record:
            active = native.read_pointer() if native.pointer.exists() else None
            if active == record["slot"]:
                native.require_volume(active)
                if record.get("prior_slot"):
                    native.retire(record["prior_slot"])
            else:
                native.retire(record["slot"])
        elif "deploy" in record:
            self.recover_deploy(record["deploy"])

    def import_zip(self, encoded, digest):
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

    def inventory(self):
        result = self.runtime.rpc(
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
                    "service.kodi_lab_bridge",
                    REPO,
                )
            )
        ]

    def installed(self, addon):
        return next((x for x in self.inventory() if x["addonid"] == addon), None)

    def verify(self, pkg):
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

    def dependencies(self, package, catalog):
        records = {}
        seen = {package.id}

        def scan(parent):
            for child in parent.node.findall("requires/import"):
                name = child.attrib["addon"]
                if name in seen or name.startswith("xbmc."):
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

    def install(self, addon, version, digest):
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
        checksum, catalog = self.packages.publish(pkg)
        allowed = {dep.node.attrib["name"] for dep in catalog.values()}
        allowed.update(
            dep.attrib["addon"]
            for p in catalog.values()
            for dep in p.node.findall("requires/import")
        )
        intent = {
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

    def install_on_estuary(self, intent):
        native = self.runtime
        prior = native.active_skin()
        if prior == ESTUARY:
            result = self.dispatch_native(intent)
            return {"prior_skin": prior, "final_skin": native.active_skin(), **result}

        gui = native.rpc("GUI.GetProperties", properties=["currentwindow"])
        if gui["currentwindow"]["id"] not in (10000, 10040):
            return {
                "error": "unexpected_window_before_install",
                "prior_skin": prior,
                "final_skin": prior,
            }

        actions = []
        saved = None
        result = {}
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
            skin = native.active_skin()
        except Exception:
            skin = None
        return {"prior_skin": prior, "final_skin": skin, "actions": actions, **result}

    def dispatch_native(self, intent):
        self.checkpoint("native_dispatch", native_pending=True)
        result = self.runtime.signed("managed_v2_install", intent)
        self.checkpoint("native_reply")
        return result

    def launch(self, addon):
        addon_id(addon)
        row = self.installed(addon)
        if not row or not row["enabled"] or row["type"] != "xbmc.python.script":
            raise Refusal("program_not_enabled")
        self.runtime.rpc("Addons.ExecuteAddon", addonid=addon, wait=False)
        return {"state": "dispatched", "addon": addon, "version": row["version"]}

    def setting(self, addon, key, value):
        addon_id(addon)
        if not self.installed(addon):
            raise Refusal("addon_not_installed")
        return self.runtime.signed(
            "managed_v2_setting", {"addon": addon, "key": key, "value": value}
        )

    def skin(self, addon):
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

    def deploy(self, addon):
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

    def recover_deploy(self, deploy):
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


def ET_bytes(value):
    from xml.etree import ElementTree as ET

    return ET.tostring(value)
