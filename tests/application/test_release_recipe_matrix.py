"""A fresh release advertises reviewed recipes without granting eligibility."""

from pathlib import Path

from kodi_lab.application.runtime_matrix import (
    MatrixAvailability,
    MatrixIneligibilityReason,
    RuntimeMatrixService,
)
from kodi_lab.storage import metadata
from kodi_lab.storage.engine import create_database_engine, create_session_factory


def test_fresh_matrix_has_three_supported_unacquired_generations(
    tmp_path: Path,
) -> None:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'matrix.db'}")
    metadata.create_all(engine)
    matrix = RuntimeMatrixService(create_session_factory(engine)).list_matrix()

    assert [entry.supported_generation for entry in matrix.runtimes] == [
        "matrix",
        "nexus",
        "omega",
    ]
    assert [entry.canonical_release for entry in matrix.runtimes] == [
        "19.4",
        "20.5",
        "21.3",
    ]
    assert all(
        not entry.selectable
        and not entry.validated_install_record
        and entry.availability is MatrixAvailability.UNAVAILABLE
        and entry.ineligibility_reason
        is MatrixIneligibilityReason.NO_REVISION_AVAILABLE
        and "not locally acquired" in entry.availability_detail
        for entry in matrix.runtimes
    )
    engine.dispose()
