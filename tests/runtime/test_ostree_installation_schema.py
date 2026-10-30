"""Existing migrated schema admits RC1 and protects sealed terminal records."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from kodi_lab.host_bootstrap import generate_master_key
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.storage.repositories import RuntimeRecipeInstallationRepository
from tests.runtime.test_ostree_installation import ready
from tests.storage.test_migrations import migration_setup
from tests.storage.test_migrations import migration_templates as migration_templates


@pytest.mark.parametrize(
    "field,value",
    [
        ("recipe_digest", "sha256:" + "f" * 64),
        ("attestation_json", "{}"),
        ("validation_json", "{}"),
    ],
)
def test_existing_head_accepts_preview_and_refuses_terminal_mutation(
    field: str,
    value: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _config, url = migration_setup(monkeypatch, tmp_path, start_revision="head")
    root = tmp_path / "data"
    root.chmod(0o700)
    generate_master_key(root)
    engine = create_database_engine(url)
    factory = create_session_factory(engine)
    try:
        operation = ready(factory)
        with engine.connect() as connection:
            assert (
                connection.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar()
                == "0090_verified_noop_deployment"
            )
        with (
            pytest.raises(IntegrityError, match="terminal_immutable"),
            factory.begin() as session,
        ):
            row = RuntimeRecipeInstallationRepository(session).get(operation)
            assert row is not None
            setattr(row, field, value)
            session.flush()
        with factory() as session:
            row = RuntimeRecipeInstallationRepository(session).get(operation)
            assert row is not None and row.state.value == "ready"
    finally:
        engine.dispose()
