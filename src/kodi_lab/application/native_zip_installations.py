"""Expiring original-ZIP delivery to Kodi's operator-driven native installer.

No add-on files are written by this workflow. A delivery lease references the
existing immutable original; expiry revokes delivery without duplicating ZIPs.
"""

from __future__ import annotations

import hashlib
import os
import socket
import stat
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, Protocol, cast
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.addon_zip_installations import InventoryAdapterFactory
from kodi_lab.application.mcp_administration import McpPrincipal
from kodi_lab.broker.docker_inventory import (
    ContainerHealthState,
    ContainerRuntimeState,
    KodiProcessState,
)
from kodi_lab.broker.operations import (
    AddonZipDestinationResult,
    ManagedContainerInventoryResult,
    ObserveManagedKodiRuntimeResult,
)
from kodi_lab.development.addon_repository import validate_dns_answers
from kodi_lab.development.addon_zip import revalidate_addon_zip_artifact
from kodi_lab.development.repository_zip import repository_zip_urls
from kodi_lab.domain import (
    AddonZipArtifact,
    AddonZipExtensionClassification,
    AddonZipOrigin,
)
from kodi_lab.kodi.addons import KodiAddonInventoryAdapter, KodiAddonInventoryResult
from kodi_lab.mcp.authorization import McpAuthorizationError, McpAuthorizationService
from kodi_lab.storage.models import (
    McpClientRow,
    NativeZipInstallationRow,
)
from kodi_lab.storage.repositories import (
    AddonZipArtifactRepository,
    ManagedInstanceRepository,
    RuntimeExecutionProfileRepository,
)

MAX_NATIVE_ZIP_BYTES = 16 * 1024 * 1024
MAX_ACTIVE_DELIVERIES = 8
MAX_NATIVE_HISTORY = 1000
DELIVERY_LIFETIME = timedelta(minutes=15)
HISTORY_RETENTION = timedelta(days=30)
DNS_TIMEOUT_SECONDS = 2.0
_DNS_SLOTS = threading.BoundedSemaphore(4)
_DNS_WORKERS = ThreadPoolExecutor(max_workers=4, thread_name_prefix="native-zip-dns")


def resolve_native_repository_host(hostname: str) -> tuple[str, ...]:
    """Bound even outstanding libc DNS work after an uninterruptible timeout."""
    if not _DNS_SLOTS.acquire(blocking=False):
        raise NativeZipError("native_zip_dns_busy")

    def lookup() -> tuple[str, ...]:
        return validate_dns_answers(
            tuple(
                dict.fromkeys(
                    str(item[4][0])
                    for item in socket.getaddrinfo(
                        hostname, 443, type=socket.SOCK_STREAM
                    )
                )
            )
        )

    try:
        future = _DNS_WORKERS.submit(lookup)
    except BaseException:
        _DNS_SLOTS.release()
        raise
    future.add_done_callback(lambda _result: _DNS_SLOTS.release())
    try:
        return future.result(timeout=DNS_TIMEOUT_SECONDS)
    except FutureTimeoutError as error:
        future.cancel()
        raise NativeZipError("native_zip_dns_timed_out") from error


class NativeZipError(RuntimeError):
    pass


class NativeZipRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: UUID
    target_id: UUID
    artifact_id: UUID


class NativeZipStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    operation_id: UUID
    target_id: UUID
    artifact_id: UUID
    state: Literal["requested", "completed", "failed", "unverified"]
    expires_at: datetime
    completed_at: datetime | None
    failure_reason: str | None
    source_url: str | None
    installation_method: Literal["kodi_gui_install_from_zip"] = (
        "kodi_gui_install_from_zip"
    )


class NativeZipBroker(Protocol):
    def managed_container_inventory(
        self, *, managed_instance_id: str | None = None
    ) -> ManagedContainerInventoryResult: ...

    def observe_managed_kodi_runtime(
        self, *, managed_instance_id: str, expected_container_id: str
    ) -> ObserveManagedKodiRuntimeResult: ...

    def observe_native_zip_installation(
        self, operation_id: UUID
    ) -> AddonZipDestinationResult: ...


def timestamp(value: datetime) -> str:
    return (
        value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    )


def original_zip(data_root: Path, artifact: AddonZipArtifact) -> bytes:
    """Open through fixed no-follow directory handles and recheck exact bytes."""
    if artifact.compressed_size > MAX_NATIVE_ZIP_BYTES:
        raise NativeZipError("native_zip_size_exceeded")
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open(data_root, flags | os.O_DIRECTORY)
    try:
        for component in ("addon-zips", "artifacts", str(artifact.id)):
            child = os.open(component, flags | os.O_DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = child
        leaf = os.open("original.zip", flags, dir_fd=fd)
        try:
            metadata = os.fstat(leaf)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or stat.S_IMODE(metadata.st_mode) != 0o400
                or metadata.st_size != artifact.compressed_size
            ):
                raise NativeZipError("native_zip_artifact_changed")
            with os.fdopen(leaf, "rb", closefd=False) as stream:
                data = stream.read(MAX_NATIVE_ZIP_BYTES + 1)
        finally:
            os.close(leaf)
    finally:
        os.close(fd)
    if (
        len(data) != artifact.compressed_size
        or "sha256:" + hashlib.sha256(data).hexdigest() != artifact.sha256_digest
    ):
        raise NativeZipError("native_zip_artifact_changed")
    return data


class NativeZipInstallationService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        broker: NativeZipBroker,
        authorization: McpAuthorizationService,
        *,
        data_root: Path,
        base_url: str | None,
        inventory_factory: InventoryAdapterFactory | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic: Callable[[], float] = time.monotonic,
        resolve: Callable[[str], tuple[str, ...]] = resolve_native_repository_host,
    ) -> None:
        self._sessions = sessions
        self._broker = broker
        self._authorization = authorization
        self._root = data_root
        self._base = base_url
        self._inventory = inventory_factory or KodiAddonInventoryAdapter
        self._clock = clock
        self._monotonic = monotonic
        self._resolve = resolve

    def sweep(self) -> None:
        """Expire delivery/history, retaining original accepted artifacts."""
        now = timestamp(self._clock())
        with self._sessions.begin() as session:
            session.execute(
                update(NativeZipInstallationRow)
                .where(
                    NativeZipInstallationRow.expires_at <= now,
                    NativeZipInstallationRow.state.in_(("requested", "unverified")),
                )
                .values(
                    state="unverified",
                    stage_ready=False,
                    failure_reason="native_installation_timed_out",
                )
            )
            session.execute(
                delete(NativeZipInstallationRow).where(
                    NativeZipInstallationRow.created_at
                    < timestamp(self._clock() - HISTORY_RETENTION),
                    NativeZipInstallationRow.stage_ready.is_(False),
                )
            )

    def request(
        self, principal: McpPrincipal, request: NativeZipRequest
    ) -> NativeZipStatus:
        self._authorization.authorize_native_zip_installation(
            principal, request.target_id
        )
        if self._base is None:
            raise NativeZipError("native_zip_delivery_unconfigured")
        self.sweep()
        started = self._monotonic()
        with self._sessions() as session:
            previous = session.scalar(
                select(NativeZipInstallationRow).where(
                    NativeZipInstallationRow.client_id == str(principal.client_id),
                    NativeZipInstallationRow.request_id == str(request.request_id),
                )
            )
        if previous is not None:
            if previous.target_id != str(
                request.target_id
            ) or previous.artifact_id != str(request.artifact_id):
                raise NativeZipError("request_id_conflict")
            return self._status(previous)
        observation = self._observe(str(request.target_id))
        now = self._clock()
        row = NativeZipInstallationRow(
            id=str(uuid4()),
            request_id=str(request.request_id),
            client_id=str(principal.client_id),
            credential_id=str(principal.credential_id),
            target_id=str(request.target_id),
            artifact_id=str(request.artifact_id),
            runtime_revision_id=observation.runtime_revision_id,
            expected_container_id=observation.container_id,
            state="requested",
            created_at=timestamp(now),
            expires_at=timestamp(now + DELIVERY_LIFETIME),
            stage_ready=False,
        )
        # Reserve before the broker can inspect an artifact. SQLite serializes
        # quota/replay decisions across workers; a failed preflight is retained.
        with self._sessions.begin() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            previous = session.scalar(
                select(NativeZipInstallationRow).where(
                    NativeZipInstallationRow.client_id == str(principal.client_id),
                    NativeZipInstallationRow.request_id == str(request.request_id),
                )
            )
            if previous is not None:
                if (
                    previous.target_id != row.target_id
                    or previous.artifact_id != row.artifact_id
                ):
                    raise NativeZipError("request_id_conflict")
                return self._status(previous)
            count = (
                session.scalar(
                    select(func.count()).select_from(NativeZipInstallationRow)
                )
                or 0
            )
            active = (
                session.scalar(
                    select(func.count())
                    .select_from(NativeZipInstallationRow)
                    .where(
                        NativeZipInstallationRow.expires_at > timestamp(now),
                        NativeZipInstallationRow.state.in_(("requested", "unverified")),
                    )
                )
                or 0
            )
            same_target = session.scalar(
                select(NativeZipInstallationRow.id).where(
                    NativeZipInstallationRow.target_id == row.target_id,
                    NativeZipInstallationRow.expires_at > timestamp(now),
                    NativeZipInstallationRow.state.in_(("requested", "unverified")),
                )
            )
            if (
                count >= MAX_NATIVE_HISTORY
                or active >= MAX_ACTIVE_DELIVERIES
                or same_target
            ):
                raise NativeZipError("native_zip_capacity_or_target_busy")
            session.add(row)
        try:
            artifact = self._artifact(row)
            validated = revalidate_addon_zip_artifact(self._root, artifact)
            descriptor = next(e for e in validated.entries if e.path == "addon.xml")
            if descriptor.size > 1024 * 1024:
                raise NativeZipError("native_zip_descriptor_invalid")
            # The validated original and payload must agree. No extraction into
            # the target, private file listing or arbitrary host read is exposed.
            import io
            import zipfile

            data = original_zip(self._root, artifact)
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                urls = repository_zip_urls(
                    artifact.addon_id, archive.read(f"{artifact.addon_id}/addon.xml")
                )
            for hostname in sorted({urlsplit(url).hostname or "" for url in urls}):
                if hostname is None:
                    raise NativeZipError("native_zip_repository_url_invalid")
                self._resolve(hostname)
                if self._monotonic() - started > 60:
                    raise NativeZipError("native_zip_preflight_timed_out")
            inventory = self._inventory_for(row, observation)
            if any(item.addon_id == artifact.addon_id for item in inventory.items):
                raise NativeZipError("native_zip_addon_already_present")
            destination = self._destination(row)
            if destination.destination_state != "absent":
                raise NativeZipError("native_zip_destination_exists")
            self._authorization.authorize_native_zip_installation(
                principal, request.target_id
            )
            self._observe(row.target_id, row)
            if self._monotonic() - started > 60:
                raise NativeZipError("native_zip_preflight_timed_out")
            with self._sessions.begin() as session:
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
                self._authorization.recheck_native_zip_installation(
                    session,
                    principal,
                    request.target_id,
                    runtime_revision_id=row.runtime_revision_id,
                )
                activated = session.scalar(
                    update(NativeZipInstallationRow)
                    .where(
                        NativeZipInstallationRow.id == row.id,
                        NativeZipInstallationRow.expires_at > timestamp(self._clock()),
                        NativeZipInstallationRow.state == "requested",
                    )
                    .values(stage_ready=True)
                    .returning(NativeZipInstallationRow.id)
                )
                if activated != row.id:
                    raise NativeZipError("native_zip_preflight_expired")
        except Exception:
            self._finish(row.id, "failed", "native_zip_preflight_failed")
            raise
        return self.get(principal, UUID(row.id))

    def get(self, principal: McpPrincipal, operation_id: UUID) -> NativeZipStatus:
        self.sweep()
        row = self._owned(principal, operation_id)
        return self._status(row)

    def verify(self, principal: McpPrincipal, operation_id: UUID) -> NativeZipStatus:
        self.sweep()
        row = self._owned(principal, operation_id)
        if row.state in {"completed", "failed"} or not row.stage_ready:
            return self._status(row)
        try:
            observation = self._observe(row.target_id, row)
            artifact = self._artifact(row)
            evidence_started_at = self._clock()
            evidence_started_tick = self._monotonic()
            matched = self._destination(row)
            observation = self._observe(row.target_id, row)
            inventory = self._inventory_for(row, observation)
            item = next(
                (i for i in inventory.items if i.addon_id == artifact.addon_id), None
            )
            self._authorization.authorize_native_zip_installation(
                principal, UUID(row.target_id)
            )
            self._observe(row.target_id, row)
            if (
                item is not None
                and item.version == artifact.addon_version
                and item.kodi_type == "xbmc.addon.repository"
                and item.enabled
                and matched.matched
                and row.zip_get_requested_at is not None
            ):
                self._finish(
                    row.id,
                    "completed",
                    None,
                    evidence_started_at=evidence_started_at,
                    evidence_started_tick=evidence_started_tick,
                    principal=principal,
                )
            else:
                self._finish(
                    row.id,
                    "unverified",
                    "native_installation_not_confirmed",
                    terminal=False,
                )
        except Exception:
            self._finish(
                row.id,
                "unverified",
                "native_installation_readback_unavailable",
                terminal=False,
            )
        return self.get(principal, operation_id)

    def package(
        self,
        operation_id: UUID,
        peer_ip: str,
        *,
        name: str | None = None,
        mark_delivery: bool = True,
    ) -> tuple[bytes, str]:
        if self._base is None:
            raise NativeZipError("native_zip_delivery_unconfigured")
        self.sweep()
        with self._sessions() as session:
            row = session.get(NativeZipInstallationRow, str(operation_id))
            client = None if row is None else session.get(McpClientRow, row.client_id)
        if (
            row is None
            or client is None
            or not row.stage_ready
            or row.state not in {"requested", "unverified"}
        ):
            raise NativeZipError("native_zip_delivery_unavailable")
        principal = McpPrincipal(
            client_id=UUID(row.client_id),
            credential_id=UUID(row.credential_id),
            preset_id=UUID(client.preset_id),
            permissions=frozenset(),
        )
        self._authorization.authorize_native_zip_installation(
            principal, UUID(row.target_id)
        )
        observation = self._observe(row.target_id, row)
        if peer_ip != observation.ipv4_address:
            raise NativeZipError("native_zip_wrong_target")
        artifact = self._artifact(row)
        filename = f"{artifact.addon_id}-{artifact.addon_version}.zip"
        if name is not None and name != filename:
            raise NativeZipError("native_zip_delivery_unavailable")
        data = original_zip(self._root, artifact)
        self._authorization.authorize_native_zip_installation(
            principal, UUID(row.target_id)
        )
        final_observation = self._observe(row.target_id, row)
        if final_observation.ipv4_address != peer_ip:
            raise NativeZipError("native_zip_target_changed")
        if timestamp(self._clock()) >= row.expires_at:
            raise NativeZipError("native_zip_delivery_expired")
        if name is not None and mark_delivery:
            self.record_zip_get(operation_id)
        return data, filename

    def admit_delivery(self, operation_id: UUID, *, record_get: bool = False) -> None:
        """Serialize final response admission, sampling time after the write lock."""
        with self._sessions.begin() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            now = timestamp(self._clock())
            row = session.get(NativeZipInstallationRow, str(operation_id))
            if (
                row is None
                or not row.stage_ready
                or row.state not in {"requested", "unverified"}
                or row.expires_at <= now
            ):
                raise NativeZipError("native_zip_delivery_expired_or_revoked")
            client = session.get(McpClientRow, row.client_id)
            if client is None:
                raise McpAuthorizationError("permission_denied")
            principal = McpPrincipal(
                client_id=UUID(row.client_id),
                credential_id=UUID(row.credential_id),
                preset_id=UUID(client.preset_id),
                permissions=frozenset(),
            )
            self._authorization.recheck_native_zip_installation(
                session,
                principal,
                UUID(row.target_id),
                runtime_revision_id=row.runtime_revision_id,
            )
            if record_get:
                row.zip_get_requested_at = now

    def record_zip_get(self, operation_id: UUID) -> None:
        """Server-only evidence of a valid named GET, not transfer completion."""
        self.admit_delivery(operation_id, record_get=True)

    def _owned(
        self, principal: McpPrincipal, identity: UUID
    ) -> NativeZipInstallationRow:
        with self._sessions() as session:
            row = session.get(NativeZipInstallationRow, str(identity))
        if row is None or row.client_id != str(principal.client_id):
            raise McpAuthorizationError("target_not_found_or_not_authorized")
        self._authorization.authorize_native_zip_installation(
            principal, UUID(row.target_id), require_running=False
        )
        return row

    def _artifact(self, row: NativeZipInstallationRow) -> AddonZipArtifact:
        with self._sessions() as session:
            artifact = AddonZipArtifactRepository(session).get(UUID(row.artifact_id))
        if (
            artifact is None
            or artifact.extension_classification
            is not AddonZipExtensionClassification.KODI_REPOSITORY
            or artifact.origin is not AddonZipOrigin.UPLOADED_ZIP
        ):
            raise NativeZipError("native_zip_artifact_unsupported")
        if (
            artifact.compressed_size > MAX_NATIVE_ZIP_BYTES
            or artifact.total_uncompressed_bytes > 64 * 1024 * 1024
            or artifact.member_count > 4096
        ):
            raise NativeZipError("native_zip_size_exceeded")
        return artifact

    def _observe(
        self, target: str, row: NativeZipInstallationRow | None = None
    ) -> ObserveManagedKodiRuntimeResult:
        items = self._broker.managed_container_inventory(
            managed_instance_id=target
        ).containers
        if (
            len(items) != 1
            or items[0].managed_instance_id != target
            or items[0].runtime_state is not ContainerRuntimeState.RUNNING
            or items[0].health_state is not ContainerHealthState.HEALTHY
        ):
            raise NativeZipError("native_zip_target_ambiguous")
        result = self._broker.observe_managed_kodi_runtime(
            managed_instance_id=target, expected_container_id=items[0].container_id
        )
        if (
            result.managed_instance_id != target
            or result.container_id != items[0].container_id
            or result.runtime_state is not ContainerRuntimeState.RUNNING
            or result.process_state is not KodiProcessState.RUNNING
        ):
            raise NativeZipError("native_zip_target_not_running")
        if row is not None and (
            result.container_id != row.expected_container_id
            or result.runtime_revision_id != row.runtime_revision_id
        ):
            raise NativeZipError("native_zip_target_changed")
        return result

    def _inventory_for(
        self,
        row: NativeZipInstallationRow,
        observation: ObserveManagedKodiRuntimeResult,
    ) -> KodiAddonInventoryResult:
        with self._sessions() as session:
            instance = ManagedInstanceRepository(session).get(UUID(row.target_id))
            profile = (
                None
                if instance is None
                else RuntimeExecutionProfileRepository(session).get(
                    instance.runtime_revision_id
                )
            )
        if (
            profile is None
            or str(profile.runtime_revision_id) != row.runtime_revision_id
        ):
            raise NativeZipError("native_zip_runtime_changed")
        result = self._inventory(observation, profile).get_inventory()
        if result.identity.kodi_release != profile.kodi_release:
            raise NativeZipError("native_zip_inventory_identity_changed")
        return result

    def _destination(self, row: NativeZipInstallationRow) -> AddonZipDestinationResult:
        result = self._broker.observe_native_zip_installation(UUID(row.id))
        if (
            result.managed_instance_id,
            result.artifact_id,
            result.installation_operation_id,
            result.runtime_revision_id,
            result.container_id,
        ) != (
            row.target_id,
            row.artifact_id,
            row.id,
            row.runtime_revision_id,
            row.expected_container_id,
        ):
            raise NativeZipError("native_zip_readback_identity_changed")
        if result.matched != (result.destination_state == "exact_payload_present"):
            raise NativeZipError("native_zip_readback_contradictory")
        return result

    def _finish(
        self,
        identity: str,
        state: str,
        reason: str | None,
        *,
        terminal: bool = True,
        evidence_started_at: datetime | None = None,
        evidence_started_tick: float | None = None,
        principal: McpPrincipal | None = None,
    ) -> None:
        with self._sessions.begin() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            now = self._clock()
            if state == "completed":
                row = session.get(NativeZipInstallationRow, identity)
                if row is None or principal is None:
                    raise McpAuthorizationError("permission_denied")
                self._authorization.recheck_native_zip_installation(
                    session,
                    principal,
                    UUID(row.target_id),
                    runtime_revision_id=row.runtime_revision_id,
                )
            if state == "completed" and (
                evidence_started_at is None
                or evidence_started_tick is None
                or now - evidence_started_at > timedelta(seconds=15)
                or self._monotonic() - evidence_started_tick > 15
            ):
                state, reason, terminal = (
                    "unverified",
                    "native_confirmation_stale",
                    False,
                )
            values: dict[str, object] = {"state": state, "failure_reason": reason}
            if terminal:
                values.update(stage_ready=False, completed_at=timestamp(self._clock()))
            statement = update(NativeZipInstallationRow).where(
                NativeZipInstallationRow.id == identity,
                NativeZipInstallationRow.state.in_(("requested", "unverified")),
            )
            if state == "completed":
                statement = statement.where(
                    NativeZipInstallationRow.expires_at > timestamp(self._clock()),
                    NativeZipInstallationRow.stage_ready.is_(True),
                )
            session.execute(statement.values(**values))

    def _status(self, row: NativeZipInstallationRow) -> NativeZipStatus:
        return NativeZipStatus(
            operation_id=UUID(row.id),
            target_id=UUID(row.target_id),
            artifact_id=UUID(row.artifact_id),
            state=cast(
                Literal["requested", "completed", "failed", "unverified"], row.state
            ),
            expires_at=datetime.fromisoformat(row.expires_at.replace("Z", "+00:00")),
            completed_at=None
            if row.completed_at is None
            else datetime.fromisoformat(row.completed_at.replace("Z", "+00:00")),
            failure_reason=row.failure_reason,
            source_url=f"{self._base}/native-zip-packages/{row.id}/"
            if row.stage_ready and self._base is not None
            else None,
        )
