"""Managed-only Narrow Bridge entry point, installed only in the Omega baseline."""

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from pathlib import Path

import narrow_bridge as bridge
import xbmc
import xbmcaddon

REPO = "repository.kodi-lab-v2"
REPO_LABEL = "Kodi Lab Managed Feed"
ESTUARY = "skin.estuary"
PRIVATE = Path("/run/kodi/managed-v2")
PROFILE = Path("/home/kodi/.kodi")
OPERATIONS = {
    "bridge_identity",
    "capture_screenshot",
    "managed_v2_install",
    "managed_v2_setting",
}
LOCK = threading.Lock()


class Refusal(Exception):
    pass


def need(condition, label):
    if not condition:
        raise Refusal(label)


class Native:
    def __init__(self):
        self.actions = []

    def rpc(self, method, **params):
        result = json.loads(
            xbmc.executeJSONRPC(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": method,
                        "params": params,
                    }
                )
            )
        )
        need("result" in result, "native_" + method)
        return result["result"]

    def gui(self):
        return self.rpc("GUI.GetProperties", properties=["currentwindow", "skin"])

    def window(self):
        return self.gui()["currentwindow"]["id"]

    def labels(self, *names):
        return self.rpc("XBMC.GetInfoLabels", labels=list(names))

    def act(self, name, command):
        self.actions.append(name)
        xbmc.executebuiltin(command, False)

    def input(self, action):
        self.actions.append(action)
        return self.rpc("Input." + action)

    def wait(self, check, label, seconds=8):
        stop = time.monotonic() + seconds
        while time.monotonic() < stop:
            if value := check():
                return value
            xbmc.sleep(100)
        raise Refusal(label)

    def focused(self, controls, label, secondary=None):
        items = [
            (ctrl, index, f"Container({ctrl}).ListItemAbsolute({index}).Label")
            for ctrl in controls
            for index in range(20)
        ]
        names = [k for _, _, k in items]
        if secondary is not None:
            names += [k + "2" for k in names]
        values = self.labels(*names)
        matches = [
            (c, i)
            for c, i, k in items
            if values[k] == label
            and (secondary is None or values[k + "2"] == secondary)
        ]
        need(len(matches) == 1, "native_candidate_not_unique")
        ctrl, index = matches[0]
        self.act("focus", f"SetFocus({ctrl},{index},absolute)")
        name = f"Container({ctrl}).ListItem.Label"
        keys = (name, name + "2") if secondary is not None else (name,)
        self.wait(
            lambda: (
                self.labels(*keys)
                == (
                    {name: label, name + "2": secondary}
                    if secondary is not None
                    else {name: label}
                )
            ),
            "focus_unconfirmed",
        )

    def confirm_dependencies(self, approved):
        if self.window() != 12000:
            return False
        names = [f"Container(6).ListItemAbsolute({index}).Label" for index in range(20)]
        heading, button = "Control.GetLabel(1)", "Control.GetLabel(5)"
        labels = self.labels(heading, button, *names)
        values = {labels[k] for k in names if labels[k]}
        need(
            labels[heading] == "The following additional add-ons will be installed"
            and labels[button] == "OK"
            and values
            and values <= approved,
            "unexpected_dependencies_dialog",
        )
        need(
            self.gui()["skin"]["id"] == ESTUARY and self.window() == 12000,
            "input_context_changed",
        )
        self.act("approve_dependencies", "SendClick(12000,5)")
        self.wait(lambda: self.window() != 12000, "dependency_consent_unconfirmed")
        return True

    def matches(self, intent):
        root = PROFILE / "addons" / intent["addon"]
        try:
            for name, sha in intent["files"].items():
                path = root / name
                if (
                    path.is_symlink()
                    or not path.is_file()
                    or hashlib.sha256(path.read_bytes()).hexdigest() != sha
                ):
                    return False
            value = self.rpc(
                "Addons.GetAddonDetails",
                addonid=intent["addon"],
                properties=["enabled", "version"],
            )["addon"]
            return value["enabled"] and value["version"] == intent["version"]
        except (OSError, Refusal):
            return False

    def catalog_ready(self, checksum):
        dbs = list((PROFILE / "userdata/Database").glob("Addons*.db"))
        if len(dbs) != 1:
            return False
        try:
            with sqlite3.connect(
                dbs[0].as_uri() + "?mode=ro&timeout=0.1", uri=True, timeout=0.1
            ) as db:
                result = db.execute(
                    "SELECT checksum FROM repo WHERE addonID=?", (REPO,)
                ).fetchone()
                return result is not None and result[0] == checksum
        except sqlite3.OperationalError:
            return False

    def install(self, intent):
        self.validate(intent)
        need(self.window() in (10000, 10040), "unexpected_window_before_install")
        need(
            self.rpc("Settings.GetSettingValue", setting="general.addonupdates")[
                "value"
            ]
            == 2,
            "automatic_updates_enabled",
        )
        need(self.gui()["skin"]["id"] == ESTUARY, "estuary_required")
        error = None
        try:
            self.repository_install(intent)
        except (Refusal, OSError, TimeoutError) as exc:
            error = (
                str(exc) if isinstance(exc, Refusal) else "native_observation_failed"
            )
        return {"actions": self.actions, "error": error}

    def repository_install(self, intent):
        self.act("refresh", "UpdateAddonRepos")
        self.wait(
            lambda: self.catalog_ready(intent["catalog"]), "catalog_not_confirmed", 12
        )
        folder = "addons://" + REPO + "/" + intent["category"]
        self.act("repository", f"ActivateWindow(10040,{folder},return)")
        self.wait(
            lambda: (
                self.window() == 10040
                and self.labels("Container.FolderPath")["Container.FolderPath"].rstrip(
                    "/"
                )
                == folder
            ),
            "repository_context_unconfirmed",
        )
        self.focused((50, 52, 55), intent["name"])
        verify = self.labels(
            "ListItem.Label", "ListItem.Property(Addon.ID)", "ListItem.FileNameAndPath"
        )
        need(
            self.window() == 10040
            and verify
            == {
                "ListItem.Label": intent["name"],
                "ListItem.Property(Addon.ID)": intent["addon"],
                "ListItem.FileNameAndPath": "addons://" + REPO + "/" + intent["addon"],
            },
            "repository_item_mismatch",
        )
        self.input("Info")
        self.wait(lambda: self.window() == 10146, "addon_info_unconfirmed")
        log = PROFILE / "temp/kodi.log"
        offset = log.stat().st_size if log.exists() else 0
        if intent["replace"]:
            fields = self.labels("ListItem.Property(Addon.ID)", "Control.GetLabel(14)")
            need(
                fields["ListItem.Property(Addon.ID)"] == intent["addon"]
                and fields["Control.GetLabel(14)"].startswith("Versions"),
                "versions_context_unconfirmed",
            )
            self.act("versions", "SendClick(10146,14)")
            self.wait(lambda: self.window() == 12000, "versions_dialog_unconfirmed")
            need(
                self.labels("Control.GetLabel(1)")["Control.GetLabel(1)"]
                == "Select version",
                "unexpected_versions_dialog",
            )
            self.focused((6,), "Version " + intent["version"], REPO_LABEL)
            need(
                self.window() == 12000
                and self.labels("Control.GetLabel(1)")["Control.GetLabel(1)"]
                == "Select version",
                "versions_context_changed",
            )
            self.input("Select")
        else:
            fields = self.labels("ListItem.Property(Addon.ID)", "Control.GetLabel(6)")
            need(
                fields["ListItem.Property(Addon.ID)"] == intent["addon"]
                and fields["Control.GetLabel(6)"]
                in {"Install", "Install ( )", "Update", "Update ( )"},
                "install_button_unconfirmed",
            )
            self.act("install", "SendClick(10146,6)")
        consent = False
        quiet_since = None

        def finished():
            nonlocal consent, quiet_since
            view = self.window()
            if view == 12000 and not consent:
                fields = self.labels("Control.GetLabel(1)")
                if fields["Control.GetLabel(1)"] == "Select version":
                    return False
                consent = self.confirm_dependencies(set(intent["dependencies"]))
            elif view == 10100:
                h, b, no = (
                    "Control.GetLabel(1)",
                    "Control.GetLabel(9)",
                    "Control.GetLabel(10)",
                )
                msg = self.labels(h, b, no)
                if not all(msg.values()):
                    return False
                need(
                    intent["category"] == "xbmc.gui.skin"
                    and "Would you like to switch to this skin?" in msg[b]
                    and msg[no] == "No",
                    "unexpected_install_dialog",
                )
                self.act("remain_in_estuary", "SendClick(10100,10)")
                self.wait(lambda: self.window() != 10100, "skin_offer_pending")
            elif view in (10138, 10160):
                # Known native busy dialogs require observation only.
                quiet_since = None
                return False
            elif view not in (10000, 10040, 10146):
                raise Refusal(f"unexpected_window_during_install:{view}")
            if log.exists():
                with log.open("rb") as source:
                    source.seek(offset)
                    tail = source.read(256 * 1024).decode(errors="replace")
                if any(
                    intent["addon"] in line
                    and (
                        "failed to find dependency" in line
                        or "The dependency on" in line
                    )
                    for line in tail.splitlines()
                ):
                    raise Refusal("missing_dependency")
            if view not in (10000, 10040) or not self.matches(intent):
                quiet_since = None
                return False
            # A skin install queues its activation offer after making files visible.
            if intent["category"] == "xbmc.gui.skin":
                quiet_since = quiet_since or time.monotonic()
                return time.monotonic() - quiet_since >= 0.6
            return True

        self.wait(finished, "installed_bytes_unconfirmed", 16)

    @staticmethod
    def validate(intent):
        need(
            set(intent)
            == {
                "addon",
                "version",
                "name",
                "category",
                "replace",
                "files",
                "dependencies",
                "catalog",
            },
            "invalid_install_intent",
        )
        need(
            bool(re.fullmatch(r"[a-z][a-z0-9_.-]{1,95}", intent["addon"]))
            and bool(re.fullmatch(r"[0-9][a-zA-Z0-9.+~-]{0,39}", intent["version"]))
            and intent["category"]
            in ("xbmc.python.script", "xbmc.python.pluginsource", "xbmc.gui.skin"),
            "invalid_install_candidate",
        )
        need(
            type(intent["replace"]) is bool
            and len(intent["files"]) <= 3000
            and len(intent["dependencies"]) <= 64
            and len(intent["name"]) <= 128,
            "install_bounds",
        )
        for key, value in intent["files"].items():
            name = Path(key)
            need(
                not name.is_absolute()
                and ".." not in name.parts
                and bool(re.fullmatch("[0-9a-f]{64}", value)),
                "invalid_install_manifest",
            )


class ManagedHandler(bridge.Handler):
    def do_GET(self):
        self._empty(404)

    def do_POST(self):
        if self.path != bridge.ENDPOINT:
            self._empty(404)
            return
        super().do_POST()

    def _authenticated(self, document, request_hash):
        if document.get("operation") not in OPERATIONS:
            raise ValueError
        super()._authenticated(document, request_hash)

    def _deployment_response(self, request, request_hash, secret, state):
        if not LOCK.acquire(blocking=False):
            result = {"error": "bridge_busy"}
        else:
            try:
                native = Native()
                if request["operation"] == "managed_v2_install":
                    result = native.install(request["payload"])
                elif request["operation"] == "managed_v2_setting":
                    value = request["payload"]
                    need(
                        set(value) == {"addon", "key", "value"}
                        and bool(
                            re.fullmatch(
                                r"(script|plugin|skin)\.[a-z0-9_.-]+", value["addon"]
                            )
                        )
                        and bool(re.fullmatch(r"[a-zA-Z0-9_.-]{1,64}", value["key"]))
                        and isinstance(value["value"], str)
                        and len(value["value"]) <= 256,
                        "invalid_setting_intent",
                    )
                    addon = xbmcaddon.Addon(value["addon"])
                    addon.setSetting(value["key"], value["value"])
                    result = {
                        "state": "verified"
                        if addon.getSetting(value["key"]) == value["value"]
                        else "unconfirmed"
                    }
            except (Refusal, OSError, TimeoutError) as exc:
                result = {
                    "error": str(exc)
                    if isinstance(exc, Refusal)
                    else "native_observation_failed"
                }
            finally:
                LOCK.release()
        self._json_response(request, request_hash, secret, result, cache=False)


def run():
    os.umask(0o077)
    PRIVATE.mkdir(mode=0o700)
    bridge.STATE_PATH = str(PRIVATE / "state.json")
    bridge.ONBOARDING_PATH = str(PRIVATE / "onboarding.json")
    Path(bridge.STATE_PATH).write_bytes(
        Path("/managed-v2-credentials/state.json").read_bytes()
    )
    bridge.MAX_REQUEST = 1024 * 1024
    bridge.OPERATIONS = OPERATIONS
    bridge.REPLAY_EVICTABLE_OPERATIONS = OPERATIONS
    bridge.CAPABILITIES = sorted(OPERATIONS - {"bridge_identity"})
    bridge.BUILD_FINGERPRINT = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    monitor = xbmc.Monitor()
    with bridge.Server(("0.0.0.0", 8765), ManagedHandler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            while not monitor.waitForAbort(0.4):
                pass
        finally:
            server.shutdown()
            thread.join(timeout=2)


if __name__ == "__main__":
    run()
