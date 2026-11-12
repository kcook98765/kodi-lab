"""Real bounded ZIP transfer, durable replay, ownership and interrupted storage."""

import base64
import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from kodi_lab.application.agent_projects import ProjectImportInput
from kodi_lab.application.mcp_administration import (
    READ_ONLY_INSPECTION_PRESET_ID,
    McpAuthenticationService,
)
from kodi_lab.application.project_transfers import (
    SOURCE_CHUNK_BYTES,
    ProjectTransferError,
    ProjectTransferInput,
    ProjectTransferStatus,
    read_source_transfer,
    transfer_source,
)
from kodi_lab.development.addon_zip import (
    AddonZipValidationError,
    read_editable_addon_zip,
)
from kodi_lab.mcp.authorization import McpAuthorizationError
from tests.application.test_mcp_workspace_mutations import _environment, _finish
from tests.application.test_project_package_import import package


def operation(payload: bytes, **kwargs: object) -> ProjectTransferInput:
    return ProjectTransferInput.model_validate(
        {
            "request_id": str(uuid4()),
            "archive_sha256": hashlib.sha256(payload).hexdigest(),
            "archive_size": len(payload),
            "chunk_base64": base64.b64encode(payload[:SOURCE_CHUNK_BYTES]).decode(),
            **kwargs,
        }
    )


def transfer(
    root: Path, owner: UUID, value: ProjectTransferInput, **kwargs: object
) -> ProjectTransferStatus:
    return transfer_source(root, owner, value, authorize=lambda: None, **kwargs)  # type: ignore[arg-type]


def test_realistic_owned_source_transfer_import_and_replay_after_cancellation(
    tmp_path: Path,
) -> None:
    sessions, workspaces, admin, tools, _submitted = _environment(tmp_path)
    owner = admin.create_client(
        display_name="Source owner",
        preset_id=READ_ONLY_INSPECTION_PRESET_ID,
        trusted_managed_lab=True,
    )
    peer = admin.create_client(
        display_name="Other owner",
        preset_id=READ_ONLY_INSPECTION_PRESET_ID,
        trusted_managed_lab=True,
    )
    auth = McpAuthenticationService(sessions)
    principal = auth.authenticate(admin.issue_credential(owner.id).token)
    other = auth.authenticate(admin.issue_credential(peer.id).token)
    assert principal is not None and other is not None
    catalog = b"\n".join(
        hashlib.sha256(str(i).encode()).hexdigest().encode() for i in range(5000)
    )
    payload = package({"resources/data/catalog.txt": catalog})
    assert len(payload) > 45 * 1024
    with pytest.raises(AddonZipValidationError, match="transport bound"):
        read_editable_addon_zip(payload)
    first = operation(payload)
    result = tools.project_transfer(principal, first)
    assert result.next_offset == SOURCE_CHUNK_BYTES
    assert tools.project_transfer(principal, first) == result
    with pytest.raises(McpAuthorizationError, match="not_found"):
        tools.project_transfer(other, first.model_copy(update={"action": "status"}))
    with pytest.raises(McpAuthorizationError, match="offset_conflict"):
        tools.project_transfer(
            principal, first.model_copy(update={"offset": SOURCE_CHUNK_BYTES * 2})
        )
    with pytest.raises(McpAuthorizationError, match="request_id_conflict"):
        tools.project_transfer(
            principal,
            first.model_copy(
                update={
                    "chunk_base64": base64.b64encode(b"x" * SOURCE_CHUNK_BYTES).decode()
                }
            ),
        )
    for offset in range(result.next_offset, len(payload), SOURCE_CHUNK_BYTES):
        chunk = first.model_copy(
            update={
                "offset": offset,
                "chunk_base64": base64.b64encode(
                    payload[offset : offset + SOURCE_CHUNK_BYTES]
                ).decode(),
            }
        )
        result = tools.project_transfer(principal, chunk)
    assert result.state == "ready" and result.next_offset == len(payload)
    imported = ProjectImportInput(
        request_id=uuid4(),
        transfer_id=first.request_id,
        archive_sha256=first.archive_sha256,
        intake_kind="reviewed_checkout",
        provenance_reference="reviewed fixed catalog@sha256",
    )
    project = tools.project_import(principal, imported)
    _finish(workspaces, project.workspace.creation_operation_id)
    assert (
        workspaces.read_file(
            project.workspace.workspace_id, "resources/data/catalog.txt"
        ).content.encode()
        == catalog
    )
    tools.project_transfer(principal, first.model_copy(update={"action": "cancel"}))
    assert tools.project_import(principal, imported).project_id == project.project_id
    assert (
        tools.project_import(principal, imported).workspace.workspace_id
        == project.workspace.workspace_id
    )
    admin.set_managed_lab_role(owner.id, False)
    with pytest.raises(McpAuthorizationError, match="permission_denied"):
        tools.project_transfer(principal, first.model_copy(update={"action": "status"}))
    with pytest.raises(McpAuthorizationError, match="permission_denied"):
        tools.project_import(principal, imported)


@pytest.mark.parametrize(
    "defect", ["symlink", "hardlink", "public", "temporary", "unknown", "truncated"]
)
def test_changed_or_interrupted_chunks_are_retained_for_review(
    tmp_path: Path, defect: str
) -> None:
    owner = uuid4()
    value = operation(b"a" * 40000)
    transfer(tmp_path, owner, value)
    directory = tmp_path / "agent-projects/transfers" / f"{owner}-{value.request_id}"
    chunk = directory / "00000000.chunk"
    if defect == "symlink":
        chunk.unlink()
        chunk.symlink_to(tmp_path / "outside")
    elif defect == "hardlink":
        os.link(chunk, tmp_path / "alias")
    elif defect == "public":
        chunk.chmod(0o644)
    elif defect == "temporary":
        (directory / ".00032768.chunk.tmp").write_text("retained")
    elif defect == "unknown":
        (directory / "unknown").write_text("retained")
    else:
        chunk.write_bytes(b"a")
    with pytest.raises(ProjectTransferError, match="requires_review"):
        transfer(tmp_path, owner, value.model_copy(update={"action": "status"}))
    assert chunk.is_symlink() or chunk.exists()


def test_expiry_cancel_digest_and_durable_quota(tmp_path: Path) -> None:
    owner = uuid4()
    value = operation(b"a" * 40000)
    now = datetime.now(UTC)
    transfer(tmp_path, owner, value, now=now)
    expired = transfer(
        tmp_path,
        owner,
        value.model_copy(update={"action": "status"}),
        now=now + timedelta(days=2),
    )
    assert expired.state == "expired" and expired.next_offset == 0
    assert not list((tmp_path / "agent-projects/transfers").rglob("*.chunk"))
    assert transfer(tmp_path, owner, value).state == "expired"
    for _ in range(3):
        transfer(tmp_path, owner, operation(b"one"))
    with pytest.raises(ProjectTransferError, match="transfer_limit"):
        transfer(tmp_path, owner, operation(b"new"))
    peer = uuid4()
    wrong = operation(b"one", archive_sha256="0" * 64)
    with pytest.raises(ProjectTransferError, match="digest_mismatch"):
        transfer(tmp_path, peer, wrong)
    assert (
        transfer(tmp_path, peer, wrong.model_copy(update={"action": "status"})).state
        == "failed"
    )
    assert (
        transfer(tmp_path, peer, wrong.model_copy(update={"action": "cancel"})).state
        == "cancelled"
    )


def test_incomplete_transfer_and_changed_receipt_are_never_imported(
    tmp_path: Path,
) -> None:
    owner = uuid4()
    value = operation(b"a" * 40000)
    transfer(tmp_path, owner, value)
    with pytest.raises(ProjectTransferError, match="not_ready"):
        read_source_transfer(
            tmp_path,
            owner,
            value.request_id,
            value.archive_sha256,
            authorize=lambda: None,
        )
    with pytest.raises(ProjectTransferError, match="request_id_conflict"):
        transfer(tmp_path, owner, value.model_copy(update={"archive_size": 40001}))
    with pytest.raises(ValidationError):
        ProjectTransferInput.model_validate(
            {**value.model_dump(), "host_path": "/tmp/source"}
        )
    with pytest.raises(ValidationError):
        ProjectTransferInput.model_validate(
            {**value.model_dump(), "archive_size": 4 * 1024 * 1024 + 1}
        )
    directory = tmp_path / "agent-projects/transfers" / f"{owner}-{value.request_id}"
    receipt = json.loads((directory / "receipt.json").read_text())
    assert receipt["state"] == "receiving"


@pytest.mark.parametrize(
    "case", ["file", "expanded", "entries", "ratio", "binary", "symlink"]
)
def test_staged_zip_keeps_structure_and_resource_bounds(case: str) -> None:
    if case == "file":
        payload = package({"large.txt": os.urandom(2 * 1024 * 1024 + 1).hex().encode()})
    elif case == "expanded":
        payload = package({f"{i}.txt": b"a" * 1800000 for i in range(10)})
    elif case == "entries":
        payload = package({f"{i}.txt": b"x" for i in range(1025)})
    elif case == "ratio":
        payload = package({"bomb.txt": b"a" * 1500000})
    elif case == "binary":
        payload = package({"native.so": b"binary"})
    else:
        payload = package(symlink=True)
    with pytest.raises(AddonZipValidationError):
        read_editable_addon_zip(payload, staged=True)
