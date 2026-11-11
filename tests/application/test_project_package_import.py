"""Reviewed source packages reuse private workspace capture, never host paths."""

import base64
import hashlib
import io
import stat
import zipfile
from pathlib import Path
from typing import Literal
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from kodi_lab.application.agent_projects import (
    ProjectCreateInput,
    ProjectImportInput,
    ProjectStatusInput,
    project_seed,
)
from kodi_lab.application.mcp_administration import (
    READ_ONLY_INSPECTION_PRESET_ID,
    McpAuthenticationService,
)
from kodi_lab.development.addon_zip import (
    AddonZipValidationError,
    read_editable_addon_zip,
)
from kodi_lab.mcp.authorization import McpAuthorizationError
from kodi_lab.mcp.tools import WorkspaceEditInput
from kodi_lab.storage.models import AgentProjectRow
from tests.application.test_mcp_workspace_mutations import _environment, _finish


def package(files: dict[str, bytes] | None = None, *, symlink: bool = False) -> bytes:
    contents = project_seed(
        ProjectCreateInput(
            request_id=uuid4(),
            addon_id="script.package.example",
            display_name="Package",
        )
    )
    contents["resources/skins/Default/720p/agent.xml"] = b"<window />\n"
    if files is not None:
        contents.update(files)
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for relative, content in contents.items():
            member = zipfile.ZipInfo("script.package.example/" + relative)
            member.create_system = 3
            member.external_attr = (
                (stat.S_IFLNK if symlink else stat.S_IFREG) | 0o644
            ) << 16
            member.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(member, content)
    return stream.getvalue()


def request(
    payload: bytes, kind: Literal["reviewed_checkout", "source_zip"] = "source_zip"
) -> ProjectImportInput:
    return ProjectImportInput(
        request_id=uuid4(),
        archive_base64=base64.b64encode(payload).decode(),
        archive_sha256=hashlib.sha256(payload).hexdigest(),
        intake_kind=kind,
        provenance_reference="agent-reviewed repository@0123456789abcdef",
    )


@pytest.mark.parametrize("kind", ["reviewed_checkout", "source_zip"])
def test_owned_import_exact_provenance_replay_and_private_edits(
    tmp_path: Path,
    kind: Literal["reviewed_checkout", "source_zip"],
) -> None:
    sessions, workspaces, admin, tools, _submitted = _environment(tmp_path)
    workspaces._development_root = None
    owner = admin.create_client(
        display_name="Package owner",
        preset_id=READ_ONLY_INSPECTION_PRESET_ID,
        trusted_managed_lab=True,
    )
    principal = McpAuthenticationService(sessions).authenticate(
        admin.issue_credential(owner.id).token
    )
    assert principal is not None
    operation = request(package(), kind)
    result = tools.project_import(principal, operation)
    _finish(workspaces, result.workspace.creation_operation_id)
    workspace_id = result.workspace.workspace_id
    assert (
        workspaces.read_file(
            workspace_id, "resources/skins/Default/720p/agent.xml"
        ).content
        == "<window />\n"
    )
    assert (
        workspaces.validate_structure(workspace_id, expected_generation=0).addon_id
        == "script.package.example"
    )
    status = tools.project_status(
        principal, ProjectStatusInput(project_id=result.project_id)
    )
    provenance = status.projects[0].intake_provenance
    assert provenance is not None
    assert provenance["package_sha256"] == operation.archive_sha256
    assert provenance["intake_kind"] == kind and provenance["file_count"] == 3
    tools.workspace_edit(
        principal,
        WorkspaceEditInput(
            workspace_id=workspace_id,
            expected_generation=0,
            operation="replace",
            relative_path="main.py",
            content="# owned change\n",
        ),
    )
    repeated = tools.project_import(principal, operation)
    assert repeated.project_id == result.project_id
    assert repeated.workspace.workspace_id == workspace_id
    assert workspaces.get(workspace_id).generation == 1
    assert workspaces.read_file(workspace_id, "main.py").content == "# owned change\n"
    with pytest.raises(McpAuthorizationError, match="request_id_conflict"):
        tools.project_import(
            principal,
            operation.model_copy(update={"provenance_reference": "different review"}),
        )
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(AgentProjectRow)) == 1
    admin.set_managed_lab_role(owner.id, False)
    with pytest.raises(McpAuthorizationError, match="permission_denied"):
        tools.project_import(principal, operation)


@pytest.mark.parametrize(
    "invalid",
    [
        "traversal",
        "symlink",
        "binary",
        "size",
        "entries",
        "corrupt",
        "service",
        "dependencies",
    ],
)
def test_source_package_structure_bounds_and_unsupported_formats(invalid: str) -> None:
    files = {}
    if invalid == "traversal":
        files["../escape.py"] = b"pass\n"
    elif invalid == "binary":
        files["native.so"] = b"binary"
    elif invalid == "size":
        files["huge.txt"] = b"a" * (1024 * 1024 + 1)
    elif invalid == "entries":
        files = {f"resources/{i}.txt": b"x" for i in range(257)}
    elif invalid in {"service", "dependencies"}:
        descriptor = project_seed(
            ProjectCreateInput(
                request_id=uuid4(),
                addon_id="script.package.example",
                display_name="Package",
            )
        )["addon.xml"]
        files["addon.xml"] = descriptor.replace(
            b"xbmc.python.script" if invalid == "service" else b'xbmc.python"',
            b"xbmc.service" if invalid == "service" else b'inputstream.adaptive"',
        )
    payload = package(files, symlink=invalid == "symlink")
    if invalid == "corrupt":
        payload = payload[:-4]
    with pytest.raises(AddonZipValidationError):
        read_editable_addon_zip(payload)


def test_unknown_host_paths_and_bad_encoding_are_not_tool_inputs() -> None:
    with pytest.raises(ValidationError):
        ProjectImportInput.model_validate(
            {**request(package()).model_dump(), "host_path": "/tmp/source"}
        )
    with pytest.raises(ValidationError):
        ProjectImportInput.model_validate(
            {**request(package()).model_dump(), "archive_base64": "秘密"}
        )


def test_digest_mismatch_has_no_project_side_effect(tmp_path: Path) -> None:
    sessions, _workspaces, admin, tools, _submitted = _environment(tmp_path)
    owner = admin.create_client(
        display_name="Digest owner",
        preset_id=READ_ONLY_INSPECTION_PRESET_ID,
        trusted_managed_lab=True,
    )
    principal = McpAuthenticationService(sessions).authenticate(
        admin.issue_credential(owner.id).token
    )
    assert principal is not None
    with pytest.raises(McpAuthorizationError, match="project_package_digest_mismatch"):
        tools.project_import(
            principal,
            request(package()).model_copy(update={"archive_sha256": "0" * 64}),
        )
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(AgentProjectRow)) == 0


def test_nested_seed_replay_after_publication_interruption_preserves_unknown_bytes(
    tmp_path: Path,
) -> None:
    from kodi_lab.application.agent_projects import AgentProjectError, _seed_source

    identity = uuid4()
    files = {"main.py": b"pass\n", "resources/new/agent.xml": b"<window />"}
    _seed_source(tmp_path, identity, files)
    # Reaching immutable seed publication before the database records it is safe
    # to replay exactly, including nested directory handles.
    _seed_source(tmp_path, identity, files)
    tree = tmp_path / "agent-projects/sources" / str(identity)
    (tree / "resources/new").chmod(0o700)
    (tree / "resources/new/unexpected.txt").write_text("retained evidence")
    (tree / "resources/new").chmod(0o500)
    with pytest.raises(AgentProjectError, match="requires_review"):
        _seed_source(tmp_path, identity, files)
    assert (tree / "resources/new/unexpected.txt").read_text() == "retained evidence"
