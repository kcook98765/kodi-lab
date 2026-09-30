"""Source observations never make historical validation or deployment current."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from kodi_lab.application.workspace_capture_presentation import (
    WorkspaceCapturePresentationService,
)
from kodi_lab.storage.models import (
    McpWorkspaceValidationRow,
    RepositoryWorkspaceDeploymentRow,
)
from tests.application.test_workspace_source_capture import (
    SOURCE_ID,
    WORKSPACE_ID,
    _fixture,
    _record,
    _v2,
)


def test_status_distinguishes_unscanned_source_validation_and_verified_generation(
    tmp_path: Path,
) -> None:
    _workspaces, capture, source, sessions = _fixture(tmp_path)
    original = _record(sessions)
    assert original.current_tree_digest is not None
    now = "2026-09-30T12:00:00.000000Z"
    with sessions.begin() as session:
        session.add(
            McpWorkspaceValidationRow(
                id=str(uuid4()),
                workspace_id=str(WORKSPACE_ID),
                development_source_id=str(SOURCE_ID),
                generation=0,
                tree_digest=original.current_tree_digest,
                classification="passed",
                addon_id="plugin.video.example",
                addon_version="1.0.0",
                extension_types_json='["xbmc.python.pluginsource"]',
                validated_at=now,
            )
        )
        session.add(
            RepositoryWorkspaceDeploymentRow(
                operation_id=str(uuid4()),
                publication_id=str(uuid4()),
                workspace_id=str(WORKSPACE_ID),
                workspace_generation=0,
                target_id=str(uuid4()),
                target_kodi_version="20.5",
                bridge_id=str(uuid4()),
                addon_id="plugin.video.example",
                addon_version="1.0.0",
                zip_sha256="a" * 64,
                package_tree_sha256="b" * 64,
                expected_manifest_digest=original.current_tree_digest,
                file_count=2,
                total_bytes=100,
                validation_receipt=str(uuid4()),
                repository_id="repository.kodi-lab-dev",
                base_url="http://192.168.50.10:18120",
                bootstrap_sha256="c" * 64,
                installed_before_version=None,
                state="succeeded",
                created_at=now,
                approved_at=now,
                published_at=now,
                observed_at=now,
                verified_at=now,
                failure_code=None,
            )
        )
    presentation = WorkspaceCapturePresentationService(sessions, capture)
    initial = presentation.status(WORKSPACE_ID)
    assert initial.source_state == "not_freshly_scanned"
    assert initial.validation_state == "current"
    assert initial.validation_digest == original.current_tree_digest
    assert initial.verified_state == "current"
    _v2(source)
    (source / "default.py").write_text("print('v2')\n", encoding="utf-8")
    observed = capture.scan_source(WORKSPACE_ID)
    scanned = presentation.status(WORKSPACE_ID, scan=observed)
    assert scanned.source_state == "differs_from_active_capture"
    assert scanned.validation_state == "current"
    assert scanned.verified_state == "older_than_scanned_source"
    changed = capture.capture(WORKSPACE_ID, expected_generation=0)
    assert changed.generation == 1
    later = presentation.status(WORKSPACE_ID)
    assert later.source_state == "not_freshly_scanned"
    assert later.validation_state == "stale"
    assert later.validation_generation == 0
    assert later.validation_digest == original.current_tree_digest
    assert later.deployment_generation == 0
    assert later.verified_generation == 0
    assert later.verified_state == "older_than_workspace"
    with sessions() as session:
        validation = session.query(McpWorkspaceValidationRow).one()
        deployment = session.query(RepositoryWorkspaceDeploymentRow).one()
        assert validation.generation == 0
        assert deployment.state == "succeeded"
        assert deployment.workspace_generation == 0
