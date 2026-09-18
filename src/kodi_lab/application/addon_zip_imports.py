"""Durable asynchronous uploaded-ZIP validation and catalog import."""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.reconciliation import StaleOperationReconciliationDispatcher
from kodi_lab.development import (
    AddonZipValidationError,
    discard_unaccepted_addon_zip_artifact,
    remove_upload_stage,
    validate_and_publish_addon_zip,
)
from kodi_lab.domain import (
    AddonZipArtifact,
    AddonZipImportOperation,
    AddonZipImportOperationPhase,
    AddonZipImportOperationState,
    GlobalHeavyOperationKind,
    normalize_development_source_logical_name,
)
from kodi_lab.storage.repositories import (
    AddonZipArtifactRepository,
    AddonZipImportOperationRepository,
    GlobalHeavyOperationLeaseRepository,
)

IMPORT_LEASE_DURATION = timedelta(minutes=10)


class AddonZipImportError(RuntimeError):
    pass


class AddonZipImportConflictError(AddonZipImportError):
    pass


@dataclass(frozen=True, slots=True)
class AddonZipImportLease:
    operation_id: UUID
    artifact_id: UUID
    token: str

    @property
    def token_digest(self) -> str:
        return hashlib.sha256(self.token.encode()).hexdigest()


class AddonZipImportCoordinator:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        data_root: Path,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        stale_dispatcher: StaleOperationReconciliationDispatcher | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._data_root = data_root
        self._clock = clock
        self._stale = stale_dispatcher or StaleOperationReconciliationDispatcher(
            session_factory, clock=clock, data_root=data_root
        )

    def queue(self, artifact_id: UUID, logical_label: str) -> AddonZipImportOperation:
        self._stale.sweep_global()
        operation = AddonZipImportOperation(
            id=uuid4(),
            artifact_id=artifact_id,
            logical_label=normalize_development_source_logical_name(logical_label),
            requested_at=self._clock(),
            state=AddonZipImportOperationState.QUEUED,
            phase=AddonZipImportOperationPhase.UPLOADED,
        )
        try:
            with self._session_factory.begin() as session:
                AddonZipImportOperationRepository(session).create(operation)
                GlobalHeavyOperationLeaseRepository(session).reserve(
                    kind=GlobalHeavyOperationKind.ADDON_ZIP_IMPORT,
                    operation_id=operation.id,
                )
        except IntegrityError as error:
            remove_upload_stage(self._data_root, artifact_id)
            raise AddonZipImportConflictError(
                "Another global-heavy operation is active."
            ) from error
        return operation

    def acquire(self, operation_id: UUID) -> AddonZipImportLease | None:
        with self._session_factory() as session:
            operation = AddonZipImportOperationRepository(session).get(operation_id)
        if (
            operation is None
            or operation.state is not AddonZipImportOperationState.QUEUED
        ):
            return None
        lease = AddonZipImportLease(
            operation.id, operation.artifact_id, secrets.token_urlsafe(32)
        )
        now = self._clock()
        expires = now + IMPORT_LEASE_DURATION
        with self._session_factory.begin() as session:
            row = AddonZipImportOperationRepository(session).acquire(
                operation.id,
                token_digest=lease.token_digest,
                now=now,
                expires_at=expires,
            )
            heavy = GlobalHeavyOperationLeaseRepository(session).claim(
                operation_id=operation.id,
                token_digest=lease.token_digest,
                now=now,
                expires_at=expires,
            )
            if not row or not heavy:
                raise AddonZipImportConflictError("ZIP import authority was lost.")
        return lease

    def run(self, lease: AddonZipImportLease) -> None:
        try:
            validated = validate_and_publish_addon_zip(
                self._data_root, lease.artifact_id
            )
            with self._session_factory.begin() as session:
                repository = AddonZipImportOperationRepository(session)
                if not repository.update_phase(
                    lease.operation_id,
                    token_digest=lease.token_digest,
                    phase=AddonZipImportOperationPhase.PUBLISHING,
                ):
                    raise AddonZipImportConflictError("ZIP import authority was lost.")
                operation = repository.get(lease.operation_id)
                if operation is None:
                    raise AddonZipImportConflictError(
                        "ZIP import operation disappeared."
                    )
                AddonZipArtifactRepository(session).create(
                    AddonZipArtifact(
                        id=lease.artifact_id,
                        logical_label=operation.logical_label,
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
                        imported_at=self._clock(),
                    )
                )
                if not repository.finish(
                    lease.operation_id,
                    token_digest=lease.token_digest,
                    succeeded=True,
                    now=self._clock(),
                    reason=None,
                ) or not GlobalHeavyOperationLeaseRepository(session).release(
                    operation_id=lease.operation_id,
                    token_digest=lease.token_digest,
                ):
                    raise AddonZipImportConflictError("ZIP import finalization failed.")
        except Exception as error:
            discard_unaccepted_addon_zip_artifact(self._data_root, lease.artifact_id)
            remove_upload_stage(self._data_root, lease.artifact_id)
            reason = (
                error.safe_message
                if isinstance(error, AddonZipValidationError)
                else "Uploaded ZIP validation could not be completed."
            )
            with self._session_factory.begin() as session:
                AddonZipImportOperationRepository(session).finish(
                    lease.operation_id,
                    token_digest=lease.token_digest,
                    succeeded=False,
                    now=self._clock(),
                    reason=reason[:256],
                )
                GlobalHeavyOperationLeaseRepository(session).release(
                    operation_id=lease.operation_id,
                    token_digest=lease.token_digest,
                )

    def get(self, operation_id: UUID) -> AddonZipImportOperation | None:
        with self._session_factory() as session:
            return AddonZipImportOperationRepository(session).get(operation_id)

    def queued(self) -> list[AddonZipImportOperation]:
        with self._session_factory() as session:
            return AddonZipImportOperationRepository(session).list_queued()


class AddonZipImportExecutor:
    def __init__(self, coordinator: AddonZipImportCoordinator) -> None:
        self._coordinator = coordinator
        self._pool = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="addon-zip-import"
        )

    def submit(self, operation_id: UUID) -> None:
        self._pool.submit(self._run, operation_id)

    def start(self) -> None:
        for operation in self._coordinator.queued():
            self.submit(operation.id)

    def _run(self, operation_id: UUID) -> None:
        lease = self._coordinator.acquire(operation_id)
        if lease is not None:
            self._coordinator.run(lease)

    def close(self) -> None:
        self._pool.shutdown(wait=True, cancel_futures=False)
