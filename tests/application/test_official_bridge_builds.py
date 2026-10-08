from __future__ import annotations

import io
import json
import re
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, delete, event, select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application import official_bridge_builds
from kodi_lab.application.official_bridge_builds import recognize_official_bridge
from kodi_lab.domain import ExternalBridgePairingState
from kodi_lab.repository import stable
from kodi_lab.storage.engine import create_session_factory
from kodi_lab.storage.metadata import Base
from kodi_lab.storage.models import (
    McpClientTargetGrantRow,
    McpProductionTargetScopeRow,
    ReviewedBridgeBuildRow,
)
from scripts.build_narrow_bridge import FINGERPRINT_INPUT
from tests.application.test_external_onboarding import (
    live_application as live_application,
)
from tests.application.test_household_onboarding import Environment
from tests.application.test_household_onboarding import env as env
from tests.repository.test_stable import BASE, candidate


@pytest.fixture
def sessions(tmp_path: Path) -> sessionmaker[Session]:
    engine = create_engine(f"sqlite:///{tmp_path / 'official.db'}")
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


def installed(tmp_path: Path) -> tuple[Path, str]:
    bundle = candidate(tmp_path)
    root = tmp_path / "feed"
    stable.generate(bundle, root, BASE)
    return root, stable.sha256((bundle / "BUNDLE.json").read_bytes())


def recognize(sessions: sessionmaker[Session], root: Path, pin: str) -> str:
    return recognize_official_bridge(
        sessions, root, base_url=BASE, expected_bundle_sha256=pin
    )


def release_fixture(bundle: Path, version: str) -> stable.ReviewedBridge:
    """Produce a different release in isolation, including its exact identity pins."""
    old = stable.CURRENT_BRIDGE
    fingerprint = stable.sha256(
        FINGERPRINT_INPUT.replace(old.version.encode(), version.encode())
    )
    source_zip = bundle / f"bridge/{stable.BRIDGE_ID}-{old.version}.zip"
    output = io.BytesIO()
    with zipfile.ZipFile(source_zip) as source, zipfile.ZipFile(output, "w") as target:
        for item in source.infolist():
            body = source.read(item).replace(old.version.encode(), version.encode())
            body = body.replace(old.fingerprint.encode(), fingerprint.encode())
            if version == old.version and item.filename.endswith("narrow_bridge.py"):
                # An unofficial artifact can claim the exact same release identity.
                # Only independently pinned package bytes establish official origin.
                body += b"\n# unofficial package change\n"
            target.writestr(item, body)
    source_zip.unlink()
    new_zip = bundle / f"bridge/{stable.BRIDGE_ID}-{version}.zip"
    new_zip.write_bytes(output.getvalue())
    release = stable.ReviewedBridge(
        version, stable.sha256(output.getvalue()), fingerprint
    )
    record = json.loads((bundle / "repository/STABLE.json").read_bytes())
    record.update(
        bridge_version=version,
        bridge_zip_sha256=release.sha256,
        bridge_semantic_fingerprint=fingerprint,
    )
    (bundle / "repository/STABLE.json").write_bytes(stable.canonical(record))
    provenance = json.loads((bundle / "bridge/BRIDGE.json").read_bytes())
    provenance.update(
        version=version,
        filename=new_zip.name,
        sha256=release.sha256,
        semantic_fingerprint=fingerprint,
    )
    (bundle / "bridge/BRIDGE.json").write_bytes(stable.canonical(provenance))
    manifest = json.loads((bundle / "BUNDLE.json").read_bytes())
    manifest["bridge"]["zip"] = "bridge/" + new_zip.name
    manifest["files"] = {
        name: {
            "sha256": "sha256:" + stable.sha256((bundle / name).read_bytes()),
            "size": (bundle / name).stat().st_size,
        }
        for name in (
            "bridge/" + new_zip.name,
            "bridge/BRIDGE.json",
            "repository/STABLE.json",
        )
    }
    (bundle / "BUNDLE.json").write_bytes(stable.canonical(manifest))
    return release


def test_exact_official_release_is_registered_once_without_other_writes(
    sessions: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    root, pin = installed(tmp_path)
    writes: list[str] = []

    def record(_connection: Any, _cursor: Any, statement: str, *_args: Any) -> None:
        if re.match(r"\s*(INSERT|UPDATE|DELETE)\b", statement, re.I):
            writes.append(statement)

    engine = sessions.kw["bind"]
    event.listen(engine, "before_cursor_execute", record)
    try:
        assert recognize(sessions, root, pin) == "registered"
        assert recognize(sessions, root, pin) == "existing"
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert len(writes) == 1 and "INSERT INTO reviewed_bridge_builds" in writes[0]
    with sessions() as session:
        rows = session.scalars(select(ReviewedBridgeBuildRow)).all()
        assert len(rows) == 1
        row = rows[0]
        assert row.zip_sha256 == stable.BRIDGE_SHA256
        assert row.semantic_fingerprint == stable.BRIDGE_FINGERPRINT
        assert row.reviewed_by == "kodi_lab_official_repository"
        assert pin in row.source_reference
        assert stable.BRIDGE_SHA256 in row.source_reference
        assert (
            row.capabilities_json
            == '["screenshot","ui_observation","workspace_deployment"]'
        )


def test_future_compatible_release_needs_no_manual_registration(
    sessions: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = candidate(tmp_path)
    release = release_fixture(bundle, "1.1.16")
    pin = stable.sha256((bundle / "BUNDLE.json").read_bytes())
    assert stable.reviewed_bridge_from_bundle(bundle, pin) == release
    root = tmp_path / "feed"
    stable.generate(bundle, root, BASE, reviewed_bridge=release)
    # A newly shipped backend updates its existing release/compatibility pins.
    # An older backend is intentionally not authorized to trust a future claim.
    monkeypatch.setattr(stable, "CURRENT_BRIDGE", release)
    monkeypatch.setattr(
        official_bridge_builds, "SUPPORTED_V1_BRIDGE_ADDON_VERSION", "1.1.16"
    )
    assert recognize(sessions, root, pin) == "registered"
    with sessions() as session:
        row = session.scalar(select(ReviewedBridgeBuildRow))
        assert row is not None and row.addon_version == "1.1.16"
        assert row.zip_sha256 == release.sha256


@pytest.mark.parametrize(
    "damage", ["package", "provenance", "pin", "no_pin", "transition", "index"]
)
def test_invalid_or_unpinned_repository_never_registers(
    sessions: sessionmaker[Session],
    tmp_path: Path,
    damage: str,
) -> None:
    root, pin = installed(tmp_path)
    generation, _ = stable.load_current(root)
    if damage == "package":
        path = (
            root
            / "packages"
            / stable.BRIDGE_ID
            / f"{stable.BRIDGE_ID}-{stable.BRIDGE_VERSION}.zip"
        )
        path.write_bytes(path.read_bytes() + b"unofficial")
    elif damage == "provenance":
        path = generation / "PROVENANCE.json"
        value = json.loads(path.read_bytes())
        value["bundle_sha256"] = "0" * 64
        path.write_bytes(stable.canonical(value))
    elif damage == "index":
        (generation / "stable/addons.xml").write_bytes(b"<addons/>")
    elif damage == "transition":
        (root / "TRANSITION.json").write_text("{}")
    elif damage == "no_pin":
        pin = ""
    else:
        pin = "0" * 64
    with pytest.raises(ValueError):
        recognize(sessions, root, pin)
    with sessions() as session:
        assert session.scalars(select(ReviewedBridgeBuildRow)).all() == []


def test_self_consistent_unofficial_package_is_not_authority(
    sessions: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    bundle = candidate(tmp_path)
    unofficial = release_fixture(bundle, stable.BRIDGE_VERSION)
    root = tmp_path / "feed"
    stable.generate(bundle, root, BASE, reviewed_bridge=unofficial)
    pin = stable.sha256((bundle / "BUNDLE.json").read_bytes())
    # Even a self-consistent feed and matching configured bundle field cannot
    # override the application's known official package digest.
    with pytest.raises(ValueError):
        recognize(sessions, root, pin)
    with sessions() as session:
        assert session.scalars(select(ReviewedBridgeBuildRow)).all() == []


@pytest.mark.parametrize(
    "existing",
    ["retired", "narrower", "different_hash", "different_identity", "manual"],
)
def test_existing_review_or_retirement_is_never_overwritten(
    sessions: sessionmaker[Session],
    tmp_path: Path,
    existing: str,
) -> None:
    root, pin = installed(tmp_path)
    recognize(sessions, root, pin)
    with sessions.begin() as session:
        row = session.scalar(select(ReviewedBridgeBuildRow))
        assert row is not None
        row.reviewed_by = "custom_administrator"
        row.source_reference = "existing manual review"
        if existing == "retired":
            row.state, row.retired_by, row.retired_at = (
                "retired",
                "admin",
                row.reviewed_at,
            )
        elif existing == "narrower":
            row.supported_kodi_versions_json = '["21.3"]'
        elif existing == "different_hash":
            row.zip_sha256 = "f" * 64
        elif existing == "different_identity":
            row.semantic_fingerprint = "f" * 64
    with sessions() as session:
        before = session.execute(select(ReviewedBridgeBuildRow.__table__)).all()
    expected = (
        "existing"
        if existing == "manual"
        else "retired"
        if existing == "retired"
        else "conflict"
    )
    assert recognize(sessions, root, pin) == expected
    with sessions() as session:
        assert session.execute(select(ReviewedBridgeBuildRow.__table__)).all() == before


def test_concurrent_startups_register_one_build(
    sessions: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    root, pin = installed(tmp_path)
    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(
            executor.map(lambda _: recognize(sessions, root, pin), range(2))
        )
    assert sorted(outcomes) == ["existing", "registered"]


def test_official_otp_pairing_without_manual_review_or_permission_expansion(
    env: Environment,
    tmp_path: Path,
) -> None:
    with env.sessions.begin() as session:
        session.execute(delete(ReviewedBridgeBuildRow))
    root, pin = installed(tmp_path)
    assert recognize(env.sessions, root, pin) == "registered"
    result = env.connect()
    assert result.state == "connected" and result.target_id is not None, result
    status = env.bridges.status(result.target_id)
    assert status.pairing_state is ExternalBridgePairingState.PAIRED
    assert status.reviewed_build_id is not None
    assert json.loads(env.otp_file.read_text())["code"] is None
    assert not env.bridges.deployment_available(result.target_id)
    target = env.targets.get(result.target_id)
    assert (
        target.production_policy is not None
        and target.production_policy.mutations_suspended
    )
    assert "semantic_remote" not in target.configured_capabilities
    with env.sessions() as session:
        assert session.scalars(select(McpClientTargetGrantRow)).all() == []
        assert session.scalars(select(McpProductionTargetScopeRow)).all() == []


@pytest.mark.parametrize("bad_identity", ["fingerprint", "protocol", "version"])
def test_remote_claim_does_not_replace_official_pairing_checks(
    env: Environment,
    tmp_path: Path,
    bad_identity: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with env.sessions.begin() as session:
        session.execute(delete(ReviewedBridgeBuildRow))
    root, pin = installed(tmp_path)
    recognize(env.sessions, root, pin)
    key = {
        "fingerprint": "BUILD_FINGERPRINT",
        "protocol": "PROTOCOL",
        "version": "ADDON_VERSION",
    }[bad_identity]
    value = {"fingerprint": "f" * 64, "protocol": "2", "version": "99.0"}[bad_identity]
    monkeypatch.setitem(env.bridge["_identity"].__globals__, key, value)
    result = env.connect()
    assert result.state != "connected"
    if bad_identity == "protocol":
        # Bootstrap discovery was compatible; the signed identity then changed.
        # Preserve the existing post-dispatch recovery boundary, not an OTP retry.
        assert result.code == "pairing_recovery_required"
        assert json.loads(env.otp_file.read_text())["code"] is None
        return
    assert result.code in {
        "bridge_build_not_reviewed",
        "bridge_protocol_unsupported",
        "identity_mismatch",
    }
    assert (
        json.loads(env.otp_file.read_text())["code"]
        == env.request.code.get_secret_value()
    )
