"""Fixed internal log tails from one unchanged stopped disposable container.

No exec, start, mount, host path, arbitrary archive selection or log mutation.
Use bounded raw HTTP reads: the SDK get_archive stream disables socket timeouts.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import tarfile
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from kodi_lab.broker.kodi_logs import _normalize_controls, _redact_obvious_secrets
from kodi_lab.broker.stopped_diagnostics import (
    SqliteStoppedDiagnosticsResolver,
    StoppedDiagnosticsError,
    StoppedDiagnosticsInput,
    StoppedDiagnosticsSpec,
    StoppedManagedRuntimeProvider,
    StoppedRuntimeDiagnostics,
    _production_client,
    _time,
)
from kodi_lab.storage.repositories import RuntimeExecutionProfileRepository

MAX_SOURCE_BYTES = 1024 * 1024
MAX_ARCHIVE_BYTES = MAX_SOURCE_BYTES + 64 * 1024
MAX_TAIL_BYTES = 4096
DEADLINE_SECONDS = 10.0
LOG_PATHS = {
    "current": "/home/kodi/.kodi/temp/kodi.log",
    "previous": "/home/kodi/.kodi/temp/kodi.old.log",
    "stdout": "/home/kodi/.kodi/kodi-stdout.log",
    "xvfb": "/home/kodi/.kodi/xvfb.log",
}
_DIR = 1 << 31  # Docker PathStat uses Go's FileMode, not POSIX st_mode.
_TYPE_BITS = 0xFFFFFE00


class StoppedLogArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    kind: Literal["current", "previous", "stdout", "xvfb"]
    state: Literal[
        "available", "missing", "too_large", "unsafe", "changed", "unavailable"
    ]
    source_size_bytes: int | None = Field(default=None, ge=0, le=2**63 - 1)
    modified_at: Annotated[datetime, Field(strict=False)] | None = None
    content: str = Field(default="", max_length=MAX_TAIL_BYTES)
    source_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    tail_truncated: bool = False
    partial_first_line: bool = False
    rotation_state: Literal["not_observed"] = "not_observed"

    @model_validator(mode="after")
    def bounded_content(self) -> StoppedLogArtifact:
        if len(self.content.encode("utf-8")) > MAX_TAIL_BYTES:
            raise ValueError("tail byte limit exceeded")
        if self.state != "available" and (
            self.content
            or self.source_sha256
            or self.tail_truncated
            or self.partial_first_line
        ):
            raise ValueError("unavailable artifact cannot carry content")
        if self.state == "available" and (
            self.source_sha256 is None or self.source_size_bytes is None
        ):
            raise ValueError("available artifact requires source evidence")
        return self


class StoppedLogSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_revision: Literal[1] = 1
    scope: Literal["fixed_internal_log_tails"] = "fixed_internal_log_tails"
    exit_metadata: StoppedRuntimeDiagnostics
    artifacts: list[StoppedLogArtifact] = Field(min_length=4, max_length=4)
    observed_at: Annotated[datetime, Field(strict=False)]
    redaction_policy: Literal["obvious-secrets-v1"] = "obvious-secrets-v1"
    crash_reports: Literal["not_collected"] = "not_collected"
    docker_output: Literal["not_collected"] = "not_collected"
    cross_file_consistency: Literal["not_established"] = "not_established"

    @model_validator(mode="after")
    def fixed_inventory(self) -> StoppedLogSnapshot:
        if [a.kind for a in self.artifacts] != list(LOG_PATHS):
            raise ValueError("fixed artifact inventory required")
        return self


class SqliteStoppedLogResolver(SqliteStoppedDiagnosticsResolver):
    def resolve(self, operation: StoppedDiagnosticsInput) -> StoppedDiagnosticsSpec:
        spec = super().resolve(operation)
        with self._sessions() as session:
            profile = RuntimeExecutionProfileRepository(session).get(
                UUID(spec.revision_id)
            )
            if (
                profile is None
                or profile.runtime_uid != 1000
                or profile.runtime_gid != 1000
            ):
                raise StoppedDiagnosticsError(
                    "managed_diagnostics_log_layout_unsupported"
                )
        return spec


class _ArtifactRefusal(Exception):
    def __init__(self, state: str, size: int | None = None) -> None:
        self.state, self.size = state, size


def _tail(data: bytes) -> tuple[str, bool, bool]:
    # Redact the complete bounded input before cutting a key away from its value.
    clean = _normalize_controls(
        _redact_obvious_secrets(data.decode("utf-8", "replace"))
    )
    encoded = clean.encode("utf-8")
    truncated = len(encoded) > MAX_TAIL_BYTES
    fragment = encoded[-MAX_TAIL_BYTES:].decode("utf-8", "ignore")
    partial = truncated and encoded[-MAX_TAIL_BYTES - 1 : -MAX_TAIL_BYTES] != b"\n"
    if partial and "\n" in fragment:
        fragment = fragment.split("\n", 1)[1]
        partial = False
    return fragment, truncated, partial


class StoppedManagedLogProvider:
    def __init__(
        self,
        client_factory: Callable[[], Any] = _production_client,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._factory, self._clock = client_factory, clock

    def capture(self, spec: StoppedDiagnosticsSpec) -> StoppedLogSnapshot:
        deadline = self._clock() + DEADLINE_SECONDS
        client = None

        def budget() -> float:
            left = deadline - self._clock()
            if left <= 0:
                raise StoppedDiagnosticsError("managed_diagnostics_deadline")
            if client is not None:
                client.timeout = min(1.0, left)
            return min(1.0, left)

        try:
            client = self._factory()
            budget()
            first = StoppedManagedRuntimeProvider._snapshot(client, spec)
            # Shared/bind-mounted profile data is outside this fixed private layer read.
            budget()
            inspected = client.inspect_container(spec.container_id)
            host = inspected.get("HostConfig")
            if (
                inspected.get("Mounts") != []
                or not isinstance(host, dict)
                or not {"Privileged", "Binds", "VolumesFrom", "PidMode"} <= host.keys()
                or host.get("Privileged") is not False
                or host.get("Binds") not in (None, [])
                or host.get("VolumesFrom") not in (None, [])
                or host.get("PidMode") != ""
            ):
                raise StoppedDiagnosticsError(
                    "managed_diagnostics_private_profile_required"
                )
            artifacts = []
            for kind, path in LOG_PATHS.items():
                try:
                    artifacts.append(
                        self._file(client, spec.container_id, kind, path, budget)
                    )
                except _ArtifactRefusal as error:
                    artifacts.append(
                        StoppedLogArtifact.model_validate(
                            dict(
                                kind=kind,
                                state=error.state,
                                source_size_bytes=error.size,
                            )
                        )
                    )
            budget()
            if StoppedManagedRuntimeProvider._snapshot(client, spec) != first:
                raise StoppedDiagnosticsError("managed_diagnostics_identity_changed")
            budget()
            now = datetime.now(UTC)
            return StoppedLogSnapshot(
                exit_metadata=StoppedRuntimeDiagnostics(
                    managed_instance_id=spec.target_id,
                    runtime_revision_id=spec.revision_id,
                    container_id=spec.container_id,
                    image_id=spec.image_id,
                    container_status=first[0],
                    exit_code=first[1],
                    oom_killed=first[2],
                    started_at=first[3],
                    finished_at=first[4],
                    observed_at=now,
                ),
                artifacts=artifacts,
                observed_at=now,
            )
        except StoppedDiagnosticsError:
            raise
        except Exception as error:
            raise StoppedDiagnosticsError("managed_diagnostics_unavailable") from error
        finally:
            if client is not None:
                client.close()

    @staticmethod
    def _stat(
        client: Any, container: str, path: str, budget: Callable[[], float]
    ) -> dict[str, Any]:
        response = client.head(
            client._url("/containers/{0}/archive", container),
            params={"path": path},
            timeout=budget(),
            allow_redirects=False,
        )
        try:
            if response.status_code == 404:
                raise _ArtifactRefusal("missing")
            if response.status_code != 200:
                raise _ArtifactRefusal("unavailable")
            encoded = response.headers.get("x-docker-container-path-stat", "")
            if not isinstance(encoded, str) or len(encoded) > 4096:
                raise _ArtifactRefusal("unsafe")
            value = json.loads(base64.b64decode(encoded, validate=True))
            if (
                not isinstance(value, dict)
                or value.get("name") != path.rsplit("/", 1)[-1]
                or value.get("linkTarget") not in ("", None)
            ):
                raise _ArtifactRefusal("unsafe")
            mode, size = value.get("mode"), value.get("size")
            if (
                type(mode) is not int
                or not 0 <= mode < 2**32
                or type(size) is not int
                or not 0 <= size < 2**63
            ):
                raise _ArtifactRefusal("unsafe")
            return value
        except _ArtifactRefusal:
            raise
        except Exception as error:
            raise _ArtifactRefusal("unavailable") from error
        finally:
            response.close()

    def _file(
        self,
        client: Any,
        container: str,
        kind: str,
        path: str,
        budget: Callable[[], float],
    ) -> StoppedLogArtifact:
        # Check every fixed ancestor without asking the daemon to archive it.
        parents = path.split("/")[1:-1]
        for end in range(1, len(parents) + 1):
            parent = self._stat(
                client, container, "/" + "/".join(parents[:end]), budget
            )
            if parent["mode"] & _TYPE_BITS != _DIR or parent["mode"] & 0o022:
                raise _ArtifactRefusal("unsafe")
        before = self._stat(client, container, path, budget)
        if before["mode"] & _TYPE_BITS or before["mode"] & 0o022:
            raise _ArtifactRefusal("unsafe")
        if before["size"] > MAX_SOURCE_BYTES:
            raise _ArtifactRefusal("too_large", before["size"])
        response = client._get(
            client._url("/containers/{0}/archive", container),
            params={"path": path},
            stream=True,
            headers={"Accept-Encoding": "identity"},
            timeout=budget(),
            allow_redirects=False,
        )
        try:
            if (
                response.status_code != 200
                or response.headers.get("Content-Encoding", "identity") != "identity"
            ):
                raise _ArtifactRefusal("unavailable")
            body = bytearray()
            while True:
                budget()
                # read1 returns at most one underlying read; do not use the SDK
                # iterator that disables timeouts for streaming attach/log APIs.
                chunk = response.raw.read1(16384, decode_content=False)
                if not chunk:
                    break
                if len(body) + len(chunk) > MAX_ARCHIVE_BYTES:
                    raise _ArtifactRefusal("too_large", before["size"])
                body.extend(chunk)
            with tarfile.open(fileobj=io.BytesIO(body), mode="r:") as archive:
                members = archive.getmembers()
                if len(members) != 1:
                    raise _ArtifactRefusal("unsafe")
                member = members[0]
                if (
                    member.name != path.rsplit("/", 1)[-1]
                    or member.type not in {tarfile.REGTYPE, tarfile.AREGTYPE}
                    or member.sparse is not None
                    or member.linkname
                    or member.uid != 1000
                    or member.gid != 1000
                    or member.mode & 0o022
                    or member.size != before["size"]
                ):
                    raise _ArtifactRefusal("unsafe")
                stream = archive.extractfile(member)
                if stream is None:
                    raise _ArtifactRefusal("unsafe")
                with stream:
                    data = stream.read(MAX_SOURCE_BYTES + 1)
                if len(data) != before["size"]:
                    raise _ArtifactRefusal("changed")
            after = self._stat(client, container, path, budget)
            if before != after:
                raise _ArtifactRefusal("changed")
            content, truncated, partial = _tail(data)
            return StoppedLogArtifact.model_validate(
                dict(
                    kind=kind,
                    state="available",
                    source_size_bytes=len(data),
                    modified_at=_time(before.get("mtime")),
                    content=content,
                    source_sha256=hashlib.sha256(data).hexdigest(),
                    tail_truncated=truncated,
                    partial_first_line=partial,
                )
            )
        except (_ArtifactRefusal, StoppedDiagnosticsError):
            raise
        except Exception as error:
            raise _ArtifactRefusal("unavailable") from error
        finally:
            response.close()
