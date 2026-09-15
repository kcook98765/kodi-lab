from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application import (
    CatalogDataValidationError,
    RuntimeCatalogAdministrationService,
    RuntimeExecutionProfileConflictError,
    RuntimeRevisionNotFoundError,
)
from kodi_lab.domain import (
    ProcessObservationKind,
    ReadinessProbeKind,
    RuntimeDefinition,
    RuntimeKind,
    RuntimeRevision,
)
from kodi_lab.storage import metadata
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.storage.repositories import (
    RuntimeDefinitionRepository,
    RuntimeExecutionProfileRepository,
    RuntimeRevisionRepository,
)

REVISION_ID = UUID(int=2)


def service_with_revision(
    tmp_path: Path,
) -> tuple[RuntimeCatalogAdministrationService, sessionmaker[Session]]:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'profile.db'}")
    metadata.create_all(engine)
    factory = create_session_factory(engine)
    definition = RuntimeDefinition(
        id=UUID(int=1), slug="kodi-21-3", kodi_version="21.3"
    )
    revision = RuntimeRevision(
        id=REVISION_ID,
        runtime_definition_id=definition.id,
        revision_number=1,
        source_kind="official-package",
        source_reference="ubuntu-resolute",
        content_digest="a" * 64,
        manifest_reference="runtime/kodi-21.3/manifest.json",
        manifest_digest="b" * 64,
        built_at=datetime(2026, 9, 14, tzinfo=UTC),
    )
    with factory.begin() as session:
        RuntimeDefinitionRepository(session).create(definition)
        RuntimeRevisionRepository(session).create(revision)
    return RuntimeCatalogAdministrationService(factory), factory


def values() -> dict[str, Any]:
    return {
        "runtime_revision_id": REVISION_ID,
        "runtime_kind": RuntimeKind.KODI,
        "expected_application_name": "Kodi",
        "expected_kodi_version": "21.3",
        "expected_version_tag": "stable",
        "jsonrpc_port": 8080,
        "jsonrpc_path": "/jsonrpc",
        "process_observation": ProcessObservationKind.DOCKER_HEALTHCHECK,
        "readiness_probe": ReadinessProbeKind.KODI_JSONRPC,
        "readiness_timeout_seconds": 90,
        "poll_interval_seconds": 1.0,
        "http_timeout_seconds": 3.0,
    }


def test_registers_exact_immutable_execution_profile(tmp_path: Path) -> None:
    service, factory = service_with_revision(tmp_path)
    profile = service.register_execution_profile(**values())
    with factory() as session:
        assert RuntimeExecutionProfileRepository(session).get(REVISION_ID) == profile
    with pytest.raises(RuntimeExecutionProfileConflictError):
        service.register_execution_profile(**values())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("expected_application_name", "Not Kodi"),
        ("expected_kodi_version", "21.2"),
        ("expected_version_tag", "beta"),
        ("jsonrpc_port", 0),
        ("jsonrpc_path", "relative"),
        ("readiness_timeout_seconds", 601),
        ("poll_interval_seconds", 0.01),
        ("http_timeout_seconds", 6.0),
    ],
)
def test_profile_rejects_noncanonical_runtime_semantics(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    service, _factory = service_with_revision(tmp_path)
    candidate = values()
    candidate[field] = value
    with pytest.raises(CatalogDataValidationError):
        service.register_execution_profile(**candidate)


def test_profile_requires_existing_revision(tmp_path: Path) -> None:
    service, _factory = service_with_revision(tmp_path)
    candidate = values()
    candidate["runtime_revision_id"] = UUID(int=99)
    with pytest.raises(RuntimeRevisionNotFoundError):
        service.register_execution_profile(**candidate)


def test_profile_allows_bounded_version_specific_execution_values(
    tmp_path: Path,
) -> None:
    service, _factory = service_with_revision(tmp_path)
    candidate = values()
    candidate.update(
        jsonrpc_port=8090,
        jsonrpc_path="/kodi-jsonrpc",
        readiness_timeout_seconds=120,
        poll_interval_seconds=2.0,
    )
    profile = service.register_execution_profile(**candidate)
    assert profile.jsonrpc_port == 8090
    assert profile.readiness_timeout_seconds == 120
