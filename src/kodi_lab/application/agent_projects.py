"""Bounded owner-scoped project bootstrap over the existing workspace pipeline."""

from __future__ import annotations

import base64
import binascii
import fcntl
import hashlib
import json
import os
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4, uuid5
from xml.etree import ElementTree

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from kodi_lab.application.agent_workspaces import AgentWorkspaceService
from kodi_lab.application.mcp_workspace_mutations import (
    MCP_PRESENT_WORKSPACES_GLOBAL,
    MCP_PRESENT_WORKSPACES_PER_CLIENT,
    McpWorkspaceCreationService,
    WorkspaceCreateResult,
)
from kodi_lab.application.project_transfers import (
    ProjectTransferInput,
    ProjectTransferStatus,
    project_import_lock,
    read_source_transfer,
    transfer_source,
)
from kodi_lab.development.addon_zip import (
    AddonZipValidationError,
    read_editable_addon_zip,
)
from kodi_lab.development.sources import (
    DevelopmentSourceValidationError,
    parse_captured_addon_xml,
)
from kodi_lab.domain import DevelopmentSource, DevelopmentSourceKind
from kodi_lab.storage.models import (
    AgentProjectRow,
    McpAgentWorkspaceBindingRow,
    McpClientDevelopmentSourceGrantRow,
    McpSecurityEventRow,
)
from kodi_lab.storage.repositories import DevelopmentSourceRepository

_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
_READ = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK


class AgentProjectError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class ProjectCreateInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    request_id: UUID
    addon_id: str = Field(
        min_length=3, max_length=128, pattern=r"^[a-z][a-z0-9_-]*(?:\.[a-z0-9_-]+)+$"
    )
    display_name: str = Field(min_length=1, max_length=120)
    version: str = Field(
        default="0.1.0",
        min_length=1,
        max_length=128,
        pattern=r"^[0-9]+(?:\.[0-9]+){0,3}$",
    )
    kind: Literal["program", "video"] = "program"


class ProjectImportInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)
    request_id: UUID
    archive_base64: str = Field(
        default="", max_length=61440, pattern=r"^(?:[A-Za-z0-9+/]+={0,2})?$"
    )
    transfer_id: UUID | None = None
    archive_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    intake_kind: Literal["reviewed_checkout", "source_zip"]
    provenance_reference: str = Field(min_length=1, max_length=512)

    @model_validator(mode="after")
    def one_transport(self) -> ProjectImportInput:
        if bool(self.archive_base64) == (self.transfer_id is not None):
            raise ValueError("Supply inline bytes or an owned source transfer.")
        return self


class AgentProjectResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = 1
    project_id: UUID
    addon_id: str
    workspace: WorkspaceCreateResult


class ProjectStatusInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    project_id: UUID | None = None
    cursor: UUID | None = None
    limit: int = Field(default=25, ge=1, le=100)


class AgentProjectSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: UUID
    request_id: UUID
    addon_id: str
    display_name: str
    intake_state: str = Field(pattern="^(reserved|seeded)$")
    workspace_ids: tuple[UUID, ...]
    intake_provenance: dict[str, str | int] | None = None


class AgentProjectList(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: int = 1
    projects: tuple[AgentProjectSummary, ...]
    next_cursor: UUID | None = None


def project_seed(operation: ProjectCreateInput) -> dict[str, bytes]:
    addon = ElementTree.Element(
        "addon",
        {
            "id": operation.addon_id,
            "name": operation.display_name,
            "version": operation.version,
            "provider-name": "Kodi Lab agent",
        },
    )
    requires = ElementTree.SubElement(addon, "requires")
    ElementTree.SubElement(requires, "import", addon="xbmc.python", version="3.0.0")
    point = (
        "xbmc.python.script"
        if operation.kind == "program"
        else "xbmc.python.pluginsource"
    )
    extension = ElementTree.SubElement(
        addon, "extension", point=point, library="main.py"
    )
    if operation.kind == "video":
        ElementTree.SubElement(extension, "provides").text = "video"
        main = (
            "import sys\nimport xbmcplugin\n"
            "xbmcplugin.endOfDirectory(int(sys.argv[1]))\n"
        )
    else:
        main = (
            "import xbmc\nxbmc.log("
            + json.dumps("Kodi Lab inert project: " + operation.addon_id)
            + ", xbmc.LOGINFO)\n"
        )
    descriptor = ElementTree.tostring(addon, encoding="utf-8", xml_declaration=True)
    # Existing offline descriptor parser is authoritative for identity and types.
    parse_captured_addon_xml(operation.addon_id, descriptor)
    return {"addon.xml": descriptor, "main.py": main.encode("utf-8")}


@contextmanager
def _private_directory(root: Path, *parts: str) -> Iterator[int]:
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(root, _DIRECTORY)
    try:
        for part in parts:
            with suppress(FileExistsError):
                os.mkdir(part, 0o700, dir_fd=descriptor)
            child = os.open(part, _DIRECTORY, dir_fd=descriptor)
            metadata = os.fstat(child)
            if (
                metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) & 0o077
            ):
                os.close(child)
                raise AgentProjectError("project_storage_unsafe")
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


def _seed_directory(directory: int, contents: dict[str, bytes]) -> None:
    files: dict[str, bytes] = {}
    folders: dict[str, dict[str, bytes]] = {}
    for path, content in contents.items():
        parts = path.split("/")
        if any(
            part in {"", ".", ".."} or "\\" in part or "\0" in part for part in parts
        ):
            raise AgentProjectError("project_storage_unsafe")
        if len(parts) == 1:
            files[path] = content
        else:
            folders.setdefault(parts[0], {})["/".join(parts[1:])] = content
    if files.keys() & folders.keys():
        raise AgentProjectError("project_storage_unsafe")
    if set(os.listdir(directory)) - (files.keys() | folders.keys()):
        raise AgentProjectError("project_seed_requires_review")
    for name, descendants in folders.items():
        with suppress(FileExistsError):
            os.mkdir(name, 0o700, dir_fd=directory)
        child = os.open(name, _DIRECTORY, dir_fd=directory)
        try:
            info = os.fstat(child)
            if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) not in {
                0o700,
                0o500,
            }:
                raise AgentProjectError("project_storage_unsafe")
            _seed_directory(child, descendants)
        finally:
            os.close(child)
    for name, content in files.items():
        try:
            fd = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=directory,
            )
        except FileExistsError:
            fd = os.open(name, _READ, dir_fd=directory)
            try:
                metadata = os.fstat(fd)
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_nlink != 1
                    or metadata.st_uid != os.geteuid()
                    or stat.S_IMODE(metadata.st_mode) != 0o400
                    or metadata.st_size != len(content)
                    or os.read(fd, len(content) + 1) != content
                ):
                    raise AgentProjectError("project_seed_requires_review")
            finally:
                os.close(fd)
        else:
            try:
                remaining = memoryview(content)
                while remaining:
                    count = os.write(fd, remaining)
                    if count <= 0:
                        raise AgentProjectError("project_seed_requires_review")
                    remaining = remaining[count:]
                os.fchmod(fd, 0o400)
                os.fsync(fd)
            finally:
                os.close(fd)
    os.fchmod(directory, 0o500)
    os.fsync(directory)


def _seed_source(data_root: Path, project_id: UUID, files: dict[str, bytes]) -> None:
    # Roots are fixed/UUID identities; member paths were validated before intake.
    with _private_directory(data_root, "agent-projects") as locks:
        lock = os.open(
            str(project_id) + ".lock",
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=locks,
        )
        try:
            identity = os.fstat(lock)
            if (
                not stat.S_ISREG(identity.st_mode)
                or identity.st_nlink != 1
                or identity.st_uid != os.geteuid()
                or stat.S_IMODE(identity.st_mode) != 0o600
            ):
                raise AgentProjectError("project_storage_unsafe")
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise AgentProjectError("project_intake_busy") from error
            with _private_directory(
                data_root, "agent-projects", "sources", str(project_id)
            ) as directory:
                _seed_directory(directory, files)
        finally:
            os.close(lock)


class AgentProjectService:
    def __init__(
        self, workspaces: AgentWorkspaceService, creation: McpWorkspaceCreationService
    ) -> None:
        self._workspaces, self._creation = workspaces, creation
        self._sessions, self._root = workspaces._session_factory, workspaces._data_root

    def transfer(
        self,
        client_id: UUID,
        operation: ProjectTransferInput,
        *,
        authorize: Callable[[Session], None],
    ) -> ProjectTransferStatus:
        def recheck() -> None:
            with self._sessions() as session:
                authorize(session)

        return transfer_source(self._root, client_id, operation, authorize=recheck)

    def status(
        self,
        client_id: UUID,
        operation: ProjectStatusInput,
        *,
        authorize: Callable[[Session], None],
    ) -> AgentProjectList:
        with self._sessions() as session:
            authorize(session)
            statement = select(AgentProjectRow).where(
                AgentProjectRow.client_id == str(client_id)
            )
            if operation.project_id is not None:
                statement = statement.where(
                    AgentProjectRow.source_id == str(operation.project_id)
                )
            if operation.cursor is not None:
                statement = statement.where(
                    AgentProjectRow.source_id > str(operation.cursor)
                )
            rows = session.scalars(
                statement.order_by(AgentProjectRow.source_id).limit(operation.limit + 1)
            ).all()
            if operation.project_id is not None and not rows:
                raise AgentProjectError("project_not_found_or_not_authorized")
            summaries = []
            for row in rows[: operation.limit]:
                source = DevelopmentSourceRepository(session).get(UUID(row.source_id))
                if source is None:
                    raise AgentProjectError("project_intake_requires_review")
                bindings = session.scalars(
                    select(McpAgentWorkspaceBindingRow.workspace_id)
                    .where(
                        McpAgentWorkspaceBindingRow.client_id == str(client_id),
                        McpAgentWorkspaceBindingRow.development_source_id
                        == row.source_id,
                    )
                    .order_by(McpAgentWorkspaceBindingRow.workspace_id)
                    .limit(5)
                ).all()
                if len(bindings) > 4:
                    raise AgentProjectError("project_intake_requires_review")
                summaries.append(
                    AgentProjectSummary(
                        project_id=UUID(row.source_id),
                        request_id=UUID(row.request_id),
                        addon_id=source.addon_id,
                        display_name=source.observed_name,
                        intake_state=row.state,
                        intake_provenance=(
                            None
                            if row.intake_provenance_json is None
                            else json.loads(row.intake_provenance_json)
                        ),
                        workspace_ids=tuple(UUID(value) for value in bindings),
                    )
                )
        return AgentProjectList(
            projects=tuple(summaries),
            next_cursor=UUID(rows[operation.limit - 1].source_id)
            if len(rows) > operation.limit
            else None,
        )

    def create(
        self,
        client_id: UUID,
        operation: ProjectCreateInput,
        *,
        authorize: Callable[[Session], None],
    ) -> AgentProjectResult:
        try:
            files = project_seed(operation)
        except DevelopmentSourceValidationError as error:
            raise AgentProjectError("project_descriptor_invalid") from error
        return self._create_seed(
            client_id,
            request_id=operation.request_id,
            addon_id=operation.addon_id,
            display_name=operation.display_name,
            fingerprint=hashlib.sha256(
                operation.model_dump_json().encode()
            ).hexdigest(),
            files=files,
            provenance=None,
            authorize=authorize,
        )

    def import_project(
        self,
        client_id: UUID,
        operation: ProjectImportInput,
        *,
        authorize: Callable[[Session], None],
    ) -> AgentProjectResult:
        def recheck() -> None:
            with self._sessions() as session:
                authorize(session)

        with project_import_lock(self._root, recheck):
            return self._import_project(client_id, operation, authorize=authorize)

    def _import_project(
        self,
        client_id: UUID,
        operation: ProjectImportInput,
        *,
        authorize: Callable[[Session], None],
    ) -> AgentProjectResult:
        with self._sessions() as session:
            authorize(session)
            metadata = operation.model_dump(exclude={"archive_base64"}, mode="json")
            if operation.transfer_id is None:
                metadata.pop("transfer_id")
            fingerprint = hashlib.sha256(
                json.dumps(metadata, sort_keys=True).encode()
            ).hexdigest()
            prior = session.scalar(
                select(AgentProjectRow).where(
                    AgentProjectRow.client_id == str(client_id),
                    AgentProjectRow.request_id == str(operation.request_id),
                )
            )
            if prior is not None and prior.parameter_fingerprint != fingerprint:
                raise AgentProjectError("request_id_conflict")
            if (
                operation.transfer_id is not None
                and prior is not None
                and prior.state == "seeded"
            ):
                source = DevelopmentSourceRepository(session).get(UUID(prior.source_id))
                if source is None:
                    raise AgentProjectError("project_intake_requires_review")
                # Published requests replay after transport cancellation/expiry.
                # A reserved publication still needs the original verified bytes.
                return self._create_seed(
                    client_id,
                    request_id=operation.request_id,
                    addon_id=source.addon_id,
                    display_name=source.observed_name,
                    fingerprint=fingerprint,
                    files={},
                    provenance=prior.intake_provenance_json,
                    authorize=authorize,
                )
        try:

            def recheck() -> None:
                with self._sessions() as session:
                    authorize(session)

            payload = (
                base64.b64decode(operation.archive_base64, validate=True)
                if operation.transfer_id is None
                else read_source_transfer(
                    self._root,
                    client_id,
                    operation.transfer_id,
                    operation.archive_sha256,
                    authorize=recheck,
                )
            )
            if hashlib.sha256(payload).hexdigest() != operation.archive_sha256:
                raise AgentProjectError("project_package_digest_mismatch")
            package = read_editable_addon_zip(
                payload, staged=operation.transfer_id is not None, source_modules=True
            )
        except binascii.Error as error:
            raise AgentProjectError("project_package_encoding_invalid") from error
        except AddonZipValidationError as error:
            raise AgentProjectError(error.code) from error
        provenance = json.dumps(
            {
                "intake_kind": operation.intake_kind,
                "reference": operation.provenance_reference,
                "package_sha256": package.sha256,
                "file_count": len(package.files),
                "total_bytes": sum(len(value) for value in package.files.values()),
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        return self._create_seed(
            client_id,
            request_id=operation.request_id,
            addon_id=package.descriptor.addon_id,
            display_name=package.descriptor.observed_name,
            fingerprint=fingerprint,
            files=package.files,
            provenance=provenance,
            authorize=authorize,
        )

    def _create_seed(
        self,
        client_id: UUID,
        *,
        request_id: UUID,
        addon_id: str,
        display_name: str,
        fingerprint: str,
        files: dict[str, bytes],
        provenance: str | None,
        authorize: Callable[[Session], None],
    ) -> AgentProjectResult:
        now = datetime.now(UTC)
        timestamp = now.isoformat(timespec="microseconds").replace("+00:00", "Z")
        try:
            with self._sessions.begin() as session:
                session.execute(text("BEGIN IMMEDIATE"))
                authorize(session)
                existing = session.scalar(
                    select(AgentProjectRow).where(
                        AgentProjectRow.client_id == str(client_id),
                        AgentProjectRow.request_id == str(request_id),
                    )
                )
                if existing is not None:
                    if existing.parameter_fingerprint != fingerprint:
                        raise AgentProjectError("request_id_conflict")
                    project_id = UUID(existing.source_id)
                    needs_seed = existing.state != "seeded"
                else:
                    own_count = (
                        session.scalar(
                            select(func.count())
                            .select_from(AgentProjectRow)
                            .where(AgentProjectRow.client_id == str(client_id))
                        )
                        or 0
                    )
                    total = (
                        session.scalar(
                            select(func.count()).select_from(AgentProjectRow)
                        )
                        or 0
                    )
                    if (
                        own_count >= MCP_PRESENT_WORKSPACES_PER_CLIENT
                        or total >= MCP_PRESENT_WORKSPACES_GLOBAL
                    ):
                        raise AgentProjectError("project_quota_exceeded")
                    sources = DevelopmentSourceRepository(session)
                    if sources.get_by_addon_id(addon_id) is not None:
                        raise AgentProjectError("project_addon_id_conflict")
                    project_id = uuid4()
                    needs_seed = True
                    observed = parse_captured_addon_xml(
                        str(project_id), files["addon.xml"]
                    )
                    sources.create(
                        DevelopmentSource(
                            id=project_id,
                            logical_name=(display_name[:80] + " " + str(project_id)),
                            source_kind=DevelopmentSourceKind.LOCAL_DIRECTORY,
                            relative_locator=str(project_id),
                            addon_id=observed.addon_id,
                            observed_name=observed.observed_name,
                            observed_version=observed.observed_version,
                            extension_types=observed.extension_types,
                            registered_at=now,
                            validated_at=now,
                        )
                    )
                    session.add(
                        AgentProjectRow(
                            source_id=str(project_id),
                            client_id=str(client_id),
                            request_id=str(request_id),
                            parameter_fingerprint=fingerprint,
                            state="reserved",
                            intake_provenance_json=provenance,
                            created_at=timestamp,
                        )
                    )
                    session.add(
                        McpClientDevelopmentSourceGrantRow(
                            client_id=str(client_id),
                            development_source_id=str(project_id),
                            created_at=timestamp,
                        )
                    )
                    session.add(
                        McpSecurityEventRow(
                            id=str(uuid4()),
                            event_kind="agent_project_created",
                            actor="system",
                            client_id=str(client_id),
                            development_source_id=str(project_id),
                            occurred_at=timestamp,
                        )
                    )
                    session.flush()
        except IntegrityError as error:
            raise AgentProjectError("project_intake_conflict") from error
        if needs_seed:
            try:
                _seed_source(self._root, project_id, files)
            except OSError as error:
                raise AgentProjectError("project_seed_requires_review") from error
            with self._sessions.begin() as session:
                session.execute(text("BEGIN IMMEDIATE"))
                authorize(session)
                row = session.get(AgentProjectRow, str(project_id))
                if row is None or row.client_id != str(client_id):
                    raise AgentProjectError("project_not_found_or_not_authorized")
                row.state = "seeded"
        workspace = self._creation.create(
            client_id=client_id,
            request_id=uuid5(project_id, "initial-workspace"),
            source_id=project_id,
            reuse_project=True,
            authorize=authorize,
        )
        return AgentProjectResult(
            project_id=project_id, addon_id=addon_id, workspace=workspace
        )
