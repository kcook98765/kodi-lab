"""Receipt-based retirement of redundant private feed copies."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal, cast
from uuid import UUID
from xml.etree import ElementTree as ET

from pydantic import BaseModel, ConfigDict, Field

from kodi_lab.storage.inventory_snapshot import read_file

from .packages import ADDON_ID, HEX, MAX_ZIP, VERSION, Package, addon_id
from .protocol import Refusal, actor_key
from .runtime import LABEL, Runtime, atomic, require_unmounted_tree, sync_dir

FEED_RETIREMENT_FILE = "feed-retirement.json"
CATALOG_LIMIT = 128
PENDING_LIMIT = 96
INDEX_LIMIT = 8 * 1024**2
RECEIPT_LIMIT = 160 * 1024


class FeedCapacity(Refusal):
    """Publication would exceed the bounded durable cleanup queue."""


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
    engine: str | None = None
    current: Publication | None = None
    planned: Publication | None = None
    pending: Annotated[tuple[PendingCopy, ...], Field(max_length=PENDING_LIMIT)] = ()


class FeedRetirement:
    def __init__(
        self,
        managed_root: Path,
        generation_root: Path,
        target: UUID,
        engine: str | None = None,
    ) -> None:
        self.managed_root = managed_root
        self.root = generation_root
        self.feed = self.root / "feed"
        self.record = self.root / FEED_RETIREMENT_FILE
        self.hold = self.root / "feed-retirement.hold"
        self.target = str(target)
        self.engine = engine

    def read(self) -> FeedState:
        try:
            record = FeedState.model_validate_json(
                read_file(self.record, RECEIPT_LIMIT)
            )
        except FileNotFoundError:
            return FeedState(target=self.target, engine=self.engine)
        if record.target != self.target or (
            self.engine and record.engine != self.engine
        ):
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
        if (
            hashlib.sha256(index).hexdigest() != publication.index
            or advertised != (publication.index + "\n").encode()
        ):
            return False
        try:
            if b"<!" in index.replace(b"<!--", b""):
                return False
            document = ET.fromstring(
                index, parser=ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))
            )
        except ET.ParseError:
            return False
        children = list(document)
        if document.tag != "addons" or len(children) != 2 * len(publication.copies):
            return False
        observed: set[tuple[str | None, str | None, str]] = set()
        for comment, addon in zip(children[::2], children[1::2], strict=True):
            if (
                cast(object, comment.tag) is not ET.Comment
                or addon.tag != "addon"
                or not comment.text
            ):
                return False
            label = "package sha256 "
            if not comment.text.startswith(label):
                return False
            observed.add(
                (addon.get("id"), addon.get("version"), comment.text[len(label) :])
            )
        exact = {(item.addon, item.version, item.digest) for item in publication.copies}
        return observed == exact

    def planned_proof(self, state: FeedState) -> IndexProof | None:
        if state.planned is None:
            return None
        paths = (self.feed, self.feed / "addons.xml", self.feed / "addons.xml.sha256")
        before = snapshot_paths(paths)
        if not self.index_matches(state.planned) or snapshot_paths(paths) != before:
            return None
        return IndexProof(paths, before)

    def reconcile(self, state: FeedState, proof: IndexProof | None) -> FeedState:
        planned = state.planned
        if (
            planned is None
            or proof is None
            or snapshot_paths(proof.paths) != proof.identities
        ):
            return state
        sync_dir(self.feed)
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
                if key not in active and key not in pending:
                    if len(pending) >= PENDING_LIMIT:
                        raise FeedCapacity("feed_retirement_capacity")
                    pending[key] = PendingCopy(artifact=item)
        settled = FeedState(
            target=self.target,
            engine=state.engine,
            current=planned,
            pending=tuple(pending.values()),
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
        if self.engine is None:
            raise Refusal("feed_engine_identity_unavailable")
        state = self.read()
        state = self.reconcile(state, self.planned_proof(state))
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
        protected = {(item.addon, item.version) for item in copies}
        obsolete = {
            (item.artifact.addon, item.artifact.version) for item in state.pending
        }
        if current is not None:
            obsolete.update((item.addon, item.version) for item in current.copies)
        if len(obsolete - protected) > PENDING_LIMIT:
            raise FeedCapacity("feed_retirement_capacity")
        self.write(
            FeedState(
                target=self.target,
                engine=self.engine,
                current=current,
                pending=state.pending,
                planned=Publication(index=index, copies=copies),
            )
        )

    def confirm_publication(self) -> None:
        # Recheck the committed index and its advertised checksum before making
        # old paths available to maintenance.
        state = self.read()
        self.reconcile(state, self.planned_proof(state))

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
        self.require_feed_mounts()
        canonical_root = self.managed_root / ancestors[0]
        require_unmounted_tree(canonical_root)
        paths = (
            *folders,
            target,
            sidecar,
            canonical,
            self.feed / "addons.xml",
            self.feed / "addons.xml.sha256",
            self.managed_root / "catalog.json",
            self.hold,
        )
        before = snapshot_paths(paths)
        if before[-1] is not None:
            return None
        if not self.index_matches(current) or item.digest in self.approved_catalog():
            return None
        if file_digest(canonical, durable=True) != item.digest:
            return None
        for folder in canonical.parents:
            sync_dir(folder)
            if folder == self.managed_root:
                break
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
        return RetirementProof(target, sidecar, canonical_root, paths, before)

    def require_feed_mounts(self) -> None:
        require_unmounted_tree(
            self.root if self.root != self.managed_root else self.feed
        )

    def retire(
        self, state: FeedState, pending: PendingCopy, proof: RetirementProof
    ) -> bool:
        # The controller holds the site lock and has observed an absent managed
        # container. Large archive reads occurred outside the lifecycle lock.
        self.require_feed_mounts()
        require_unmounted_tree(proof.canonical_root)
        if self.read() != state or snapshot_paths(proof.paths) != proof.identities:
            return False
        if pending.phase == "pending":
            marked = PendingCopy(artifact=pending.artifact, phase="removing")
            self.write(
                FeedState(
                    target=state.target,
                    engine=state.engine,
                    current=state.current,
                    pending=tuple(
                        marked if entry == pending else entry for entry in state.pending
                    ),
                )
            )
        else:
            sync_dir(self.record.parent)
        self.require_feed_mounts()
        require_unmounted_tree(proof.canonical_root)
        if snapshot_paths(proof.paths) != proof.identities:
            return False
        proof.target.unlink(missing_ok=True)
        proof.sidecar.unlink(missing_ok=True)
        sync_dir(proof.target.parent)
        self.write(
            FeedState(
                target=state.target,
                engine=state.engine,
                current=state.current,
                pending=tuple(entry for entry in state.pending if entry != pending),
            )
        )
        return True


Identity = tuple[int, int, int, int, int]


@dataclass(frozen=True)
class IndexProof:
    paths: tuple[Path, ...]
    identities: tuple[Identity | None, ...]


@dataclass(frozen=True)
class RetirementProof:
    target: Path
    sidecar: Path
    canonical_root: Path
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


def file_digest(path: Path, *, durable: bool = False) -> str | None:
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
        if durable:
            os.fsync(stream.fileno())
        observed = os.fstat(stream.fileno())
        if (observed.st_size, observed.st_mtime_ns) != (
            metadata.st_size,
            metadata.st_mtime_ns,
        ):
            raise Refusal("feed_archive_changed")
        return digest


def docker_read(*args: str, deadline: float) -> str:
    from .runtime import DOCKER

    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise Refusal("feed_engine_observation_budget")
    return subprocess.run(
        [*DOCKER, *args], capture_output=True, text=True, timeout=remaining, check=True
    ).stdout.strip()


def observed_engine(deadline: float) -> str:
    identity = docker_read("info", "--format", "{{.ID}}", deadline=deadline)
    if not identity or len(identity) > 128:
        raise Refusal("feed_engine_identity_unavailable")
    return identity


def runtime_idle(native: Runtime, expected: str | None) -> bool:
    deadline = time.monotonic() + 0.75
    if not expected or observed_engine(deadline) != expected:
        return False
    if docker_read(
        "ps", "-aq", "--filter", f"name=^/{native.name}$", deadline=deadline
    ):
        return False
    labelled = docker_read(
        "ps",
        "-a",
        "--no-trunc",
        "--filter",
        f"label={LABEL}={native.target}",
        "--format",
        "{{.ID}} {{.Names}}",
        deadline=deadline,
    ).splitlines()
    if not labelled:
        return True
    if len(labelled) != 1:
        return False
    identifier, name = labelled[0].split()
    if not HEX.fullmatch(identifier) or name != native.feed_name:
        return False
    observed = json.loads(
        docker_read(
            "inspect",
            "--type",
            "container",
            identifier,
            deadline=deadline,
        )
    )[0]
    mounts = observed["Mounts"]
    return bool(
        observed["Id"] == identifier
        and observed["Name"] == "/" + native.feed_name
        and observed["Image"] == native.image
        and observed["HostConfig"]["ReadonlyRootfs"] is True
        and observed["Config"]["Labels"].get(LABEL) == str(native.target)
        and observed["Config"]["Entrypoint"] == ["/usr/bin/python3"]
        and observed["Config"]["Cmd"]
        == ["-u", "-B", "-m", "http.server", "8090", "--directory", "/feed"]
        and len(mounts) == 1
        and mounts[0]["Type"] == "bind"
        and mounts[0]["Source"] == str(native.daemon_path(native.private_feed))
        and mounts[0]["Destination"] == "/feed"
        and mounts[0]["RW"] is False
    )
