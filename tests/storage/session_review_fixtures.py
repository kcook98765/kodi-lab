"""Exact 0082/0083 review data for historical-schema transition tests.

The current application requires its matched schema. Older transitions use their
actual column vocabulary; current coordinator behavior is tested at the new head.
These are data fixtures, not compatibility readers or alternate application code.
"""

import hashlib
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text

from kodi_lab.application.session_replacement_reviews import (
    ReplacementReviewSnapshot,
    ReplacementReviewStatus,
)
from kodi_lab.storage.managed_activity import timestamp
from tests.application import test_mcp_workspace_development as fixture


def save_historical_review(env: Any) -> ReplacementReviewStatus:
    operation = env.request()
    now = datetime.now(UTC)
    snapshot = ReplacementReviewSnapshot(
        request=operation,
        target_id=fixture.TARGET_ID,
        workspace_id=env.state.env.workspace_id,
        source_id=fixture.SOURCE_ID,
        addon_id="plugin.video.slice4",
        mode="fresh_test",
        observed_at=now,
    )
    payload = snapshot.model_dump_json()
    digest = hashlib.sha256(payload.encode()).hexdigest()
    review_id = uuid4()
    with env.state.env.sessions.begin() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        session.execute(
            text(
                "INSERT INTO development_session_replacement_reviews "
                "(id,request_id,session_generation,"
                "state,snapshot_json,snapshot_sha256,created_at) "
                "VALUES (:id,:request,:generation,'open',:body,:digest,:created)"
            ),
            dict(
                id=str(review_id),
                request=str(operation.request_id),
                generation=operation.expected_generation,
                body=payload,
                digest=digest,
                created=timestamp(now),
            ),
        )
        for selected in operation.evidence:
            session.execute(
                text(
                    "INSERT INTO development_session_evidence_holds "
                    "(review_id,capture_id,payload_sha256) "
                    "VALUES (:review,:capture,:digest)"
                ),
                dict(
                    review=str(review_id),
                    capture=str(selected.capture_id),
                    digest=selected.payload_sha256,
                ),
            )
    return load_historical_review(env, review_id)


def load_historical_review(env: Any, review_id: UUID) -> ReplacementReviewStatus:
    with env.state.env.sessions() as session:
        row = (
            session.execute(
                text(
                    "SELECT id,state,snapshot_json,snapshot_sha256,cancelled_at "
                    "FROM development_session_replacement_reviews WHERE id=:id"
                ),
                dict(id=str(review_id)),
            )
            .mappings()
            .one()
        )
        return ReplacementReviewStatus.model_validate(
            dict(
                review_id=row["id"],
                state=row["state"],
                snapshot_sha256=row["snapshot_sha256"],
                snapshot=ReplacementReviewSnapshot.model_validate_json(
                    row["snapshot_json"]
                ),
                cancelled_at=row["cancelled_at"],
            )
        )


def cancel_historical_review(env: Any, review_id: UUID) -> None:
    with env.state.env.sessions.begin() as session:
        session.execute(
            text(
                "UPDATE development_session_replacement_reviews "
                "SET state='cancelled',cancelled_at=:now WHERE id=:id AND state='open'"
            ),
            dict(id=str(review_id), now=timestamp(datetime.now(UTC))),
        )
