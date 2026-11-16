"""Persistent consent and authority carried only by immutable reviewed transitions.

No network work, credential reads, policy mutations, or global revision bypasses.
Manual trust transitions have no adoption receipt and retain their old rules.
"""

from __future__ import annotations

import hashlib
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from kodi_lab.storage.models import (
    ExternalBridgePairingRow,
    ExternalKodiTargetRow,
    ExternalProductionPolicyRow,
    ProductionBridgeAdoptionRow,
    ProductionBridgeHttpAcknowledgementRow,
    ReviewedBridgeBuildRow,
)

PRODUCTION_TRANSPORT_POLICY_REVISION = 1


def continuity_binding(
    external: ExternalKodiTargetRow,
    pairing: ExternalBridgePairingRow,
    policy: ExternalProductionPolicyRow,
) -> str:
    """Bind persistent authority to device, secret references and actual risks."""
    return hashlib.sha256(
        json.dumps(
            {
                "target": external.target_id,
                "native_generation": policy.identity_generation,
                "native_observation": policy.confirmed_observation_digest,
                "native_credential": external.secret_reference,
                "bridge_credential": pairing.bridge_secret_reference,
                "bridge_id": pairing.pinned_bridge_id,
                "protocol": pairing.pinned_protocol_version,
                "kodi_version": pairing.pinned_kodi_version,
                "endpoint": pairing.endpoint_revision_digest,
                "host": external.host,
                "native_port": external.port,
                "bridge_port": pairing.bridge_port,
                "transport": external.protocol_kind.value,
                "configured_maximum": external.configured_capabilities_json,
                "transport_policy": PRODUCTION_TRANSPORT_POLICY_REVISION,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def current_adoption(
    session: Session,
    external: ExternalKodiTargetRow,
    pairing: ExternalBridgePairingRow,
) -> ProductionBridgeAdoptionRow | None:
    policy = session.get(ExternalProductionPolicyRow, external.target_id)
    receipt = session.scalar(
        select(ProductionBridgeAdoptionRow).where(
            ProductionBridgeAdoptionRow.target_id == external.target_id,
            ProductionBridgeAdoptionRow.to_revision
            == pairing.production_bridge_trust_revision,
        )
    )
    if (
        policy is None
        or receipt is None
        or pairing.pairing_state.value != "paired"
        or pairing.production_identity_generation != policy.identity_generation
        or receipt.to_build_id != pairing.reviewed_build_id
        or receipt.bridge_id != pairing.pinned_bridge_id
        or receipt.binding_digest != continuity_binding(external, pairing, policy)
    ):
        return None
    build = session.get(ReviewedBridgeBuildRow, receipt.to_build_id)
    if (
        build is None
        or build.state != "active"
        or build.semantic_fingerprint != pairing.pinned_build_fingerprint
        or build.addon_id != pairing.pinned_addon_id
        or build.addon_version != pairing.pinned_addon_version
        or build.protocol_version != pairing.pinned_protocol_version
        or pairing.pinned_kodi_version
        not in json.loads(build.supported_kodi_versions_json)
    ):
        return None
    return receipt


def transport_acknowledgement(
    session: Session,
    external: ExternalKodiTargetRow,
    pairing: ExternalBridgePairingRow | None,
    *,
    bridge_port: int | None = None,
    identity_generation: int,
    bridge_revision: int,
    endpoint_digest: str,
    transport_policy_revision: int = PRODUCTION_TRANSPORT_POLICY_REVISION,
) -> ProductionBridgeHttpAcknowledgementRow | None:
    """Find exact consent, or consent explicitly retained by this transition."""
    port = (
        bridge_port
        if bridge_port is not None
        else (pairing.bridge_port if pairing is not None else None)
    )
    binding = (
        ProductionBridgeHttpAcknowledgementRow.target_id == external.target_id,
        ProductionBridgeHttpAcknowledgementRow.identity_generation
        == identity_generation,
        ProductionBridgeHttpAcknowledgementRow.protocol == "jsonrpc_http",
        ProductionBridgeHttpAcknowledgementRow.ip_literal == external.host,
        ProductionBridgeHttpAcknowledgementRow.native_port == external.port,
        ProductionBridgeHttpAcknowledgementRow.bridge_port == port,
        ProductionBridgeHttpAcknowledgementRow.endpoint_digest == endpoint_digest,
        ProductionBridgeHttpAcknowledgementRow.transport_policy_revision
        == transport_policy_revision,
    )
    direct = session.scalar(
        select(ProductionBridgeHttpAcknowledgementRow).where(
            *binding,
            ProductionBridgeHttpAcknowledgementRow.bridge_trust_revision
            == bridge_revision,
        )
    )
    if direct is not None:
        return direct
    if pairing is None or pairing.production_bridge_trust_revision != bridge_revision:
        return None
    receipt = current_adoption(session, external, pairing)
    if receipt is None or receipt.http_acknowledgement_id is None:
        return None
    return session.scalar(
        select(ProductionBridgeHttpAcknowledgementRow).where(
            *binding,
            ProductionBridgeHttpAcknowledgementRow.id
            == receipt.http_acknowledgement_id,
        )
    )


def persistent_navigation_anchor(
    session: Session, external: ExternalKodiTargetRow, pairing: ExternalBridgePairingRow
) -> tuple[int | None, str | None]:
    receipt = current_adoption(session, external, pairing)
    if receipt is not None:
        return receipt.authority_revision, receipt.authority_build_id
    return pairing.production_bridge_trust_revision, pairing.reviewed_build_id
