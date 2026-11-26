"""Receipt-based retirement of redundant private feed copies."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from kodi_lab.storage.inventory_snapshot import read_file

from .packages import ADDON_ID, HEX, MAX_ZIP, VERSION, Package, addon_id
from .protocol import Refusal, actor_key
from .runtime import atomic, require_unmounted_tree, sync_dir

FEED_RETIREMENT_FILE = "feed-retirement.json"
CATALOG_LIMIT = 128
PENDING_LIMIT = 96
INDEX_LIMIT = 8 * 1024**2
RECEIPT_LIMIT = 160 * 1024

Digest = Annotated[str, Field(pattern=f"^{HEX.pattern}$")]
Addon = Annotated[str, Field(pattern=f"^{ADDON_ID.pattern}$")]
Version = Annotated[str, Field(pattern=f"^{VERSION.pattern}$")]
Actor = Annotated[str, Field(pattern=r"^(admin|[0-9a-f-]{36})$")]


class FeedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class FeedCopy(FeedModel):
    addon: Addon
    version: Version
    digest: Digest
    source: Actor

    @property
    def name(self) -> str:
        return f"{self.addon}-{self.version}.zip"


class Publication(FeedModel):
    index: Digest
    copies: Annotated[tuple[FeedCopy, ...], Field(max_length=CATALOG_LIMIT)]


class PendingCopy(FeedModel):
    artifact: FeedCopy
    phase: Literal["pending", "removing"] = "pending"


class FeedState(FeedModel):
    revision: Literal[1] = 1
    target: str
    current: Publication | None = None
    planned: Publication | None = None
    pending: Annotated[tuple[PendingCopy, ...], Field(max_length=PENDING_LIMIT)] = ()


class FeedRetirement:
    def __init__(self, managed_root: Path, generation_root: Path, target: UUID) -> None:
        self.managed_root = managed_root
        self.root = generation_root
        self.feed = self.root / "feed"
        self.record = self.root / FEED_RETIREMENT_FILE
        self.target = str(target)

    def read(self) -> FeedState:
        try:
            record = FeedState.model_validate_json(
                read_file(self.record, RECEIPT_LIMIT)
            )
        except FileNotFoundError:
            return FeedState(target=self.target)
        if record.target != self.target:
            raise Refusal("feed_retirement_site_mismatch")
        for index in (record.current, record.planned):
            if index and len(
                {(item.addon, item.version) for item in index.copies}
            ) != len(index.copies):
                raise Refusal("feed_publication_duplicate_path")
        keys = {(item.artifact.addon, item.artifact.version) for item in record.pending}
        if len(keys) != len(record.pending):
            raise Refusal("feed_retirement_duplicate_path")
        return record

    def write(self, state: FeedState) -> None:
        payload = state.model_dump_json().encode()
        if len(payload) > RECEIPT_LIMIT:
            raise Refusal("feed_retirement_receipt_limit")
        atomic(self.record, payload)

    def index_matches(self, publication: Publication) -> bool:
        if self.feed.is_symlink():
            return False
        try:
            index = read_file(self.feed / "addons.xml", INDEX_LIMIT)
            advertised = read_file(self.feed / "addons.xml.sha256", 65)
        except FileNotFoundError:
            return False
        return (
            hashlib.sha256(index).hexdigest() == publication.index
            and advertised == (publication.index + "\n").encode()
        )

    def reconcile(self, state: FeedState) -> FeedState:
        planned = state.planned
        if planned is None or not self.index_matches(planned):
            return state
        active = {(item.addon, item.version) for item in planned.copies}
        pending = {
            (item.artifact.addon, item.artifact.version): item
            for item in state.pending
            if (item.artifact.addon, item.artifact.version) not in active
        }
        # Earlier or overlapping feed paths without a tracked publication remain
        # inventory only. Current paths can be overwritten by same-version updates.
        if state.current is not None:
            for item in state.current.copies:
                key = (item.addon, item.version)
                if (
                    key not in active
                    and key not in pending
                    and len(pending) < PENDING_LIMIT
                ):
                    pending[key] = PendingCopy(artifact=item)
        settled = FeedState(
            target=self.target, current=planned, pending=tuple(pending.values())
        )
        self.write(settled)
        return settled

    def plan(
        self,
        package: Package,
        candidates: Mapping[str, Package],
        index: str,
        owner: str,
    ) -> None:
        actor_key(owner)
        if len(candidates) > CATALOG_LIMIT:
            raise Refusal("feed_retirement_catalog_limit")
        state = self.reconcile(self.read())
        current = state.current
        if state.planned is not None or (current and not self.index_matches(current)):
            # An abandoned or changed publication is never a deletion proof.
            current = None
        copies = tuple(
            FeedCopy(
                addon=pkg.id,
                version=pkg.version,
                digest=pkg.digest,
                source=owner if pkg.id == package.id else "admin",
            )
            for pkg in sorted(candidates.values(), key=lambda pkg: pkg.id)
        )
        self.write(
            FeedState(
                target=self.target,
                current=current,
                pending=state.pending,
                planned=Publication(index=index, copies=copies),
            )
        )

    def confirm_publication(self) -> None:
        # Recheck the committed index and its advertised checksum before making
        # old paths available to maintenance.
        self.reconcile(self.read())

    def canonical(self, item: FeedCopy) -> Path:
        actor = actor_key(item.source)
        base = self.managed_root
        if actor != "admin":
            base = base / "agents" / actor
        return base / "approved" / (item.digest + ".zip")

    def approved_catalog(self) -> set[str]:
        try:
            raw = json.loads(read_file(self.managed_root / "catalog.json", INDEX_LIMIT))
        except FileNotFoundError:
            return set()
        if not isinstance(raw, list) or not all(
            isinstance(item, str) and HEX.fullmatch(item) for item in raw
        ):
            raise Refusal("feed_catalog_protection_unknown")
        return set(raw)

    def prove(self, state: FeedState, pending: PendingCopy) -> RetirementProof | None:
        current, item = state.current, pending.artifact
        if (
            not current
            or state.planned is not None
            or any(
                (copy.addon, copy.version) == (item.addon, item.version)
                for copy in current.copies
            )
        ):
            return None
        addon_id(item.addon)
        base = self.feed / "zips" / item.addon
        target = base / item.name
        sidecar = target.with_suffix(".zip.sha256")
        canonical = self.canonical(item)
        ancestors = canonical.relative_to(self.managed_root).parts[:-1]
        folders = (
            self.feed,
            base.parent,
            base,
            *(
                self.managed_root.joinpath(*ancestors[:depth])
                for depth in range(1, len(ancestors) + 1)
            ),
        )
        if any(path.is_symlink() or not path.is_dir() for path in folders):
            return None
        require_unmounted_tree(base)
        paths = (
            *folders,
            target,
            sidecar,
            canonical,
            self.feed / "addons.xml",
            self.feed / "addons.xml.sha256",
            self.managed_root / "catalog.json",
        )
        before = snapshot_paths(paths)
        if not self.index_matches(current) or item.digest in self.approved_catalog():
            return None
        if file_digest(canonical) != item.digest:
            return None
        removing = pending.phase == "removing"
        primary = file_digest(target)
        try:
            secondary = read_file(sidecar, 65)
        except FileNotFoundError:
            secondary = None
        if (
            (primary is None and not removing)
            or (secondary is None and not removing)
            or (primary is not None and primary != item.digest)
            or (secondary is not None and secondary != (item.digest + "\n").encode())
            or snapshot_paths(paths) != before
        ):
            return None
        return RetirementProof(target, sidecar, paths, before)

    def retire(
        self, state: FeedState, pending: PendingCopy, proof: RetirementProof
    ) -> bool:
        # The controller holds the site lock and has observed an absent managed
        # container. Large archive reads occurred outside the lifecycle lock.
        require_unmounted_tree(proof.target.parent)
        if self.read() != state or snapshot_paths(proof.paths) != proof.identities:
            return False
        if pending.phase == "pending":
            marked = PendingCopy(artifact=pending.artifact, phase="removing")
            self.write(
                FeedState(
                    target=state.target,
                    current=state.current,
                    pending=tuple(
                        marked if entry == pending else entry for entry in state.pending
                    ),
                )
            )
        else:
            sync_dir(self.record.parent)
        proof.target.unlink(missing_ok=True)
        proof.sidecar.unlink(missing_ok=True)
        sync_dir(proof.target.parent)
        self.write(
            FeedState(
                target=state.target,
                current=state.current,
                pending=tuple(entry for entry in state.pending if entry != pending),
            )
        )
        return True


Identity = tuple[int, int, int, int, int]


@dataclass(frozen=True)
class RetirementProof:
    target: Path
    sidecar: Path
    paths: tuple[Path, ...]
    identities: tuple[Identity | None, ...]


def snapshot_paths(paths: tuple[Path, ...]) -> tuple[Identity | None, ...]:
    identities: list[Identity | None] = []
    for path in paths:
        try:
            observed = path.lstat()
        except FileNotFoundError:
            identities.append(None)
        else:
            identities.append(
                (
                    observed.st_dev,
                    observed.st_ino,
                    observed.st_size,
                    observed.st_mtime_ns,
                    observed.st_ctime_ns,
                )
            )
    return tuple(identities)


def file_digest(path: Path) -> str | None:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    with os.fdopen(descriptor, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_size > MAX_ZIP
        ):
            raise Refusal("feed_archive_identity")
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
        observed = os.fstat(stream.fileno())
        if (observed.st_size, observed.st_mtime_ns) != (
            metadata.st_size,
            metadata.st_mtime_ns,
        ):
            raise Refusal("feed_archive_changed")
        return digest
