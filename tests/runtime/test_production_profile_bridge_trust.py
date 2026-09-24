from __future__ import annotations

from copy import deepcopy
from uuid import UUID

import pytest

from kodi_lab.production_profile_bridge_trust import (
    BRIDGE_ADDON_ID,
    BRIDGE_ADVERTISED_CAPABILITIES,
    BRIDGE_PROTOCOL,
    BRIDGE_SEMANTIC_FINGERPRINT,
    BRIDGE_VERSION,
    BRIDGE_ZIP_SHA256,
    ProductionProfileBridgeTrustError,
    validate_confirmed_trust,
    validate_pairing_observation,
    validate_reviewed_build,
)


def _reviewed() -> dict[str, object]:
    return {
        "build_id": str(UUID(int=1)),
        "semantic_fingerprint": BRIDGE_SEMANTIC_FINGERPRINT,
        "zip_sha256": BRIDGE_ZIP_SHA256,
        "addon_id": BRIDGE_ADDON_ID,
        "addon_version": BRIDGE_VERSION,
        "protocol_version": BRIDGE_PROTOCOL,
        "capabilities": list(BRIDGE_ADVERTISED_CAPABILITIES),
        "supported_kodi_versions": ["19.5"],
        "state": "active",
    }


@pytest.mark.parametrize(
    ("field", "wrong"),
    [
        ("zip_sha256", "0" * 64),
        ("semantic_fingerprint", "0" * 64),
        ("addon_version", "1.1.7"),
        ("protocol_version", "2"),
    ],
)
def test_exact_reviewed_build_rejects_every_identity_mismatch(
    field: str, wrong: object
) -> None:
    document = _reviewed()
    validate_reviewed_build(document)
    document[field] = wrong
    with pytest.raises(
        ProductionProfileBridgeTrustError,
        match="reviewed_bridge_identity_mismatch",
    ):
        validate_reviewed_build(document)


def test_observed_and_verified_capabilities_are_separate_closed_sets() -> None:
    observation = {
        "pairing_state": "pairing_pending",
        "reachability_state": "reachable",
        "observed_bridge_id": str(UUID(int=2)),
        "observed_protocol_version": BRIDGE_PROTOCOL,
        "observed_addon_id": BRIDGE_ADDON_ID,
        "observed_addon_version": BRIDGE_VERSION,
        "observed_kodi_version": "19.5",
        "observed_build_fingerprint": BRIDGE_SEMANTIC_FINGERPRINT,
        "observed_capabilities": list(BRIDGE_ADVERTISED_CAPABILITIES),
        "verified_capabilities": [],
    }
    validate_pairing_observation(observation)
    unsafe = deepcopy(observation)
    unsafe["verified_capabilities"] = ["workspace_deployment"]
    with pytest.raises(ProductionProfileBridgeTrustError):
        validate_pairing_observation(unsafe)

    confirmed = {
        "pairing_state": "paired",
        "reachability_state": "reachable",
        "production_identity_generation": 1,
        "production_bridge_trust_revision": 1,
        "verified_capabilities": ["screenshot"],
        "reviewed_build_id": str(UUID(int=1)),
        "production_confirmed_at": "2026-09-23T00:00:00.000000Z",
    }
    validate_confirmed_trust(confirmed)
    confirmed["verified_capabilities"] = ["screenshot", "workspace_deployment"]
    with pytest.raises(ProductionProfileBridgeTrustError):
        validate_confirmed_trust(confirmed)
