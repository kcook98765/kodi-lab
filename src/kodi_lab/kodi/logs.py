"""Closed, bounded Kodi log request and response contracts."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class KodiLogRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    log: Literal["current", "previous"] = "current"
    mode: Literal["stats", "tail"] = "stats"
    max_lines: int = Field(default=200, ge=1, le=500)
    max_bytes: int = Field(default=8192, ge=1, le=8192)
    before: int | None = Field(default=None, ge=0, le=2**63 - 1)
    snapshot_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def require_snapshot_for_paging(self) -> "KodiLogRequest":
        if self.before is not None and (
            self.mode != "tail" or self.snapshot_id is None
        ):
            raise ValueError("paging requires tail mode and a snapshot identity")
        return self


class KodiLogResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    log: Literal["current", "previous"]
    mode: Literal["stats", "tail"]
    # Logical Kodi location only; never expose a host filesystem path.
    location: Literal["special://logpath/kodi.log", "special://logpath/kodi.old.log"]
    snapshot_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0, le=2**63 - 1)
    modified_at_ns: int = Field(ge=0)
    observed_at: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")
    start_byte: int = Field(ge=0)
    end_byte: int = Field(ge=0)
    bytes_read: int = Field(ge=0, le=8192)
    lines_returned: int = Field(ge=0, le=500)
    truncated: bool
    partial_first_line: bool
    content: str = Field(max_length=8192)

    def require_request(self, request: KodiLogRequest) -> None:
        """Do not publish a signed but contradictory or oversized response."""
        if (
            self.log != request.log
            or self.mode != request.mode
            or self.location
            != (
                "special://logpath/kodi.log"
                if request.log == "current"
                else "special://logpath/kodi.old.log"
            )
            or self.start_byte > self.end_byte
            or self.end_byte > self.size_bytes
            or self.bytes_read != self.end_byte - self.start_byte
            or self.bytes_read > request.max_bytes
            or len(self.content) > self.bytes_read
            or self.lines_returned > request.max_lines
            or self.lines_returned != len(self.content.splitlines())
            or self.truncated
            != (
                request.mode == "tail"
                and (self.start_byte > 0 or self.end_byte < self.size_bytes)
            )
            or (
                request.snapshot_id is not None
                and self.snapshot_id != request.snapshot_id
            )
            or (request.before is not None and self.end_byte != request.before)
            or (
                request.mode == "tail"
                and request.before is None
                and self.end_byte != self.size_bytes
            )
            or (self.partial_first_line and self.start_byte == 0)
            or (
                request.mode == "stats"
                and (
                    self.content
                    or self.bytes_read
                    or self.start_byte
                    or self.end_byte
                    or self.lines_returned
                    or self.partial_first_line
                )
            )
        ):
            raise ValueError("invalid log response")


LogSeverity = Literal[
    "DEBUG", "INFO", "NOTICE", "WARNING", "ERROR", "FATAL", "SEVERE", "UNKNOWN"
]


class KodiLogScanRequest(BaseModel):
    """Public query; continuation repeats the entire query unchanged."""

    model_config = ConfigDict(extra="forbid", strict=True)

    logs: Literal["current", "previous", "both"] = "both"
    severities: list[LogSeverity] = Field(
        default_factory=lambda: list[LogSeverity](["ERROR", "FATAL"]),
        min_length=1,
        max_length=8,
    )
    text: str | None = Field(default=None, min_length=1, max_length=256)
    start_time: str | None = Field(default=None, max_length=26)
    end_time: str | None = Field(default=None, max_length=26)
    context_records: int = Field(default=0, ge=0, le=2)
    max_matches: int = Field(default=10, ge=0, le=20)
    cursor: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_query(self) -> "KodiLogScanRequest":
        import re

        if len(set(self.severities)) != len(self.severities):
            raise ValueError("duplicate severity")
        if self.text is not None:
            self.text.encode("utf-8")
        for value in (self.start_time, self.end_time):
            if value is not None:
                if (
                    re.fullmatch(
                        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?", value
                    )
                    is None
                ):
                    raise ValueError("use a local timestamp without a timezone")
                datetime.fromisoformat(value)
        if (
            self.start_time is not None
            and self.end_time is not None
            and datetime.fromisoformat(self.start_time)
            > datetime.fromisoformat(self.end_time)
        ):
            raise ValueError("reversed time range")
        return self


class KodiLogScanBridgeRequest(KodiLogScanRequest):
    """Lab supplies this binding; it is absent from the public MCP schema."""

    client_binding: str = Field(pattern=r"^[0-9a-f]{64}$")


class KodiLogScanMatch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    log: Literal["current", "previous"]
    start_byte: int = Field(ge=0)
    end_byte: int = Field(ge=0)
    severity: LogSeverity
    timestamp: str | None = Field(max_length=26)
    content: str = Field(max_length=1024)
    content_truncated: bool
    before: list[str] = Field(max_length=2)
    after: list[str] = Field(max_length=2)


class KodiLogScanFile(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    log: Literal["current", "previous"]
    file_id: str | None = Field(pattern=r"^[0-9a-f]{64}$")
    observed_at: str
    boundary_bytes: int = Field(ge=0, le=2**63 - 1)
    scanned_bytes: int = Field(ge=0, le=2**63 - 1)
    verified_bytes: int = Field(ge=0, le=2**63 - 1)
    state: Literal[
        "scanning", "verifying", "complete", "changed", "missing", "unavailable"
    ]
    counts_exact: bool
    records_scanned: int = Field(ge=0)
    matched_records: int = Field(ge=0)
    severity_counts: dict[LogSeverity, int]
    unknown_timestamp_records: int = Field(ge=0)
    append_observed: bool


class KodiLogScanResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    files: list[KodiLogScanFile] = Field(min_length=1, max_length=2)
    matches: list[KodiLogScanMatch] = Field(max_length=20)
    complete: bool
    counts_exact: bool
    excerpts_truncated: bool
    reason: Literal[
        "complete",
        "byte_budget",
        "time_budget",
        "file_changed",
        "file_unavailable",
        "aborted",
    ]
    cursor: str | None = Field(pattern=r"^[0-9a-f]{64}$")
    bytes_read: int = Field(ge=0, le=8 * 1024 * 1024)
    count_unit: Literal["logical_records"]

    def require_request(self, request: KodiLogScanRequest) -> None:
        selected = ["current", "previous"] if request.logs == "both" else [request.logs]
        successful = all(f.state == "complete" for f in self.files)
        terminal = all(f.state not in {"scanning", "verifying"} for f in self.files)
        if (
            [f.log for f in self.files] != selected
            or self.complete != successful
            or self.counts_exact != successful
            or (self.cursor is None) != terminal
            or (self.reason == "complete") != successful
            or len(self.matches) > request.max_matches
        ):
            raise ValueError("invalid scan response")
        for file in self.files:
            if (
                file.scanned_bytes > file.boundary_bytes
                or file.verified_bytes > file.boundary_bytes
                or file.matched_records > file.records_scanned
                or any(n < 0 for n in file.severity_counts.values())
                or sum(file.severity_counts.values()) != file.matched_records
                or file.counts_exact != (file.state == "complete")
                or (
                    file.state == "complete"
                    and file.scanned_bytes != file.boundary_bytes
                )
                or (
                    file.state in {"scanning", "verifying", "complete"}
                    and file.file_id is None
                )
            ):
                raise ValueError("invalid file coverage")
        for match in self.matches:
            matched_file = next((f for f in self.files if f.log == match.log), None)
            if (
                matched_file is None
                or not match.start_byte < match.end_byte <= matched_file.scanned_bytes
                or match.severity not in request.severities
                or len(match.before) > request.context_records
                or len(match.after) > request.context_records
                or any(len(s) > 256 for s in (*match.before, *match.after))
            ):
                raise ValueError("invalid match coverage")
        if len(self.model_dump_json().encode()) > 32768:
            raise ValueError("oversized scan response")
