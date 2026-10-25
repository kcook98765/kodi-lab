"""Fixed internal log tails from one unchanged stopped disposable container.

No lifecycle dispatch, host filesystem access or arbitrary artifact selection.
Use bounded raw HTTP reads: the SDK get_archive stream disables socket timeouts.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import tarfile
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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
MAX_DOCKER_WINDOW_BYTES = 64 * 1024
MAX_CRASH_REFERENCES = 2
type LogKind = Literal["current", "previous", "stdout", "xvfb", "docker_output"]
_REPORT_NAME = re.compile(r"kodi_crashlog-([0-9]{8}_[0-9]{6})\.log")
_REPORT_LINE = re.compile(
    r"^(?:[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]+(?:Z|[+-][0-9:]+) )?"
    r"Crash report available at /home/kodi/"
    r"(?P<name>kodi_crashlog-[0-9]{8}_[0-9]{6}\.log)$"
)
ALL_ARTIFACTS: tuple[LogKind, ...] = (
    "current",
    "previous",
    "stdout",
    "xvfb",
    "docker_output",
)
_REFERENCE_PRIORITY: tuple[LogKind, ...] = (
    "docker_output",
    "stdout",
    "current",
    "previous",
    "xvfb",
)
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
    kind: LogKind
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


class ReferencedCrashReportMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    basename: str = Field(pattern=r"^kodi_crashlog-[0-9]{8}_[0-9]{6}\.log$")
    state: Literal["available", "missing", "unsafe", "changed", "unavailable"]
    referenced_by: list[LogKind] = Field(min_length=1, max_length=5)
    relationship: Literal["log_reported_reference"] = "log_reported_reference"
    source_size_bytes: int | None = Field(default=None, ge=0, le=2**63 - 1)
    modified_at: Annotated[datetime, Field(strict=False)] | None = None
    body: Literal["not_collected"] = "not_collected"
    filename_timestamp_zone: Literal["not_established"] = "not_established"

    @field_validator("basename")
    @classmethod
    def calendar_name(cls, value: str) -> str:
        stamp = value.removeprefix("kodi_crashlog-").removesuffix(".log")
        datetime.strptime(stamp, "%Y%m%d_%H%M%S")
        return value

    @model_validator(mode="after")
    def observed_metadata(self) -> ReferencedCrashReportMetadata:
        if self.state == "available" and self.source_size_bytes is None:
            raise ValueError("available report metadata requires observed size")
        if self.state != "available" and (
            self.source_size_bytes is not None or self.modified_at is not None
        ):
            raise ValueError("unavailable report cannot retain file metadata")
        if len(set(self.referenced_by)) != len(self.referenced_by):
            raise ValueError("duplicate reference source")
        if self.modified_at is not None and (
            self.modified_at.tzinfo is None or self.modified_at.utcoffset() is None
        ):
            raise ValueError("observed modification time must be timezone-aware")
        return self


def _crash_references(
    artifacts: list[StoppedLogArtifact],
) -> tuple[dict[str, list[LogKind]], bool]:
    """Only complete retained lines with the exact launcher phrase and fixed name.

    This is a bounded log claim, not directory discovery or native causality.
    Filename calendar validity says nothing about its timezone or actual event.
    """
    found: dict[str, list[LogKind]] = {}
    by_kind = {artifact.kind: artifact for artifact in artifacts}
    # Prefer the end of captured startup windows, without treating filename
    # dates or cross-file ordering as native event chronology.
    for kind in _REFERENCE_PRIORITY:
        artifact = by_kind.get(kind)
        if artifact is None:
            continue
        if artifact.state != "available":
            continue
        lines = artifact.content.splitlines()
        if artifact.partial_first_line:
            lines = lines[1:]
        for line in reversed(lines):
            matched = _REPORT_LINE.fullmatch(line)
            if matched is None:
                continue
            name = matched.group("name")
            date_match = _REPORT_NAME.fullmatch(name)
            assert date_match is not None
            try:
                datetime.strptime(date_match.group(1), "%Y%m%d_%H%M%S")
            except ValueError:
                continue
            sources = found.setdefault(name, [])
            if artifact.kind not in sources:
                sources.append(artifact.kind)
    names = list(found)
    return {
        name: [kind for kind in ALL_ARTIFACTS if kind in found[name]]
        for name in names[:MAX_CRASH_REFERENCES]
    }, len(names) > MAX_CRASH_REFERENCES


class StoppedLogSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_revision: Literal[1, 2, 3] = 2
    scope: Literal[
        "fixed_internal_log_tails",
        "fixed_internal_and_docker_log_tails",
        "fixed_log_tails_and_referenced_crash_metadata",
    ] = "fixed_internal_and_docker_log_tails"
    exit_metadata: StoppedRuntimeDiagnostics
    artifacts: list[StoppedLogArtifact] = Field(min_length=4, max_length=5)
    observed_at: Annotated[datetime, Field(strict=False)]
    redaction_policy: Literal["obvious-secrets-v1"] = "obvious-secrets-v1"
    crash_reports: Literal["not_collected"] = "not_collected"
    docker_output: Literal["not_collected", "bounded_window"] = "bounded_window"
    cross_file_consistency: Literal["not_established"] = "not_established"
    crash_report_metadata: list[ReferencedCrashReportMetadata] = Field(
        default_factory=list, max_length=MAX_CRASH_REFERENCES
    )
    crash_report_discovery: Literal[
        "not_collected", "bounded_retained_log_references"
    ] = "not_collected"
    crash_report_reference_limit_exceeded: bool = False
    unreferenced_crash_reports: Literal["not_observed"] = "not_observed"

    @model_validator(mode="after")
    def fixed_inventory(self) -> StoppedLogSnapshot:
        expected = (
            (list(LOG_PATHS), "fixed_internal_log_tails", "not_collected")
            if self.schema_revision == 1
            else (
                list(ALL_ARTIFACTS),
                "fixed_log_tails_and_referenced_crash_metadata"
                if self.schema_revision == 3
                else "fixed_internal_and_docker_log_tails",
                "bounded_window",
            )
        )
        if (
            [a.kind for a in self.artifacts],
            self.scope,
            self.docker_output,
        ) != expected:
            raise ValueError("versioned fixed artifact inventory required")
        if self.schema_revision < 3:
            if (
                self.crash_report_metadata
                or self.crash_report_discovery != "not_collected"
                or self.crash_report_reference_limit_exceeded
            ):
                raise ValueError("older snapshot cannot claim crash report discovery")
        else:
            expected_refs, exceeded = _crash_references(self.artifacts)
            if self.crash_report_discovery != "bounded_retained_log_references" or (
                {
                    item.basename: item.referenced_by
                    for item in self.crash_report_metadata
                }
                != expected_refs
                or [item.basename for item in self.crash_report_metadata]
                != list(expected_refs)
                or self.crash_report_reference_limit_exceeded != exceeded
            ):
                raise ValueError(
                    "report metadata must match bounded retained references"
                )
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
                or host.get("Mounts") not in (None, [])
                or host.get("Tmpfs") not in (None, {})
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
            try:
                artifacts.append(
                    self._docker_window(client, spec.container_id, inspected, budget)
                )
            except _ArtifactRefusal as error:
                artifacts.append(
                    StoppedLogArtifact.model_validate(
                        dict(
                            kind="docker_output",
                            state=error.state,
                            source_size_bytes=error.size,
                        )
                    )
                )
            references, reference_limit_exceeded = _crash_references(artifacts)
            reports = [
                self._report_metadata(client, spec.container_id, name, sources, budget)
                for name, sources in references.items()
            ]
            budget()
            if StoppedManagedRuntimeProvider._snapshot(client, spec) != first:
                raise StoppedDiagnosticsError("managed_diagnostics_identity_changed")
            budget()
            now = datetime.now(UTC)
            return StoppedLogSnapshot(
                schema_revision=3,
                scope="fixed_log_tails_and_referenced_crash_metadata",
                crash_report_metadata=reports,
                crash_report_discovery="bounded_retained_log_references",
                crash_report_reference_limit_exceeded=reference_limit_exceeded,
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

    def _report_metadata(
        self,
        client: Any,
        container: str,
        name: str,
        sources: list[LogKind],
        budget: Callable[[], float],
    ) -> ReferencedCrashReportMetadata:
        # name comes only from the closed server-side parser, never caller input.
        path = "/home/kodi/" + name
        state = "unavailable"
        try:
            for parent in ("/home", "/home/kodi"):
                stat = self._stat(client, container, parent, budget)
                if stat["mode"] & _TYPE_BITS != _DIR or stat["mode"] & 0o022:
                    raise _ArtifactRefusal("unsafe")
            before = self._stat(client, container, path, budget)
            if before["mode"] & _TYPE_BITS or before["mode"] & 0o022:
                raise _ArtifactRefusal("unsafe")
            try:
                after = self._stat(client, container, path, budget)
            except _ArtifactRefusal as error:
                if error.state == "missing":
                    raise _ArtifactRefusal("changed") from error
                raise
            if before != after:
                raise _ArtifactRefusal("changed")
            return ReferencedCrashReportMetadata(
                basename=name,
                state="available",
                referenced_by=sources,
                source_size_bytes=before["size"],
                modified_at=_time(before.get("mtime")),
            )
        except _ArtifactRefusal as error:
            state = error.state
        except StoppedDiagnosticsError:
            raise
        except Exception:
            state = "unavailable"
        return ReferencedCrashReportMetadata.model_validate(
            dict(
                basename=name,
                state=state,
                referenced_by=sources,
            )
        )

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

    @staticmethod
    def _body(response: Any, budget: Callable[[], float], limit: int) -> bytes:
        try:
            body = bytearray()
            while True:
                budget()
                chunk = response.raw.read1(
                    min(16384, limit - len(body) + 1), decode_content=False
                )
                if not chunk:
                    break
                if len(body) + len(chunk) > limit:
                    raise _ArtifactRefusal("too_large")
                body.extend(chunk)
            return bytes(body)
        finally:
            # Engine archive reads can retain the container lock until closure.
            response.close()

    def _docker_window(
        self,
        client: Any,
        container: str,
        inspected: dict[str, Any],
        budget: Callable[[], float],
    ) -> StoppedLogArtifact:
        config = inspected.get("Config")
        tty = config.get("Tty") if isinstance(config, dict) else None
        if type(tty) is not bool:
            raise _ArtifactRefusal("unavailable")
        response = client._get(
            client._url("/containers/{0}/logs", container),
            params={
                "stdout": "true",
                "stderr": "true",
                "tail": "200",
                "timestamps": "true",
                "follow": "false",
            },
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
            raw = self._body(response, budget, MAX_DOCKER_WINDOW_BYTES)
            if tty:
                data = raw
            else:
                decoded = bytearray()
                offset = 0
                while offset < len(raw):
                    if (
                        len(raw) - offset < 8
                        or raw[offset] not in (1, 2)
                        or raw[offset + 1 : offset + 4] != b"\x00\x00\x00"
                    ):
                        raise _ArtifactRefusal("unsafe")
                    size = int.from_bytes(raw[offset + 4 : offset + 8], "big")
                    offset += 8
                    if size > len(raw) - offset:
                        raise _ArtifactRefusal("unsafe")
                    decoded.extend(raw[offset : offset + size])
                    offset += size
                data = bytes(decoded)
            content, truncated, partial = _tail(data)
            return StoppedLogArtifact(
                kind="docker_output",
                state="available",
                source_size_bytes=len(data),
                content=content,
                source_sha256=hashlib.sha256(data).hexdigest(),
                tail_truncated=truncated,
                partial_first_line=partial,
            )
        except (_ArtifactRefusal, StoppedDiagnosticsError):
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
            body = self._body(response, budget, MAX_ARCHIVE_BYTES)
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
