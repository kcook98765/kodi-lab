"""Recognize this release's official Bridge from the existing installed feed.

The application's compiled ZIP digest is the authority, not a remote version,
an uploaded package, or the feed's own provenance claims. Release staging already
binds that digest and fingerprint to BRIDGE.json and the source inventory.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from sqlalchemy import or_, select, text
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.kodi.bridge import (
    BRIDGE_PROTOCOL_VERSION,
    SUPPORTED_V1_BRIDGE_ADDON_VERSION,
)
from kodi_lab.repository import stable
from kodi_lab.storage.models import ReviewedBridgeBuildRow

# Explicit compatible native identity labels for the current official release.
# Do not expand these from a remote Kodi claim or a wildcard prerelease match.
SUPPORTED_KODI_VERSIONS = ("19.5", "20.5", "21.3", "22.0", "22.0-beta.2")
RecognitionResult = Literal["registered", "existing", "retired", "conflict"]


def recognize_official_bridge(
    sessions: sessionmaker[Session],
    data_root: Path,
    *,
    base_url: str,
    expected_bundle_sha256: str,
) -> RecognitionResult:
    """Verify local release artifacts before inserting immutable build provenance.

    Called at startup, never while viewing a page or in response to discovery.
    No target, policy, pairing, managed trust, or client grant is changed.
    The deployment pin is required; the installed manifest is not self-authority.
    """
    if not stable.HEX_PATTERN.fullmatch(expected_bundle_sha256) or not base_url:
        raise ValueError("official repository requires a deployment bundle pin")
    if (data_root / "TRANSITION.json").exists() or (
        data_root / "TRANSITION.json"
    ).is_symlink():
        raise ValueError("official repository transition is incomplete")
    release = stable.CURRENT_BRIDGE
    if release.version != SUPPORTED_V1_BRIDGE_ADDON_VERSION:
        raise ValueError("official Bridge and backend compatibility pins differ")
    _, provenance = stable.load_current(
        data_root,
        expected_base_url=base_url,
        expected_bundle_sha256=expected_bundle_sha256,
        reviewed_bridge=release,
    )
    package = (
        data_root
        / "packages"
        / stable.BRIDGE_ID
        / (f"{stable.BRIDGE_ID}-{release.version}.zip")
    )
    body = package.read_bytes()
    if package.is_symlink() or stable.sha256(body) != release.sha256:
        raise ValueError("official Bridge package changed during verification")
    capabilities = json.dumps(
        sorted(stable.bridge_capabilities(body)), separators=(",", ":")
    )
    versions = json.dumps(SUPPORTED_KODI_VERSIONS, separators=(",", ":"))
    source_reference = (
        f"official-repository:bundle={expected_bundle_sha256};"
        f"generation={provenance['tree_sha256']};zip={release.sha256}"
    )
    # Serialize lookup+insert across startup processes. Preserve both uniqueness
    # keys, retirement, manual attribution and narrower existing compatibility.
    with sessions() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        rows = list(
            session.scalars(
                select(ReviewedBridgeBuildRow).where(
                    or_(
                        ReviewedBridgeBuildRow.zip_sha256 == release.sha256,
                        (
                            ReviewedBridgeBuildRow.semantic_fingerprint
                            == release.fingerprint
                        )
                        & (ReviewedBridgeBuildRow.addon_id == stable.BRIDGE_ID)
                        & (ReviewedBridgeBuildRow.addon_version == release.version)
                        & (
                            ReviewedBridgeBuildRow.protocol_version
                            == BRIDGE_PROTOCOL_VERSION
                        ),
                    )
                )
            )
        )
        if rows:
            if any(row.state == "retired" for row in rows):
                return "retired"
            if len(rows) != 1:
                return "conflict"
            row = rows[0]
            if (
                row.zip_sha256 != release.sha256
                or row.semantic_fingerprint != release.fingerprint
                or row.addon_id != stable.BRIDGE_ID
                or row.addon_version != release.version
                or row.protocol_version != BRIDGE_PROTOCOL_VERSION
                or row.capabilities_json != capabilities
                or row.supported_kodi_versions_json != versions
            ):
                return "conflict"
            return "existing"
        session.add(
            ReviewedBridgeBuildRow(
                id=str(uuid4()),
                semantic_fingerprint=release.fingerprint,
                zip_sha256=release.sha256,
                addon_id=stable.BRIDGE_ID,
                addon_version=release.version,
                protocol_version=BRIDGE_PROTOCOL_VERSION,
                capabilities_json=capabilities,
                supported_kodi_versions_json=versions,
                source_reference=source_reference,
                reviewed_by="kodi_lab_official_repository",
                reviewed_at=datetime.now(UTC)
                .isoformat(timespec="microseconds")
                .replace("+00:00", "Z"),
                state="active",
                retired_by=None,
                retired_at=None,
            )
        )
        session.commit()
    return "registered"
