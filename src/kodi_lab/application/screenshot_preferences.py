"""Administrator capture preference, independent of capability and Bridge trust."""

from datetime import UTC, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool
from sqlalchemy import update
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.domain import TargetType
from kodi_lab.storage.models import ExternalScreenshotPreferenceRow, TargetRow


class ScreenshotPreferenceError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ScreenshotPreferenceRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    target_id: UUID
    enabled: bool
    revision: int
    updated_by: str
    updated_at: datetime


class ScreenshotPreferenceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: StrictBool
    expected_revision: int = Field(ge=1, strict=True)


def read_screenshot_preference(
    session: Session, target_id: UUID
) -> ScreenshotPreferenceRead:
    target = session.get(TargetRow, str(target_id))
    if target is None or target.target_type not in {
        TargetType.EXTERNAL_DEVELOPMENT,
        TargetType.EXTERNAL_PRODUCTION,
    }:
        raise ScreenshotPreferenceError("target_not_found")
    row = session.get(ExternalScreenshotPreferenceRow, str(target_id))
    if row is None:
        # A missing record is never an implicit enable or a policy reset.
        raise ScreenshotPreferenceError("screenshot_preference_unavailable")
    return ScreenshotPreferenceRead(
        target_id=target_id,
        enabled=row.enabled,
        revision=row.revision,
        updated_by=row.updated_by,
        updated_at=datetime.fromisoformat(row.updated_at.replace("Z", "+00:00")),
    )


def require_screenshot_preference(
    session: Session, target_id: UUID, *, expected_revision: int | None = None
) -> int:
    preference = read_screenshot_preference(session, target_id)
    if not preference.enabled:
        raise ScreenshotPreferenceError("screenshots_disabled")
    if expected_revision is not None and preference.revision != expected_revision:
        raise ScreenshotPreferenceError("screenshot_preference_changed")
    return preference.revision


class ScreenshotPreferenceService:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def read(self, target_id: UUID) -> ScreenshotPreferenceRead:
        with self._sessions() as session:
            return read_screenshot_preference(session, target_id)

    def update(
        self,
        target_id: UUID,
        *,
        enabled: bool,
        expected_revision: int,
        administrator_reference: str,
    ) -> ScreenshotPreferenceRead:
        if not 1 <= len(administrator_reference.strip()) <= 64:
            raise ScreenshotPreferenceError("administrator_reference_invalid")
        with self._sessions.begin() as session:
            current = read_screenshot_preference(session, target_id)
            if current.revision != expected_revision:
                raise ScreenshotPreferenceError("screenshot_preference_conflict")
            if current.enabled == enabled:
                return current
            changed = session.execute(
                update(ExternalScreenshotPreferenceRow)
                .where(
                    ExternalScreenshotPreferenceRow.target_id == str(target_id),
                    ExternalScreenshotPreferenceRow.revision == expected_revision,
                )
                .values(
                    enabled=enabled,
                    revision=expected_revision + 1,
                    updated_by=administrator_reference.strip(),
                    updated_at=datetime.now(UTC)
                    .isoformat(timespec="microseconds")
                    .replace("+00:00", "Z"),
                )
                .returning(ExternalScreenshotPreferenceRow.revision)
            )
            if changed.scalar_one_or_none() is None:
                raise ScreenshotPreferenceError("screenshot_preference_conflict")
            session.expire_all()
            return read_screenshot_preference(session, target_id)
