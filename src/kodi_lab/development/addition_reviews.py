"""Exact proofs for administrator-reviewed destination file additions."""

from __future__ import annotations

import hashlib
import json
from pathlib import PurePosixPath
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

from kodi_lab.development.deployment import (
    DevelopmentTreeError,
    DevelopmentTreeManifest,
    _canonical_manifest,
    _safe_relative_path,
)

Digest = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
ReviewPaths = Annotated[tuple[str, ...], Field(min_length=1, max_length=32)]


def identity_digest(value: object) -> str:
    return (
        "sha256:"
        + hashlib.sha256(
            json.dumps(
                value, sort_keys=True, separators=(",", ":"), default=str
            ).encode()
        ).hexdigest()
    )


class AddedFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str = Field(min_length=1, max_length=512)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size: int = Field(ge=0, le=256 * 1024 * 1024)
    mode: int = Field(ge=0, le=0o7777)
    uid: int = Field(ge=0)
    gid: int = Field(ge=0)

    @field_validator("path")
    @classmethod
    def safe_path(cls, value: str) -> str:
        _safe_relative_path(value)
        return value


class SourceAdditionProof(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scope_digest: Digest
    source_digest: Digest
    destination_digest: Digest
    container_digest: Digest
    trusted_destination_digest: Digest
    additions: Annotated[tuple[AddedFile, ...], Field(min_length=1, max_length=32)]


def prove_additions(
    source: DevelopmentTreeManifest,
    destination: DevelopmentTreeManifest,
    *,
    paths: tuple[str, ...],
    trusted_digest: str,
    scope_digest: str,
    container: object,
    runtime_uid: int,
    runtime_gid: int,
) -> SourceAdditionProof:
    """Subtract only named regular files, then authenticate every remaining entry."""

    def refuse() -> None:
        raise DevelopmentTreeError(
            "development_source_additions_review_conflict",
            "Only added regular files in existing directories may be reviewed; "
            "every trusted entry must remain exact and candidate paths "
            "must not collide.",
        )

    if not 1 <= len(paths) <= 32 or len(set(paths)) != len(paths):
        refuse()
    entries = destination.by_path()
    source_paths = source.by_path()
    additions: list[AddedFile] = []
    for path in sorted(paths, key=lambda value: value.encode("utf-8")):
        _safe_relative_path(path)
        entry = entries.get(path)
        parent = str(PurePosixPath(path).parent)
        if (
            entry is None
            or entry.entry_type != "file"
            or entry.sha256 is None
            or entry.uid != runtime_uid
            or entry.gid != runtime_gid
            or entry.mode not in {0o644, 0o755}
            or path in source_paths
            or any(candidate.startswith(path + "/") for candidate in source_paths)
            or (
                parent != "."
                and (parent not in entries or entries[parent].entry_type != "directory")
            )
        ):
            refuse()
        assert entry is not None and entry.sha256 is not None
        additions.append(
            AddedFile(
                path=path,
                sha256=entry.sha256,
                size=entry.size,
                mode=entry.mode,
                uid=runtime_uid,
                gid=runtime_gid,
            )
        )
    retained = tuple(entry for entry in destination.entries if entry.path not in paths)
    canonical = _canonical_manifest(retained, include_ownership=True)
    if "sha256:" + hashlib.sha256(canonical).hexdigest() != trusted_digest:
        refuse()
    return SourceAdditionProof(
        scope_digest=scope_digest,
        source_digest=source.digest,
        destination_digest=destination.digest,
        container_digest=identity_digest(container),
        trusted_destination_digest=trusted_digest,
        additions=tuple(additions),
    )
