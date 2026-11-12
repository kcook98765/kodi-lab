"""Bounded original ZIP intake through the reviewed immutable ZIP validator.

A reserved request is never automatically repeated after an interrupted publish.
Native delivery/verification remains the existing installation service's job.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import os
import zipfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.development.addon_zip import (
    create_upload_stage,
    discard_unaccepted_addon_zip_artifact,
    finish_upload_stage,
    read_editable_addon_zip,
    remove_upload_stage,
    validate_and_publish_addon_zip,
    write_upload_chunk,
)
from kodi_lab.domain import AddonZipArtifact, AddonZipExtensionClassification
from kodi_lab.storage.models import AgentPackageRow
from kodi_lab.storage.repositories import AddonZipArtifactRepository


class AgentPackageError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class PackageImportInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)
    request_id: UUID
    archive_base64: str = Field(
        min_length=4, max_length=61440, pattern=r"^[A-Za-z0-9+/]+={0,2}$"
    )
    archive_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    provenance_reference: str = Field(min_length=1, max_length=512)


class PackageStatusInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    artifact_id: UUID


class PackageStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    artifact_id: UUID
    request_id: UUID
    state: Literal["reserved", "accepted", "failed"]
    archive_sha256: str
    provenance_reference: str
    addon_id: str | None
    addon_version: str | None
    classification: str | None
    next_action: Literal[
        "inspect_original_request", "request_native_installation", "review_failed_input"
    ]


class AgentPackageService:
    def __init__(self, sessions: sessionmaker[Session], data_root: Path) -> None:
        self.sessions = sessions
        self.root = data_root

    def _status(self, session: Session, row: AgentPackageRow) -> PackageStatus:
        artifact = AddonZipArtifactRepository(session).get(UUID(row.id))
        return PackageStatus(
            artifact_id=UUID(row.id),
            request_id=UUID(row.request_id),
            state=cast(Literal["reserved", "accepted", "failed"], row.state),
            archive_sha256=row.archive_sha256,
            provenance_reference=row.provenance_reference,
            addon_id=artifact.addon_id if artifact else None,
            addon_version=artifact.addon_version if artifact else None,
            classification=artifact.extension_classification.value
            if artifact
            else None,
            next_action=(
                "request_native_installation"
                if row.state == "accepted"
                else "review_failed_input"
                if row.state == "failed"
                else "inspect_original_request"
            ),
        )

    def status(
        self,
        client_id: UUID,
        artifact_id: UUID,
        *,
        authorize: Callable[[Session], None],
    ) -> PackageStatus:
        with self.sessions() as session:
            authorize(session)
            row = session.get(AgentPackageRow, str(artifact_id))
            if row is None or row.client_id != str(client_id):
                raise AgentPackageError("package_not_found_or_not_authorized")
            return self._status(session, row)

    def import_package(
        self,
        client_id: UUID,
        operation: PackageImportInput,
        *,
        authorize: Callable[[Session], None],
    ) -> PackageStatus:
        try:
            data = base64.b64decode(operation.archive_base64, validate=True)
        except (ValueError, binascii.Error) as error:
            raise AgentPackageError("package_base64_invalid") from error
        if not data or len(data) > 45 * 1024:
            raise AgentPackageError("package_size_exceeded")
        if hashlib.sha256(data).hexdigest() != operation.archive_sha256:
            raise AgentPackageError("package_digest_mismatch")
        # Cheap additional request bounds before the existing full stream validator.
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                members = archive.infolist()
                if (
                    len(members) > 256
                    or sum(m.file_size for m in members) > 4 * 1024 * 1024
                    or any(m.file_size > 1024 * 1024 for m in members)
                ):
                    raise AgentPackageError("package_size_exceeded")
        except (ValueError, zipfile.BadZipFile) as error:
            raise AgentPackageError("package_zip_invalid") from error
        with self.sessions.begin() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            authorize(session)
            previous = session.scalar(
                select(AgentPackageRow).where(
                    AgentPackageRow.client_id == str(client_id),
                    AgentPackageRow.request_id == str(operation.request_id),
                )
            )
            if previous is not None:
                if (
                    previous.archive_sha256 != operation.archive_sha256
                    or previous.provenance_reference != operation.provenance_reference
                ):
                    raise AgentPackageError("request_id_conflict")
                return self._status(session, previous)
            total = (
                session.scalar(select(func.count()).select_from(AgentPackageRow)) or 0
            )
            owned = (
                session.scalar(
                    select(func.count())
                    .select_from(AgentPackageRow)
                    .where(AgentPackageRow.client_id == str(client_id))
                )
                or 0
            )
            if total >= 32 or owned >= 4:
                raise AgentPackageError("package_quota_exceeded")
            identity = uuid4()
            session.add(
                AgentPackageRow(
                    id=str(identity),
                    client_id=str(client_id),
                    request_id=str(operation.request_id),
                    archive_sha256=operation.archive_sha256,
                    provenance_reference=operation.provenance_reference,
                    state="reserved",
                    created_at=datetime.now(UTC)
                    .isoformat(timespec="microseconds")
                    .replace("+00:00", "Z"),
                )
            )
        try:
            fd, _ = create_upload_stage(self.root, identity)
            try:
                finish_upload_stage(fd, write_upload_chunk(fd, data, 0))
            finally:
                os.close(fd)
            validated = validate_and_publish_addon_zip(self.root, identity)
            if (
                validated.extension_classification
                is AddonZipExtensionClassification.PYTHON_VIDEO_PLUGIN
            ):
                # Apply the existing editable input policy to Python test packages.
                read_editable_addon_zip(data)
            with self.sessions.begin() as session:
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
                authorize(session)
                row = session.get(AgentPackageRow, str(identity))
                if row is None or row.state != "reserved":
                    raise AgentPackageError("package_intake_requires_review")
                AddonZipArtifactRepository(session).create(
                    AddonZipArtifact(
                        id=identity,
                        logical_label="Agent package " + str(identity),
                        sha256_digest=validated.sha256_digest,
                        compressed_size=validated.compressed_size,
                        total_uncompressed_bytes=validated.total_uncompressed_bytes,
                        member_count=validated.member_count,
                        addon_id=validated.addon_id,
                        addon_name=validated.addon_name,
                        addon_version=validated.addon_version,
                        extension_classification=validated.extension_classification,
                        dependencies=validated.dependencies,
                        payload_manifest_digest=validated.payload_manifest_digest,
                        imported_at=datetime.now(UTC),
                    )
                )
                row.state = "accepted"
                result = self._status(session, row)
            return result
        except Exception:
            # No target was touched; these are exact unaccepted server-owned IDs.
            discard_unaccepted_addon_zip_artifact(self.root, identity)
            remove_upload_stage(self.root, identity)
            with self.sessions.begin() as session:
                row = session.get(AgentPackageRow, str(identity))
                if row is not None and row.state == "reserved":
                    row.state = "failed"
            raise
