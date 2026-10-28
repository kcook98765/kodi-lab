"""Constructive parsing of a fixed, post-cursor Kodi log window."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any
from uuid import UUID

import docker  # type: ignore[import-untyped]
from docker.errors import DockerException  # type: ignore[import-untyped]
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.broker.kodi_logs import KodiLogAuthority, KodiLogWindowError
from kodi_lab.config import Settings
from kodi_lab.domain import (
    AgentWorkspaceBytesState,
    AgentWorkspaceState,
    InstanceType,
    TargetType,
)
from kodi_lab.domain.smoke_diagnostics import (
    AddonExceptionDiagnostic,
    SmokeDiagnosticStatus,
)
from kodi_lab.storage.engine import create_database_engine
from kodi_lab.storage.models import (
    AgentWorkspaceRow,
    ManagedInstanceDevelopmentSourceRow,
    ManagedInstanceRow,
    McpWorkspaceDeploymentOperationRow,
    McpWorkspaceExecutionOperationRow,
    TargetRow,
)

MAX_SMOKE_WINDOW_BYTES = 64 * 1024
MAX_EXCEPTION_BLOCK_BYTES = 16 * 1024
_START = "Traceback (most recent call last):"
_END = "-->End of Python script error report<--"
_FRAME = re.compile(r'^\s*File "([^"\r\n]+)", line ([0-9]{1,7})(?:, in [^\r\n]*)?\s*$')
_FINAL = re.compile(r"^([A-Za-z_][A-Za-z_0-9]{0,63})(?::.*)?$")
_PREFIX = re.compile(r"^.{0,120}?\b(?:error|warning|info|debug) <[^>]+>: ?", re.I)
_ADDON_ID = re.compile(r"^[-a-zA-Z0-9_.]{1,128}$")
_ROOT = "/home/kodi/.kodi/addons/"


class SqliteSmokeLogAuthorityResolver:
    """Resolve one running disposable execution to its current labelled container."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        client_factory: Callable[[], Any] = docker.APIClient,
    ) -> None:
        self._sessions = session_factory
        self._client_factory = client_factory

    @classmethod
    def production(cls) -> SqliteSmokeLogAuthorityResolver:
        engine = create_database_engine(Settings().database_url)
        return cls(sessionmaker(engine, expire_on_commit=False))

    def resolve(self, execution_id: str) -> KodiLogAuthority:
        # Application package initialization itself loads broker operations.
        from kodi_lab.application.workspace_deployment_selection import (
            WorkspaceDeploymentAmbiguousError,
            latest_verified_workspace_deployment,
        )

        try:
            if str(UUID(execution_id)) != execution_id:
                raise ValueError
        except ValueError as error:
            raise KodiLogWindowError(
                "smoke_log_authority_invalid", "Execution identity is invalid."
            ) from error
        with self._sessions() as session:
            execution = session.get(McpWorkspaceExecutionOperationRow, execution_id)
            deployment = (
                None
                if execution is None
                else session.get(
                    McpWorkspaceDeploymentOperationRow, execution.deployment_id
                )
            )
            workspace = (
                None
                if execution is None
                else session.get(AgentWorkspaceRow, execution.workspace_id)
            )
            instance = (
                None
                if execution is None
                else session.get(ManagedInstanceRow, execution.target_id)
            )
            target = (
                None
                if execution is None
                else session.get(TargetRow, execution.target_id)
            )
            attachment = (
                None
                if execution is None
                else session.get(
                    ManagedInstanceDevelopmentSourceRow,
                    {
                        "managed_instance_id": execution.target_id,
                        "development_source_id": execution.development_source_id,
                    },
                )
            )
            try:
                latest = (
                    None
                    if execution is None
                    else latest_verified_workspace_deployment(
                        session,
                        workspace_id=execution.workspace_id,
                        target_id=execution.target_id,
                        source_id=execution.development_source_id,
                        generation=execution.generation,
                    )
                )
            except WorkspaceDeploymentAmbiguousError as error:
                raise KodiLogWindowError(
                    "smoke_log_authority_invalid", "Smoke log authority is ambiguous."
                ) from error
            if (
                execution is None
                or deployment is None
                or workspace is None
                or instance is None
                or target is None
                or attachment is None
                or latest is None
                or latest.id != deployment.id
                or execution.state != "running"
                or execution.phase != "invoke"
                or not execution.invocation_dispatched
                or deployment.state != "succeeded"
                or deployment.target_class != "internal_lab"
                or deployment.deployment_mode != "direct"
                or deployment.verified_destination_digest is None
                or not deployment.addon_enabled
                or deployment.id != execution.deployment_id
                or deployment.workspace_id != execution.workspace_id
                or deployment.development_source_id != execution.development_source_id
                or deployment.target_id != execution.target_id
                or deployment.generation != execution.generation
                or deployment.addon_id != execution.addon_id
                or deployment.addon_version != execution.addon_version
                or workspace.generation != execution.generation
                or workspace.state is not AgentWorkspaceState.READY
                or workspace.bytes_state is not AgentWorkspaceBytesState.PRESENT
                or workspace.development_source_id != execution.development_source_id
                or target.target_type is not TargetType.MANAGED
                or instance.instance_type is not InstanceType.DISPOSABLE
                or deployment.runtime_revision_id != instance.runtime_revision_id
                or not _ADDON_ID.fullmatch(execution.addon_id)
            ):
                raise KodiLogWindowError(
                    "smoke_log_authority_invalid", "Smoke log authority is unavailable."
                )
            instance_id = execution.target_id
            revision_id = instance.runtime_revision_id
            addon_id = execution.addon_id
        client = self._client_factory()
        try:
            matches = client.containers(
                all=True,
                filters={
                    "label": [
                        "io.kodi-lab.managed=true",
                        f"io.kodi-lab.instance-id={instance_id}",
                    ]
                },
            )
            if not isinstance(matches, list) or len(matches) != 1:
                raise KodiLogWindowError(
                    "smoke_log_container_drift",
                    "Managed container identity is ambiguous.",
                )
            container_id = matches[0].get("Id")
            if not isinstance(container_id, str) or not re.fullmatch(
                r"[0-9a-f]{64}", container_id
            ):
                raise KodiLogWindowError(
                    "smoke_log_container_drift",
                    "Managed container identity is invalid.",
                )
            inspected = client.inspect_container(container_id)
            labels = (inspected.get("Config") or {}).get("Labels") or {}
            if (
                inspected.get("Id") != container_id
                or not (inspected.get("State") or {}).get("Running", False)
                or labels.get("io.kodi-lab.managed") != "true"
                or labels.get("io.kodi-lab.instance-id") != instance_id
                or labels.get("io.kodi-lab.runtime-revision-id") != revision_id
            ):
                raise KodiLogWindowError(
                    "smoke_log_container_drift", "Managed container identity changed."
                )
        except KodiLogWindowError:
            raise
        except (DockerException, OSError, TypeError, ValueError) as error:
            raise KodiLogWindowError(
                "smoke_log_container_drift", "Managed container is unavailable."
            ) from error
        finally:
            client.close()
        return KodiLogAuthority(
            execution_id, instance_id, container_id, revision_id, addon_id
        )


def _payload(line: str) -> str:
    return _PREFIX.sub("", line).rstrip()


def _addon_frame(path: str, addon_id: str) -> str | None:
    prefix = f"{_ROOT}{addon_id}/"
    if not path.startswith(prefix):
        return None
    relative = path[len(prefix) :]
    if len(relative) > 256 or ".." in relative or not relative.endswith(".py"):
        return None
    parts = relative.split("/")
    if any(not part or part in {".", ".."} for part in parts):
        return None
    if any(not re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in parts):
        return None
    return relative


def parse_smoke_exception_window(
    data: bytes, addon_id: str
) -> AddonExceptionDiagnostic:
    """Return one summary only when a full traceback and exact root failure agree."""
    no_match = AddonExceptionDiagnostic(status=SmokeDiagnosticStatus.NO_MATCH)
    rejected = AddonExceptionDiagnostic(status=SmokeDiagnosticStatus.REJECTED)
    if not _ADDON_ID.fullmatch(addon_id) or len(data) > MAX_SMOKE_WINDOW_BYTES:
        return rejected
    try:
        lines = [_payload(line) for line in data.decode("utf-8", "strict").splitlines()]
    except UnicodeDecodeError:
        return rejected
    root = f"plugin://{addon_id}/"
    failures = [
        i
        for i, line in enumerate(lines)
        if "GetDirectory - Error getting " in line and line.endswith(root)
    ]
    other_failures = [
        i
        for i, line in enumerate(lines)
        if "GetDirectory - Error getting plugin://" in line and i not in failures
    ]
    if len(failures) != 1 or other_failures:
        return rejected if failures or other_failures else no_match
    starts = [i for i, line in enumerate(lines) if line.strip() == _START]
    if not starts:
        return no_match
    if len(starts) != 1:
        return rejected
    start = starts[0]
    ends = [i for i in range(start + 1, failures[0]) if _END in lines[i]]
    if len(ends) != 1 or start >= failures[0]:
        return rejected
    end = ends[0]
    block = lines[start + 1 : end]
    if not block or len("\n".join(block).encode()) > MAX_EXCEPTION_BLOCK_BYTES:
        return rejected
    # Kodi writes traceback source lines between frame and exception lines. They
    # are never retained, and only the last structural exception line is used.
    final_line = next((line.strip() for line in reversed(block) if line.strip()), "")
    final = _FINAL.fullmatch(final_line)
    if final is None or ":" not in final_line:
        return rejected
    frames: list[tuple[str, int]] = []
    for line in block:
        match = _FRAME.fullmatch(line)
        if match is None:
            continue
        path = match.group(1)
        if "/addons/" in path and not path.startswith(_ROOT):
            return rejected
        relative = _addon_frame(path, addon_id)
        if relative is not None:
            number = int(match.group(2))
            if not 1 <= number <= 1_000_000:
                return rejected
            frames.append((relative, number))
        elif path.startswith(f"{_ROOT}{addon_id}"):
            return rejected
    if not frames:
        return no_match
    relative, number = frames[-1]
    return AddonExceptionDiagnostic(
        status=SmokeDiagnosticStatus.MATCHED,
        exception_class=final.group(1),
        addon_file=relative,
        line_number=number,
    )
