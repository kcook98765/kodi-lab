"""Approved development feed: bounded admission, atomic indexes and reconciliation."""

from __future__ import annotations

import fcntl
import io
import json
import os
import re
import shutil
import tempfile
import threading
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.development.sources import (
    DevelopmentSourceValidationError,
    copied_video_plugin_library,
    parse_captured_addon_xml,
)
from kodi_lab.repository.policy import (
    DEVELOPMENT_REPOSITORY_ID,
    RESERVED_ADDON_IDS,
    require_development_addon_id,
)
from kodi_lab.repository.stable import (
    HEX_PATTERN,
    MAX_ZIP_BYTES,
    ZIP_DATE,
    _immutable,
    _write_exact,
    canonical,
    inspect_addon_zip,
    sha256,
)
from kodi_lab.storage.models import (
    DevelopmentPublicationHoldRow,
    DevelopmentPublicationRow,
    DevelopmentRepositoryStateRow,
)

VERSION = "1.0.0"
GENERATOR_ID = "kodi-lab-development-repository-v1"
VERSION_PATTERN = re.compile(r"(?:0|[1-9][0-9]{0,8})(?:\.(?:0|[1-9][0-9]{0,8})){2}\Z")
GENERATIONS = ("matrix", "nexus", "omega")
PYTHON_ABI = {"matrix": (3, 0, 0), "nexus": (3, 0, 1), "omega": (3, 0, 1)}
DEFAULT_MAX_ARTIFACTS = 128
MAX_VERIFIED_ARTIFACTS = 128
DEFAULT_MAX_BYTES = 256 * 1024 * 1024
DEFAULT_GRACE_DAYS = 7
MAX_PENDING_IMPORTS = 8
_LOCAL_LOCK = threading.RLock()


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _version(value: str) -> tuple[int, int, int]:
    """Use a strict numeric subset whose ordering equals Kodi CAddonVersion."""
    if not VERSION_PATTERN.fullmatch(value):
        raise ValueError("development add-on version must be major.minor.patch")
    parts = tuple(int(part) for part in value.split("."))
    return parts[0], parts[1], parts[2]


def _compatibility(value: tuple[str, ...]) -> str:
    if (
        not value
        or len(value) != len(set(value))
        or any(item not in GENERATIONS for item in value)
    ):
        raise ValueError("Kodi compatibility must select supported generations")
    return ",".join(item for item in GENERATIONS if item in value)


def descriptor_xml(base_url: str) -> bytes:
    addon = ET.Element(
        "addon",
        {
            "id": DEVELOPMENT_REPOSITORY_ID,
            "name": "Kodi Lab Development",
            "version": VERSION,
            "provider-name": "Kodi Lab",
        },
    )
    extension = ET.SubElement(
        addon,
        "extension",
        {"point": "xbmc.addon.repository", "name": "Kodi Lab Development"},
    )
    directory = ET.SubElement(extension, "dir", {"minversion": "19.0.0"})
    ET.SubElement(directory, "info", {"compressed": "false"}).text = (
        base_url + "/development/addons.xml"
    )
    ET.SubElement(directory, "checksum", {"verify": "sha256"}).text = (
        base_url + "/development/addons.xml.sha256"
    )
    ET.SubElement(directory, "datadir", {"zip": "true"}).text = (
        base_url + "/development/zips/"
    )
    ET.SubElement(directory, "hashes").text = "sha256"
    metadata = ET.SubElement(addon, "extension", {"point": "xbmc.addon.metadata"})
    ET.SubElement(
        metadata, "summary", {"lang": "en_GB"}
    ).text = "Optional Kodi Lab development add-ons"
    ET.SubElement(
        metadata, "description", {"lang": "en_GB"}
    ).text = "Approved test artifacts for development Kodi installations."
    ET.SubElement(metadata, "platform").text = "all"
    ET.SubElement(metadata, "license").text = "MIT"
    return bytes(ET.tostring(addon, encoding="utf-8", xml_declaration=True)) + b"\n"


def bootstrap_zip(base_url: str) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, content, directory in (
            (f"{DEVELOPMENT_REPOSITORY_ID}/", b"", True),
            (f"{DEVELOPMENT_REPOSITORY_ID}/addon.xml", descriptor_xml(base_url), False),
        ):
            info = zipfile.ZipInfo(name, ZIP_DATE)
            info.create_system = 3
            info.external_attr = ((0o40755 if directory else 0o100644) << 16) | (
                0x10 if directory else 0
            )
            info.compress_type = (
                zipfile.ZIP_STORED if directory else zipfile.ZIP_DEFLATED
            )
            archive.writestr(
                info, content, compress_type=info.compress_type, compresslevel=9
            )
    return output.getvalue()


def _index(packages: list[dict[str, str]]) -> bytes:
    root = ET.Element("addons")
    for record in sorted(
        (item for item in packages if item["status"] == "current"),
        key=lambda item: item["id"],
    ):
        package_path = Path(record["path"])
        package = inspect_addon_zip(
            package_path.read_bytes(), record["id"], record["version"]
        )
        if (
            package.sha256 != record["sha256"]
            or package.tree_sha256 != record["tree_sha256"]
        ):
            raise ValueError("development package bytes differ")
        root.append(package.descriptor)
    return bytes(ET.tostring(root, encoding="utf-8", xml_declaration=True)) + b"\n"


def _fsync(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _pointer(path: Path, value: str) -> None:
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write((value + "\n").encode())
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
        _fsync(path.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _generation(
    root: Path,
    base_url: str,
    package_records: list[dict[str, str]],
    *,
    candidate_identity: dict[str, str] | None = None,
) -> dict[str, str]:
    if candidate_identity is None:
        _, prior = load_current(root, expected_base_url=base_url)
        candidate_identity = {
            key: str(prior[key])
            for key in ("bundle_sha256", "source_commit", "source_tree")
        }
    bootstrap = bootstrap_zip(base_url)
    bootstrap_package = inspect_addon_zip(bootstrap, DEVELOPMENT_REPOSITORY_ID, VERSION)
    index = _index(package_records)
    index_digest = sha256(index)
    public_records = [
        {
            key: item[key]
            for key in (
                "publication_id",
                "id",
                "version",
                "sha256",
                "tree_sha256",
                "status",
            )
        }
        for item in sorted(
            package_records, key=lambda item: (item["id"], _version(item["version"]))
        )
    ]
    identity = {
        "schema": "kodi-lab-installed-development-repository-v1",
        "generator": GENERATOR_ID,
        "base_url": base_url,
        "repository_id": DEVELOPMENT_REPOSITORY_ID,
        "repository_version": VERSION,
        "bootstrap_zip_sha256": bootstrap_package.sha256,
        "bootstrap_tree_sha256": bootstrap_package.tree_sha256,
        "addons_xml_sha256": index_digest,
        "packages": public_records,
        **candidate_identity,
    }
    digest = sha256(canonical(identity))
    provenance = {**identity, "tree_sha256": digest}
    generations = root / "generations"
    generations.mkdir(mode=0o755, exist_ok=True)
    final = generations / digest
    if final.exists():
        if final.is_symlink() or (final / "PROVENANCE.json").read_bytes() != canonical(
            provenance
        ):
            raise ValueError("development generation identity differs")
        load_generation(root, digest, expected_base_url=base_url)
    else:
        temp = Path(tempfile.mkdtemp(prefix=".generation-", dir=generations))
        try:
            os.chmod(temp, 0o755)
            install = temp / "install"
            feed = temp / "development"
            install.mkdir(mode=0o755)
            feed.mkdir(mode=0o755)
            _write_exact(
                install / f"{DEVELOPMENT_REPOSITORY_ID}-{VERSION}.zip", bootstrap
            )
            _write_exact(feed / "addons.xml", index)
            _write_exact(feed / "addons.xml.sha256", (index_digest + "\n").encode())
            _write_exact(temp / "PROVENANCE.json", canonical(provenance))
            for file_path in temp.rglob("*"):
                os.chmod(file_path, 0o555 if file_path.is_dir() else 0o444)
            os.chmod(temp, 0o555)
            _fsync(install)
            _fsync(feed)
            _fsync(temp)
            os.rename(temp, final)
            _fsync(generations)
        finally:
            if temp.exists():
                shutil.rmtree(temp)
    return {
        "tree_sha256": digest,
        "addons_xml_sha256": index_digest,
        "bootstrap_zip_sha256": bootstrap_package.sha256,
        "bootstrap_tree_sha256": bootstrap_package.tree_sha256,
    }


def load_generation(
    root: Path, digest: str, *, expected_base_url: str | None = None
) -> tuple[Path, dict[str, object]]:
    """Verify a selected development generation and every referenced package."""
    if (
        not HEX_PATTERN.fullmatch(digest)
        or root.is_symlink()
        or (root / "generations").is_symlink()
    ):
        raise ValueError("development generation path is unsafe")
    selected = root / "generations" / digest
    if selected.is_symlink():
        raise ValueError("development generation path is unsafe")
    raw = (selected / "PROVENANCE.json").read_bytes()
    provenance = json.loads(raw)
    if (
        not isinstance(provenance, dict)
        or canonical(provenance) != raw
        or provenance.get("schema") != "kodi-lab-installed-development-repository-v1"
        or provenance.get("tree_sha256") != digest
        or provenance.get("generator") != GENERATOR_ID
        or provenance.get("repository_id") != DEVELOPMENT_REPOSITORY_ID
        or provenance.get("repository_version") != VERSION
    ):
        raise ValueError("development provenance differs")
    base_url = provenance.get("base_url")
    if not isinstance(base_url, str) or (
        expected_base_url is not None and base_url != expected_base_url
    ):
        raise ValueError("development origin differs")
    public_records = provenance.get("packages")
    if (
        not isinstance(public_records, list)
        or len(public_records) > MAX_VERIFIED_ARTIFACTS
    ):
        raise ValueError("development package inventory differs")
    rebuilt: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    total_bytes = 0
    for record in public_records:
        if not isinstance(record, dict) or set(record) != {
            "publication_id",
            "id",
            "version",
            "sha256",
            "tree_sha256",
            "status",
        }:
            raise ValueError("development package record differs")
        addon_id = record["id"]
        version = record["version"]
        require_development_addon_id(addon_id)
        _version(version)
        UUID(record["publication_id"])
        if (addon_id, version) in seen or record["status"] not in {
            "current",
            "superseded",
        }:
            raise ValueError("development package inventory duplicates")
        seen.add((addon_id, version))
        path = root / "packages" / addon_id / f"{addon_id}-{version}.zip"
        if (
            (root / "packages").is_symlink()
            or path.parent.is_symlink()
            or path.is_symlink()
            or path.stat().st_nlink != 1
        ):
            raise ValueError("development package path is unsafe")
        size = path.stat().st_size
        total_bytes += size
        if size > MAX_ZIP_BYTES or total_bytes > DEFAULT_MAX_BYTES:
            raise ValueError("development package inventory exceeds bounds")
        if (
            sha256(path.read_bytes()) != record["sha256"]
            or not HEX_PATTERN.fullmatch(record["tree_sha256"])
            or path.with_name(path.name + ".sha256").read_bytes()
            != (record["sha256"] + "\n").encode()
        ):
            raise ValueError("development package checksum differs")
        rebuilt.append({**record, "path": str(path)})
    expected_index = (selected / "development/addons.xml").read_bytes()
    if len(expected_index) > 8 * 1024 * 1024 or any(
        marker in expected_index.upper() for marker in (b"<!DOCTYPE", b"<!ENTITY")
    ):
        raise ValueError("development index is unsafe")
    try:
        parsed_index = ET.fromstring(expected_index)
    except ET.ParseError as error:
        raise ValueError("development index is malformed") from error
    expected_current = {
        (item["id"], item["version"]) for item in rebuilt if item["status"] == "current"
    }
    actual_current = {
        (item.attrib.get("id"), item.attrib.get("version")) for item in parsed_index
    }
    if (
        parsed_index.tag != "addons"
        or any(item.tag != "addon" for item in parsed_index)
        or len(actual_current) != len(parsed_index)
        or actual_current != expected_current
    ):
        raise ValueError("development index inventory differs")
    expected_identity = {
        key: provenance[key]
        for key in (
            "schema",
            "generator",
            "base_url",
            "repository_id",
            "repository_version",
            "bootstrap_zip_sha256",
            "bootstrap_tree_sha256",
            "addons_xml_sha256",
            "packages",
            "bundle_sha256",
            "source_commit",
            "source_tree",
        )
    }
    if (
        sha256(canonical(expected_identity)) != digest
        or sha256(expected_index) != provenance["addons_xml_sha256"]
        or (selected / "development/addons.xml.sha256").read_bytes()
        != (provenance["addons_xml_sha256"] + "\n").encode()
    ):
        raise ValueError("development index differs")
    bootstrap = bootstrap_zip(base_url)
    package = inspect_addon_zip(bootstrap, DEVELOPMENT_REPOSITORY_ID, VERSION)
    if (
        package.sha256 != provenance["bootstrap_zip_sha256"]
        or package.tree_sha256 != provenance["bootstrap_tree_sha256"]
        or (
            selected / "install" / f"{DEVELOPMENT_REPOSITORY_ID}-{VERSION}.zip"
        ).read_bytes()
        != bootstrap
    ):
        raise ValueError("development bootstrap differs")
    return selected, provenance


def load_current(
    root: Path, *, expected_base_url: str | None = None
) -> tuple[Path, dict[str, object]]:
    pointer = root / "CURRENT"
    ack = root / "ACK"
    if pointer.is_symlink() or ack.is_symlink():
        raise ValueError("development pointer is unsafe")
    digest = pointer.read_text().strip()
    if ack.read_text().strip() != digest:
        raise ValueError("development generation is not acknowledged")
    return load_generation(root, digest, expected_base_url=expected_base_url)


def verify_artifact_inventory(
    root: Path,
    records: list[tuple[str, str, str, str, str]],
    *,
    recover_known_leftovers: bool = False,
) -> None:
    """Match every stored package and import to a durable publication row.

    A crash may leave a verified approved import or retired package behind. Only
    those named, hash-matched leftovers may be removed during reconciliation.
    """
    packages_root = root / "packages"
    pending_root = root / "pending"
    if any(
        path.is_symlink() or not path.is_dir() for path in (packages_root, pending_root)
    ):
        raise ValueError("development artifact storage is unsafe")
    expected_packages: set[Path] = set()
    expected_pending: set[Path] = set()
    for identifier, addon_id, version, digest, status in records:
        require_development_addon_id(addon_id)
        _version(version)
        if not HEX_PATTERN.fullmatch(digest):
            raise ValueError("development artifact hash is malformed")
        package = packages_root / addon_id / f"{addon_id}-{version}.zip"
        checksum = package.with_name(package.name + ".sha256")
        pending = pending_root / f"{identifier}.zip"
        if package.parent.is_symlink():
            raise ValueError("development package directory is unsafe")
        if status in {"current", "superseded", "publishing"}:
            expected_packages.update((package, checksum))
        elif status == "prepared" and package.exists():
            # The immutable copy can precede a durable approval intent.
            expected_packages.update((package, checksum))
        elif status == "retired" and package.exists():
            if not recover_known_leftovers:
                raise ValueError("retired development package remains stored")
            for path in (package, checksum):
                if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
                    raise ValueError("retired development package path is unsafe")
            if (
                sha256(package.read_bytes()) != digest
                or checksum.read_bytes() != (digest + "\n").encode()
            ):
                raise ValueError("retired development package bytes differ")
            package.unlink()
            checksum.unlink()
        elif status not in {"prepared", "retired", "failed"}:
            raise ValueError("development publication status is invalid")
        if status == "prepared":
            expected_pending.add(pending)
        elif pending.exists():
            if (
                not recover_known_leftovers
                or pending.is_symlink()
                or not pending.is_file()
            ):
                raise ValueError("unexpected development import remains")
            if pending.stat().st_nlink != 1 or sha256(pending.read_bytes()) != digest:
                raise ValueError("development import bytes differ")
            pending.unlink()
        if package in expected_packages:
            if (
                package.is_symlink()
                or checksum.is_symlink()
                or package.stat().st_nlink != 1
            ):
                raise ValueError("development package path is unsafe")
            if (
                sha256(package.read_bytes()) != digest
                or checksum.read_bytes() != (digest + "\n").encode()
            ):
                raise ValueError("development package bytes differ")
        if pending in expected_pending and (
            pending.is_symlink()
            or pending.stat().st_nlink != 1
            or sha256(pending.read_bytes()) != digest
        ):
            raise ValueError("prepared development import bytes differ")
    actual_packages: set[Path] = set()
    for addon_dir in packages_root.iterdir():
        if addon_dir.is_symlink() or not addon_dir.is_dir():
            raise ValueError("development package directory is unsafe")
        actual_packages.update(addon_dir.iterdir())
    actual_pending = set(pending_root.iterdir())
    if actual_packages != expected_packages or actual_pending != expected_pending:
        raise ValueError("development artifact inventory contains unknown bytes")


def _candidate_identity(bundle: Path) -> dict[str, str]:
    manifest_bytes = (bundle / "BUNDLE.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    reviewed_bytes = (bundle / "repository/DEVELOPMENT.json").read_bytes()
    reviewed = json.loads(reviewed_bytes)
    expected = {
        "schema": "kodi-lab-development-repository-input-v1",
        "repository_id": DEVELOPMENT_REPOSITORY_ID,
        "repository_version": VERSION,
        "generator": GENERATOR_ID,
        "allowed_type": "plugin.video",
        "reserved_ids": sorted(RESERVED_ADDON_IDS),
    }
    if (
        canonical(manifest) != manifest_bytes
        or manifest.get("schema") != "kodi-lab-linux-v1-bundle-v3"
        or canonical(reviewed) != reviewed_bytes
        or reviewed != expected
        or manifest.get("files", {}).get("repository/DEVELOPMENT.json")
        != {"sha256": "sha256:" + sha256(reviewed_bytes), "size": len(reviewed_bytes)}
    ):
        raise ValueError("reviewed development repository inputs differ")
    return {
        "bundle_sha256": sha256(manifest_bytes),
        "source_commit": str(manifest["source_commit"]),
        "source_tree": str(manifest["source_tree"]),
    }


def initialize(root: Path, base_url: str, bundle: Path) -> dict[str, str]:
    """One-shot source-free generation; never replace an initialized feed."""
    candidate_identity = _candidate_identity(bundle)
    root.mkdir(mode=0o755, exist_ok=True)
    for name in ("generations", "packages", "pending"):
        (root / name).mkdir(mode=0o755 if name != "pending" else 0o700, exist_ok=True)
    current = root / "CURRENT"
    if current.exists():
        _, existing = load_current(root, expected_base_url=base_url)
        if any(existing[key] != value for key, value in candidate_identity.items()):
            raise ValueError("development feed belongs to another candidate")
        return {
            key: str(existing[key])
            for key in (
                "tree_sha256",
                "addons_xml_sha256",
                "bootstrap_zip_sha256",
                "bootstrap_tree_sha256",
            )
        }
    result = _generation(root, base_url, [], candidate_identity=candidate_identity)
    _pointer(current, result["tree_sha256"])
    _pointer(root / "ACK", result["tree_sha256"])
    return result


@contextmanager
def publication_lock(root: Path) -> Iterator[None]:
    with _LOCAL_LOCK:
        fd = os.open(
            root / ".publication.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
        )
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


class DevelopmentRepositoryService:
    """Authenticated application-side publication authority; no LAN write API."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        data_root: Path,
        base_url: str,
        *,
        max_artifacts: int = DEFAULT_MAX_ARTIFACTS,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> None:
        if (
            not 1 <= max_artifacts <= MAX_VERIFIED_ARTIFACTS
            or not 1 <= max_bytes <= DEFAULT_MAX_BYTES
        ):
            raise ValueError("development repository quota configuration is invalid")
        self.sessions = sessions
        self.root = data_root / "development-data"
        self.base_url = base_url
        self.max_artifacts = max_artifacts
        self.max_bytes = max_bytes

    def _state(self, session: Session) -> DevelopmentRepositoryStateRow:
        state = session.get(DevelopmentRepositoryStateRow, 1)
        if state is None:
            _, provenance = load_current(self.root, expected_base_url=self.base_url)
            state = DevelopmentRepositoryStateRow(
                id=1,
                base_url=self.base_url,
                bootstrap_sha256=str(provenance["bootstrap_zip_sha256"]),
                bootstrap_tree_sha256=str(provenance["bootstrap_tree_sha256"]),
                current_generation=str(provenance["tree_sha256"]),
                current_index_sha256=str(provenance["addons_xml_sha256"]),
                updated_at=_now(),
            )
            session.add(state)
            session.commit()
        if state.base_url != self.base_url:
            raise ValueError("development repository origin differs from durable state")
        return state

    def reconcile(self) -> dict[str, object]:
        """Finish only known approved switches; unknown bytes fail closed."""
        with publication_lock(self.root), self.sessions() as session:
            self.reconcile_unlocked(session)
            return load_current(self.root, expected_base_url=self.base_url)[1]

    def prepare_import(
        self,
        data: bytes,
        compatibility: tuple[str, ...],
        *,
        scope_kind: str = "development_only",
        intended_targets: tuple[str, ...] = (),
    ) -> DevelopmentPublicationRow:
        if scope_kind != "development_only" or intended_targets:
            raise ValueError("Phase 2 publication is development-only and unbound")
        package = inspect_addon_zip(data)
        require_development_addon_id(package.addon_id)
        _version(package.version)
        supported = _compatibility(compatibility)
        xml = package.descriptor
        try:
            observation = parse_captured_addon_xml(
                package.addon_id, ET.tostring(xml, encoding="utf-8")
            )
        except DevelopmentSourceValidationError as error:
            raise ValueError("development add-on descriptor is invalid") from error
        library = copied_video_plugin_library(observation)
        if library is None or f"{package.addon_id}/{library}" not in package.file_names:
            raise ValueError("development artifact must be a valid Python video plugin")
        required = observation.dependencies
        if (
            len(required) != 1
            or required[0].optional
            or required[0].dependency_id != "xbmc.python"
            or required[0].version is None
        ):
            raise ValueError("development plugin must require only xbmc.python")
        dependency = _version(required[0].version)
        if dependency < (3, 0, 0) or any(
            dependency > PYTHON_ABI[generation] for generation in compatibility
        ):
            raise ValueError(
                "development plugin is incompatible with selected Kodi generation"
            )
        with publication_lock(self.root), self.sessions() as session:
            self.reconcile_unlocked(session)
            existing = session.scalar(
                select(DevelopmentPublicationRow).where(
                    DevelopmentPublicationRow.addon_id == package.addon_id,
                    DevelopmentPublicationRow.version == package.version,
                )
            )
            if existing is not None:
                if existing.zip_sha256 != package.sha256:
                    raise ValueError("published add-on ID/version has different bytes")
                return existing
            stored = list(
                session.scalars(
                    select(DevelopmentPublicationRow).where(
                        DevelopmentPublicationRow.status.in_(
                            ("prepared", "current", "superseded")
                        )
                    )
                )
            )
            if (
                sum(row.status == "prepared" for row in stored) >= MAX_PENDING_IMPORTS
                or len(stored) + 1 > self.max_artifacts
                or sum(row.zip_bytes for row in stored) + len(data) > self.max_bytes
            ):
                raise ValueError("development repository quota is exhausted")
            identifier = str(uuid4())
            stage = self.root / "pending" / f"{identifier}.zip"
            _immutable(stage, data)
            record = DevelopmentPublicationRow(
                id=identifier,
                channel="development",
                addon_id=package.addon_id,
                version=package.version,
                addon_type="plugin.video",
                status="prepared",
                source_kind="web_import",
                source_id=identifier,
                workspace_id=None,
                workspace_generation=None,
                validation_receipt=None,
                source_tree_sha256=None,
                compatible_generations=supported,
                scope_kind="development_only",
                intended_targets=None,
                zip_sha256=package.sha256,
                tree_sha256=package.tree_sha256,
                zip_bytes=len(data),
                created_at=_now(),
            )
            session.add(record)
            session.commit()
            session.refresh(record)
            return record

    def _records(
        self, session: Session, extra: DevelopmentPublicationRow | None = None
    ) -> list[dict[str, str]]:
        rows = list(
            session.scalars(
                select(DevelopmentPublicationRow).where(
                    DevelopmentPublicationRow.status.in_(("current", "superseded"))
                )
            )
        )
        if extra is not None:
            rows.append(extra)
        result = []
        for row in rows:
            status = (
                "current"
                if row is extra
                else (
                    "superseded"
                    if extra is not None and row.addon_id == extra.addon_id
                    else row.status
                )
            )
            path = (
                self.root
                / "packages"
                / row.addon_id
                / f"{row.addon_id}-{row.version}.zip"
            )
            result.append(
                {
                    "publication_id": row.id,
                    "id": row.addon_id,
                    "version": row.version,
                    "sha256": row.zip_sha256,
                    "tree_sha256": row.tree_sha256,
                    "status": status,
                    "path": str(path),
                }
            )
        return result

    def approve(
        self,
        publication_id: UUID,
        *,
        observed_sha256: str,
        approval_identity: str = "local_admin",
    ) -> DevelopmentPublicationRow:
        self.prune()
        with publication_lock(self.root), self.sessions() as session:
            self.reconcile_unlocked(session)
            record = session.get(DevelopmentPublicationRow, str(publication_id))
            if (
                record is None
                or record.status != "prepared"
                or record.zip_sha256 != observed_sha256
                or not approval_identity
                or len(approval_identity) > 128
            ):
                raise ValueError("prepared publication identity or approval differs")
            current = session.scalar(
                select(DevelopmentPublicationRow).where(
                    DevelopmentPublicationRow.addon_id == record.addon_id,
                    DevelopmentPublicationRow.status == "current",
                )
            )
            if current is not None and _version(record.version) <= _version(
                current.version
            ):
                raise ValueError("new development version must exceed current version")
            retained = list(
                session.scalars(
                    select(DevelopmentPublicationRow).where(
                        DevelopmentPublicationRow.status.in_(
                            ("prepared", "current", "superseded")
                        )
                    )
                )
            )
            if (
                len(retained) > self.max_artifacts
                or sum(row.zip_bytes for row in retained) > self.max_bytes
            ):
                raise ValueError("development repository quota is exhausted")
            pending = self.root / "pending" / f"{record.id}.zip"
            body = pending.read_bytes()
            checked = inspect_addon_zip(body, record.addon_id, record.version)
            if (
                checked.sha256 != record.zip_sha256
                or checked.tree_sha256 != record.tree_sha256
            ):
                raise ValueError("prepared development package bytes differ")
            package_path = (
                self.root
                / "packages"
                / record.addon_id
                / f"{record.addon_id}-{record.version}.zip"
            )
            _immutable(package_path, body)
            os.chmod(package_path.parent, 0o755)
            _immutable(
                package_path.with_name(package_path.name + ".sha256"),
                (record.zip_sha256 + "\n").encode(),
            )
            os.chmod(package_path, 0o444)
            os.chmod(package_path.with_name(package_path.name + ".sha256"), 0o444)
            candidate = _generation(
                self.root, self.base_url, self._records(session, record)
            )
            record.status = "publishing"
            record.approval_identity = approval_identity
            record.approved_at = _now()
            record.feed_generation = candidate["tree_sha256"]
            record.index_sha256 = candidate["addons_xml_sha256"]
            session.commit()  # Durable approved intent precedes the public switch.
            (self.root / "ACK").unlink(missing_ok=True)
            _pointer(self.root / "CURRENT", candidate["tree_sha256"])
            self.reconcile_unlocked(session)
            return record

    def reconcile_unlocked(self, session: Session) -> None:
        # Internal equivalent of reconcile() while publication_lock is held.
        state = self._state(session)
        pointer = (self.root / "CURRENT").read_text().strip()
        _, provenance = load_generation(
            self.root, pointer, expected_base_url=self.base_url
        )
        publishing = list(
            session.scalars(
                select(DevelopmentPublicationRow).where(
                    DevelopmentPublicationRow.status == "publishing"
                )
            )
        )
        if pointer != state.current_generation:
            if (
                state.pending_generation == pointer
                and state.pending_retirements is not None
                and not publishing
            ):
                ids = json.loads(state.pending_retirements)
                if not isinstance(ids, list) or not all(
                    isinstance(item, str) for item in ids
                ):
                    raise ValueError("retention intent is malformed")
                for identifier in ids:
                    row = session.get(DevelopmentPublicationRow, identifier)
                    if row is None or row.status != "superseded":
                        raise ValueError("retention intent differs")
                    row.status = "retired"
                    row.retired_at = _now()
                state.pending_generation = None
                state.pending_index_sha256 = None
                state.pending_retirements = None
            else:
                if (
                    len(publishing) != 1
                    or publishing[0].feed_generation != pointer
                    or publishing[0].approved_at is None
                    or publishing[0].approval_identity is None
                ):
                    raise ValueError("development generation has no approved intent")
                record = publishing[0]
                previous = session.scalar(
                    select(DevelopmentPublicationRow).where(
                        DevelopmentPublicationRow.addon_id == record.addon_id,
                        DevelopmentPublicationRow.status == "current",
                    )
                )
                if previous is not None:
                    previous.status = "superseded"
                    previous.superseded_at = _now()
                    # Release the unique current index in this transaction.
                    session.flush()
                record.status = "current"
                record.published_at = _now()
            state.current_generation = pointer
            state.current_index_sha256 = str(provenance["addons_xml_sha256"])
            state.updated_at = _now()
            session.commit()
        elif publishing:
            # The approved generation was durable before the interrupted switch.
            if (
                len(publishing) != 1
                or publishing[0].approved_at is None
                or publishing[0].feed_generation is None
            ):
                raise ValueError("interrupted publication intent is ambiguous")
            pending_generation = publishing[0].feed_generation
            _, pending_provenance = load_generation(
                self.root, pending_generation, expected_base_url=self.base_url
            )
            if not any(
                item["publication_id"] == publishing[0].id
                and item["sha256"] == publishing[0].zip_sha256
                and item["status"] == "current"
                for item in cast(list[dict[str, str]], pending_provenance["packages"])
            ):
                raise ValueError("interrupted publication generation differs")
            (self.root / "ACK").unlink(missing_ok=True)
            _pointer(self.root / "CURRENT", pending_generation)
            self.reconcile_unlocked(session)
            return
        elif state.pending_generation is not None:
            _, pending_provenance = load_generation(
                self.root, state.pending_generation, expected_base_url=self.base_url
            )
            if pending_provenance["addons_xml_sha256"] != state.pending_index_sha256:
                raise ValueError("interrupted retention generation differs")
            (self.root / "ACK").unlink(missing_ok=True)
            _pointer(self.root / "CURRENT", state.pending_generation)
            self.reconcile_unlocked(session)
            return
        expected = {
            (
                row.id,
                row.addon_id,
                row.version,
                row.zip_sha256,
                row.tree_sha256,
                row.status,
            )
            for row in session.scalars(
                select(DevelopmentPublicationRow).where(
                    DevelopmentPublicationRow.status.in_(("current", "superseded"))
                )
            )
        }
        actual = {
            (
                item["publication_id"],
                item["id"],
                item["version"],
                item["sha256"],
                item["tree_sha256"],
                item["status"],
            )
            for item in cast(list[dict[str, str]], provenance["packages"])
        }
        if (
            expected != actual
            or state.current_generation != pointer
            or state.current_index_sha256 != provenance["addons_xml_sha256"]
            or state.bootstrap_sha256 != provenance["bootstrap_zip_sha256"]
            or state.bootstrap_tree_sha256 != provenance["bootstrap_tree_sha256"]
        ):
            raise ValueError("development database and feed inventory disagree")
        verify_artifact_inventory(
            self.root,
            [
                (row.id, row.addon_id, row.version, row.zip_sha256, row.status)
                for row in session.scalars(select(DevelopmentPublicationRow))
            ],
            recover_known_leftovers=True,
        )
        if (
            not (self.root / "ACK").exists()
            or (self.root / "ACK").read_text().strip() != pointer
        ):
            _pointer(self.root / "ACK", pointer)

    def list_publications(self) -> list[DevelopmentPublicationRow]:
        with self.sessions() as session:
            return list(
                session.scalars(
                    select(DevelopmentPublicationRow).order_by(
                        DevelopmentPublicationRow.created_at.desc()
                    )
                )
            )

    def get_publication(self, publication_id: UUID) -> DevelopmentPublicationRow | None:
        with self.sessions() as session:
            return session.get(DevelopmentPublicationRow, str(publication_id))

    def storage_usage(self) -> tuple[int, int]:
        with self.sessions() as session:
            rows = session.scalars(
                select(DevelopmentPublicationRow).where(
                    DevelopmentPublicationRow.status.in_(
                        ("prepared", "current", "superseded")
                    )
                )
            )
            records = list(rows)
            return len(records), sum(row.zip_bytes for row in records)

    def prune(self, *, grace_days: int = DEFAULT_GRACE_DAYS) -> list[str]:
        """Retire only unheld, old superseded bytes beyond two prior versions."""
        with publication_lock(self.root), self.sessions() as session:
            self.reconcile_unlocked(session)
            rows = list(
                session.scalars(
                    select(DevelopmentPublicationRow).where(
                        DevelopmentPublicationRow.status == "superseded"
                    )
                )
            )
            by_id: dict[str, list[DevelopmentPublicationRow]] = {}
            for row in rows:
                by_id.setdefault(row.addon_id, []).append(row)
            selected: list[DevelopmentPublicationRow] = []
            cutoff = datetime.now(UTC) - timedelta(days=grace_days)
            for versions in by_id.values():
                versions.sort(key=lambda row: _version(row.version), reverse=True)
                for row in versions[2:]:
                    if (
                        row.superseded_at is None
                        or datetime.fromisoformat(row.superseded_at) > cutoff
                    ):
                        continue
                    if (
                        session.scalar(
                            select(DevelopmentPublicationHoldRow).where(
                                DevelopmentPublicationHoldRow.publication_id == row.id
                            )
                        )
                        is None
                    ):
                        selected.append(row)
            if not selected:
                return []
            remaining = [
                item
                for item in self._records(session)
                if item["publication_id"] not in {row.id for row in selected}
            ]
            candidate = _generation(self.root, self.base_url, remaining)
            state = self._state(session)
            state.pending_generation = candidate["tree_sha256"]
            state.pending_index_sha256 = candidate["addons_xml_sha256"]
            state.pending_retirements = json.dumps(
                [row.id for row in selected], separators=(",", ":")
            )
            session.commit()
            (self.root / "ACK").unlink(missing_ok=True)
            _pointer(self.root / "CURRENT", candidate["tree_sha256"])
            self.reconcile_unlocked(session)
            for row in selected:
                path = (
                    self.root
                    / "packages"
                    / row.addon_id
                    / f"{row.addon_id}-{row.version}.zip"
                )
                path.unlink(missing_ok=True)
                path.with_name(path.name + ".sha256").unlink(missing_ok=True)
            return [row.id for row in selected]
