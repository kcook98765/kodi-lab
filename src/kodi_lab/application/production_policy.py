"""Central production-policy revision transitions."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from kodi_lab.storage.models import (
    ExternalKodiTargetRow,
    ExternalProductionPolicyRow,
)


def _to_text(value: datetime) -> str:
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def advance_production_policy(
    policy: ExternalProductionPolicyRow, *, now: datetime
) -> None:
    """Advance one material production-policy transition exactly once."""

    policy.policy_revision += 1
    policy.updated_at = _to_text(now)


def bind_external_production_transition(
    session: Session,
    external: ExternalKodiTargetRow,
    policy: ExternalProductionPolicyRow,
    *,
    now: datetime,
) -> None:
    """Advance policy first, then bind one native-row material transition."""

    advance_production_policy(policy, now=now)
    # The database guard on the native row requires the already-durable policy
    # revision.  Restrict this flush to the policy so other dirty aggregate rows
    # remain part of the surrounding atomic transaction.
    session.flush([policy])
    external.production_policy_revision = policy.policy_revision
