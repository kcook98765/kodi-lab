"""Exact operation-scoped Preview consent in existing installation evidence."""

from __future__ import annotations

import json
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator, model_validator
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.runtime.attestation_integrity import canonical_json
from kodi_lab.runtime.recipes import (
    HISTORICAL_PREVIEW_INSTALLATION_DIGEST,
    PREVIEW_INSTALLATION_DIGEST,
)
from kodi_lab.storage.repositories import RuntimeRecipeInstallationRepository


class PreviewAcknowledgement(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    recipe_id: Literal["piers-22.0-rc1-r1", "piers-22.0-rc1-r2"]
    recipe_revision: Literal[1, 2]
    recipe_digest: Literal[
        "sha256:9679a31a2ff4007c63e117319b7893054e475d5a24e69240a2c2e21adbfdd793",
        "sha256:75d32434f6208efc051bc71ff0d501f07e91a1fcd666eed6f8806d49d43e3454",
    ]
    release: Literal["22.0-rc.1"]
    support_tier: Literal["preview"]
    acknowledgement: Literal["development-only-prerelease"]

    @field_validator("recipe_revision", mode="before")
    @classmethod
    def _strict_revision(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("Preview revision must be an integer")
        return value

    @model_validator(mode="after")
    def _exact_reviewed_identity(self) -> PreviewAcknowledgement:
        if (self.recipe_id, self.recipe_revision, self.recipe_digest) not in {
            ("piers-22.0-rc1-r1", 1, HISTORICAL_PREVIEW_INSTALLATION_DIGEST),
            ("piers-22.0-rc1-r2", 2, PREVIEW_INSTALLATION_DIGEST),
        }:
            raise ValueError("Preview recipe identity differs")
        return self


def admission_evidence(ack: PreviewAcknowledgement, operation_id: UUID) -> str:
    if ack.recipe_digest != PREVIEW_INSTALLATION_DIGEST:
        raise ValueError("Preview recipe differs")
    return canonical_json(
        {
            "schema": "reviewed-preview-admission-v1",
            "operation_id": str(operation_id),
            "preview_acknowledgement": ack.model_dump(mode="json"),
        }
    )


def recorded_acknowledgement(
    factory: sessionmaker[Session],
    operation_id: UUID,
) -> PreviewAcknowledgement:
    with factory() as session:
        row = RuntimeRecipeInstallationRepository(session).get(operation_id)
        if (
            row is None
            or row.generation != "piers"
            or row.recipe_digest != PREVIEW_INSTALLATION_DIGEST
        ):
            raise ValueError("Preview admission missing")
        raw = row.attestation_json or ""
        value = json.loads(raw)
        if canonical_json(value) != raw or value.get("operation_id") != str(
            operation_id
        ):
            raise ValueError("Preview operation admission differs")
        return PreviewAcknowledgement.model_validate(value["preview_acknowledgement"])
