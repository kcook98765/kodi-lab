"""Fixed metadata-only inspection of one exact stopped disposable container."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import UUID

import docker  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.config import Settings
from kodi_lab.domain import InstanceType, TargetType
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.storage.models import ManagedInstanceRow, TargetRow
from kodi_lab.storage.repositories import RuntimeContainerArtifactRepository


class StoppedDiagnosticsError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class StoppedDiagnosticsInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    managed_instance_id: str = Field(min_length=36, max_length=36)
    expected_runtime_revision_id: str = Field(min_length=36, max_length=36)
    expected_container_id: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("managed_instance_id", "expected_runtime_revision_id")
    @classmethod
    def canonical_uuid(cls, value: str) -> str:
        if str(UUID(value)) != value:
            raise ValueError("identity must be a canonical UUID")
        return value


class StoppedRuntimeDiagnostics(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_revision: Literal[1] = 1
    managed_instance_id: str = Field(min_length=36, max_length=36)
    runtime_revision_id: str = Field(min_length=36, max_length=36)
    container_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    image_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    container_status: Literal["created", "exited"]
    exit_code: int | None = Field(ge=0, le=2**31 - 1)
    oom_killed: bool | None
    started_at: Annotated[datetime, Field(strict=False)] | None
    finished_at: Annotated[datetime, Field(strict=False)] | None
    observed_at: Annotated[datetime, Field(strict=False)]
    cause: Literal["unknown"] = "unknown"
    log_bodies_collected: Literal[False] = False


@dataclass(frozen=True)
class StoppedDiagnosticsSpec:
    target_id: str
    revision_id: str
    container_id: str
    image_id: str


class SqliteStoppedDiagnosticsResolver:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    @classmethod
    def production(cls) -> SqliteStoppedDiagnosticsResolver:
        return cls(
            create_session_factory(create_database_engine(Settings().database_url))
        )

    def resolve(self, operation: StoppedDiagnosticsInput) -> StoppedDiagnosticsSpec:
        with self._sessions() as session:
            instance = session.get(ManagedInstanceRow, operation.managed_instance_id)
            target = session.get(TargetRow, operation.managed_instance_id)
            artifact = RuntimeContainerArtifactRepository(session).get(
                UUID(operation.expected_runtime_revision_id)
            )
            if (
                target is None
                or target.target_type is not TargetType.MANAGED
                or instance is None
                or instance.instance_type is not InstanceType.DISPOSABLE
                or instance.runtime_revision_id
                != operation.expected_runtime_revision_id
                or artifact is None
            ):
                raise StoppedDiagnosticsError("managed_diagnostics_identity_changed")
            return StoppedDiagnosticsSpec(
                operation.managed_instance_id,
                instance.runtime_revision_id,
                operation.expected_container_id,
                artifact.image_id,
            )


def _production_client() -> Any:
    return docker.APIClient(
        base_url="unix:///var/run/docker.sock", version="auto", timeout=5
    )


def _time(value: object) -> datetime | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or len(value) > 64:
        raise StoppedDiagnosticsError("managed_diagnostics_unavailable")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise StoppedDiagnosticsError("managed_diagnostics_unavailable") from error
    if parsed.tzinfo is None:
        raise StoppedDiagnosticsError("managed_diagnostics_unavailable")
    return None if parsed.year < 1970 else parsed.astimezone(UTC)


class StoppedManagedRuntimeProvider:
    def __init__(self, client_factory: Callable[[], Any] = _production_client) -> None:
        self._client_factory = client_factory

    @staticmethod
    def _snapshot(client: Any, spec: StoppedDiagnosticsSpec) -> tuple[Any, ...]:
        selected = client.containers(
            all=True,
            filters={
                "label": [
                    "io.kodi-lab.managed=true",
                    f"io.kodi-lab.instance-id={spec.target_id}",
                ]
            },
        )
        if (
            not isinstance(selected, list)
            or len(selected) != 1
            or not isinstance(selected[0], dict)
            or selected[0].get("Id") != spec.container_id
        ):
            raise StoppedDiagnosticsError("managed_diagnostics_identity_changed")
        value = client.inspect_container(spec.container_id)
        if not isinstance(value, dict):
            raise StoppedDiagnosticsError("managed_diagnostics_unavailable")
        config = value.get("Config")
        labels = config.get("Labels") if isinstance(config, dict) else None
        if (
            not isinstance(labels, dict)
            or value.get("Id") != spec.container_id
            or value.get("Image") != spec.image_id
            or value.get("Name") != f"/kodi-lab-managed-{UUID(spec.target_id).hex}"
            or labels.get("io.kodi-lab.managed") != "true"
            or labels.get("io.kodi-lab.instance-id") != spec.target_id
            or labels.get("io.kodi-lab.runtime-revision-id") != spec.revision_id
        ):
            raise StoppedDiagnosticsError("managed_diagnostics_identity_changed")
        state = value.get("State")
        if (
            not isinstance(state, dict)
            or state.get("Running") is not False
            or state.get("Status") not in {"created", "exited"}
            or state.get("Restarting") is not False
            or state.get("Paused") is not False
        ):
            raise StoppedDiagnosticsError("managed_diagnostics_not_stopped")
        exit_code = state.get("ExitCode") if state["Status"] == "exited" else None
        oom = state.get("OOMKilled")
        if (
            exit_code is not None
            and (type(exit_code) is not int or not 0 <= exit_code < 2**31)
        ) or (oom is not None and type(oom) is not bool):
            raise StoppedDiagnosticsError("managed_diagnostics_unavailable")
        return (
            state["Status"],
            exit_code,
            oom,
            _time(state.get("StartedAt")),
            _time(state.get("FinishedAt")),
        )

    def inspect(self, spec: StoppedDiagnosticsSpec) -> StoppedRuntimeDiagnostics:
        client = None
        try:
            client = self._client_factory()
            first = self._snapshot(client, spec)
            if self._snapshot(client, spec) != first:
                raise StoppedDiagnosticsError("managed_diagnostics_identity_changed")
            return StoppedRuntimeDiagnostics(
                managed_instance_id=spec.target_id,
                runtime_revision_id=spec.revision_id,
                container_id=spec.container_id,
                image_id=spec.image_id,
                container_status=first[0],
                exit_code=first[1],
                oom_killed=first[2],
                started_at=first[3],
                finished_at=first[4],
                observed_at=datetime.now(UTC),
            )
        except StoppedDiagnosticsError:
            raise
        except Exception as error:
            raise StoppedDiagnosticsError("managed_diagnostics_unavailable") from error
        finally:
            if client is not None:
                client.close()
