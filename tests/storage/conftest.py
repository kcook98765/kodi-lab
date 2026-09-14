from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from kodi_lab.storage import metadata
from kodi_lab.storage.engine import create_database_engine, create_session_factory


@pytest.fixture
def db_session(tmp_path: Path) -> Iterator[Session]:
    database_path = tmp_path / "repository.db"
    engine = create_database_engine(f"sqlite:///{database_path}")
    metadata.create_all(engine)
    factory = create_session_factory(engine)

    with factory() as session:
        yield session

    engine.dispose()
