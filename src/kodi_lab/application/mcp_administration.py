"""Durable MCP clients, credentials, presets, grants, and policy assignments."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.mcp_workspace_development import (
    McpWorkspaceDevelopmentService,
)
from kodi_lab.application.mcp_workspace_retention import McpWorkspaceRetentionService
from kodi_lab.domain import ExternalAdministrativeState, TargetType
from kodi_lab.storage.models import (
    AgentWorkspaceRow,
    DevelopmentSourceRow,
    ExternalKodiTargetRow,
    McpAgentWorkspaceBindingRow,
    McpClientDevelopmentSourceGrantRow,
    McpClientRow,
    McpClientTargetGrantRow,
    McpCredentialRow,
    McpPresetPermissionRow,
    McpPresetRow,
    McpSecurityEventRow,
    McpTargetPresetAssignmentRow,
    TargetRow,
)

ADDON_DEVELOPMENT_PRESET_ID = UUID("11000000-0000-4000-8000-000000000001")
READ_ONLY_INSPECTION_PRESET_ID = UUID("11000000-0000-4000-8000-000000000002")
FULL_LAB_CONTROL_PRESET_ID = UUID("11000000-0000-4000-8000-000000000003")
PRODUCTION_REMOTE_KODI_PRESET_ID = UUID("11000000-0000-4000-8000-000000000004")
DEFAULT_TOKEN_LIFETIME = timedelta(hours=24)
LAST_USED_WRITE_INTERVAL = timedelta(minutes=5)

_OBSERVATION_PERMISSIONS = ("target.inspect", "target.visual")
_DEVELOPMENT_PERMISSIONS = (
    *_OBSERVATION_PERMISSIONS,
    "target.control",
    "workspace.edit",
    "workspace.inspect",
    "workspace.deploy",
    "development.execute",
    "development.request_promotion",
)

_BUILTINS = (
    (
        ADDON_DEVELOPMENT_PRESET_ID,
        "Add-on Development",
        True,
        _DEVELOPMENT_PERMISSIONS,
    ),
    (
        READ_ONLY_INSPECTION_PRESET_ID,
        "Read-only Inspection",
        True,
        _OBSERVATION_PERMISSIONS,
    ),
    (
        FULL_LAB_CONTROL_PRESET_ID,
        "Full Lab Control",
        True,
        _DEVELOPMENT_PERMISSIONS,
    ),
    (PRODUCTION_REMOTE_KODI_PRESET_ID, "Production Remote Kodi", False, ()),
)


class McpAdministrationError(ValueError):
    """A bounded MCP administration failure safe for Web translation."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class McpPresetRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    display_name: str = Field(max_length=120)
    builtin: bool
    assignable: bool
    permissions: tuple[str, ...]


class McpCredentialRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    client_id: UUID
    created_at: datetime
    expires_at: datetime | None
    revoked_at: datetime | None
    last_used_at: datetime | None
    status: str


class McpWorkspaceBindingRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workspace_id: UUID
    client_id: UUID
    development_source_id: UUID
    binding_origin: str = Field(pattern="^(web_admin|mcp_created)$")
    created_at: datetime
    owning_client_enabled: bool = True
    retention_classification: str = "active"
    retention_deadline_at: datetime | None = None
    automatic_cleanup_blocked: bool = False
    latest_deployment_id: UUID | None = None
    latest_deployment_target_id: UUID | None = None
    latest_deployment_generation: int | None = Field(default=None, ge=0)
    latest_deployment_state: str | None = None
    latest_execution_id: UUID | None = None
    latest_execution_target_id: UUID | None = None
    latest_execution_generation: int | None = Field(default=None, ge=0)
    latest_execution_state: str | None = None


class McpEligibleWorkspaceRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workspace_id: UUID
    development_source_id: UUID
    source_logical_name: str = Field(max_length=120)
    state: str
    bytes_state: str
    generation: int = Field(ge=0)
    created_at: datetime


class McpClientRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    display_name: str = Field(max_length=120)
    enabled: bool
    preset: McpPresetRead
    created_at: datetime
    last_used_at: datetime | None
    disabled_at: datetime | None = None
    credentials: tuple[McpCredentialRead, ...]
    target_grants: tuple[UUID, ...]
    development_source_grants: tuple[UUID, ...]
    workspace_bindings: tuple[McpWorkspaceBindingRead, ...]


class McpIssuedCredential(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    credential: McpCredentialRead
    token: str = Field(min_length=43, max_length=43)


class McpTargetPolicyRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    target_id: UUID
    target_name: str = Field(max_length=256)
    preset: McpPresetRead | None


@dataclass(frozen=True, slots=True)
class McpPrincipal:
    client_id: UUID
    credential_id: UUID
    preset_id: UUID
    permissions: frozenset[str]


def _to_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone aware")
    return (
        value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    )


def _from_text(value: str | None) -> datetime | None:
    return (
        None if value is None else datetime.fromisoformat(value.replace("Z", "+00:00"))
    )


def _from_required_text(value: str) -> datetime:
    parsed = _from_text(value)
    assert parsed is not None
    return parsed


def _status(row: McpCredentialRow, now: datetime) -> str:
    if row.revoked_at is not None:
        return "revoked"
    expires_at = _from_text(row.expires_at)
    if expires_at is not None and expires_at <= now:
        return "expired"
    return "active"


class McpAdministrationService:
    """Web-admin-only mutation boundary for all durable MCP identities."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        token_bytes: Callable[[int], bytes] = secrets.token_bytes,
        retention: McpWorkspaceRetentionService | None = None,
        development: McpWorkspaceDevelopmentService | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock
        self._token_bytes = token_bytes
        self._retention = retention
        self._development = development
        self.ensure_builtin_presets()

    def ensure_builtin_presets(self) -> None:
        """Seed built-ins for create_all tests; migrations own production."""

        with self._session_factory.begin() as session:
            existing = {
                row.id: row for row in session.scalars(select(McpPresetRow)).all()
            }
            builtin_ids = {str(item[0]) for item in _BUILTINS}
            if existing and not builtin_ids.issubset(existing):
                raise RuntimeError("MCP built-in preset state is incomplete")
            created: set[str] = set()
            for preset_id, name, assignable, _permissions in _BUILTINS:
                key = str(preset_id)
                row = existing.get(key)
                if row is None:
                    session.add(
                        McpPresetRow(
                            id=key,
                            display_name=name,
                            builtin=True,
                            assignable=assignable,
                        )
                    )
                    created.add(key)
                elif (
                    row.display_name != name
                    or not row.builtin
                    or row.assignable != assignable
                ):
                    raise RuntimeError("MCP built-in preset state has drifted")
            session.flush()
            for preset_id, _name, _assignable, permissions in _BUILTINS:
                key = str(preset_id)
                actual = set(
                    session.scalars(
                        select(McpPresetPermissionRow.permission).where(
                            McpPresetPermissionRow.preset_id == key
                        )
                    ).all()
                )
                expected = set(permissions)
                if key in created:
                    for permission in expected:
                        session.add(
                            McpPresetPermissionRow(preset_id=key, permission=permission)
                        )
                elif actual != expected:
                    raise RuntimeError("MCP built-in preset permissions have drifted")

    def list_presets(self) -> list[McpPresetRead]:
        with self._session_factory() as session:
            rows = session.scalars(
                select(McpPresetRow).order_by(
                    McpPresetRow.display_name, McpPresetRow.id
                )
            ).all()
            return [self._preset_read(session, row) for row in rows]

    def create_custom_preset(
        self, *, display_name: str, template_preset_id: UUID
    ) -> McpPresetRead:
        """Clone one active built-in into an independent permission snapshot."""

        name = self._preset_name(display_name)
        now = self._clock()
        preset_id = uuid4()
        try:
            with self._session_factory.begin() as session:
                template = self._require_clone_template(session, template_preset_id)
                permissions = tuple(
                    session.scalars(
                        select(McpPresetPermissionRow.permission)
                        .where(McpPresetPermissionRow.preset_id == template.id)
                        .order_by(McpPresetPermissionRow.permission)
                    ).all()
                )
                known_permissions = frozenset(
                    permission
                    for _id, _name, _assignable, builtin_permissions in _BUILTINS
                    for permission in builtin_permissions
                )
                if not set(permissions).issubset(known_permissions):
                    raise McpAdministrationError("mcp_preset_template_invalid")
                session.add(
                    McpPresetRow(
                        id=str(preset_id),
                        display_name=name,
                        builtin=False,
                        assignable=True,
                    )
                )
                session.flush()
                for permission in permissions:
                    session.add(
                        McpPresetPermissionRow(
                            preset_id=str(preset_id), permission=permission
                        )
                    )
                self._event(
                    session,
                    "preset_created",
                    now,
                    preset_id=preset_id,
                )
        except IntegrityError as error:
            raise McpAdministrationError("mcp_preset_name_conflict") from error
        with self._session_factory() as session:
            row = session.get(McpPresetRow, str(preset_id))
            if row is None:  # pragma: no cover - committed insert must be readable
                raise RuntimeError("created MCP preset is missing")
            return self._preset_read(session, row)

    def list_clients(self) -> list[McpClientRead]:
        now = self._clock()
        with self._session_factory() as session:
            rows = session.scalars(
                select(McpClientRow).order_by(
                    McpClientRow.display_name, McpClientRow.id
                )
            ).all()
            return [self._client_read(session, row, now) for row in rows]

    def get_client(self, client_id: UUID) -> McpClientRead:
        now = self._clock()
        with self._session_factory() as session:
            row = session.get(McpClientRow, str(client_id))
            if row is None:
                raise McpAdministrationError("mcp_client_not_found")
            return self._client_read(session, row, now)

    def create_client(self, *, display_name: str, preset_id: UUID) -> McpClientRead:
        name = self._name(display_name)
        now = self._clock()
        client_id = uuid4()
        with self._session_factory.begin() as session:
            self._require_assignable_preset(session, preset_id)
            session.add(
                McpClientRow(
                    id=str(client_id),
                    display_name=name,
                    enabled=True,
                    preset_id=str(preset_id),
                    created_at=_to_text(now),
                    last_used_at=None,
                    disabled_at=None,
                )
            )
            self._event(
                session,
                "client_created",
                now,
                client_id=client_id,
                preset_id=preset_id,
            )
        return self.get_client(client_id)

    def rename_client(self, client_id: UUID, display_name: str) -> McpClientRead:
        name = self._name(display_name)
        now = self._clock()
        with self._session_factory.begin() as session:
            row = self._require_client(session, client_id)
            row.display_name = name
            self._event(session, "client_renamed", now, client_id=client_id)
        return self.get_client(client_id)

    def set_client_enabled(self, client_id: UUID, enabled: bool) -> McpClientRead:
        now = self._clock()
        with self._session_factory.begin() as session:
            row = self._require_client(session, client_id)
            changed = row.enabled != enabled
            row.enabled = enabled
            if changed:
                row.disabled_at = None if enabled else _to_text(now)
                self._event(
                    session,
                    "client_enabled" if enabled else "client_disabled",
                    now,
                    client_id=client_id,
                )
                self._event(
                    session,
                    (
                        "retention_quarantine_cancelled"
                        if enabled
                        else "retention_quarantine_entered"
                    ),
                    now,
                    client_id=client_id,
                )
        return self.get_client(client_id)

    def assign_client_preset(self, client_id: UUID, preset_id: UUID) -> McpClientRead:
        now = self._clock()
        with self._session_factory.begin() as session:
            row = self._require_client(session, client_id)
            self._require_assignable_preset(session, preset_id)
            previous_id = UUID(row.preset_id)
            row.preset_id = str(preset_id)
            if previous_id != preset_id:
                self._event(
                    session,
                    "client_preset_unassigned",
                    now,
                    client_id=client_id,
                    preset_id=previous_id,
                )
            self._event(
                session,
                "client_preset_assigned",
                now,
                client_id=client_id,
                preset_id=preset_id,
            )
        return self.get_client(client_id)

    def issue_credential(
        self,
        client_id: UUID,
        *,
        lifetime: timedelta | None = DEFAULT_TOKEN_LIFETIME,
    ) -> McpIssuedCredential:
        now = self._clock()
        token, digest = self._new_token()
        credential_id = uuid4()
        expires_at = self._expiry(now, lifetime)
        with self._session_factory.begin() as session:
            self._require_client(session, client_id)
            row = McpCredentialRow(
                id=str(credential_id),
                client_id=str(client_id),
                token_digest=digest,
                created_at=_to_text(now),
                expires_at=None if expires_at is None else _to_text(expires_at),
                revoked_at=None,
                last_used_at=None,
            )
            session.add(row)
            self._event(
                session,
                "credential_issued",
                now,
                client_id=client_id,
                credential_id=credential_id,
            )
        return McpIssuedCredential(
            credential=self._credential_read(row, now), token=token
        )

    def revoke_credential(
        self, client_id: UUID, credential_id: UUID
    ) -> McpCredentialRead:
        now = self._clock()
        with self._session_factory.begin() as session:
            row = self._require_credential(session, client_id, credential_id)
            if row.revoked_at is None:
                row.revoked_at = _to_text(now)
                self._event(
                    session,
                    "credential_revoked",
                    now,
                    client_id=client_id,
                    credential_id=credential_id,
                )
        return self._credential_read(row, now)

    def rotate_credential(
        self,
        client_id: UUID,
        credential_id: UUID,
        *,
        lifetime: timedelta | None = DEFAULT_TOKEN_LIFETIME,
    ) -> McpIssuedCredential:
        now = self._clock()
        token, digest = self._new_token()
        new_id = uuid4()
        expires_at = self._expiry(now, lifetime)
        with self._session_factory.begin() as session:
            old = self._require_credential(session, client_id, credential_id)
            if old.revoked_at is not None:
                raise McpAdministrationError("mcp_credential_not_active")
            old_expiry = _from_text(old.expires_at)
            if old_expiry is not None and old_expiry <= now:
                raise McpAdministrationError("mcp_credential_not_active")
            old.revoked_at = _to_text(now)
            new = McpCredentialRow(
                id=str(new_id),
                client_id=str(client_id),
                token_digest=digest,
                created_at=_to_text(now),
                expires_at=None if expires_at is None else _to_text(expires_at),
                revoked_at=None,
                last_used_at=None,
            )
            session.add(new)
            self._event(
                session,
                "credential_rotated",
                now,
                client_id=client_id,
                credential_id=new_id,
            )
        return McpIssuedCredential(
            credential=self._credential_read(new, now), token=token
        )

    def grant_target(self, client_id: UUID, target_id: UUID) -> McpClientRead:
        now = self._clock()
        with self._session_factory.begin() as session:
            self._require_client(session, client_id)
            self._require_mcp_target(session, target_id)
            key = {"client_id": str(client_id), "target_id": str(target_id)}
            if session.get(McpClientTargetGrantRow, key) is None:
                session.add(McpClientTargetGrantRow(**key, created_at=_to_text(now)))
                self._event(
                    session,
                    "target_granted",
                    now,
                    client_id=client_id,
                    target_id=target_id,
                )
        return self.get_client(client_id)

    def remove_target_grant(self, client_id: UUID, target_id: UUID) -> McpClientRead:
        now = self._clock()
        with self._session_factory.begin() as session:
            self._require_client(session, client_id)
            key = {"client_id": str(client_id), "target_id": str(target_id)}
            grant = session.get(McpClientTargetGrantRow, key)
            if grant is not None:
                session.delete(grant)
                self._event(
                    session,
                    "target_grant_removed",
                    now,
                    client_id=client_id,
                    target_id=target_id,
                )
        return self.get_client(client_id)

    def grant_development_source(
        self, client_id: UUID, source_id: UUID
    ) -> McpClientRead:
        """Grant one exact registered source; no wildcard form exists."""

        now = self._clock()
        with self._session_factory.begin() as session:
            self._require_client(session, client_id)
            if session.get(DevelopmentSourceRow, str(source_id)) is None:
                raise McpAdministrationError("development_source_not_found")
            key = {
                "client_id": str(client_id),
                "development_source_id": str(source_id),
            }
            if session.get(McpClientDevelopmentSourceGrantRow, key) is not None:
                raise McpAdministrationError("mcp_development_source_already_granted")
            session.add(
                McpClientDevelopmentSourceGrantRow(**key, created_at=_to_text(now))
            )
            self._event(
                session,
                "development_source_granted",
                now,
                client_id=client_id,
                development_source_id=source_id,
            )
        return self.get_client(client_id)

    def remove_development_source_grant(
        self, client_id: UUID, source_id: UUID
    ) -> McpClientRead:
        """Revoke source authority without deleting durable workspace bindings."""

        now = self._clock()
        with self._session_factory.begin() as session:
            self._require_client(session, client_id)
            key = {
                "client_id": str(client_id),
                "development_source_id": str(source_id),
            }
            grant = session.get(McpClientDevelopmentSourceGrantRow, key)
            if grant is not None:
                session.delete(grant)
                self._event(
                    session,
                    "development_source_grant_removed",
                    now,
                    client_id=client_id,
                    development_source_id=source_id,
                )
        return self.get_client(client_id)

    def list_eligible_unbound_workspaces(
        self, client_id: UUID
    ) -> list[McpEligibleWorkspaceRead]:
        """List only ready, byte-present, granted-source workspaces with no owner."""

        with self._session_factory() as session:
            self._require_client(session, client_id)
            rows = session.execute(
                select(AgentWorkspaceRow, DevelopmentSourceRow.logical_name)
                .join(
                    McpClientDevelopmentSourceGrantRow,
                    McpClientDevelopmentSourceGrantRow.development_source_id
                    == AgentWorkspaceRow.development_source_id,
                )
                .join(
                    DevelopmentSourceRow,
                    DevelopmentSourceRow.id == AgentWorkspaceRow.development_source_id,
                )
                .outerjoin(
                    McpAgentWorkspaceBindingRow,
                    McpAgentWorkspaceBindingRow.workspace_id == AgentWorkspaceRow.id,
                )
                .where(
                    McpClientDevelopmentSourceGrantRow.client_id == str(client_id),
                    McpAgentWorkspaceBindingRow.workspace_id.is_(None),
                    AgentWorkspaceRow.state == "ready",
                    AgentWorkspaceRow.bytes_state == "present",
                )
                .order_by(AgentWorkspaceRow.created_at, AgentWorkspaceRow.id)
                .limit(1001)
            ).all()
        if len(rows) > 1000:
            raise McpAdministrationError("mcp_workspace_result_too_large")
        return [
            McpEligibleWorkspaceRead(
                workspace_id=UUID(workspace.id),
                development_source_id=UUID(workspace.development_source_id),
                source_logical_name=logical_name,
                state=workspace.state.value,
                bytes_state=workspace.bytes_state.value,
                generation=workspace.generation,
                created_at=_from_required_text(workspace.created_at),
            )
            for workspace, logical_name in rows
        ]

    def bind_workspace(
        self, client_id: UUID, workspace_id: UUID
    ) -> McpWorkspaceBindingRead:
        """Bind one eligible existing workspace; ownership cannot be transferred."""

        now = self._clock()
        with self._session_factory.begin() as session:
            self._require_client(session, client_id)
            workspace = session.get(AgentWorkspaceRow, str(workspace_id))
            if workspace is None:
                raise McpAdministrationError("agent_workspace_not_found")
            if session.get(McpAgentWorkspaceBindingRow, str(workspace_id)) is not None:
                raise McpAdministrationError("mcp_workspace_already_bound")
            grant = session.get(
                McpClientDevelopmentSourceGrantRow,
                {
                    "client_id": str(client_id),
                    "development_source_id": workspace.development_source_id,
                },
            )
            if (
                grant is None
                or workspace.state.value != "ready"
                or workspace.bytes_state.value != "present"
            ):
                raise McpAdministrationError("mcp_workspace_not_eligible")
            row = McpAgentWorkspaceBindingRow(
                workspace_id=str(workspace_id),
                client_id=str(client_id),
                development_source_id=workspace.development_source_id,
                binding_origin="web_admin",
                created_at=_to_text(now),
            )
            session.add(row)
            self._event(
                session,
                "workspace_bound",
                now,
                client_id=client_id,
                development_source_id=UUID(workspace.development_source_id),
                workspace_id=workspace_id,
            )
        return self._binding_read(row)

    def list_workspace_bindings(self, client_id: UUID) -> list[McpWorkspaceBindingRead]:
        with self._session_factory() as session:
            self._require_client(session, client_id)
            rows = session.scalars(
                select(McpAgentWorkspaceBindingRow)
                .where(McpAgentWorkspaceBindingRow.client_id == str(client_id))
                .order_by(McpAgentWorkspaceBindingRow.workspace_id)
                .limit(1001)
            ).all()
        if len(rows) > 1000:
            raise McpAdministrationError("mcp_workspace_result_too_large")
        return [self._binding_read(row) for row in rows]

    def assign_target_preset(
        self, target_id: UUID, preset_id: UUID
    ) -> McpTargetPolicyRead:
        now = self._clock()
        with self._session_factory.begin() as session:
            self._require_mcp_target(session, target_id)
            self._require_assignable_preset(session, preset_id)
            row = session.get(McpTargetPresetAssignmentRow, str(target_id))
            if row is None:
                session.add(
                    McpTargetPresetAssignmentRow(
                        target_id=str(target_id),
                        preset_id=str(preset_id),
                        assigned_at=_to_text(now),
                    )
                )
            else:
                previous_id = UUID(row.preset_id)
                row.preset_id = str(preset_id)
                row.assigned_at = _to_text(now)
                if previous_id != preset_id:
                    self._event(
                        session,
                        "target_preset_unassigned",
                        now,
                        target_id=target_id,
                        preset_id=previous_id,
                    )
            self._event(
                session,
                "target_preset_assigned",
                now,
                target_id=target_id,
                preset_id=preset_id,
            )
        return self.get_target_policy(target_id)

    def list_target_policies(self) -> list[McpTargetPolicyRead]:
        with self._session_factory() as session:
            targets = session.scalars(
                select(TargetRow)
                .where(
                    TargetRow.target_type.in_(
                        [TargetType.MANAGED, TargetType.EXTERNAL_DEVELOPMENT]
                    )
                )
                .order_by(TargetRow.name, TargetRow.id)
            ).all()
            return [self._target_policy_read(session, row) for row in targets]

    def get_target_policy(self, target_id: UUID) -> McpTargetPolicyRead:
        with self._session_factory() as session:
            target = session.get(TargetRow, str(target_id))
            if target is None or target.target_type not in {
                TargetType.MANAGED,
                TargetType.EXTERNAL_DEVELOPMENT,
            }:
                raise McpAdministrationError("managed_target_not_found")
            if target.target_type is TargetType.EXTERNAL_DEVELOPMENT:
                external = session.get(ExternalKodiTargetRow, target.id)
                if (
                    external is None
                    or external.administrative_state
                    is ExternalAdministrativeState.DEACTIVATED
                ):
                    raise McpAdministrationError("managed_target_not_found")
            return self._target_policy_read(session, target)

    def _target_policy_read(
        self, session: Session, target: TargetRow
    ) -> McpTargetPolicyRead:
        assignment = session.get(McpTargetPresetAssignmentRow, target.id)
        preset = None
        if assignment is not None:
            preset_row = session.get(McpPresetRow, assignment.preset_id)
            if preset_row is None:
                raise RuntimeError("MCP target preset assignment is corrupt")
            preset = self._preset_read(session, preset_row)
        return McpTargetPolicyRead(
            target_id=UUID(target.id), target_name=target.name, preset=preset
        )

    @staticmethod
    def _name(value: str) -> str:
        normalized = value.strip()
        if not normalized or len(normalized) > 120:
            raise McpAdministrationError("mcp_client_name_invalid")
        return normalized

    @staticmethod
    def _preset_name(value: str) -> str:
        normalized = value.strip()
        if not normalized or len(normalized) > 120:
            raise McpAdministrationError("mcp_preset_name_invalid")
        return normalized

    @staticmethod
    def _expiry(now: datetime, lifetime: timedelta | None) -> datetime | None:
        if lifetime is None:
            return None
        if lifetime < timedelta(minutes=1) or lifetime > timedelta(days=31):
            raise McpAdministrationError("mcp_credential_lifetime_invalid")
        return now + lifetime

    def _new_token(self) -> tuple[str, str]:
        raw = self._token_bytes(32)
        if len(raw) != 32:
            raise RuntimeError("MCP token generator did not return 256 bits")
        token = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
        return token, hashlib.sha256(raw).hexdigest()

    @staticmethod
    def _require_client(session: Session, client_id: UUID) -> McpClientRow:
        row = session.get(McpClientRow, str(client_id))
        if row is None:
            raise McpAdministrationError("mcp_client_not_found")
        return row

    @staticmethod
    def _require_credential(
        session: Session, client_id: UUID, credential_id: UUID
    ) -> McpCredentialRow:
        row = session.get(McpCredentialRow, str(credential_id))
        if row is None or row.client_id != str(client_id):
            raise McpAdministrationError("mcp_credential_not_found")
        return row

    @staticmethod
    def _require_managed_target(session: Session, target_id: UUID) -> None:
        target = session.get(TargetRow, str(target_id))
        if target is None or target.target_type is not TargetType.MANAGED:
            raise McpAdministrationError("managed_target_not_found")

    @staticmethod
    def _require_mcp_target(session: Session, target_id: UUID) -> None:
        target = session.get(TargetRow, str(target_id))
        if target is None:
            raise McpAdministrationError("managed_target_not_found")
        if target.target_type is TargetType.MANAGED:
            return
        if target.target_type is TargetType.EXTERNAL_DEVELOPMENT:
            external = session.get(ExternalKodiTargetRow, str(target_id))
            if (
                external is not None
                and external.administrative_state
                is not ExternalAdministrativeState.DEACTIVATED
            ):
                return
        raise McpAdministrationError("managed_target_not_found")

    @staticmethod
    def _require_assignable_preset(session: Session, preset_id: UUID) -> McpPresetRow:
        row = session.get(McpPresetRow, str(preset_id))
        if row is None or not row.assignable:
            raise McpAdministrationError("mcp_preset_not_assignable")
        return row

    @staticmethod
    def _require_clone_template(session: Session, preset_id: UUID) -> McpPresetRow:
        row = session.get(McpPresetRow, str(preset_id))
        if row is None or not row.builtin or not row.assignable:
            raise McpAdministrationError("mcp_preset_template_invalid")
        return row

    def _preset_read(self, session: Session, row: McpPresetRow) -> McpPresetRead:
        permissions = tuple(
            session.scalars(
                select(McpPresetPermissionRow.permission)
                .where(McpPresetPermissionRow.preset_id == row.id)
                .order_by(McpPresetPermissionRow.permission)
            ).all()
        )
        return McpPresetRead(
            id=UUID(row.id),
            display_name=row.display_name,
            builtin=row.builtin,
            assignable=row.assignable,
            permissions=permissions,
        )

    def _client_read(
        self, session: Session, row: McpClientRow, now: datetime
    ) -> McpClientRead:
        preset = session.get(McpPresetRow, row.preset_id)
        if preset is None:
            raise RuntimeError("MCP client preset is corrupt")
        credentials = session.scalars(
            select(McpCredentialRow)
            .where(McpCredentialRow.client_id == row.id)
            .order_by(McpCredentialRow.created_at, McpCredentialRow.id)
        ).all()
        grants = tuple(
            UUID(value)
            for value in session.scalars(
                select(McpClientTargetGrantRow.target_id)
                .where(McpClientTargetGrantRow.client_id == row.id)
                .order_by(McpClientTargetGrantRow.target_id)
            ).all()
        )
        source_grants = tuple(
            UUID(value)
            for value in session.scalars(
                select(McpClientDevelopmentSourceGrantRow.development_source_id)
                .where(McpClientDevelopmentSourceGrantRow.client_id == row.id)
                .order_by(McpClientDevelopmentSourceGrantRow.development_source_id)
            ).all()
        )
        bindings = tuple(
            self._binding_read(item)
            for item in session.scalars(
                select(McpAgentWorkspaceBindingRow)
                .where(McpAgentWorkspaceBindingRow.client_id == row.id)
                .order_by(McpAgentWorkspaceBindingRow.workspace_id)
            ).all()
        )
        created_at = _from_text(row.created_at)
        assert created_at is not None
        return McpClientRead(
            id=UUID(row.id),
            display_name=row.display_name,
            enabled=row.enabled,
            preset=self._preset_read(session, preset),
            created_at=created_at,
            last_used_at=_from_text(row.last_used_at),
            disabled_at=_from_text(row.disabled_at),
            credentials=tuple(self._credential_read(item, now) for item in credentials),
            target_grants=grants,
            development_source_grants=source_grants,
            workspace_bindings=bindings,
        )

    def _binding_read(
        self, row: McpAgentWorkspaceBindingRow
    ) -> McpWorkspaceBindingRead:
        projection = (
            None
            if self._retention is None
            else self._retention.projection(UUID(row.workspace_id))
        )
        development = (
            None
            if self._development is None
            else self._development.projection(UUID(row.workspace_id))
        )
        return McpWorkspaceBindingRead(
            workspace_id=UUID(row.workspace_id),
            client_id=UUID(row.client_id),
            development_source_id=UUID(row.development_source_id),
            binding_origin=row.binding_origin,
            created_at=_from_required_text(row.created_at),
            owning_client_enabled=(
                True if projection is None else projection.owning_client_enabled
            ),
            retention_classification=(
                "active" if projection is None else projection.classification
            ),
            retention_deadline_at=(
                None if projection is None else projection.deadline_at
            ),
            automatic_cleanup_blocked=(
                False if projection is None else projection.automatic_cleanup_blocked
            ),
            latest_deployment_id=(
                None if development is None else development.deployment_id
            ),
            latest_deployment_target_id=(
                None if development is None else development.deployment_target_id
            ),
            latest_deployment_generation=(
                None if development is None else development.deployment_generation
            ),
            latest_deployment_state=(
                None if development is None else development.deployment_state
            ),
            latest_execution_id=(
                None if development is None else development.execution_id
            ),
            latest_execution_target_id=(
                None if development is None else development.execution_target_id
            ),
            latest_execution_generation=(
                None if development is None else development.execution_generation
            ),
            latest_execution_state=(
                None if development is None else development.execution_state
            ),
        )

    @staticmethod
    def _credential_read(row: McpCredentialRow, now: datetime) -> McpCredentialRead:
        created_at = _from_text(row.created_at)
        assert created_at is not None
        return McpCredentialRead(
            id=UUID(row.id),
            client_id=UUID(row.client_id),
            created_at=created_at,
            expires_at=_from_text(row.expires_at),
            revoked_at=_from_text(row.revoked_at),
            last_used_at=_from_text(row.last_used_at),
            status=_status(row, now),
        )

    @staticmethod
    def _event(
        session: Session,
        kind: str,
        now: datetime,
        *,
        client_id: UUID | None = None,
        credential_id: UUID | None = None,
        target_id: UUID | None = None,
        preset_id: UUID | None = None,
        development_source_id: UUID | None = None,
        workspace_id: UUID | None = None,
    ) -> None:
        session.add(
            McpSecurityEventRow(
                id=str(uuid4()),
                event_kind=kind,
                actor="web_admin",
                client_id=None if client_id is None else str(client_id),
                credential_id=(None if credential_id is None else str(credential_id)),
                target_id=None if target_id is None else str(target_id),
                preset_id=None if preset_id is None else str(preset_id),
                development_source_id=(
                    None
                    if development_source_id is None
                    else str(development_source_id)
                ),
                workspace_id=None if workspace_id is None else str(workspace_id),
                occurred_at=_to_text(now),
            )
        )


class McpAuthenticationService:
    """Resolve one 256-bit bearer token against current durable authority."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock

    def authenticate(self, token: str) -> McpPrincipal | None:
        raw = self._decode(token)
        if raw is None:
            return None
        digest = hashlib.sha256(raw).hexdigest()
        now = self._clock()
        with self._session_factory.begin() as session:
            row = session.scalar(
                select(McpCredentialRow).where(McpCredentialRow.token_digest == digest)
            )
            persisted_digest = "0" * 64 if row is None else row.token_digest
            digest_matches = hmac.compare_digest(persisted_digest, digest)
            if row is None or not digest_matches:
                return None
            client = session.get(McpClientRow, row.client_id)
            if (
                client is None
                or not client.enabled
                or row.revoked_at is not None
                or (
                    (expires_at := _from_text(row.expires_at)) is not None
                    and expires_at <= now
                )
            ):
                return None
            preset = session.get(McpPresetRow, client.preset_id)
            if preset is None or not preset.assignable:
                return None
            permissions = frozenset(
                session.scalars(
                    select(McpPresetPermissionRow.permission).where(
                        McpPresetPermissionRow.preset_id == preset.id
                    )
                ).all()
            )
            last_used = _from_text(row.last_used_at)
            if last_used is None or now - last_used >= LAST_USED_WRITE_INTERVAL:
                timestamp = _to_text(now)
                row.last_used_at = timestamp
                client.last_used_at = timestamp
            return McpPrincipal(
                client_id=UUID(client.id),
                credential_id=UUID(row.id),
                preset_id=UUID(preset.id),
                permissions=permissions,
            )

    @staticmethod
    def _decode(token: str) -> bytes | None:
        if len(token) != 43 or any(
            character
            not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
            for character in token
        ):
            return None
        try:
            raw = base64.urlsafe_b64decode(token + "=")
        except (ValueError, UnicodeEncodeError):
            return None
        return raw if len(raw) == 32 else None
