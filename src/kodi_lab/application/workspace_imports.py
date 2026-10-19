"""Review-bound registered-source import; no host-path or upload authority."""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal, cast
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, update
from sqlalchemy.exc import SQLAlchemyError

from kodi_lab.application.agent_workspaces import AgentWorkspaceService
from kodi_lab.application.workspace_source_captures import WorkspaceSourceCaptureService
from kodi_lab.development.deployment import (
    DevelopmentTreeEntry,
    DevelopmentTreeManifest,
    capture_development_tree,
    compare_captures,
)
from kodi_lab.development.sources import parse_captured_addon_xml
from kodi_lab.domain import AgentWorkspaceState
from kodi_lab.storage.models import AgentWorkspaceRow, WorkspaceImportOperationRow
from kodi_lab.storage.repositories import AgentWorkspaceRepository, _timestamp_to_text
from kodi_lab.workspace.batch_import import switch_import_tree
from kodi_lab.workspace.storage import (
    AgentWorkspaceReconciliationRequired,
    _storage_root,
    normalize_workspace_relative_path,
    workspace_coordination_lock,
)

MAX_IMPORT_FILES = 32
MAX_IMPORT_FILE_BYTES = 48 * 1024
MAX_IMPORT_RECEIPTS = 64
IMPORT_FREE_RESERVE = 2 * 1024**3
ImportState = Literal["applying", "succeeded", "failed", "reconciliation_required"]
IMPORT_ERROR_REASONS = frozenset(
    {
        "workspace_import_source_unavailable",
        "workspace_import_bytes_changed",
        "workspace_import_storage_capacity",
        "workspace_import_batch_too_large",
        "workspace_import_request_conflict",
        "workspace_import_preview_changed",
        "workspace_import_policy_rejected",
        "workspace_import_receipt_capacity",
        "workspace_import_generation_conflict",
        "workspace_import_reconciliation_required",
        "workspace_generation_conflict",
        "capture_reconciliation_required",
        "registered_source_unavailable",
        "workspace_not_clean_and_ready",
        "source_identity_changed",
        "source_invalid",
    }
)


class WorkspaceImportError(RuntimeError):
    """Only server-owned bounded error codes cross the MCP boundary."""


class ImportInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workspace_id: UUID
    expected_generation: int = Field(ge=0)


class ImportApplyInput(ImportInput):
    request_id: UUID
    source_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    preview_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class ImportFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(max_length=512)
    operation: Literal["added", "modified", "deleted", "type_changed"]
    size: int
    sha256: str | None
    mode: int = Field(default=0o644, ge=0, le=0o777)
    rejection: (
        Literal[
            "deletion_unsupported",
            "type_change_unsupported",
            "mode_change_unsupported",
            "private_edit_overlap",
            "parent_missing",
            "file_too_large",
            "not_utf8_text",
            "identity_change",
            "batch_too_large",
        ]
        | None
    ) = None


class ImportPreview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workspace_id: UUID
    source_id: UUID
    generation: int
    source_digest: str
    workspace_digest: str
    preview_digest: str
    eligible: bool
    files: tuple[ImportFile, ...]


class ImportResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workspace_id: UUID
    source_id: UUID
    operation_id: UUID
    request_id: UUID
    state: ImportState
    generation: int
    tree_digest: str | None
    source_digest: str
    files: tuple[ImportFile, ...]


class WorkspaceImportService:
    def __init__(self, workspaces: AgentWorkspaceService) -> None:
        self._workspaces = workspaces
        self._sessions = workspaces._session_factory
        self._data_root = workspaces._data_root

    @contextmanager
    def _plan(
        self, operation: ImportInput
    ) -> Iterator[tuple[ImportPreview, dict[str, bytes]]]:
        development_root = self._workspaces._development_root
        if development_root is None:
            raise WorkspaceImportError("workspace_import_source_unavailable")
        captures = WorkspaceSourceCaptureService(
            self._sessions,
            data_root=self._data_root,
            development_root=development_root,
            clock=self._workspaces._clock,
        )
        with self._sessions() as session:
            workspace, source = captures._preflight(
                session,
                operation.workspace_id,
                operation.expected_generation,
                allow_private_edits=True,
            )
        root = _storage_root(self._data_root)
        first = capture_development_tree(development_root, source.relative_locator)
        current = capture_development_tree(
            root, f"{workspace.id}/tree", reject_vcs=True
        )
        baseline = capture_development_tree(
            root, f"{workspace.id}/baseline/tree", reject_vcs=True
        )
        if (
            current.digest != workspace.current_tree_digest
            or baseline.digest != workspace.baseline_manifest_digest
        ):
            raise WorkspaceImportError("workspace_import_bytes_changed")
        required = first.included_bytes + current.included_bytes + IMPORT_FREE_RESERVE
        if shutil.disk_usage(root).free < required:
            raise WorkspaceImportError("workspace_import_storage_capacity")
        with tempfile.TemporaryDirectory(prefix=".import-scan-", dir=root) as temporary:
            staged = Path(temporary)
            captured = capture_development_tree(
                development_root,
                source.relative_locator,
                stage_root=staged,
            )
            compare_captures(first, captured)
            compare_captures(
                captured,
                capture_development_tree(development_root, source.relative_locator),
            )
            tracked = baseline.by_path()
            # Imported bytes have source provenance. A later batch can update
            # them while the workspace still matches that exact postimage;
            # intervening private edits remain conflicts. The promotion baseline
            # itself is never changed.
            with self._sessions() as session:
                history = session.scalars(
                    select(WorkspaceImportOperationRow)
                    .where(
                        WorkspaceImportOperationRow.workspace_id == str(workspace.id),
                        WorkspaceImportOperationRow.state == "succeeded",
                    )
                    .order_by(WorkspaceImportOperationRow.generation)
                ).all()
                for prior in history:
                    for item in json.loads(prior.files_json):
                        tracked[item["path"]] = DevelopmentTreeEntry(
                            path=item["path"],
                            entry_type="file",
                            mode=item["mode"],
                            size=item["size"],
                            sha256=item["sha256"],
                        )
            files, payloads = self._changes(
                tracked, current, captured, staged, source.addon_id
            )
            material = {
                "workspace_id": str(workspace.id),
                "source_id": str(source.id),
                "generation": workspace.generation,
                "source_digest": captured.digest,
                "workspace_digest": current.digest,
                "files": [item.model_dump() for item in files],
            }
            digest = hashlib.sha256(
                json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            yield (
                ImportPreview(
                    workspace_id=workspace.id,
                    source_id=source.id,
                    generation=workspace.generation,
                    source_digest=captured.digest,
                    workspace_digest=current.digest,
                    preview_digest=digest,
                    eligible=bool(files) and not any(f.rejection for f in files),
                    files=files,
                ),
                payloads,
            )

    @staticmethod
    def _changes(
        tracked: dict[str, DevelopmentTreeEntry],
        current: DevelopmentTreeManifest,
        source: DevelopmentTreeManifest,
        staged: Path,
        addon_id: str,
    ) -> tuple[tuple[ImportFile, ...], dict[str, bytes]]:
        original, private, incoming = (
            tracked,
            current.by_path(),
            source.by_path(),
        )
        changed = [
            p
            for p in sorted(original.keys() | incoming.keys())
            if original.get(p) != incoming.get(p)
        ]
        if len(changed) > MAX_IMPORT_FILES:
            raise WorkspaceImportError("workspace_import_batch_too_large")
        files: list[ImportFile] = []
        payloads: dict[str, bytes] = {}
        for path in changed:
            normalize_workspace_relative_path(path, allow_root=False)
            before, after, local = (
                original.get(path),
                incoming.get(path),
                private.get(path),
            )
            # Unchanged directory entries need no write. New directories would
            # expand the existing text-edit rule that the parent must exist.
            if after is not None and after.entry_type == "directory" and before is None:
                continue
            item = ImportFile(
                path=path,
                operation="added" if before is None else "modified",
                size=0 if after is None else after.size,
                sha256=None if after is None else after.sha256,
                mode=0o644 if after is None else after.mode,
            )
            if after is None:
                item.operation, item.rejection = "deleted", "deletion_unsupported"
            elif after.entry_type != "file" or (
                before is not None and before.entry_type != "file"
            ):
                item.operation, item.rejection = (
                    "type_changed",
                    "type_change_unsupported",
                )
            elif local != before:
                item.rejection = "private_edit_overlap"
            elif (before is not None and after.mode != before.mode) or (
                before is None and after.mode != 0o644
            ):
                item.rejection = "mode_change_unsupported"
            elif "/" in path and private.get(path.rsplit("/", 1)[0]) is None:
                item.rejection = "parent_missing"
            elif after.size > MAX_IMPORT_FILE_BYTES:
                item.rejection = "file_too_large"
            else:
                payload = (staged / path).read_bytes()
                try:
                    text = payload.decode("utf-8", "strict")
                    if "\x00" in text:
                        raise UnicodeError("binary text")
                    if (
                        path == "addon.xml"
                        and parse_captured_addon_xml("import", payload).addon_id
                        != addon_id
                    ):
                        item.rejection = "identity_change"
                    payloads[path] = payload
                except UnicodeError:
                    item.rejection = "not_utf8_text"
            files.append(item)
        return tuple(files), payloads

    def preview(
        self,
        operation: ImportInput,
        *,
        authorize: Callable[[], object] = lambda: None,
    ) -> ImportPreview:
        with (
            workspace_coordination_lock(
                self._data_root, operation.workspace_id, exclusive=False
            ),
            self._plan(operation) as (preview, _payloads),
        ):
            authorize()
            return preview

    @staticmethod
    def _result(row: WorkspaceImportOperationRow, source_id: UUID) -> ImportResult:
        return ImportResult(
            workspace_id=UUID(row.workspace_id),
            source_id=source_id,
            operation_id=UUID(row.id),
            request_id=UUID(row.request_id),
            state=cast(ImportState, row.state),
            generation=row.generation + (row.state == "succeeded"),
            tree_digest=row.result_digest if row.state == "succeeded" else None,
            source_digest=row.source_digest,
            files=tuple(
                ImportFile.model_validate(item) for item in json.loads(row.files_json)
            ),
        )

    def _publish(self, row: WorkspaceImportOperationRow, digest: str) -> None:
        try:
            with self._sessions.begin() as session:
                changed = session.execute(
                    update(AgentWorkspaceRow)
                    .where(
                        AgentWorkspaceRow.id == row.workspace_id,
                        AgentWorkspaceRow.state
                        == AgentWorkspaceState.RECONCILIATION_REQUIRED,
                        AgentWorkspaceRow.generation == row.generation,
                        AgentWorkspaceRow.current_tree_digest == row.prior_digest,
                    )
                    .values(
                        state=AgentWorkspaceState.READY,
                        generation=row.generation + 1,
                        current_tree_digest=digest,
                        updated_at=_timestamp_to_text(self._workspaces._clock()),
                    )
                )
                if getattr(changed, "rowcount", 0) != 1:
                    raise AgentWorkspaceReconciliationRequired(
                        "workspace_import_reconciliation_required",
                        "Import publication state changed.",
                    )
                session.execute(
                    update(WorkspaceImportOperationRow)
                    .where(
                        WorkspaceImportOperationRow.id == row.id,
                    )
                    .values(state="succeeded", result_digest=digest)
                )
        except SQLAlchemyError as error:
            try:
                with self._sessions() as session:
                    durable = session.get(WorkspaceImportOperationRow, row.id)
                    workspace = session.get(AgentWorkspaceRow, row.workspace_id)
                    if (
                        durable is not None
                        and workspace is not None
                        and (
                            durable.state == "succeeded"
                            and durable.result_digest == digest
                            and workspace.generation == row.generation + 1
                            and workspace.current_tree_digest == digest
                        )
                    ):
                        return
            except SQLAlchemyError:
                pass
            raise AgentWorkspaceReconciliationRequired(
                "workspace_import_reconciliation_required",
                "Import publication needs reconciliation.",
            ) from error

    def _recover(self, row: WorkspaceImportOperationRow) -> None:
        root = _storage_root(self._data_root)
        current = capture_development_tree(
            root, f"{row.workspace_id}/tree", reject_vcs=True
        )
        stage = root / row.workspace_id / f".import-{row.id}"
        if current.digest == row.result_digest and stage.is_dir():
            old = capture_development_tree(
                root, f"{row.workspace_id}/.import-{row.id}", reject_vcs=True
            )
            if old.digest == row.prior_digest:
                self._publish(row, current.digest)
                row.state = "succeeded"
                return
        if current.digest == row.prior_digest:
            with self._sessions.begin() as session:
                changed = session.execute(
                    update(AgentWorkspaceRow)
                    .where(
                        AgentWorkspaceRow.id == row.workspace_id,
                        AgentWorkspaceRow.state
                        == AgentWorkspaceState.RECONCILIATION_REQUIRED,
                        AgentWorkspaceRow.generation == row.generation,
                        AgentWorkspaceRow.current_tree_digest == row.prior_digest,
                    )
                    .values(state=AgentWorkspaceState.READY)
                )
                if getattr(changed, "rowcount", 0) != 1:
                    raise WorkspaceImportError(
                        "workspace_import_reconciliation_required"
                    )
                session.execute(
                    update(WorkspaceImportOperationRow)
                    .where(
                        WorkspaceImportOperationRow.id == row.id,
                    )
                    .values(state="failed")
                )
            row.state = "failed"
            return
        with self._sessions.begin() as session:
            session.execute(
                update(WorkspaceImportOperationRow)
                .where(
                    WorkspaceImportOperationRow.id == row.id,
                )
                .values(state="reconciliation_required")
            )
        row.state = "reconciliation_required"

    def apply(
        self,
        client_id: UUID,
        operation: ImportApplyInput,
        *,
        authorize: Callable[[], object] = lambda: None,
    ) -> ImportResult:
        workspace = self._workspaces.get(operation.workspace_id)
        with workspace_coordination_lock(
            self._data_root, operation.workspace_id, exclusive=True
        ):
            authorize()
            with self._sessions() as session:
                prior = session.scalar(
                    select(WorkspaceImportOperationRow).where(
                        WorkspaceImportOperationRow.client_id == str(client_id),
                        WorkspaceImportOperationRow.request_id
                        == str(operation.request_id),
                    )
                )
                if prior is not None:
                    if (
                        prior.workspace_id,
                        prior.generation,
                        prior.source_digest,
                        prior.preview_digest,
                    ) != (
                        str(operation.workspace_id),
                        operation.expected_generation,
                        operation.source_digest,
                        operation.preview_digest,
                    ):
                        raise WorkspaceImportError("workspace_import_request_conflict")
                    if prior.state in {"applying", "reconciliation_required"}:
                        # The exclusive lock proves that no writer still owns it.
                        self._recover(prior)
                    return self._result(prior, workspace.development_source_id)
            with self._plan(operation) as (preview, files):
                if (
                    preview.source_digest != operation.source_digest
                    or preview.preview_digest != operation.preview_digest
                ):
                    raise WorkspaceImportError("workspace_import_preview_changed")
                if not preview.eligible:
                    raise WorkspaceImportError("workspace_import_policy_rejected")
                with self._sessions.begin() as session:
                    count = (
                        session.scalar(
                            select(func.count())
                            .select_from(WorkspaceImportOperationRow)
                            .where(
                                WorkspaceImportOperationRow.workspace_id
                                == str(operation.workspace_id),
                            )
                        )
                        or 0
                    )
                    if count >= MAX_IMPORT_RECEIPTS:
                        raise WorkspaceImportError("workspace_import_receipt_capacity")
                    row = WorkspaceImportOperationRow(
                        id=str(uuid4()),
                        client_id=str(client_id),
                        request_id=str(operation.request_id),
                        workspace_id=str(operation.workspace_id),
                        generation=preview.generation,
                        source_digest=preview.source_digest,
                        preview_digest=preview.preview_digest,
                        prior_digest=preview.workspace_digest,
                        result_digest=None,
                        files_json=json.dumps(
                            [item.model_dump() for item in preview.files],
                            separators=(",", ":"),
                        ),
                        state="applying",
                        created_at=_timestamp_to_text(self._workspaces._clock()),
                    )
                    session.add(row)
                    if not AgentWorkspaceRepository(session).require_reconciliation(
                        operation.workspace_id,
                        expected_generation=preview.generation,
                        now=self._workspaces._clock(),
                    ):
                        raise WorkspaceImportError(
                            "workspace_import_generation_conflict"
                        )

                def prepared(digest: str) -> None:
                    with self._sessions.begin() as session:
                        session.execute(
                            update(WorkspaceImportOperationRow)
                            .where(
                                WorkspaceImportOperationRow.id == row.id,
                            )
                            .values(result_digest=digest)
                        )
                    row.result_digest = digest

                try:
                    with switch_import_tree(
                        self._data_root,
                        operation.workspace_id,
                        UUID(row.id),
                        files,
                        expected_digest=preview.workspace_digest,
                        prepared=prepared,
                    ) as digest:
                        authorize()
                        self._publish(row, digest)
                    row.state, row.result_digest = "succeeded", digest
                except BaseException as error:
                    reconciliation = isinstance(
                        error, AgentWorkspaceReconciliationRequired
                    )
                    state = "reconciliation_required" if reconciliation else "failed"
                    with self._sessions.begin() as session:
                        session.execute(
                            update(WorkspaceImportOperationRow)
                            .where(
                                WorkspaceImportOperationRow.id == row.id,
                            )
                            .values(state=state)
                        )
                        if not reconciliation:
                            session.execute(
                                update(AgentWorkspaceRow)
                                .where(
                                    AgentWorkspaceRow.id == row.workspace_id,
                                    AgentWorkspaceRow.generation == row.generation,
                                    AgentWorkspaceRow.current_tree_digest
                                    == row.prior_digest,
                                    AgentWorkspaceRow.state
                                    == AgentWorkspaceState.RECONCILIATION_REQUIRED,
                                )
                                .values(state=AgentWorkspaceState.READY)
                            )
                    raise
                return self._result(row, preview.source_id)
