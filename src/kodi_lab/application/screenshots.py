"""Web-admin managed screenshot capability, capture, and latest-only cache."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from threading import BoundedSemaphore, Lock
from typing import Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.errors import ManagedInstanceNotFoundError
from kodi_lab.application.reconciliation import ManagedInstanceReconciliationService
from kodi_lab.application.runtime_status import (
    ManagedRuntimeStatusService,
    SemanticUsabilityState,
)
from kodi_lab.broker.docker_inventory import (
    ContainerHealthState,
    ContainerRuntimeState,
    KodiProcessState,
)
from kodi_lab.broker.errors import ClientError, RemoteBrokerError
from kodi_lab.broker.operations import (
    CaptureManagedKodiScreenshotResult,
    ManagedContainerInventoryResult,
)
from kodi_lab.kodi.screenshots import (
    PNG_MIME_TYPE,
    ScreenshotArtifactError,
    cleanup_capture_stage,
    is_supported_screenshot_runtime,
    prepare_capture_stage,
    publish_png,
    raw_capture_path,
    read_published_png,
    remove_published_png,
    validate_png,
    x11_frame_to_png,
)
from kodi_lab.storage.models import (
    ManagedScreenshotRow,
    RuntimeDefinitionRow,
    RuntimeExecutionProfileRow,
    RuntimeRevisionRow,
)

LOGGER = logging.getLogger("kodi_lab.screenshot.audit")


class ScreenshotServiceError(RuntimeError):
    def __init__(self, code: str, safe_message: str) -> None:
        self.code = code
        self.safe_message = safe_message[:256]
        super().__init__(self.safe_message)


class ManagedScreenshotStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    managed_instance_id: UUID
    screenshot_supported: bool
    screenshot_available_now: bool
    latest_capture_available: bool
    capture_id: UUID | None = None
    captured_at: datetime | None = None
    age_seconds: int | None = Field(default=None, ge=0)
    width: int | None = Field(default=None, ge=1, le=4096)
    height: int | None = Field(default=None, ge=1, le=2160)
    mime_type: str | None = Field(default=None, max_length=16)
    byte_size: int | None = Field(default=None, ge=1, le=8 * 1024 * 1024)
    last_attempt_at: datetime | None = None
    last_error_code: str | None = Field(default=None, max_length=64)


class ManagedScreenshotCaptureResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capture_id: UUID
    screenshot: ManagedScreenshotStatus


class _ScreenshotBroker(Protocol):
    def managed_container_inventory(self) -> ManagedContainerInventoryResult: ...

    def capture_managed_kodi_screenshot(
        self,
        *,
        managed_instance_id: str,
        expected_container_id: str,
        expected_runtime_revision_id: str,
        capture_id: str,
    ) -> CaptureManagedKodiScreenshotResult: ...


class ScreenshotCaptureCoordinator:
    """Short process-local per-instance exclusion plus a four-capture global cap."""

    def __init__(self, *, global_limit: int = 4) -> None:
        self._guard = Lock()
        self._active: set[UUID] = set()
        self._global = BoundedSemaphore(global_limit)

    def acquire(self, instance_id: UUID) -> None:
        with self._guard:
            if instance_id in self._active:
                raise ScreenshotServiceError(
                    "screenshot_capture_in_progress",
                    "A screenshot capture is already running for this instance.",
                )
            self._active.add(instance_id)
        if not self._global.acquire(blocking=False):
            with self._guard:
                self._active.remove(instance_id)
            raise ScreenshotServiceError(
                "visual_rate_limited",
                "The global screenshot capture limit is currently reached.",
            )

    def release(self, instance_id: UUID) -> None:
        with self._guard:
            self._active.discard(instance_id)
        self._global.release()


class ManagedScreenshotService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        broker: _ScreenshotBroker,
        status: ManagedRuntimeStatusService,
        reconciliation: ManagedInstanceReconciliationService,
        *,
        data_root: Path,
        coordinator: ScreenshotCaptureCoordinator | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._broker = broker
        self._status = status
        self._reconciliation = reconciliation
        self._data_root = data_root
        self._coordinator = coordinator or ScreenshotCaptureCoordinator()

    def get_status(self, instance_id: UUID) -> ManagedScreenshotStatus:
        runtime = self._status.get(instance_id)
        supported = self._runtime_supported(runtime.runtime_revision_id)
        available_now = (
            supported
            and runtime.container_state is ContainerRuntimeState.RUNNING
            and runtime.process_state is KodiProcessState.RUNNING
            and runtime.usability_state is SemanticUsabilityState.USABLE
            and not self._reconciliation.status(instance_id).blocking
        )
        with self._session_factory() as session:
            row = session.get(ManagedScreenshotRow, str(instance_id))
            return self._project(instance_id, supported, available_now, row)

    def capture(self, instance_id: UUID) -> ManagedScreenshotCaptureResult:
        capture_id = uuid4()
        requested_at = datetime.now(UTC)
        runtime = self._status.get(instance_id)
        if not self._runtime_supported(runtime.runtime_revision_id):
            self._record_failure(instance_id, requested_at, "screenshot_unsupported")
            raise ScreenshotServiceError(
                "screenshot_unsupported",
                "Screenshot capture is not supported for this runtime revision.",
            )
        if runtime.container_state is not ContainerRuntimeState.RUNNING:
            self._record_failure(
                instance_id, requested_at, "visual_instance_not_running"
            )
            raise ScreenshotServiceError(
                "visual_instance_not_running", "The managed instance is not running."
            )
        if (
            runtime.process_state is not KodiProcessState.RUNNING
            or runtime.usability_state is not SemanticUsabilityState.USABLE
        ):
            self._record_failure(instance_id, requested_at, "visual_kodi_not_ready")
            raise ScreenshotServiceError(
                "visual_kodi_not_ready", "Kodi is not healthy and usable."
            )
        if self._reconciliation.status(instance_id).blocking:
            self._record_failure(
                instance_id, requested_at, "screenshot_capture_unavailable"
            )
            raise ScreenshotServiceError(
                "screenshot_capture_unavailable",
                "Screenshot capture is blocked pending runtime reconciliation.",
            )

        self._coordinator.acquire(instance_id)
        stage_prepared = False
        published = False
        try:
            container_id = self._eligible_container(instance_id)
            prepare_capture_stage(self._data_root, capture_id)
            stage_prepared = True
            outcome = self._broker.capture_managed_kodi_screenshot(
                managed_instance_id=str(instance_id),
                expected_container_id=container_id,
                expected_runtime_revision_id=str(runtime.runtime_revision_id),
                capture_id=str(capture_id),
            )
            if (
                outcome.capture_id != str(capture_id)
                or outcome.container_id != container_id
                or outcome.runtime_revision_id != str(runtime.runtime_revision_id)
            ):
                raise ScreenshotServiceError(
                    "screenshot_capture_unavailable",
                    "Screenshot runtime identity changed during capture.",
                )
            raw = raw_capture_path(self._data_root, capture_id).read_bytes()
            if len(raw) != outcome.raw_byte_size:
                raise ScreenshotArtifactError(
                    "Screenshot raw artifact size differs from broker result."
                )
            if outcome.backend != "x11_get_image":
                raise ScreenshotArtifactError(
                    "Screenshot broker returned an unsupported backend."
                )
            png = x11_frame_to_png(raw)
            validated = validate_png(png)
            self._revalidate_container(instance_id, container_id)
            old_capture_id = self._current_capture_id(instance_id)
            publish_png(
                data_root=self._data_root,
                instance_id=instance_id,
                capture_id=capture_id,
                png=png,
            )
            published = True
            captured_at = datetime.now(UTC)
            self._publish_metadata(
                instance_id=instance_id,
                capture_id=capture_id,
                captured_at=captured_at,
                width=validated.width,
                height=validated.height,
                byte_size=validated.byte_size,
            )
            if old_capture_id is not None and old_capture_id != capture_id:
                remove_published_png(self._data_root, instance_id, old_capture_id)
            result = ManagedScreenshotCaptureResult(
                capture_id=capture_id,
                screenshot=self.get_status(instance_id),
            )
            LOGGER.info(
                "managed screenshot capture completed",
                extra={
                    "capture_id": str(capture_id),
                    "managed_instance_id": str(instance_id),
                    "interface": "web",
                    "actor": "local_admin",
                    "requested_at": requested_at.isoformat(),
                    "completed_at": captured_at.isoformat(),
                    "result": "succeeded",
                    "byte_count": validated.byte_size,
                    "width": validated.width,
                    "height": validated.height,
                },
            )
            return result
        except ManagedInstanceNotFoundError:
            raise
        except ScreenshotServiceError as error:
            self._record_failure(instance_id, requested_at, error.code)
            self._audit_failure(instance_id, capture_id, requested_at, error.code)
            raise
        except (ClientError, ScreenshotArtifactError, OSError) as error:
            code = self._safe_failure_code(error)
            self._record_failure(instance_id, requested_at, code)
            self._audit_failure(instance_id, capture_id, requested_at, code)
            raise ScreenshotServiceError(
                code, "Screenshot refresh could not be completed safely."
            ) from error
        finally:
            if published and self._current_capture_id(instance_id) != capture_id:
                remove_published_png(self._data_root, instance_id, capture_id)
            if stage_prepared:
                cleanup_capture_stage(self._data_root, capture_id)
            self._coordinator.release(instance_id)

    def latest_png(self, instance_id: UUID) -> bytes:
        self._status.get(instance_id)
        capture_id = self._current_capture_id(instance_id)
        if capture_id is None:
            raise ScreenshotServiceError(
                "screenshot_capture_unavailable", "No current screenshot is available."
            )
        try:
            return read_published_png(self._data_root, instance_id, capture_id)
        except ScreenshotArtifactError as error:
            raise ScreenshotServiceError(
                "screenshot_capture_unavailable",
                "The current screenshot cache object is unavailable.",
            ) from error

    def _eligible_container(self, instance_id: UUID) -> str:
        try:
            inventory = self._broker.managed_container_inventory()
        except ClientError as error:
            raise ScreenshotServiceError(
                "screenshot_capture_unavailable",
                "Managed runtime inventory is unavailable.",
            ) from error
        matches = [
            item
            for item in inventory.containers
            if item.managed_instance_id == str(instance_id)
        ]
        if (
            len(matches) != 1
            or matches[0].runtime_state is not ContainerRuntimeState.RUNNING
            or matches[0].health_state is not ContainerHealthState.HEALTHY
        ):
            raise ScreenshotServiceError(
                "screenshot_capture_unavailable",
                "The exact healthy managed runtime is unavailable.",
            )
        return matches[0].container_id

    def _revalidate_container(self, instance_id: UUID, container_id: str) -> None:
        if self._eligible_container(instance_id) != container_id:
            raise ScreenshotServiceError(
                "screenshot_capture_unavailable",
                "Managed runtime identity changed before screenshot publication.",
            )

    def _project(
        self,
        instance_id: UUID,
        supported: bool,
        available_now: bool,
        row: ManagedScreenshotRow | None,
    ) -> ManagedScreenshotStatus:
        if row is None:
            return ManagedScreenshotStatus(
                managed_instance_id=instance_id,
                screenshot_supported=supported,
                screenshot_available_now=available_now,
                latest_capture_available=False,
            )
        capture_id = None if row.capture_id is None else UUID(row.capture_id)
        captured_at = (
            None
            if row.captured_at is None
            else self._timestamp_from_text(row.captured_at)
        )
        latest = False
        if capture_id is not None:
            try:
                read_published_png(self._data_root, instance_id, capture_id)
                latest = True
            except ScreenshotArtifactError:
                latest = False
        age = None
        if latest and captured_at is not None:
            age = max(0, int((datetime.now(UTC) - captured_at).total_seconds()))
        return ManagedScreenshotStatus(
            managed_instance_id=instance_id,
            screenshot_supported=supported,
            screenshot_available_now=available_now,
            latest_capture_available=latest,
            capture_id=capture_id if latest else None,
            captured_at=captured_at if latest else None,
            age_seconds=age,
            width=row.width if latest else None,
            height=row.height if latest else None,
            mime_type=row.mime_type if latest else None,
            byte_size=row.byte_size if latest else None,
            last_attempt_at=self._timestamp_from_text(row.last_attempt_at),
            last_error_code=row.last_error_code,
        )

    def _runtime_supported(self, runtime_revision_id: UUID) -> bool:
        with self._session_factory() as session:
            revision = session.get(RuntimeRevisionRow, str(runtime_revision_id))
            profile = session.get(RuntimeExecutionProfileRow, str(runtime_revision_id))
            if revision is None or profile is None:
                return False
            definition = session.get(
                RuntimeDefinitionRow, revision.runtime_definition_id
            )
            if definition is None:
                return False
            return is_supported_screenshot_runtime(
                runtime_revision_id=str(runtime_revision_id),
                canonical_release=definition.kodi_version,
                expected_application_name=profile.expected_application_name,
                expected_kodi_version=profile.expected_kodi_version,
                expected_version_tag=profile.expected_version_tag,
                expected_prerelease_ordinal=profile.expected_prerelease_ordinal,
            )

    def _current_capture_id(self, instance_id: UUID) -> UUID | None:
        with self._session_factory() as session:
            row = session.get(ManagedScreenshotRow, str(instance_id))
            return (
                None if row is None or row.capture_id is None else UUID(row.capture_id)
            )

    def _publish_metadata(
        self,
        *,
        instance_id: UUID,
        capture_id: UUID,
        captured_at: datetime,
        width: int,
        height: int,
        byte_size: int,
    ) -> None:
        timestamp = self._timestamp_to_text(captured_at)
        with self._session_factory.begin() as session:
            row = session.get(ManagedScreenshotRow, str(instance_id))
            if row is None:
                row = ManagedScreenshotRow(managed_instance_id=str(instance_id))
                session.add(row)
            row.capture_id = str(capture_id)
            row.captured_at = timestamp
            row.mime_type = PNG_MIME_TYPE
            row.width = width
            row.height = height
            row.byte_size = byte_size
            row.last_attempt_at = timestamp
            row.last_error_code = None

    def _record_failure(
        self, instance_id: UUID, attempted_at: datetime, code: str
    ) -> None:
        with self._session_factory.begin() as session:
            row = session.get(ManagedScreenshotRow, str(instance_id))
            if row is None:
                row = ManagedScreenshotRow(
                    managed_instance_id=str(instance_id),
                    capture_id=None,
                    captured_at=None,
                    mime_type=None,
                    width=None,
                    height=None,
                    byte_size=None,
                    last_attempt_at=self._timestamp_to_text(attempted_at),
                    last_error_code=code,
                )
                session.add(row)
            else:
                row.last_attempt_at = self._timestamp_to_text(attempted_at)
                row.last_error_code = code

    @staticmethod
    def _safe_failure_code(error: BaseException) -> str:
        if isinstance(error, RemoteBrokerError):
            if error.code == "screenshot_unsupported":
                return "screenshot_unsupported"
            if error.code == "screenshot_runtime_identity_changed":
                return "screenshot_capture_unavailable"
            if error.code in {
                "screenshot_capture_failed",
                "screenshot_capture_unavailable",
            }:
                return "screenshot_capture_unavailable"
        if isinstance(error, ScreenshotArtifactError):
            return "screenshot_invalid_result"
        return "screenshot_capture_unavailable"

    @staticmethod
    def _audit_failure(
        instance_id: UUID, capture_id: UUID, requested_at: datetime, code: str
    ) -> None:
        LOGGER.warning(
            "managed screenshot capture failed",
            extra={
                "capture_id": str(capture_id),
                "managed_instance_id": str(instance_id),
                "interface": "web",
                "actor": "local_admin",
                "requested_at": requested_at.isoformat(),
                "completed_at": datetime.now(UTC).isoformat(),
                "result": code,
            },
        )

    @staticmethod
    def _timestamp_to_text(value: datetime) -> str:
        return (
            value.astimezone(UTC)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )

    @staticmethod
    def _timestamp_from_text(value: str) -> datetime:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
