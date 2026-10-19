"""Correlated post-action observation; action dispatch has its own receipt."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from kodi_lab.kodi.ui_state import UiObservation


class RemotePostActionObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["pending", "succeeded", "failed", "not_retained"]
    observed_at: datetime | None = None
    error_code: (
        Literal[
            "observation_unavailable",
            "observation_not_retained",
            "observation_denied",
            "runtime_changed",
            "observation_persistence_failed",
        ]
        | None
    ) = None
    runtime_state_revision: int | None = Field(default=None, ge=0)
    ui_state: UiObservation | None = None
