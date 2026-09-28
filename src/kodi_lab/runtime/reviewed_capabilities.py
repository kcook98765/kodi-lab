"""Exact-revision capability proof from completed reviewed recipe validation."""

from __future__ import annotations

import json
from typing import Literal
from uuid import UUID

from sqlalchemy.orm import Session

from kodi_lab.application.runtime_eligibility import (
    RuntimeRevisionEligibilityPolicy,
    RuntimeSelectionPolicy,
)
from kodi_lab.runtime.recipes import REVIEWED_SIGNED_DISTRO_SOURCE_KIND
from kodi_lab.storage.repositories import (
    RuntimeDefinitionRepository,
    RuntimeRecipeInstallationRepository,
    RuntimeRevisionRepository,
)

type ReviewedCapability = Literal["screenshot", "navigation"]


def supports_reviewed_recipe_capability(
    session: Session, revision_id: UUID, capability: ReviewedCapability
) -> bool:
    """Accept only a currently eligible exact revision and recorded test result."""

    revision = RuntimeRevisionRepository(session).get(revision_id)
    if revision is None or revision.source_kind != REVIEWED_SIGNED_DISTRO_SOURCE_KIND:
        return False
    definition = RuntimeDefinitionRepository(session).get(
        revision.runtime_definition_id
    )
    if (
        definition is None
        or not RuntimeRevisionEligibilityPolicy()
        .evaluate_for_release(
            session,
            required_release=definition.kodi_release,
            definition=definition,
            revision=revision,
            policy=RuntimeSelectionPolicy(include_legacy=True),
        )
        .eligible
    ):
        return False
    installation = RuntimeRecipeInstallationRepository(session).ready_for_revision(
        revision_id
    )
    if installation is None or installation.validation_json is None:
        return False
    try:
        report = json.loads(installation.validation_json)
    except ValueError:
        return False
    if not isinstance(report, dict) or report.get("result") != "passed":
        return False
    if capability == "navigation":
        return report.get("navigation") is True
    screenshot = report.get("screenshot")
    return (
        isinstance(screenshot, dict)
        and screenshot.get("width") == 1280
        and screenshot.get("height") == 720
        and screenshot.get("software_renderer") == "llvmpipe"
    )
