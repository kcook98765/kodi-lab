#!/usr/bin/env python3
"""Exercise the shipped installer in disposable trees, without contacting Kodi.

Run with native Windows Python 3.8+ for Win32 ABI/filesystem coverage. Kodi APIs
are doubles; this does not establish live Kodi inventory or workstation acceptance.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import runpy
import subprocess
import sys
import tempfile
import types
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch


class DirectInstallIntegration(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="kodi-lab-direct-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.addons = self.root / "addons"
        self.addons.mkdir()
        self.state_path = self.root / "state.json"
        self.source = self.root / "source"
        self.destination = self.addons / "plugin.video.fixture"
        self.enabled = True
        for root, version in ((self.source, "2.0.0"), (self.destination, "1.0.0")):
            root.mkdir()
            (root / "addon.xml").write_text(
                '<addon id="plugin.video.fixture" version="' + version + '">'
                '<extension point="xbmc.python.pluginsource" library="addon.py">'
                "<provides>video</provides></extension></addon>",
                encoding="utf-8",
            )
            (root / "addon.py").write_text(
                "print('" + version + "')\n", encoding="utf-8"
            )

        def rpc(raw):
            request = json.loads(raw)
            if request["method"] == "Addons.SetAddonEnabled":
                self.enabled = request["params"]["enabled"]
                result = "OK"
            else:
                import xml.etree.ElementTree as ET

                version = (
                    ET.parse(self.destination / "addon.xml").getroot().attrib["version"]
                )
                result = {
                    "addon": {
                        "addonid": "plugin.video.fixture",
                        "enabled": self.enabled,
                        "version": version,
                    }
                }
            return json.dumps({"id": "kodi-lab-bridge", "result": result})

        modules = {}
        for name in ("xbmc", "xbmcaddon", "xbmcvfs"):
            modules[name] = types.ModuleType(name)
        modules["xbmc"].executeJSONRPC = rpc
        modules["xbmc"].log = lambda *_args: None
        modules["xbmc"].LOGERROR = 2
        modules["xbmcvfs"].translatePath = lambda value: str(
            self.addons if value == "special://home/addons" else self.state_path
        )
        modules["xbmcvfs"].exists = os.path.exists
        modules["xbmcvfs"].mkdirs = lambda path: os.makedirs(path, exist_ok=True)

        class VfsFile:
            def __init__(self, path, mode="rb"):
                self.stream = open(path, mode)  # noqa: SIM115 - Kodi-shaped double.

            def read(self):
                return self.stream.read()

            def write(self, content):
                return self.stream.write(content)

            def close(self):
                self.stream.close()

        modules["xbmcvfs"].File = VfsFile
        module_patch = patch.dict(sys.modules, modules)
        module_patch.start()
        self.addCleanup(module_patch.stop)
        addon = Path(__file__).resolve().parents[1] / "bridge/service.kodi_lab_bridge"
        sys.path.insert(0, str(addon))
        self.addCleanup(sys.path.remove, str(addon))
        self.bridge = runpy.run_path(str(addon / "narrow_bridge.py"))
        self.state = self.bridge["_load_state"]()
        self.handler = object.__new__(self.bridge["Handler"])

    def stage(self, transfer=True, executable=False):
        entries, digest, files, total = self.bridge["_tree_manifest"](str(self.source))
        if executable:
            for entry in entries:
                if entry["path"] == "addon.py":
                    entry["mode"] = 0o755
            digest = (
                "sha256:"
                + hashlib.sha256(self.bridge["_manifest_bytes"](entries)).hexdigest()
            )
        identity = str(uuid.uuid4())
        payload = {
            "addon_id": "plugin.video.fixture",
            "addon_version": "2.0.0",
            "deployment_id": identity,
            "deployment_request_id": str(uuid.uuid4()),
            "entries": entries,
            "file_count": files,
            "generation": 6,
            "manifest_digest": digest,
            "manifest_version": 1,
            "total_bytes": total,
        }
        self.handler._deployment_begin(payload, self.state)
        if transfer:
            for entry in entries:
                if entry["type"] != "file":
                    continue
                content = (self.source / entry["path"]).read_bytes()
                self.handler._deployment_chunk(
                    {
                        "content": base64.urlsafe_b64encode(content)
                        .rstrip(b"=")
                        .decode("ascii"),
                        "content_digest": hashlib.sha256(content).hexdigest(),
                        "deployment_id": identity,
                        "file_digest": entry["sha256"],
                        "offset": 0,
                        "path": entry["path"],
                    },
                    self.state,
                )
        return {"deployment_id": identity, "manifest_digest": digest}

    def test_install_restart_and_receipt_replay(self):
        payload = self.stage()
        first = self.handler._deployment_commit(payload, self.state)
        self.assertEqual(first["state"], "succeeded")
        self.assertTrue(first["inventory_verified"])
        state = self.bridge["_load_state"]()
        restarted = object.__new__(self.bridge["Handler"])
        self.assertEqual(first, restarted._deployment_commit(payload, state))
        self.assertEqual(
            (self.destination / "addon.py").read_bytes(),
            (self.source / "addon.py").read_bytes(),
        )

    def test_partial_tree_never_succeeds(self):
        payload = self.stage(transfer=False)
        with self.assertRaises(ValueError):
            self.handler._deployment_commit(payload, self.state)
        self.assertIn("1.0.0", (self.destination / "addon.py").read_text())

    def test_replacement_failure_restores_previous_tree(self):
        payload = self.stage()
        globals_ = self.handler._deployment_commit.__func__.__globals__
        original = globals_["_deployment_replace"]
        stage = self.bridge["_stage_path"](payload["deployment_id"])

        def replace(source, destination):
            if source == stage:
                raise PermissionError("simulated open-file sharing conflict")
            return original(source, destination)

        with patch.dict(globals_, _deployment_replace=replace):
            receipt = self.handler._deployment_commit(payload, self.state)
        self.assertEqual(receipt["state"], "failed")
        self.assertEqual(receipt["classification"], "replacement_failed_rolled_back")
        self.assertFalse(receipt["inventory_verified"])
        self.assertIn("1.0.0", (self.destination / "addon.py").read_text())
        self.assertTrue(self.enabled)

    def test_hardlink_rejected(self):
        outside = self.root / "outside"
        outside.write_bytes(b"unchanged")
        linked = self.source / "linked.py"
        os.link(outside, linked)
        with self.assertRaises(ValueError):
            self.bridge["_tree_manifest"](str(self.source))
        self.assertEqual(outside.read_bytes(), b"unchanged")


@unittest.skipUnless(os.name == "nt", "requires native Windows")
class WindowsDirectInstallIntegration(DirectInstallIntegration):
    def test_logical_executable_mode_survives_ntfs(self):
        receipt = self.handler._deployment_commit(
            self.stage(executable=True), self.state
        )
        self.assertEqual(receipt["state"], "succeeded")

    def test_held_parent_cannot_be_renamed(self):
        guard = self.bridge["_deployment_directory"](str(self.source))
        with guard, self.assertRaises(OSError):
            self.source.rename(self.root / "moved")

    def test_junction_cannot_escape_scan_or_cleanup(self):
        outside = self.root / "outside"
        outside.mkdir()
        sentinel = outside / "keep.txt"
        sentinel.write_bytes(b"unchanged")
        junction = self.source / "junction"
        result = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(outside)],
            capture_output=True,
            check=False,
        )
        self.assertEqual(
            result.returncode, 0, "could not create disposable test junction"
        )
        with self.assertRaises(ValueError):
            self.bridge["_tree_manifest"](str(self.source))
        with self.assertRaises(ValueError):
            self.bridge["_remove_deployment_tree"](str(junction))
        self.bridge["_remove_deployment_tree"](str(self.source))
        self.assertEqual(sentinel.read_bytes(), b"unchanged")


if __name__ == "__main__":
    unittest.main(verbosity=2)
