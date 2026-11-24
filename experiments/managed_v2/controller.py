"""Closed managed operation surface shared with the experimental MCP transport."""

from __future__ import annotations

import base64
import binascii
import shutil
import sqlite3
import time
from uuid import uuid4

from .packages import Packages, addon_id, manifest, sha, workspace_files
from .runtime import ESTUARY, REPO, Refusal, Runtime, atomic
from .snapshots import Snapshots


class Controller:
    def __init__(self, root):
        self.runtime = Runtime(root)
        self.packages = Packages(self.runtime.root, self.runtime.feed)
        self.snapshots = Snapshots(self.runtime)
        self.actions = {
            "managed_prepare": self.runtime.prepare,
            "managed_start": self.runtime.start,
            "managed_stop": self.runtime.stop,
            "managed_reset": self.runtime.reset,
            "managed_status": self.status,
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

    def perform(self, name, args, *, write):
        started = time.monotonic()
        if name not in self.actions:
            raise Refusal("unknown_tool")
        read = name in {"managed_status", "snapshot_list", "addon_inventory"}
        if not read and not write:
            raise Refusal("managed_write_denied")
        with self.runtime.exclusive():
            if (self.runtime.root / "uncertain").exists() and name not in {
                "managed_status",
                "managed_stop",
                "addon_inventory",
                "snapshot_list",
            }:
                raise Refusal("native_outcome_uncertain_stop_required")
            if name == "workspace_package":
                pkg = self.actions[name](**args)
                result = {"digest": pkg.digest, "addon": pkg.id, "version": pkg.version}
            else:
                result = self.actions[name](**args)
        return {
            "result": result,
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }

    def status(self):
        return self.runtime.ready() or {
            "state": (
                "running_unconfirmed"
                if self.runtime.inspect()
                else "stopped"
                if self.runtime.pointer.exists()
                else "unprepared"
            ),
            "uncertain": (self.runtime.root / "uncertain").exists(),
        }

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
        fence = self.runtime.root / "uncertain"
        atomic(fence, b"native install dispatched")
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
                fence.unlink()
            return {"state": "failed", "native": response}
        record = self.verify(pkg)
        deps = self.dependencies(pkg, catalog)
        if response.get("prior_skin") != response.get("final_skin"):
            raise Refusal("skin_restore_unconfirmed")
        fence.unlink()
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
            result = native.signed("managed_v2_install", intent)
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
            native.stop(clear_fence=False)
            _, tree = native.skin_settings()
            saved = [
                ET_bytes(entry)
                for entry in tree.getroot()
                if entry.get("id", "").startswith("lookandfeel.")
            ]
            native.write_skin(ESTUARY)
            native.start_with_skin(ESTUARY)
            actions.append("stop_edit_start:" + ESTUARY)
            result = native.signed("managed_v2_install", intent)
            actions.extend(result.pop("actions", []))
        except Exception as exc:
            result["error"] = (
                str(exc)
                if isinstance(exc, Refusal)
                else "native_install_outcome_unconfirmed"
            )
        if saved is not None:
            try:
                native.stop(clear_fence=False)
                native.write_skin(prior, saved=saved)
                native.start_with_skin(prior)
                actions.append("stop_edit_start:" + prior)
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
        fence = self.runtime.root / "uncertain"
        atomic(fence, b"native skin change dispatched")
        self.runtime.stop(clear_fence=False)
        self.runtime.write_skin(addon)
        result = self.runtime.start_with_skin(addon)
        fence.unlink()
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


def ET_bytes(value):
    from xml.etree import ElementTree as ET

    return ET.tostring(value)
