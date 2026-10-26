"""Closed managed add-on settings request and value-free outcome contracts."""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StringConstraints,
    model_validator,
)

SettingValue = (
    StrictBool
    | StrictInt
    | StrictFloat
    | Annotated[str, StringConstraints(strict=True, max_length=1024)]
)


class AddonSettingWrite(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    id: str = Field(strict=True, pattern=r"^[a-z][a-z0-9_.-]{0,63}$")
    value: SettingValue = Field(repr=False)


class AddonSettingsSetInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    request_id: UUID = Field(strict=False)
    expected_session_generation: int = Field(strict=True, gt=0)
    expected_workspace_generation: int = Field(strict=True, ge=0)
    settings: tuple[AddonSettingWrite, ...] = Field(
        min_length=1, max_length=32, repr=False
    )

    @model_validator(mode="after")
    def unique_identifiers(self) -> AddonSettingsSetInput:
        if len({item.id for item in self.settings}) != len(self.settings):
            raise ValueError("duplicate setting identifiers")
        return self


class AddonSettingOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(strict=True, pattern=r"^[a-z][a-z0-9_.-]{0,63}$")
    status: Literal[
        "verified",
        "applied_readback_unavailable",
        "applied_unverified",
        "invalid_or_unknown",
        "invalid_type",
        "invalid_range",
        "unsupported_type",
        "denied",
        "internal_failure",
        "write_unconfirmed",
        "not_attempted",
    ]


class AddonSettingsSetResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: int = 1
    operation_id: UUID
    request_id: UUID
    workspace_id: UUID
    target_id: UUID
    session_generation: int
    generation: int
    state: Literal[
        "pending", "dispatching", "completed", "failed", "ambiguous", "contained"
    ]
    replayed: bool = False
    results: tuple[AddonSettingOutcome, ...] = Field(max_length=32)
    code: str | None = Field(default=None, max_length=64)
    atomic: Literal[False] = False
    reload_note: Literal[
        "Some add-ons read settings only at startup; reload separately if needed."
    ] = "Some add-ons read settings only at startup; reload separately if needed."


class AddonSettingsResultInput(BaseModel):
    """Recover a value-free original receipt, never another write or live read."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    request_id: UUID = Field(strict=False)
