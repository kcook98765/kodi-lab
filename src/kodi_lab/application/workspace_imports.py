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

from pydantic import BaseModel, ConfigDict, Field, model_validator
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
    AgentWorkspaceStorageError,
    _storage_root,
    normalize_workspace_relative_path,
    workspace_coordination_lock,
)

MAX_IMPORT_FILES = 32
MAX_IMPORT_FILE_BYTES = 256 * 1024
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


class ImportResolution(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=512)
    choice: Literal["keep_private", "take_source"]


class ImportInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workspace_id: UUID
    expected_generation: int = Field(ge=0)
    mode: Literal["changes", "reviewed_tree"] = "changes"
    resolutions: tuple[ImportResolution, ...] = Field(default=(), max_length=32)

    @model_validator(mode="after")
    def validate_resolutions(self) -> ImportInput:
        paths = [item.path for item in self.resolutions]
        if len(set(paths)) != len(paths) or (paths and self.mode != "reviewed_tree"):
            raise ValueError("Unique resolutions require reviewed_tree mode.")
        for path in paths:
            try:
                parts = normalize_workspace_relative_path(path, allow_root=False)
                if any(part in {".git", ".hg", ".svn"} for part in parts):
                    raise ValueError("Resolution path is invalid.")
            except AgentWorkspaceStorageError as error:
                raise ValueError("Resolution path is invalid.") from error
        return self


class ImportApplyInput(ImportInput):
    request_id: UUID
    source_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    preview_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class ImportFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(max_length=512)
    operation: Literal[
        "added", "modified", "deleted", "type_changed", "retained_private"
    ]
    size: int
    sha256: str | None
    mode: int = Field(default=0o644, ge=0, le=0o777)
    preimage_sha256: str | None = None
    preimage_size: int | None = None
    preimage_mode: int | None = None
    resolution: Literal["keep_private", "take_source"] | None = None
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
    mode: Literal["changes", "reviewed_tree"] = "changes"
    matches_source: bool = False
    max_file_bytes: int = Field(ge=1)
    max_changed_files: int = Field(ge=1)
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
    mode: Literal["changes", "reviewed_tree"] = "changes"
    matches_source: bool = False


def _receipt(
    row: WorkspaceImportOperationRow,
) -> tuple[ImportInput, tuple[ImportFile, ...]]:
    """Read original list receipts and versioned reviewed-tree lineage."""
    document = json.loads(row.files_json)
    if isinstance(document, list):
        options = ImportInput(
            workspace_id=UUID(row.workspace_id), expected_generation=row.generation
        )
        files = document
    elif isinstance(document, dict) and document.get("version") == 2:
        options = ImportInput.model_validate(document["input"])
        files = document["files"]
    else:
        raise WorkspaceImportError("workspace_import_reconciliation_required")
    return options, tuple(ImportFile.model_validate(item) for item in files)


class WorkspaceImportService:
    def __init__(self, workspaces: AgentWorkspaceService) -> None:
        self._workspaces = workspaces
        self._sessions = workspaces._session_factory
        self._data_root = workspaces._data_root

    @contextmanager
    def _plan(
        self, operation: ImportInput
    ) -> Iterator[tuple[ImportPreview, dict[str, bytes | None]]]:
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
                    _options, prior_files = _receipt(prior)
                    for item in prior_files:
                        if item.operation == "retained_private":
                            continue
                        if item.operation == "deleted":
                            tracked.pop(item.path, None)
                            continue
                        tracked[item.path] = DevelopmentTreeEntry(
                            path=item.path,
                            entry_type="file",
                            mode=item.mode,
                            size=item.size,
                            sha256=item.sha256,
                        )
            files, payloads = self._changes(
                tracked, current, captured, staged, source.addon_id, operation
            )
            material = {
                "workspace_id": str(workspace.id),
                "source_id": str(source.id),
                "generation": workspace.generation,
                "source_digest": captured.digest,
                "workspace_digest": current.digest,
                "files": [item.model_dump() for item in files],
                "mode": operation.mode,
                "resolutions": [
                    item.model_dump()
                    for item in sorted(
                        operation.resolutions, key=lambda item: item.path
                    )
                ],
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
                    mode=operation.mode,
                    matches_source=(
                        operation.mode == "reviewed_tree"
                        and not any(
                            f.rejection or f.operation == "retained_private"
                            for f in files
                        )
                    ),
                    max_file_bytes=MAX_IMPORT_FILE_BYTES,
                    max_changed_files=MAX_IMPORT_FILES,
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
        operation: ImportInput,
    ) -> tuple[tuple[ImportFile, ...], dict[str, bytes | None]]:
        original, private, incoming = (
            tracked,
            current.by_path(),
            source.by_path(),
        )
        changed = [
            p
            for p in sorted(
                original.keys()
                | incoming.keys()
                | (private.keys() if operation.mode == "reviewed_tree" else set())
            )
            if original.get(p) != incoming.get(p)
            or (operation.mode == "reviewed_tree" and private.get(p) != incoming.get(p))
        ]
        if len(changed) > MAX_IMPORT_FILES:
            raise WorkspaceImportError("workspace_import_batch_too_large")
        files: list[ImportFile] = []
        payloads: dict[str, bytes | None] = {}
        resolutions = {item.path: item.choice for item in operation.resolutions}
        conflicts = {
            path
            for path in changed
            if private.get(path) != original.get(path)
            and private.get(path) != incoming.get(path)
        }
        if resolutions.keys() - conflicts:
            raise WorkspaceImportError("workspace_import_policy_rejected")
        for path in changed:
            normalize_workspace_relative_path(path, allow_root=False)
            before, after, local = (
                original.get(path),
                incoming.get(path),
                private.get(path),
            )
            # Unchanged directory entries need no write. New directories would
            # expand the existing text-edit rule that the parent must exist.
            if (
                operation.mode == "changes"
                and after is not None
                and after.entry_type == "directory"
                and before is None
            ):
                continue
            item = ImportFile(
                path=path,
                operation="added" if before is None else "modified",
                size=0 if after is None else after.size,
                sha256=None if after is None else after.sha256,
                mode=0o644 if after is None else after.mode,
                preimage_sha256=None if local is None else local.sha256,
                preimage_size=None if local is None else local.size,
                preimage_mode=None if local is None else local.mode,
                resolution=resolutions.get(path),
            )
            if after is None and operation.mode == "changes":
                item.operation, item.rejection = "deleted", "deletion_unsupported"
            elif any(
                entry is not None and entry.entry_type != "file"
                for entry in (before, after, local)
            ):
                item.operation, item.rejection = (
                    "type_changed",
                    "type_change_unsupported",
                )
            elif (
                operation.mode == "reviewed_tree"
                and path in conflicts
                and item.resolution is None
            ) or (operation.mode == "changes" and local != before):
                item.rejection = "private_edit_overlap"
            elif item.resolution == "keep_private":
                item.operation = "retained_private"
                item.size = 0 if local is None else local.size
                item.sha256 = None if local is None else local.sha256
                item.mode = 0o644 if local is None else local.mode
            elif after is None:
                item.operation = "deleted"
                if path == "addon.xml":
                    item.rejection = "identity_change"
                elif local is not None:
                    payloads[path] = None
            elif (local is not None and after.mode != local.mode) or (
                local is None and after.mode != 0o644
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
        options, files = _receipt(row)
        return ImportResult(
            workspace_id=UUID(row.workspace_id),
            source_id=source_id,
            operation_id=UUID(row.id),
            request_id=UUID(row.request_id),
            state=cast(ImportState, row.state),
            generation=row.generation + (row.state == "succeeded"),
            tree_digest=row.result_digest if row.state == "succeeded" else None,
            source_digest=row.source_digest,
            files=files,
            mode=options.mode,
            matches_source=(
                row.state == "succeeded"
                and options.mode == "reviewed_tree"
                and not any(item.operation == "retained_private" for item in files)
            ),
        )

    def _publication_state(
        self, row: WorkspaceImportOperationRow, digest: str
    ) -> Literal["committed", "unpublished", "unknown"]:
        """A cancellation can arrive after SQLite committed but before return."""
        try:
            with self._sessions() as session:
                durable = session.get(WorkspaceImportOperationRow, row.id)
                workspace = session.get(AgentWorkspaceRow, row.workspace_id)
                if (
                    durable is None
                    or workspace is None
                    or (
                        durable.workspace_id,
                        durable.generation,
                        durable.preview_digest,
                        durable.source_digest,
                        durable.prior_digest,
                        durable.result_digest,
                    )
                    != (
                        row.workspace_id,
                        row.generation,
                        row.preview_digest,
                        row.source_digest,
                        row.prior_digest,
                        digest,
                    )
                ):
                    return "unknown"
                if (
                    durable.state == "succeeded"
                    and workspace.generation == row.generation + 1
                    and workspace.current_tree_digest == digest
                    and workspace.state == AgentWorkspaceState.READY
                ):
                    return "committed"
                if (
                    durable.state in {"applying", "reconciliation_required"}
                    and workspace.generation == row.generation
                    and workspace.current_tree_digest == row.prior_digest
                    and workspace.state == AgentWorkspaceState.RECONCILIATION_REQUIRED
                ):
                    return "unpublished"
        except BaseException:
            # Even another cancellation during readback cannot authorize rollback
            # of bytes whose database publication may already have committed.
            return "unknown"
        return "unknown"

    def _publish(
        self,
        row: WorkspaceImportOperationRow,
        digest: str,
        *,
        already_advanced: bool = False,
    ) -> None:
        try:
            with self._sessions.begin() as session:
                changed = session.execute(
                    update(AgentWorkspaceRow)
                    .where(
                        AgentWorkspaceRow.id == row.workspace_id,
                        AgentWorkspaceRow.state
                        == AgentWorkspaceState.RECONCILIATION_REQUIRED,
                        AgentWorkspaceRow.generation
                        == row.generation + already_advanced,
                        AgentWorkspaceRow.current_tree_digest
                        == (digest if already_advanced else row.prior_digest),
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
                with self._sessions() as session:
                    workspace = session.get(AgentWorkspaceRow, row.workspace_id)
                    already_advanced = bool(
                        workspace is not None
                        and workspace.generation == row.generation + 1
                        and workspace.current_tree_digest == current.digest
                    )
                self._publish(row, current.digest, already_advanced=already_advanced)
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
                    prior_options, _prior_files = _receipt(prior)
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
                    ) or prior_options != ImportInput.model_validate(
                        operation.model_dump(
                            include={
                                "workspace_id",
                                "expected_generation",
                                "mode",
                                "resolutions",
                            }
                        )
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
                lineage = json.dumps(
                    {
                        "version": 2,
                        "input": operation.model_dump(
                            mode="json",
                            include={
                                "workspace_id",
                                "expected_generation",
                                "mode",
                                "resolutions",
                            },
                        ),
                        "files": [item.model_dump() for item in preview.files],
                    },
                    separators=(",", ":"),
                )
                if len(lineage) > 49152:
                    raise WorkspaceImportError("workspace_import_batch_too_large")
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
                        files_json=lineage,
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
                        exact_source_digest=preview.source_digest
                        if preview.matches_source
                        else None,
                        prepared=prepared,
                    ) as digest:
                        authorize()
                        try:
                            self._publish(row, digest)
                        except BaseException as error:
                            publication = self._publication_state(row, digest)
                            if publication != "committed":
                                if publication == "unknown" or isinstance(
                                    error, AgentWorkspaceReconciliationRequired
                                ):
                                    raise AgentWorkspaceReconciliationRequired(
                                        "workspace_import_reconciliation_required",
                                        "Import publication requires reconciliation.",
                                    ) from error
                                raise
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
                        if reconciliation:
                            session.execute(
                                update(AgentWorkspaceRow)
                                .where(
                                    AgentWorkspaceRow.id == row.workspace_id,
                                    (
                                        (AgentWorkspaceRow.generation == row.generation)
                                        & (
                                            AgentWorkspaceRow.current_tree_digest
                                            == row.prior_digest
                                        )
                                    )
                                    | (
                                        (
                                            AgentWorkspaceRow.generation
                                            == row.generation + 1
                                        )
                                        & (
                                            AgentWorkspaceRow.current_tree_digest
                                            == row.result_digest
                                        )
                                    ),
                                )
                                .values(
                                    state=AgentWorkspaceState.RECONCILIATION_REQUIRED
                                )
                            )
                        else:
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
