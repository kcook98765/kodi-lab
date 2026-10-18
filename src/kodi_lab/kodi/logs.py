"""Closed, bounded Kodi log request and response contracts."""

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
