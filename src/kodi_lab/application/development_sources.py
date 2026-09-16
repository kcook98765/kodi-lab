"""Transactional administration of global local-directory development sources."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.errors import (
    DevelopmentSourceConflictError,
    DevelopmentSourceDataValidationError,
    DevelopmentSourceNotFoundError,
)
from kodi_lab.development import DevelopmentSourceValidator
from kodi_lab.domain import (
    MAX_ADDON_ID,
    MAX_ADDON_NAME,
    MAX_ADDON_VERSION,
    MAX_DEVELOPMENT_SOURCE_LOGICAL_NAME,
    MAX_EXTENSION_TYPES,
    DevelopmentSource,
    DevelopmentSourceKind,
    normalize_development_source_logical_name,
)
from kodi_lab.storage.repositories import DevelopmentSourceRepository


class DevelopmentSourceRead(BaseModel):
    """Conservative catalog DTO that contains no host filesystem locator."""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    logical_name: str = Field(max_length=MAX_DEVELOPMENT_SOURCE_LOGICAL_NAME)
    source_kind: DevelopmentSourceKind
    addon_id: str = Field(max_length=MAX_ADDON_ID)
    observed_name: str = Field(max_length=MAX_ADDON_NAME)
    observed_version: str = Field(max_length=MAX_ADDON_VERSION)
    extension_types: tuple[str, ...] = Field(max_length=MAX_EXTENSION_TYPES)
    validated_at: datetime


def development_source_read(source: DevelopmentSource) -> DevelopmentSourceRead:
    """Project one registration without exposing its host locator."""

    return DevelopmentSourceRead(
        id=source.id,
        logical_name=source.logical_name,
        source_kind=source.source_kind,
        addon_id=source.addon_id,
        observed_name=source.observed_name,
        observed_version=source.observed_version,
        extension_types=source.extension_types,
        validated_at=source.validated_at,
    )


class DevelopmentSourceAdministrationService:
    """Register and read immutable observations of mutable local sources."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        validator: DevelopmentSourceValidator,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        uuid_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        self._session_factory = session_factory
        self._validator = validator
        self._clock = clock
        self._uuid_factory = uuid_factory

    def register_source(
        self, *, logical_name: str, relative_locator: str
    ) -> DevelopmentSourceRead:
        """Validate and atomically register one server-identified source."""

        try:
            normalized_name = normalize_development_source_logical_name(logical_name)
        except (TypeError, ValueError) as error:
            raise DevelopmentSourceDataValidationError(str(error)) from error
        observation = self._validator.validate(relative_locator)
        timestamp = self._clock()
        try:
            source = DevelopmentSource(
                id=self._uuid_factory(),
                logical_name=normalized_name,
                source_kind=DevelopmentSourceKind.LOCAL_DIRECTORY,
                relative_locator=observation.relative_locator,
                addon_id=observation.addon_id,
                observed_name=observation.observed_name,
                observed_version=observation.observed_version,
                extension_types=observation.extension_types,
                registered_at=timestamp,
                validated_at=timestamp,
            )
        except ValueError as error:
            raise DevelopmentSourceDataValidationError(str(error)) from error

        try:
            with self._session_factory.begin() as session:
                repository = DevelopmentSourceRepository(session)
                self._require_unique(repository, source)
                repository.create(source)
        except IntegrityError as error:
            raise DevelopmentSourceConflictError(
                field="catalog identity", value="concurrent conflict"
            ) from error
        return development_source_read(source)

    def get_source(self, source_id: UUID) -> DevelopmentSourceRead:
        """Return one conservative registration DTO."""

        with self._session_factory() as session:
            source = DevelopmentSourceRepository(session).get(source_id)
        if source is None:
            raise DevelopmentSourceNotFoundError(source_id)
        return development_source_read(source)

    def list_sources(self) -> list[DevelopmentSourceRead]:
        """Return deterministic logical-name/UUID ordered registrations."""

        with self._session_factory() as session:
            sources = DevelopmentSourceRepository(session).list()
        return [development_source_read(source) for source in sources]

    @staticmethod
    def _require_unique(
        repository: DevelopmentSourceRepository, source: DevelopmentSource
    ) -> None:
        checks = (
            (
                "logical name",
                source.logical_name,
                repository.get_by_logical_name(source.logical_name),
            ),
            (
                "relative locator",
                source.relative_locator,
                repository.get_by_relative_locator(source.relative_locator),
            ),
            ("add-on ID", source.addon_id, repository.get_by_addon_id(source.addon_id)),
        )
        for field, value, existing in checks:
            if existing is not None:
                raise DevelopmentSourceConflictError(field=field, value=value)
