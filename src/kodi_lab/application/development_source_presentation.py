"""Path-free read projections for development-source Web presentation."""

from uuid import UUID

from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.development_sources import (
    DevelopmentSourceRead,
    development_source_read,
)
from kodi_lab.application.errors import (
    DevelopmentSourceNotFoundError,
)
from kodi_lab.storage.repositories import (
    DevelopmentSourceRepository,
)


class DevelopmentSourceSummary(BaseModel):
    """One registered source and its current live attachment count."""

    model_config = ConfigDict(extra="forbid")

    source: DevelopmentSourceRead


class DevelopmentSourceDetail(BaseModel):
    """Stored source observation plus current live associations."""

    model_config = ConfigDict(extra="forbid")

    source: DevelopmentSourceRead


class DevelopmentSourcePresentationService:
    """Build read-only DTOs without consulting the host source filesystem."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def list_sources(self) -> list[DevelopmentSourceSummary]:
        with self._session_factory() as session:
            sources = DevelopmentSourceRepository(session).list()
            return [
                DevelopmentSourceSummary(source=development_source_read(source))
                for source in sources
            ]

    def get_source(self, source_id: UUID) -> DevelopmentSourceDetail:
        with self._session_factory() as session:
            sources = DevelopmentSourceRepository(session)
            source = sources.get(source_id)
            if source is None:
                raise DevelopmentSourceNotFoundError(source_id)
            return DevelopmentSourceDetail(source=development_source_read(source))
