"""Development publication uses an approved intent and immutable Kodi feed."""

from __future__ import annotations

import io
import json
import stat
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest

from kodi_lab.repository import development
from kodi_lab.repository.development import (
    DevelopmentRepositoryService,
    bootstrap_zip,
    initialize,
    load_current,
)
from kodi_lab.repository.stable import canonical, sha256
from kodi_lab.storage import metadata
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.storage.models import (
    DevelopmentPublicationHoldRow,
    DevelopmentPublicationRow,
    DevelopmentRepositoryStateRow,
)
from tests.repository.test_stable import archive_with, candidate

BASE = "http://192.168.50.10:18120"
ADDON_ID = "plugin.video.kodi-lab-phase2-synthetic"


def fixture_zip(
    version: str, *, addon_id: str = ADDON_ID, body: bytes = b"pass\n"
) -> bytes:
    descriptor = (
        f'<addon id="{addon_id}" name="Synthetic Kodi Lab Video" '
        f'version="{version}" provider-name="Kodi Lab">'
        '<requires><import addon="xbmc.python" version="3.0.0"/></requires>'
        '<extension point="xbmc.python.pluginsource" library="default.py">'
        "<provides>video</provides></extension>"
        '<extension point="xbmc.addon.metadata"><summary lang="en_GB">'
        "Synthetic test only</summary><platform>all</platform></extension></addon>"
    ).encode()
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, content, directory in (
            (f"{addon_id}/", b"", True),
            (f"{addon_id}/addon.xml", descriptor, False),
            (f"{addon_id}/default.py", body, False),
        ):
            info = zipfile.ZipInfo(name, (2026, 9, 29, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (
                stat.S_IFDIR | 0o755 if directory else stat.S_IFREG | 0o644
            ) << 16
            archive.writestr(info, content)
    return output.getvalue()


def phase2_candidate(tmp_path: Path) -> Path:
    bundle = candidate(tmp_path)
    reviewed = Path(__file__).parents[2] / "repository/DEVELOPMENT.json"
    (bundle / "repository/DEVELOPMENT.json").write_bytes(reviewed.read_bytes())
    manifest = json.loads((bundle / "BUNDLE.json").read_bytes())
    manifest["repository"] = {"migration_head": "0058_workspace_source_capture"}
    manifest["files"]["repository/DEVELOPMENT.json"] = {
        "sha256": "sha256:" + sha256(reviewed.read_bytes()),
        "size": reviewed.stat().st_size,
    }
    (bundle / "BUNDLE.json").write_bytes(canonical(manifest))
    return bundle


@pytest.fixture
def service(tmp_path: Path) -> DevelopmentRepositoryService:
    root = tmp_path / "repository"
    root.mkdir()
    bundle = phase2_candidate(tmp_path)
    initialize(root / "development-data", BASE, bundle)
    engine = create_database_engine(f"sqlite:///{tmp_path / 'database.db'}")
    metadata.create_all(engine)
    result = DevelopmentRepositoryService(create_session_factory(engine), root, BASE)
    result.reconcile()
    return result


def test_bootstrap_determinism_and_approved_version_history(
    service: DevelopmentRepositoryService,
) -> None:
    first_bootstrap = bootstrap_zip(BASE)
    assert first_bootstrap == bootstrap_zip(BASE)
    v1 = fixture_zip("1.0.0")
    prepared = service.prepare_import(v1, ("matrix", "nexus", "omega"))
    assert prepared.status == "prepared"
    assert (
        b"plugin.video.kodi-lab-phase2-synthetic"
        not in (load_current(service.root)[0] / "development/addons.xml").read_bytes()
    )
    published = service.approve(UUID(prepared.id), observed_sha256=sha256(v1))
    assert published.status == "current"
    assert service.reconcile()["tree_sha256"] == published.feed_generation
    assert service.prepare_import(v1, ("matrix", "nexus", "omega")).id == prepared.id
    with pytest.raises(ValueError, match="different bytes"):
        service.prepare_import(fixture_zip("1.0.0", body=b"different\n"), ("matrix",))
    v2 = fixture_zip("1.0.1")
    next_record = service.prepare_import(v2, ("matrix", "nexus", "omega"))
    service.approve(UUID(next_record.id), observed_sha256=sha256(v2))
    root, provenance = load_current(service.root)
    index = (root / "development/addons.xml").read_bytes()
    assert b' version="1.0.1"' in index
    assert b' version="1.0.0"' not in index
    assert len(cast(list[dict[str, str]], provenance["packages"])) == 2
    assert (
        service.root / "packages" / ADDON_ID / f"{ADDON_ID}-1.0.0.zip"
    ).read_bytes() == v1
    assert {item.status for item in service.list_publications()} == {
        "current",
        "superseded",
    }


@pytest.mark.parametrize(
    "forbidden",
    [
        "repository.kodi-lab",
        "repository.kodi-lab-dev",
        "service.kodi_lab_bridge",
        "script.some-test",
    ],
)
def test_reserved_or_wrong_type_denied(
    service: DevelopmentRepositoryService, forbidden: str
) -> None:
    with pytest.raises(ValueError):
        service.prepare_import(fixture_zip("1.0.0", addon_id=forbidden), ("matrix",))


def test_unapproved_and_wrong_hash_never_publish(
    service: DevelopmentRepositoryService,
) -> None:
    pending = service.prepare_import(fixture_zip("1.0.0"), ("matrix",))
    with pytest.raises(ValueError, match="approval"):
        service.approve(UUID(pending.id), observed_sha256="0" * 64)
    assert service.reconcile()["packages"] == []


def test_tampered_package_fails_closed(service: DevelopmentRepositoryService) -> None:
    content = fixture_zip("1.0.0")
    pending = service.prepare_import(content, ("matrix",))
    service.approve(UUID(pending.id), observed_sha256=sha256(content))
    package = service.root / "packages" / ADDON_ID / f"{ADDON_ID}-1.0.0.zip"
    package.unlink()
    package.write_bytes(b"tampered")
    with pytest.raises(ValueError):
        service.reconcile()


def test_unknown_artifact_bytes_fail_closed(
    service: DevelopmentRepositoryService,
) -> None:
    unknown = service.root / "pending" / "unapproved.zip"
    unknown.write_bytes(b"unknown")
    with pytest.raises(ValueError, match="unknown bytes"):
        service.reconcile()


def test_unrelated_optional_dependency_is_denied(
    service: DevelopmentRepositoryService,
) -> None:
    source = fixture_zip("1.0.0")
    with zipfile.ZipFile(io.BytesIO(source)) as archive:
        descriptor = archive.read(f"{ADDON_ID}/addon.xml")
    changed = descriptor.replace(
        b"</requires>",
        b'<import addon="script.module.unapproved" optional="true"/></requires>',
    )
    entries = [
        (f"{ADDON_ID}/", b"", stat.S_IFDIR | 0o755),
        (f"{ADDON_ID}/addon.xml", changed, stat.S_IFREG | 0o644),
        (f"{ADDON_ID}/default.py", b"pass\n", stat.S_IFREG | 0o644),
    ]
    with pytest.raises(ValueError, match=r"require only xbmc\.python"):
        service.prepare_import(archive_with(entries), ("matrix",))


def test_database_feed_disagreement_fails_closed(
    service: DevelopmentRepositoryService,
) -> None:
    body = fixture_zip("1.0.0")
    pending = service.prepare_import(body, ("matrix",))
    service.approve(UUID(pending.id), observed_sha256=sha256(body))
    with service.sessions.begin() as session:
        state = session.get(DevelopmentRepositoryStateRow, 1)
        assert state is not None
        state.current_index_sha256 = "0" * 64
    with pytest.raises(ValueError, match="database and feed"):
        service.reconcile()


def test_version_compatibility_scope_and_quota_fail_closed(
    service: DevelopmentRepositoryService,
) -> None:
    with pytest.raises(ValueError, match="development-only"):
        service.prepare_import(
            fixture_zip("1.0.0"), ("matrix",), scope_kind="production"
        )
    with pytest.raises(ValueError, match=r"major\.minor\.patch"):
        service.prepare_import(fixture_zip("1.0.0beta"), ("matrix",))
    with pytest.raises(ValueError, match="supported generations"):
        service.prepare_import(fixture_zip("1.0.0"), ("unknown",))
    first = fixture_zip("2.0.0")
    pending = service.prepare_import(first, ("matrix",))
    service.approve(UUID(pending.id), observed_sha256=sha256(first))
    lower = fixture_zip("1.0.0")
    older = service.prepare_import(lower, ("matrix",))
    with pytest.raises(ValueError, match="exceed current"):
        service.approve(UUID(older.id), observed_sha256=sha256(lower))
    service.max_artifacts = 1
    newer = fixture_zip("3.0.0")
    with pytest.raises(ValueError, match="quota"):
        service.prepare_import(newer, ("matrix",))
    assert (
        cast(list[dict[str, str]], service.reconcile()["packages"])[0]["version"]
        == "2.0.0"
    )


def test_concurrent_approvals_keep_complete_index(
    service: DevelopmentRepositoryService,
) -> None:
    candidates = []
    for suffix in ("first", "second"):
        addon_id = f"plugin.video.kodi-lab-phase2-{suffix}"
        body = fixture_zip("1.0.0", addon_id=addon_id)
        record = service.prepare_import(body, ("matrix", "nexus"))
        candidates.append((UUID(record.id), sha256(body)))
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(
            workers.map(
                lambda pair: service.approve(pair[0], observed_sha256=pair[1]),
                candidates,
            )
        )
    assert {row.status for row in results} == {"current"}
    assert len(cast(list[dict[str, str]], service.reconcile()["packages"])) == 2
    index = (load_current(service.root)[0] / "development/addons.xml").read_bytes()
    assert index.count(b"plugin.video.kodi-lab-phase2-") == 2


def test_concurrent_same_addon_has_one_current_version(
    service: DevelopmentRepositoryService,
) -> None:
    candidates = []
    for version in ("1.0.0", "1.0.1"):
        body = fixture_zip(version)
        record = service.prepare_import(body, ("matrix",))
        candidates.append((UUID(record.id), sha256(body)))

    def attempt(pair: tuple[UUID, str]) -> str:
        try:
            return service.approve(pair[0], observed_sha256=pair[1]).status
        except ValueError as error:
            return str(error)

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(attempt, candidates))
    assert "current" in results
    rows = service.list_publications()
    assert sum(row.status == "current" for row in rows) == 1
    current = next(row for row in rows if row.status == "current")
    assert current.version in {"1.0.0", "1.0.1"}
    assert len(cast(list[dict[str, str]], service.reconcile()["packages"])) in {1, 2}


def test_byte_quota_rejects_import_without_switch(
    service: DevelopmentRepositoryService,
) -> None:
    body = fixture_zip("1.0.0")
    service.max_bytes = len(body) - 1
    with pytest.raises(ValueError, match="quota"):
        service.prepare_import(body, ("matrix",))
    assert service.reconcile()["packages"] == []


def test_interrupted_switch_recovers_approved_intent(
    service: DevelopmentRepositoryService, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_generation = service.reconcile()["tree_sha256"]
    body = fixture_zip("1.0.0")
    record = service.prepare_import(body, ("matrix",))
    original_pointer = development._pointer
    injected = False

    def fail_once(path: Path, value: str) -> None:
        nonlocal injected
        if path.name == "CURRENT" and not injected:
            injected = True
            raise OSError("injected switch interruption")
        original_pointer(path, value)

    monkeypatch.setattr(development, "_pointer", fail_once)
    with pytest.raises(OSError, match="interruption"):
        service.approve(UUID(record.id), observed_sha256=sha256(body))
    assert (service.root / "CURRENT").read_text().strip() == original_generation
    assert not (service.root / "ACK").exists()
    monkeypatch.setattr(development, "_pointer", original_pointer)
    assert service.reconcile()["tree_sha256"] != original_generation
    recovered = service.get_publication(UUID(record.id))
    assert recovered is not None and recovered.status == "current"


def test_retention_keeps_current_and_two_prior(
    service: DevelopmentRepositoryService,
) -> None:
    records = []
    for version in ("1.0.0", "1.0.1", "1.0.2", "1.0.3"):
        body = fixture_zip(version)
        pending = service.prepare_import(body, ("matrix",))
        records.append(service.approve(UUID(pending.id), observed_sha256=sha256(body)))
    with service.sessions.begin() as session:
        oldest = session.get(DevelopmentPublicationRow, records[0].id)
        assert oldest is not None
        oldest.superseded_at = (datetime.now(UTC) - timedelta(days=9)).isoformat(
            timespec="seconds"
        )
    assert service.prune() == [records[0].id]
    retired = service.get_publication(UUID(records[0].id))
    assert retired is not None and retired.status == "retired"
    assert len(cast(list[dict[str, str]], service.reconcile()["packages"])) == 3
    assert not (service.root / "packages" / ADDON_ID / f"{ADDON_ID}-1.0.0.zip").exists()
    with pytest.raises(ValueError, match="different bytes"):
        service.prepare_import(fixture_zip("1.0.0", body=b"changed\n"), ("matrix",))


def test_evidence_hold_protects_prior_version(
    service: DevelopmentRepositoryService,
) -> None:
    records = []
    for version in ("1.0.0", "1.0.1", "1.0.2", "1.0.3"):
        body = fixture_zip(version)
        pending = service.prepare_import(body, ("matrix",))
        records.append(service.approve(UUID(pending.id), observed_sha256=sha256(body)))
    with service.sessions.begin() as session:
        oldest = session.get(DevelopmentPublicationRow, records[0].id)
        assert oldest is not None
        oldest.superseded_at = (datetime.now(UTC) - timedelta(days=9)).isoformat(
            timespec="seconds"
        )
        session.add(
            DevelopmentPublicationHoldRow(
                id=str(uuid4()),
                publication_id=oldest.id,
                reason="deployment_receipt",
                reference_id="test-receipt",
                created_at=datetime.now(UTC).isoformat(timespec="seconds"),
            )
        )
    assert service.prune() == []
    retained = service.get_publication(UUID(records[0].id))
    assert retained is not None and retained.status == "superseded"


@pytest.mark.parametrize(
    "damage",
    [
        "malformed_xml",
        "dtd",
        "traversal",
        "duplicate",
        "symlink",
        "excessive",
        "missing_asset",
    ],
)
def test_hostile_zip_is_rejected_by_development_admission(
    service: DevelopmentRepositoryService, damage: str
) -> None:
    source = fixture_zip("1.0.0")
    with zipfile.ZipFile(io.BytesIO(source)) as archive:
        descriptor = archive.read(f"{ADDON_ID}/addon.xml")
    entries = [
        (f"{ADDON_ID}/", b"", stat.S_IFDIR | 0o755),
        (f"{ADDON_ID}/addon.xml", descriptor, stat.S_IFREG | 0o644),
        (f"{ADDON_ID}/default.py", b"pass\n", stat.S_IFREG | 0o644),
    ]
    if damage == "malformed_xml":
        entries[1] = (entries[1][0], b"<addon", entries[1][2])
    elif damage == "dtd":
        entries[1] = (entries[1][0], b"<!DOCTYPE addon>" + descriptor, entries[1][2])
    elif damage == "traversal":
        entries.append((f"{ADDON_ID}/../outside", b"x", stat.S_IFREG | 0o644))
    elif damage == "duplicate":
        entries.append(entries[2])
    elif damage == "symlink":
        entries.append((f"{ADDON_ID}/link", b"default.py", stat.S_IFLNK | 0o777))
    elif damage == "excessive":
        entries.append(
            (f"{ADDON_ID}/huge", b"A" * (9 * 1024 * 1024), stat.S_IFREG | 0o644)
        )
    elif damage == "missing_asset":
        entries[1] = (
            entries[1][0],
            descriptor.replace(
                b"<summary", b"<assets><icon>missing.png</icon></assets><summary"
            ),
            entries[1][2],
        )
    with pytest.raises(ValueError):
        service.prepare_import(archive_with(entries), ("matrix",))
