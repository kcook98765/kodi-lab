"""Additive exact Bridge identity preserves stable managed trust."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from kodi_lab.application.managed_bridge_trust import (
    ManagedBridgeTrustError,
    identity_json,
    require_approved_identity,
)
from kodi_lab.broker.managed_bridge_trust import approved_bridge_build
from kodi_lab.kodi.bridge import ExternalBridgeClient, ExternalBridgeError
from kodi_lab.kodi.rc1_bridge import reviewed_rc1_bridge_sources

from .test_narrow_bridge_addon import ADDON_ROOT, _load


@pytest.mark.parametrize(
    ("tag", "ordinal", "expected"),
    [
        ("releasecandidate", "1", "22.0-rc.1"),
        ("releasecandidate", "2", "22.0-rc.2"),
        ("beta", "2", "22.0-beta.2"),
        ("stable", None, "22.0"),
        ("releasecandidate", None, None),
        ("releasecandidate", "0", None),
        ("releasecandidate", "01", None),
        ("releasecandidate", True, None),
    ],
)
def test_native_identity_is_exact(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    tag: str,
    ordinal: object,
    expected: str | None,
) -> None:
    sources = reviewed_rc1_bridge_sources(ADDON_ROOT)
    candidate = tmp_path / "narrow_bridge.py"
    candidate.write_bytes(sources["narrow_bridge.py"])
    monkeypatch.setattr("tests.kodi.test_narrow_bridge_addon.ADDON", candidate)
    bridge = _load(monkeypatch)
    xbmc: Any = bridge["xbmc"]
    xbmc.getInfoLabel = lambda _label: "22.0-RC1 (20260927-28ea2eac)"
    xbmc.executeJSONRPC = lambda _request: json.dumps(
        {
            "result": {
                "version": {
                    "major": 22,
                    "minor": 0,
                    "tag": tag,
                    "tagversion": ordinal,
                }
            }
        }
    )
    identity = bridge["_identity"]({"bridge_id": str(uuid4())})
    assert identity["kodi_version"] == "22.0"
    assert identity.get("kodi_release") == expected


def test_exact_preview_trust_and_legacy_serialization() -> None:
    legacy = {
        **approved_bridge_build(),
        "bridge_id": str(uuid4()),
        "kodi_version": "21.3",
        "capabilities": ["ui_observation"],
    }
    legacy.pop("files")
    parsed = ExternalBridgeClient._identity(legacy)
    require_approved_identity(parsed, "21.3")
    assert "kodi_release" not in json.loads(identity_json(parsed))
    preview = {
        **approved_bridge_build("22.0-rc.1"),
        "bridge_id": str(uuid4()),
        "kodi_version": "22.0",
        "kodi_release": "22.0-rc.1",
        "capabilities": ["ui_observation"],
    }
    preview.pop("files")
    require_approved_identity(ExternalBridgeClient._identity(preview), "22.0-rc.1")
    for replacement in (None, "22.0", "22.0-rc.2"):
        with pytest.raises(ManagedBridgeTrustError):
            require_approved_identity(
                ExternalBridgeClient._identity(
                    {**preview, "kodi_release": replacement}
                ),
                "22.0-rc.1",
            )
    with pytest.raises(ExternalBridgeError):
        ExternalBridgeClient._identity({**preview, "kodi_release": "21.3"})


@pytest.mark.parametrize("release", ["20.5", "21.3", "22.0-rc.1"])
@pytest.mark.parametrize("tier", ["previous", "retained", "legacy"])
def test_previous_exact_build_only_preserves_established_trust(
    release: str, tier: str
) -> None:
    previous = approved_bridge_build(
        release,
        previous=tier == "previous",
        retained=tier == "retained",
        legacy=tier == "legacy",
    )
    if tier == "legacy":
        assert previous["addon_version"] == ("1.1.16" if "-" in release else "1.1.15")
    document = {
        **previous,
        "bridge_id": str(uuid4()),
        "kodi_version": "22.0" if "-" in release else release,
        "capabilities": ["ui_observation"],
    }
    document.pop("files")
    if "-" in release:
        document["kodi_release"] = release
    identity = ExternalBridgeClient._identity(document)
    require_approved_identity(identity, release, established=True)
    with pytest.raises(ManagedBridgeTrustError):
        require_approved_identity(identity, release)
    for field in ("addon_version", "build_fingerprint"):
        with pytest.raises(ManagedBridgeTrustError):
            require_approved_identity(
                ExternalBridgeClient._identity(
                    {
                        **document,
                        field: "f" * 64 if field == "build_fingerprint" else "unknown",
                    }
                ),
                release,
                established=True,
            )
