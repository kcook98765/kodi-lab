"""Import limits remain visible and preserve atomic generation-bound publication."""

from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

from kodi_lab.application.workspace_imports import (
    MAX_IMPORT_FILE_BYTES,
    MAX_IMPORT_FILES,
    ImportApplyInput,
    ImportInput,
    WorkspaceImportService,
)
from kodi_lab.mcp.authorization import McpAuthorizationError
from kodi_lab.storage.models import WorkspaceImportOperationRow
from tests.application.test_workspace_imports import _environment
from tests.application.test_workspace_source_capture import WORKSPACE_ID


@pytest.mark.parametrize("size", [48 * 1024 + 1, 256 * 1024])
def test_import_accepts_larger_reviewed_utf8_files(tmp_path: Path, size: int) -> None:
    workspaces, tools, source, _sessions, _admin, principal, _client = _environment(
        tmp_path
    )
    # Valid Python and non-ASCII UTF-8 demonstrate that the limit counts bytes.
    content = b"# " + "\u00e9".encode() * ((size - 3) // 2)
    content += b" " * (size - len(content) - 1) + b"\n"
    assert len(content) == size
    (source / "default.py").write_bytes(content)
    preview = tools.workspace_import_preview(
        principal, ImportInput(workspace_id=WORKSPACE_ID, expected_generation=0)
    )
    assert preview.eligible
    assert preview.max_file_bytes == MAX_IMPORT_FILE_BYTES == 262_144
    assert preview.max_changed_files == MAX_IMPORT_FILES == 32
    assert preview.files[0].size == size
    result = tools.workspace_import(
        principal,
        ImportApplyInput(
            workspace_id=WORKSPACE_ID,
            expected_generation=0,
            request_id=uuid4(),
            source_digest=preview.source_digest,
            preview_digest=preview.preview_digest,
        ),
    )
    assert result.state == "succeeded" and result.generation == 1
    assert workspaces.read_file(WORKSPACE_ID, "default.py").content.encode() == content
    assert workspaces.validate_structure(WORKSPACE_ID, expected_generation=1)


def test_oversized_batch_reports_actual_size_and_publishes_nothing(
    tmp_path: Path,
) -> None:
    workspaces, tools, source, sessions, _admin, principal, _client = _environment(
        tmp_path
    )
    (source / "default.py").write_bytes(b"#" * (256 * 1024 + 1))
    (source / "helper.py").write_text("pass\n")
    preview = tools.workspace_import_preview(
        principal, ImportInput(workspace_id=WORKSPACE_ID, expected_generation=0)
    )
    assert not preview.eligible
    oversized = next(item for item in preview.files if item.path == "default.py")
    assert oversized.size == preview.max_file_bytes + 1 == 262_145
    assert oversized.rejection == "file_too_large"
    with pytest.raises(McpAuthorizationError, match="workspace_import_rejected"):
        tools.workspace_import(
            principal,
            ImportApplyInput(
                workspace_id=WORKSPACE_ID,
                expected_generation=0,
                request_id=uuid4(),
                source_digest=preview.source_digest,
                preview_digest=preview.preview_digest,
            ),
        )
    assert workspaces.get(WORKSPACE_ID).generation == 0
    assert workspaces.read_file(WORKSPACE_ID, "default.py").content == "print('v1')\n"
    assert not (
        tmp_path / "data/agent-workspaces" / str(WORKSPACE_ID) / "tree/helper.py"
    ).exists()
    with sessions() as session:
        assert session.scalars(select(WorkspaceImportOperationRow)).all() == []


@pytest.mark.parametrize("count", [32, 33])
def test_changed_entry_cap_is_unchanged(tmp_path: Path, count: int) -> None:
    _workspaces, tools, source, _sessions, _admin, principal, _client = _environment(
        tmp_path
    )
    for number in range(count):
        (source / f"new_{number}.py").write_text("pass\n")
    request = ImportInput(workspace_id=WORKSPACE_ID, expected_generation=0)
    if count == 32:
        preview = tools.workspace_import_preview(principal, request)
        assert preview.eligible and len(preview.files) == preview.max_changed_files
    else:
        with pytest.raises(McpAuthorizationError, match="workspace_import_rejected"):
            tools.workspace_import_preview(principal, request)


def test_imported_limit_file_passes_existing_validate_deploy_path(
    tmp_path: Path,
) -> None:
    from tests.application.test_mcp_workspace_development import (
        NOW,
        SOURCE_ID,
        TARGET_ID,
    )
    from tests.application.test_mcp_workspace_development import (
        _environment as deployment_environment,
    )

    env = deployment_environment(tmp_path)
    payload = b"#" * (256 * 1024 - 1) + b"\n"
    (tmp_path / "sources/plugin.video.slice4/main.py").write_bytes(payload)
    importer = WorkspaceImportService(env.workspaces)
    preview = importer.preview(
        ImportInput(workspace_id=env.workspace_id, expected_generation=0)
    )
    imported = importer.apply(
        env.client_id,
        ImportApplyInput(
            workspace_id=env.workspace_id,
            expected_generation=0,
            request_id=uuid4(),
            source_digest=preview.source_digest,
            preview_digest=preview.preview_digest,
        ),
    )
    validated = env.workspaces.validate_structure(
        env.workspace_id, expected_generation=imported.generation
    )
    env.development.record_validation(
        workspace_id=env.workspace_id,
        source_id=SOURCE_ID,
        generation=imported.generation,
        addon_id=validated.addon_id,
        addon_version=validated.addon_version,
        extension_types=validated.extension_types,
        validated_at=NOW,
    )
    deployed = env.development.deploy(
        client_id=env.client_id,
        request_id=uuid4(),
        workspace_id=env.workspace_id,
        expected_generation=imported.generation,
        target_id=TARGET_ID,
    )
    assert deployed.state == "succeeded" and deployed.generation == 1
    assert env.broker.sync_count == 1
