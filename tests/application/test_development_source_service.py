from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application import (
    DevelopmentSourceAdministrationService,
    DevelopmentSourceConflictError,
    DevelopmentSourceNotFoundError,
)
from kodi_lab.development import DevelopmentSourceValidator
from kodi_lab.storage import metadata
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.storage.repositories import DevelopmentSourceRepository

NOW = datetime(2026, 9, 16, 19, 0, tzinfo=UTC)
SOURCE_ID = UUID("93000000-0000-4000-8000-000000000001")


def write_source(
    root: Path,
    locator: str,
    *,
    addon_id: str,
    name: str,
    version: str,
) -> Path:
    directory = root.joinpath(*locator.split("/"))
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "addon.xml").write_text(
        f'<addon id="{addon_id}" name="{name}" version="{version}" '
        'provider-name="Kodi Lab"><extension point="xbmc.python.pluginsource" />'
        '<extension point="xbmc.service" /></addon>',
        encoding="utf-8",
    )
    return directory


@pytest.fixture
def factory(tmp_path: Path) -> sessionmaker[Session]:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'catalog.db'}")
    metadata.create_all(engine)
    return create_session_factory(engine)


def service(
    factory: sessionmaker[Session], root: Path, *, source_id: UUID = SOURCE_ID
) -> DevelopmentSourceAdministrationService:
    return DevelopmentSourceAdministrationService(
        factory,
        DevelopmentSourceValidator(root),
        clock=lambda: NOW,
        uuid_factory=lambda: source_id,
    )


def test_registration_generates_identity_and_uses_parsed_metadata(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    write_source(
        tmp_path,
        "sources/example",
        addon_id="plugin.video.authoritative",
        name="Authoritative Name",
        version="2.4.6",
    )

    result = service(factory, tmp_path).register_source(
        logical_name="  Local Example  ", relative_locator="sources/example"
    )

    assert result.id == SOURCE_ID
    assert result.logical_name == "Local Example"
    assert result.addon_id == "plugin.video.authoritative"
    assert result.observed_name == "Authoritative Name"
    assert result.observed_version == "2.4.6"
    assert result.extension_types == ("xbmc.python.pluginsource", "xbmc.service")
    assert "relative_locator" not in result.model_dump()
    with factory() as session:
        stored = DevelopmentSourceRepository(session).get(SOURCE_ID)
    assert stored is not None
    assert stored.relative_locator == "sources/example"


@pytest.mark.parametrize(
    ("duplicate", "expected_field"),
    (("name", "logical name"), ("locator", "relative locator"), ("id", "add-on ID")),
)
def test_registration_rejects_each_global_duplicate(
    factory: sessionmaker[Session], tmp_path: Path, duplicate: str, expected_field: str
) -> None:
    write_source(
        tmp_path,
        "first",
        addon_id="plugin.video.first",
        name="First",
        version="1.0.0",
    )
    write_source(
        tmp_path,
        "second",
        addon_id="plugin.video.first" if duplicate == "id" else "plugin.video.second",
        name="Second",
        version="2.0.0",
    )
    catalog = service(factory, tmp_path)
    catalog.register_source(logical_name="First", relative_locator="first")

    with pytest.raises(DevelopmentSourceConflictError) as captured:
        catalog.register_source(
            logical_name="First" if duplicate == "name" else "Second",
            relative_locator="first" if duplicate == "locator" else "second",
        )
    assert captured.value.field == expected_field


def test_list_get_and_not_found_are_deterministic(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    write_source(
        tmp_path, "zulu", addon_id="plugin.video.zulu", name="Zulu", version="1"
    )
    write_source(
        tmp_path, "alpha", addon_id="plugin.video.alpha", name="Alpha", version="1"
    )
    catalog = service(factory, tmp_path)
    zulu = catalog.register_source(logical_name="Zulu", relative_locator="zulu")
    alpha_id = UUID("93000000-0000-4000-8000-000000000002")
    alpha = service(factory, tmp_path, source_id=alpha_id).register_source(
        logical_name="Alpha", relative_locator="alpha"
    )

    assert catalog.list_sources() == [alpha, zulu]
    assert catalog.get_source(alpha_id) == alpha
    with pytest.raises(DevelopmentSourceNotFoundError):
        catalog.get_source(UUID("93000000-0000-4000-8000-000000000099"))


def test_mutable_source_does_not_silently_rewrite_stored_observation(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    write_source(
        tmp_path,
        "mutable",
        addon_id="plugin.video.mutable",
        name="Before",
        version="1.0.0",
    )
    validator = DevelopmentSourceValidator(tmp_path)
    catalog = DevelopmentSourceAdministrationService(
        factory, validator, clock=lambda: NOW, uuid_factory=lambda: SOURCE_ID
    )
    registered = catalog.register_source(
        logical_name="Mutable", relative_locator="mutable"
    )
    write_source(
        tmp_path,
        "mutable",
        addon_id="plugin.video.mutable",
        name="After",
        version="2.0.0",
    )

    assert catalog.get_source(SOURCE_ID) == registered
    assert catalog.get_source(SOURCE_ID).observed_name == "Before"
    current = validator.validate("mutable")
    assert current.observed_name == "After"
    assert current.observed_version == "2.0.0"
    assert catalog.get_source(SOURCE_ID).observed_version == "1.0.0"
