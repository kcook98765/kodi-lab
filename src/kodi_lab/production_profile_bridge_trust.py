"""Exact non-secret Slice F identity and authority contract validators."""

from __future__ import annotations

from collections.abc import Mapping

BRIDGE_ADDON_ID = "service.kodi_lab_bridge"
BRIDGE_VERSION = "1.1.8"
BRIDGE_PROTOCOL = "1"
BRIDGE_ZIP_SHA256 = "20a14873a46969cd3f1f962875a3905caa56ce6f088e95f6d3b2b8de6e666762"
BRIDGE_SEMANTIC_FINGERPRINT = (
    "4a055b9479aeb72d2f8fe16d317ed525f69991908b28580d8a071be33aa26e5a"
)
BRIDGE_ADVERTISED_CAPABILITIES = ("screenshot", "workspace_deployment")
PRODUCTION_VERIFIED_BRIDGE_CAPABILITIES = ("screenshot",)


class ProductionProfileBridgeTrustError(RuntimeError):
    """A bounded Slice F contract failure."""


def validate_reviewed_build(document: Mapping[str, object]) -> None:
    expected = {
        "semantic_fingerprint": BRIDGE_SEMANTIC_FINGERPRINT,
        "zip_sha256": BRIDGE_ZIP_SHA256,
        "addon_id": BRIDGE_ADDON_ID,
        "addon_version": BRIDGE_VERSION,
        "protocol_version": BRIDGE_PROTOCOL,
        "capabilities": list(BRIDGE_ADVERTISED_CAPABILITIES),
        "supported_kodi_versions": ["19.5"],
        "state": "active",
    }
    if any(document.get(key) != value for key, value in expected.items()):
        raise ProductionProfileBridgeTrustError("reviewed_bridge_identity_mismatch")
    build_id = document.get("build_id")
    if not isinstance(build_id, str) or len(build_id) != 36:
        raise ProductionProfileBridgeTrustError("reviewed_bridge_identity_mismatch")


def validate_pairing_observation(document: Mapping[str, object]) -> None:
    expected = {
        "pairing_state": "pairing_pending",
        "reachability_state": "reachable",
        "observed_protocol_version": BRIDGE_PROTOCOL,
        "observed_addon_id": BRIDGE_ADDON_ID,
        "observed_addon_version": BRIDGE_VERSION,
        "observed_kodi_version": "19.5",
        "observed_build_fingerprint": BRIDGE_SEMANTIC_FINGERPRINT,
        "observed_capabilities": list(BRIDGE_ADVERTISED_CAPABILITIES),
        "verified_capabilities": [],
    }
    if any(document.get(key) != value for key, value in expected.items()):
        raise ProductionProfileBridgeTrustError("observed_bridge_identity_mismatch")
    bridge_id = document.get("observed_bridge_id")
    if not isinstance(bridge_id, str) or len(bridge_id) != 36:
        raise ProductionProfileBridgeTrustError("observed_bridge_identity_mismatch")


def validate_confirmed_trust(document: Mapping[str, object]) -> None:
    expected = {
        "pairing_state": "paired",
        "reachability_state": "reachable",
        "production_identity_generation": 1,
        "production_bridge_trust_revision": 1,
        "verified_capabilities": list(PRODUCTION_VERIFIED_BRIDGE_CAPABILITIES),
    }
    if any(document.get(key) != value for key, value in expected.items()):
        raise ProductionProfileBridgeTrustError("confirmed_bridge_trust_mismatch")
    if not isinstance(document.get("reviewed_build_id"), str) or not document.get(
        "production_confirmed_at"
    ):
        raise ProductionProfileBridgeTrustError("confirmed_bridge_trust_mismatch")
