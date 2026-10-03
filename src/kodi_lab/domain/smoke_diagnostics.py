"""Bounded observation of one synchronous managed workspace smoke failure."""

from __future__ import annotations

import re
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SmokeDiagnosticStatus(StrEnum):
    NO_MATCH = "no_match"
    MATCHED = "matched"
    REJECTED = "rejected"
    UNAVAILABLE = "unavailable"


class AddonExceptionDiagnostic(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: SmokeDiagnosticStatus
    exception_class: str | None = Field(default=None, max_length=64)
    addon_file: str | None = Field(default=None, max_length=256)
    line_number: int | None = Field(default=None, ge=1, le=1_000_000)
    detail: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def valid_shape(self) -> AddonExceptionDiagnostic:
        # V1 emits class, file and line only. No free-form exception text crosses
        # the broker boundary, even when a future schema column can hold detail.
        if self.detail is not None:
            raise ValueError("exception detail has no approved V1 template")
        fields = (self.exception_class, self.addon_file, self.line_number)
        if self.status is SmokeDiagnosticStatus.MATCHED:
            if any(value is None for value in fields):
                raise ValueError("matched diagnostic needs a complete addon frame")
            if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", self.exception_class or ""):
                raise ValueError("invalid exception class")
            if (
                not self.addon_file
                or self.addon_file.startswith("/")
                or not self.addon_file.endswith(".py")
                or ".." in self.addon_file
                or any(
                    part == "." or re.fullmatch(r"[A-Za-z0-9_.-]+", part) is None
                    for part in self.addon_file.split("/")
                )
            ):
                raise ValueError("invalid addon-relative file")
        elif any(value is not None for value in (*fields, self.detail)):
            raise ValueError("unmatched diagnostic cannot contain exception details")
        return self

    def observation_text(self) -> str | None:
        if self.status is not SmokeDiagnosticStatus.MATCHED:
            return None
        result = (
            "Kodi logged a matching addon exception during this smoke:\n"
            f"{self.exception_class} at {self.addon_file}:{self.line_number}"
        )
        return result if self.detail is None else f"{result} — {self.detail}"
