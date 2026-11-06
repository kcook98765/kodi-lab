"""Read-only clean Reset ownership checks; uncertainty has no timeout bypass."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from kodi_lab.storage.models import ManagedCleanResetRow


class CleanResetPending(RuntimeError):
    code = "managed_reset_pending"


def active_clean_reset(session: Session, target_id: str) -> ManagedCleanResetRow | None:
    return session.scalar(
        select(ManagedCleanResetRow).where(
            ManagedCleanResetRow.target_id == target_id,
            ManagedCleanResetRow.phase.not_in(("complete", "blocked")),
        )
    )


def require_no_clean_reset(session: Session, target_id: str) -> None:
    if active_clean_reset(session, target_id) is not None:
        raise CleanResetPending(
            "Resume the original clean Reset request or review history"
        )
