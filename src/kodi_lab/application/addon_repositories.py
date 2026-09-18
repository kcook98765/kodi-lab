"""Repository catalog, attachment, refresh, discovery, and package fetch services."""

from __future__ import annotations

import builtins
import hashlib
import os
import secrets
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.mutations import (
    ManagedInstanceMutationConflictError,
    ManagedInstanceMutationCoordinator,
)
from kodi_lab.development import (
    MAX_COMPRESSED_BYTES,
    AddonRepositoryError,
    PinnedHttpsTransport,
    RepositoryHttpResponse,
    canonicalize_repository_url,
    create_upload_stage,
    derive_repository_package_url,
    discard_unaccepted_addon_zip_artifact,
    finish_upload_stage,
    parse_repository_metadata,
    remove_upload_stage,
    validate_and_publish_addon_zip,
    write_upload_chunk,
)
from kodi_lab.domain import (
    AddonRepositoryAttachmentKind,
    AddonRepositoryAttachmentOperation,
    AddonRepositoryAttachmentState,
    AddonRepositoryCandidate,
    AddonRepositoryCandidateSupport,
    AddonRepositoryFetchOperation,
    AddonRepositoryFetchPhase,
    AddonRepositoryMetadataSnapshot,
    AddonRepositoryOperationState,
    AddonRepositoryRefreshOperation,
    AddonRepositoryRefreshPhase,
    AddonRepositorySource,
    AddonZipArtifact,
    AddonZipOrigin,
    GlobalHeavyOperationKind,
    ManagedInstanceAddonRepositorySource,
    ManagedInstanceMutationKind,
    normalize_development_source_logical_name,
)
from kodi_lab.storage.repositories import (
    AddonRepositoryAttachmentOperationRepository,
    AddonRepositoryFetchOperationRepository,
    AddonRepositoryMetadataRepository,
    AddonRepositoryRefreshOperationRepository,
    AddonRepositorySourceRepository,
    AddonZipArtifactRepository,
    GlobalHeavyOperationLeaseRepository,
    ManagedInstanceAddonRepositorySourceRepository,
    ManagedInstanceRepository,
)

REPOSITORY_OPERATION_LEASE = timedelta(minutes=10)


class AddonRepositoryServiceError(RuntimeError):
    code = "addon_repository_failed"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


class AddonRepositoryNotFoundError(AddonRepositoryServiceError):
    code = "addon_repository_not_found"


class AddonRepositoryConflictError(AddonRepositoryServiceError):
    code = "addon_repository_conflict"


class AddonRepositoryUnsupportedError(AddonRepositoryServiceError):
    code = "addon_repository_candidate_unsupported"


class AddonRepositorySourceRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository_id: UUID
    logical_name: str = Field(max_length=120)
    canonical_index_url: str = Field(max_length=2304)
    origin: str
    created_at: datetime
    attached: bool | None = None


class AddonRepositoryCandidateRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: UUID
    repository_id: UUID
    snapshot_id: UUID
    repository_logical_name: str = Field(max_length=120)
    addon_id: str = Field(max_length=128)
    name: str = Field(max_length=256)
    version: str = Field(max_length=128)
    classification: str = Field(max_length=64)
    supported: bool
    unsupported_reason: str | None = Field(default=None, max_length=64)
    fetched_artifact_id: UUID | None = None


class AddonRepositoryRefreshStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation_id: UUID
    repository_id: UUID
    state: AddonRepositoryOperationState
    phase: AddonRepositoryRefreshPhase
    snapshot_id: UUID | None
    requested_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    failure_reason: str | None = Field(default=None, max_length=128)


class AddonRepositoryFetchStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation_id: UUID
    repository_id: UUID
    snapshot_id: UUID
    candidate_id: UUID
    artifact_id: UUID
    state: AddonRepositoryOperationState
    phase: AddonRepositoryFetchPhase
    requested_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    failure_reason: str | None = Field(default=None, max_length=128)


class _Transport(Protocol):
    def get(
        self,
        url: str,
        *,
        maximum_body: int,
        require_index: bool = True,
    ) -> RepositoryHttpResponse: ...


class AddonRepositoryAdministrationService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        mutation_coordinator: ManagedInstanceMutationCoordinator | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._session_factory = session_factory
        self._mutations = mutation_coordinator or ManagedInstanceMutationCoordinator(
            session_factory
        )
        self._clock = clock

    def register(self, logical_name: str, index_url: str) -> AddonRepositorySourceRead:
        try:
            normalized_name = normalize_development_source_logical_name(logical_name)
            canonical = canonicalize_repository_url(index_url)
        except ValueError as error:
            if isinstance(error, AddonRepositoryError):
                raise
            raise AddonRepositoryServiceError(
                "Repository registration is invalid.", code="addon_repository_invalid"
            ) from error
        source = AddonRepositorySource(
            id=uuid4(),
            logical_name=normalized_name,
            canonical_index_url=canonical.url,
            created_at=self._clock(),
        )
        try:
            with self._session_factory.begin() as session:
                AddonRepositorySourceRepository(session).create(source)
        except IntegrityError as error:
            raise AddonRepositoryConflictError(
                "Repository name or canonical URL is already registered."
            ) from error
        return self._source_read(source)

    def list(self) -> list[AddonRepositorySourceRead]:
        with self._session_factory() as session:
            values = AddonRepositorySourceRepository(session).list()
        return [self._source_read(item) for item in values]

    def get(self, repository_id: UUID) -> AddonRepositorySourceRead:
        with self._session_factory() as session:
            source = AddonRepositorySourceRepository(session).get(repository_id)
        if source is None:
            raise AddonRepositoryNotFoundError("Repository is unavailable.")
        return self._source_read(source)

    def list_for_instance(
        self, instance_id: UUID
    ) -> builtins.list[AddonRepositorySourceRead]:
        with self._session_factory() as session:
            if ManagedInstanceRepository(session).get(instance_id) is None:
                raise AddonRepositoryNotFoundError("Managed instance is unavailable.")
            attachments = ManagedInstanceAddonRepositorySourceRepository(
                session
            ).list_for_instance(instance_id)
            sources = AddonRepositorySourceRepository(session)
            values = [
                source
                for item in attachments
                if (source := sources.get(item.repository_source_id)) is not None
            ]
        return [self._source_read(item, attached=True) for item in values]

    def attach(
        self, instance_id: UUID, repository_id: UUID
    ) -> AddonRepositorySourceRead:
        return self._set_attachment(instance_id, repository_id, attached=True)

    def detach(
        self, instance_id: UUID, repository_id: UUID
    ) -> AddonRepositorySourceRead:
        return self._set_attachment(instance_id, repository_id, attached=False)

    def _set_attachment(
        self, instance_id: UUID, repository_id: UUID, *, attached: bool
    ) -> AddonRepositorySourceRead:
        operation_id = uuid4()
        requested_at = self._clock()
        try:
            lease = self._mutations.acquire(
                instance_id, ManagedInstanceMutationKind.ADDON_REPOSITORY_ATTACHMENT
            )
        except ManagedInstanceMutationConflictError as error:
            raise AddonRepositoryConflictError(
                "Another managed-instance mutation is active."
            ) from error
        try:
            with self._session_factory.begin() as session:
                source = AddonRepositorySourceRepository(session).get(repository_id)
                instance = ManagedInstanceRepository(session).get(instance_id)
                attachments = ManagedInstanceAddonRepositorySourceRepository(session)
                current = attachments.get(instance_id, repository_id)
                if source is None or instance is None:
                    raise AddonRepositoryNotFoundError(
                        "Repository or managed instance is unavailable."
                    )
                if attached and current is None:
                    attachments.create(
                        ManagedInstanceAddonRepositorySource(
                            managed_instance_id=instance_id,
                            repository_source_id=repository_id,
                            attached_at=self._clock(),
                        )
                    )
                elif not attached and current is None:
                    raise AddonRepositoryConflictError(
                        "Repository is not attached to this instance."
                    )
                elif not attached:
                    attachments.delete(instance_id, repository_id)
                AddonRepositoryAttachmentOperationRepository(session).create(
                    AddonRepositoryAttachmentOperation(
                        id=operation_id,
                        original_managed_instance_id=instance_id,
                        managed_instance_id=instance_id,
                        repository_source_id=repository_id,
                        repository_logical_name=source.logical_name,
                        kind=(
                            AddonRepositoryAttachmentKind.ATTACH
                            if attached
                            else AddonRepositoryAttachmentKind.DETACH
                        ),
                        requested_at=requested_at,
                        completed_at=self._clock(),
                        state=AddonRepositoryAttachmentState.SUCCEEDED,
                    )
                )
            return self._source_read(source, attached=attached)
        finally:
            self._mutations.release(lease)

    @staticmethod
    def _source_read(
        source: AddonRepositorySource, *, attached: bool | None = None
    ) -> AddonRepositorySourceRead:
        return AddonRepositorySourceRead(
            repository_id=source.id,
            logical_name=source.logical_name,
            canonical_index_url=source.canonical_index_url,
            origin=source.origin,
            created_at=source.created_at,
            attached=attached,
        )


@dataclass(frozen=True, slots=True)
class _OperationLease:
    operation_id: UUID
    token: str

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.token.encode()).hexdigest()


class AddonRepositoryRefreshCoordinator:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        transport: _Transport | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._session_factory = session_factory
        self._transport = transport or PinnedHttpsTransport()
        self._clock = clock

    def queue(self, repository_id: UUID) -> AddonRepositoryRefreshStatus:
        with self._session_factory() as session:
            source = AddonRepositorySourceRepository(session).get(repository_id)
        if source is None:
            raise AddonRepositoryNotFoundError("Repository is unavailable.")
        operation = AddonRepositoryRefreshOperation(
            id=uuid4(),
            repository_source_id=repository_id,
            requested_at=self._clock(),
            state=AddonRepositoryOperationState.QUEUED,
            phase=AddonRepositoryRefreshPhase.QUEUED,
        )
        try:
            with self._session_factory.begin() as session:
                AddonRepositoryRefreshOperationRepository(session).create(operation)
                GlobalHeavyOperationLeaseRepository(session).reserve(
                    kind=GlobalHeavyOperationKind.ADDON_REPOSITORY_REFRESH,
                    operation_id=operation.id,
                )
        except IntegrityError as error:
            raise AddonRepositoryConflictError(
                "Another repository or global-heavy operation is active."
            ) from error
        return self.status(operation)

    def acquire(self, operation_id: UUID) -> _OperationLease | None:
        with self._session_factory() as session:
            operation = AddonRepositoryRefreshOperationRepository(session).get(
                operation_id
            )
        if (
            operation is None
            or operation.state is not AddonRepositoryOperationState.QUEUED
        ):
            return None
        lease = _OperationLease(operation_id, secrets.token_urlsafe(32))
        now = self._clock()
        with self._session_factory.begin() as session:
            owned = AddonRepositoryRefreshOperationRepository(session).acquire(
                operation_id,
                token_digest=lease.digest,
                now=now,
                expires_at=now + REPOSITORY_OPERATION_LEASE,
            )
            heavy = GlobalHeavyOperationLeaseRepository(session).claim(
                operation_id=operation_id,
                token_digest=lease.digest,
                now=now,
                expires_at=now + REPOSITORY_OPERATION_LEASE,
            )
            if not owned or not heavy:
                raise AddonRepositoryConflictError(
                    "Repository refresh authority was lost."
                )
        return lease

    def run(self, lease: _OperationLease) -> None:
        snapshot_id: UUID | None = None
        reason: str | None = None
        try:
            with self._session_factory() as session:
                operation = AddonRepositoryRefreshOperationRepository(session).get(
                    lease.operation_id
                )
                source = (
                    None
                    if operation is None
                    else AddonRepositorySourceRepository(session).get(
                        operation.repository_source_id
                    )
                )
            if operation is None or source is None:
                raise AddonRepositoryNotFoundError(
                    "Repository refresh state is unavailable."
                )
            self._phase(lease, AddonRepositoryRefreshPhase.FETCHING)
            response = self._transport.get(
                source.canonical_index_url, maximum_body=8 * 1024 * 1024
            )
            if response.status != 200:
                raise AddonRepositoryError(
                    "repository_http_status",
                    "Repository metadata request was rejected.",
                )
            self._phase(lease, AddonRepositoryRefreshPhase.PARSING)
            parsed = parse_repository_metadata(
                response.body,
                request_path=canonicalize_repository_url(response.final_url).path,
                content_type=response.content_type,
            )
            snapshot_id = uuid4()
            fetched_at = self._clock()
            snapshot = AddonRepositoryMetadataSnapshot(
                id=snapshot_id,
                repository_source_id=source.id,
                fetched_at=fetched_at,
                transfer_digest=parsed.transfer_digest,
                canonical_metadata_digest=parsed.canonical_metadata_digest,
                transfer_size=parsed.transfer_size,
                expanded_size=parsed.expanded_size,
                entry_count=len(parsed.candidates),
            )
            candidates = tuple(
                AddonRepositoryCandidate(
                    id=uuid4(),
                    snapshot_id=snapshot_id,
                    repository_source_id=source.id,
                    addon_id=item.addon_id,
                    name=item.name,
                    version=item.version,
                    classification=item.classification,
                    dependencies=item.dependencies,
                    support=item.support,
                    unsupported_reason=item.unsupported_reason,
                )
                for item in parsed.candidates
            )
            self._phase(lease, AddonRepositoryRefreshPhase.PUBLISHING)
            with self._session_factory.begin() as session:
                AddonRepositoryMetadataRepository(session).create_snapshot(
                    snapshot, candidates
                )
        except Exception as error:
            reason = (
                error.code
                if isinstance(
                    error, (AddonRepositoryError, AddonRepositoryServiceError)
                )
                else "addon_repository_refresh_failed"
            )
        finally:
            with self._session_factory.begin() as session:
                AddonRepositoryRefreshOperationRepository(session).finish(
                    lease.operation_id,
                    token_digest=lease.digest,
                    now=self._clock(),
                    snapshot_id=snapshot_id if reason is None else None,
                    reason=reason,
                )
                GlobalHeavyOperationLeaseRepository(session).release(
                    operation_id=lease.operation_id, token_digest=lease.digest
                )

    def _phase(
        self, lease: _OperationLease, phase: AddonRepositoryRefreshPhase
    ) -> None:
        with self._session_factory.begin() as session:
            if not AddonRepositoryRefreshOperationRepository(session).phase(
                lease.operation_id, token_digest=lease.digest, phase=phase
            ):
                raise AddonRepositoryConflictError(
                    "Repository refresh authority was lost."
                )

    def get(self, operation_id: UUID) -> AddonRepositoryRefreshStatus:
        with self._session_factory() as session:
            value = AddonRepositoryRefreshOperationRepository(session).get(operation_id)
        if value is None:
            raise AddonRepositoryNotFoundError("Repository refresh is unavailable.")
        return self.status(value)

    def queued(self) -> list[UUID]:
        with self._session_factory() as session:
            return [
                item.id
                for item in AddonRepositoryRefreshOperationRepository(
                    session
                ).list_queued()
            ]

    def candidates(self, repository_id: UUID) -> list[AddonRepositoryCandidateRead]:
        with self._session_factory() as session:
            source = AddonRepositorySourceRepository(session).get(repository_id)
            snapshot = AddonRepositoryMetadataRepository(session).latest_snapshot(
                repository_id
            )
            values = (
                []
                if snapshot is None
                else AddonRepositoryMetadataRepository(session).list_candidates(
                    snapshot.id
                )
            )
            fetches = AddonRepositoryFetchOperationRepository(session)
            fetched_artifacts = {
                item.id: fetched.artifact_id
                for item in values
                if (fetched := fetches.latest_succeeded_for_candidate(item.id))
                is not None
            }
        if source is None:
            raise AddonRepositoryNotFoundError("Repository is unavailable.")
        return [
            AddonRepositoryCandidateRead(
                candidate_id=item.id,
                repository_id=repository_id,
                snapshot_id=item.snapshot_id,
                repository_logical_name=source.logical_name,
                addon_id=item.addon_id,
                name=item.name,
                version=item.version,
                classification=item.classification,
                supported=item.support is AddonRepositoryCandidateSupport.SUPPORTED,
                unsupported_reason=item.unsupported_reason,
                fetched_artifact_id=fetched_artifacts.get(item.id),
            )
            for item in values
        ]

    @staticmethod
    def status(value: AddonRepositoryRefreshOperation) -> AddonRepositoryRefreshStatus:
        return AddonRepositoryRefreshStatus(
            operation_id=value.id,
            repository_id=value.repository_source_id,
            state=value.state,
            phase=value.phase,
            snapshot_id=value.snapshot_id,
            requested_at=value.requested_at,
            started_at=value.started_at,
            completed_at=value.completed_at,
            failure_reason=value.failure_reason,
        )


class AddonRepositoryFetchCoordinator:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        data_root: Path,
        transport: _Transport | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._session_factory = session_factory
        self._data_root = data_root
        self._transport = transport or PinnedHttpsTransport()
        self._clock = clock

    def queue(
        self, repository_id: UUID, candidate_id: UUID
    ) -> AddonRepositoryFetchStatus:
        with self._session_factory() as session:
            source = AddonRepositorySourceRepository(session).get(repository_id)
            metadata = AddonRepositoryMetadataRepository(session)
            snapshot = metadata.latest_snapshot(repository_id)
            candidate = metadata.get_candidate(candidate_id)
        if source is None or snapshot is None or candidate is None:
            raise AddonRepositoryNotFoundError("Repository candidate is unavailable.")
        if (
            candidate.repository_source_id != repository_id
            or candidate.snapshot_id != snapshot.id
        ):
            raise AddonRepositoryConflictError(
                "Candidate is not in the repository's current snapshot."
            )
        if candidate.support is not AddonRepositoryCandidateSupport.SUPPORTED:
            raise AddonRepositoryUnsupportedError(
                "Repository candidate is unsupported."
            )
        operation = AddonRepositoryFetchOperation(
            id=uuid4(),
            repository_source_id=repository_id,
            snapshot_id=snapshot.id,
            candidate_id=candidate.id,
            artifact_id=uuid4(),
            requested_at=self._clock(),
            state=AddonRepositoryOperationState.QUEUED,
            phase=AddonRepositoryFetchPhase.QUEUED,
        )
        try:
            with self._session_factory.begin() as session:
                AddonRepositoryFetchOperationRepository(session).create(operation)
                GlobalHeavyOperationLeaseRepository(session).reserve(
                    kind=GlobalHeavyOperationKind.ADDON_REPOSITORY_FETCH,
                    operation_id=operation.id,
                )
        except IntegrityError as error:
            raise AddonRepositoryConflictError(
                "Another repository or global-heavy operation is active."
            ) from error
        return self.status(operation)

    def acquire(self, operation_id: UUID) -> _OperationLease | None:
        with self._session_factory() as session:
            operation = AddonRepositoryFetchOperationRepository(session).get(
                operation_id
            )
        if (
            operation is None
            or operation.state is not AddonRepositoryOperationState.QUEUED
        ):
            return None
        lease = _OperationLease(operation_id, secrets.token_urlsafe(32))
        now = self._clock()
        with self._session_factory.begin() as session:
            owned = AddonRepositoryFetchOperationRepository(session).acquire(
                operation_id,
                token_digest=lease.digest,
                now=now,
                expires_at=now + REPOSITORY_OPERATION_LEASE,
            )
            heavy = GlobalHeavyOperationLeaseRepository(session).claim(
                operation_id=operation_id,
                token_digest=lease.digest,
                now=now,
                expires_at=now + REPOSITORY_OPERATION_LEASE,
            )
            if not owned or not heavy:
                raise AddonRepositoryConflictError(
                    "Repository fetch authority was lost."
                )
        return lease

    def run(self, lease: _OperationLease) -> None:
        reason: str | None = None
        artifact_id: UUID | None = None
        try:
            with self._session_factory() as session:
                operation = AddonRepositoryFetchOperationRepository(session).get(
                    lease.operation_id
                )
                source = (
                    None
                    if operation is None
                    else AddonRepositorySourceRepository(session).get(
                        operation.repository_source_id
                    )
                )
                metadata = AddonRepositoryMetadataRepository(session)
                snapshot = (
                    None
                    if operation is None
                    else metadata.latest_snapshot(operation.repository_source_id)
                )
                candidate = (
                    None
                    if operation is None
                    else metadata.get_candidate(operation.candidate_id)
                )
            if (
                operation is None
                or source is None
                or snapshot is None
                or candidate is None
                or snapshot.id != operation.snapshot_id
                or candidate.snapshot_id != snapshot.id
                or candidate.repository_source_id != source.id
                or candidate.support is not AddonRepositoryCandidateSupport.SUPPORTED
            ):
                raise AddonRepositoryConflictError(
                    "Repository candidate lineage is no longer current."
                )
            artifact_id = operation.artifact_id
            self._phase(lease, AddonRepositoryFetchPhase.FETCHING)
            package_url = derive_repository_package_url(
                source.canonical_index_url, candidate.addon_id, candidate.version
            )
            response = self._transport.get(
                package_url,
                maximum_body=MAX_COMPRESSED_BYTES,
                require_index=False,
            )
            if response.status != 200 or response.content_type != "application/zip":
                raise AddonRepositoryError(
                    "repository_package_response",
                    "Repository package response is invalid.",
                )
            fd, _path = create_upload_stage(self._data_root, artifact_id)
            try:
                total = write_upload_chunk(fd, response.body, 0)
                finish_upload_stage(fd, total)
            finally:
                os.close(fd)
            self._phase(lease, AddonRepositoryFetchPhase.VALIDATING)
            validated = validate_and_publish_addon_zip(self._data_root, artifact_id)
            if (
                validated.addon_id != candidate.addon_id
                or validated.addon_version != candidate.version
                or validated.extension_classification.value
                != candidate.classification.value
                or validated.dependencies != candidate.dependencies
            ):
                raise AddonRepositoryError(
                    "repository_package_descriptor_mismatch",
                    "Repository package descriptor does not match the candidate.",
                )
            self._phase(lease, AddonRepositoryFetchPhase.PUBLISHING)
            label = f"{source.logical_name}: {candidate.addon_id} {candidate.version}"
            label = label[:120]
            with self._session_factory.begin() as session:
                AddonZipArtifactRepository(session).create(
                    AddonZipArtifact(
                        id=artifact_id,
                        logical_label=label,
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
                        origin=AddonZipOrigin.REPOSITORY_DOWNLOAD,
                        repository_source_id=source.id,
                        repository_snapshot_id=snapshot.id,
                        repository_candidate_id=candidate.id,
                        repository_fetched_at=self._clock(),
                    )
                )
        except Exception as error:
            if artifact_id is not None:
                discard_unaccepted_addon_zip_artifact(self._data_root, artifact_id)
                remove_upload_stage(self._data_root, artifact_id)
            reason = (
                error.code
                if isinstance(
                    error, (AddonRepositoryError, AddonRepositoryServiceError)
                )
                else "addon_repository_fetch_failed"
            )
        finally:
            with self._session_factory.begin() as session:
                AddonRepositoryFetchOperationRepository(session).finish(
                    lease.operation_id,
                    token_digest=lease.digest,
                    now=self._clock(),
                    reason=reason,
                )
                GlobalHeavyOperationLeaseRepository(session).release(
                    operation_id=lease.operation_id, token_digest=lease.digest
                )

    def _phase(self, lease: _OperationLease, phase: AddonRepositoryFetchPhase) -> None:
        with self._session_factory.begin() as session:
            if not AddonRepositoryFetchOperationRepository(session).phase(
                lease.operation_id, token_digest=lease.digest, phase=phase
            ):
                raise AddonRepositoryConflictError(
                    "Repository fetch authority was lost."
                )

    def get(self, operation_id: UUID) -> AddonRepositoryFetchStatus:
        with self._session_factory() as session:
            value = AddonRepositoryFetchOperationRepository(session).get(operation_id)
        if value is None:
            raise AddonRepositoryNotFoundError("Repository fetch is unavailable.")
        return self.status(value)

    def queued(self) -> list[UUID]:
        with self._session_factory() as session:
            return [
                item.id
                for item in AddonRepositoryFetchOperationRepository(
                    session
                ).list_queued()
            ]

    @staticmethod
    def status(value: AddonRepositoryFetchOperation) -> AddonRepositoryFetchStatus:
        return AddonRepositoryFetchStatus(
            operation_id=value.id,
            repository_id=value.repository_source_id,
            snapshot_id=value.snapshot_id,
            candidate_id=value.candidate_id,
            artifact_id=value.artifact_id,
            state=value.state,
            phase=value.phase,
            requested_at=value.requested_at,
            started_at=value.started_at,
            completed_at=value.completed_at,
            failure_reason=value.failure_reason,
        )


class AddonRepositoryExecutor:
    def __init__(
        self,
        coordinator: AddonRepositoryRefreshCoordinator
        | AddonRepositoryFetchCoordinator,
        *,
        thread_name: str,
    ) -> None:
        self._coordinator = coordinator
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix=thread_name)

    def submit(self, operation_id: UUID) -> None:
        self._pool.submit(self._run, operation_id)

    def start(self) -> None:
        for operation_id in self._coordinator.queued():
            self.submit(operation_id)

    def _run(self, operation_id: UUID) -> None:
        with suppress(Exception):
            lease = self._coordinator.acquire(operation_id)
            if lease is not None:
                self._coordinator.run(lease)

    def close(self) -> None:
        self._pool.shutdown(wait=True, cancel_futures=False)
