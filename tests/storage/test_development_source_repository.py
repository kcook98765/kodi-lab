import json
from datetime import UTC, datetime
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from kodi_lab.domain import DevelopmentSource, DevelopmentSourceKind
from kodi_lab.storage.repositories import DevelopmentSourceRepository

NOW = datetime(2026, 9, 16, 18, 0, tzinfo=UTC)


def source(
    ordinal: int,
    *,
    logical_name: str,
    locator: str,
    addon_id: str,
    extensions: tuple[str, ...] = ("xbmc.python.pluginsource",),
) -> DevelopmentSource:
    return DevelopmentSource(
        id=UUID(f"92000000-0000-4000-8000-{ordinal:012d}"),
        logical_name=logical_name,
        source_kind=DevelopmentSourceKind.LOCAL_DIRECTORY,
        relative_locator=locator,
        addon_id=addon_id,
        observed_name=f"Source {ordinal}",
        observed_version=f"1.0.{ordinal}",
        extension_types=extensions,
        registered_at=NOW,
        validated_at=NOW,
    )


def test_repository_create_get_duplicate_lookups_and_order(db_session: Session) -> None:
    repository = DevelopmentSourceRepository(db_session)
    zulu = source(
        1,
        logical_name="Zulu",
        locator="sources/zulu",
        addon_id="plugin.video.zulu",
        extensions=("xbmc.service", "xbmc.python.pluginsource"),
    )
    alpha = source(
        2,
        logical_name="Alpha",
        locator="sources/alpha",
        addon_id="plugin.video.alpha",
    )
    repository.create(zulu)
    repository.create(alpha)

    assert repository.get(zulu.id) == zulu
    assert repository.get_by_logical_name("Zulu") == zulu
    assert repository.get_by_relative_locator("sources/zulu") == zulu
    assert repository.get_by_addon_id("plugin.video.zulu") == zulu
    assert repository.list() == [alpha, zulu]

    persisted = db_session.execute(
        text(
            "SELECT relative_locator, extension_types_json FROM development_sources "
            "WHERE id = :id"
        ),
        {"id": str(zulu.id)},
    ).one()
    assert persisted.relative_locator == "sources/zulu"
    assert persisted.extension_types_json == json.dumps(
        ["xbmc.python.pluginsource", "xbmc.service"], separators=(",", ":")
    )
    assert "/home/" not in persisted.relative_locator


@pytest.mark.parametrize("field", ("logical_name", "locator", "addon_id"))
def test_repository_database_uniqueness(db_session: Session, field: str) -> None:
    repository = DevelopmentSourceRepository(db_session)
    first = source(
        1,
        logical_name="First",
        locator="sources/first",
        addon_id="plugin.video.first",
    )
    repository.create(first)
    duplicate = source(
        2,
        logical_name="First" if field == "logical_name" else "Second",
        locator="sources/first" if field == "locator" else "sources/second",
        addon_id=(
            "plugin.video.first" if field == "addon_id" else "plugin.video.second"
        ),
    )
    with pytest.raises(IntegrityError):
        repository.create(duplicate)
