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
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.storage.models import (
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

_BUILTINS = (
    (ADDON_DEVELOPMENT_PRESET_ID, "Add-on Development", True, ("target.inspect",)),
    (
        READ_ONLY_INSPECTION_PRESET_ID,
        "Read-only Inspection",
        True,
        ("target.inspect",),
    ),
    (FULL_LAB_CONTROL_PRESET_ID, "Full Lab Control", True, ("target.inspect",)),
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


class McpClientRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    display_name: str = Field(max_length=120)
    enabled: bool
    preset: McpPresetRead
    created_at: datetime
    last_used_at: datetime | None
    credentials: tuple[McpCredentialRead, ...]
    target_grants: tuple[UUID, ...]


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
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock
        self._token_bytes = token_bytes
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
            row.enabled = enabled
            self._event(
                session,
                "client_enabled" if enabled else "client_disabled",
                now,
                client_id=client_id,
            )
        return self.get_client(client_id)

    def assign_client_preset(self, client_id: UUID, preset_id: UUID) -> McpClientRead:
        now = self._clock()
        with self._session_factory.begin() as session:
            row = self._require_client(session, client_id)
            self._require_assignable_preset(session, preset_id)
            row.preset_id = str(preset_id)
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
            self._require_managed_target(session, target_id)
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

    def assign_target_preset(
        self, target_id: UUID, preset_id: UUID
    ) -> McpTargetPolicyRead:
        now = self._clock()
        with self._session_factory.begin() as session:
            self._require_managed_target(session, target_id)
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
                row.preset_id = str(preset_id)
                row.assigned_at = _to_text(now)
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
                select(TargetRow).order_by(TargetRow.name, TargetRow.id)
            ).all()
            return [self._target_policy_read(session, row) for row in targets]

    def get_target_policy(self, target_id: UUID) -> McpTargetPolicyRead:
        with self._session_factory() as session:
            target = session.get(TargetRow, str(target_id))
            if target is None:
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
        if target is None or str(target.target_type) not in {
            "managed",
            "TargetType.MANAGED",
        }:
            raise McpAdministrationError("managed_target_not_found")

    @staticmethod
    def _require_assignable_preset(session: Session, preset_id: UUID) -> McpPresetRow:
        row = session.get(McpPresetRow, str(preset_id))
        if row is None or not row.assignable:
            raise McpAdministrationError("mcp_preset_not_assignable")
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
        created_at = _from_text(row.created_at)
        assert created_at is not None
        return McpClientRead(
            id=UUID(row.id),
            display_name=row.display_name,
            enabled=row.enabled,
            preset=self._preset_read(session, preset),
            created_at=created_at,
            last_used_at=_from_text(row.last_used_at),
            credentials=tuple(self._credential_read(item, now) for item in credentials),
            target_grants=grants,
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
            if row is None or not hmac.compare_digest(row.token_digest, digest):
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
