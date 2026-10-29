"""Historical copied failures need review guidance before another deployment."""

from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import event

from kodi_lab.application.workspace_deployment_preflight import (
    WorkspaceDeploymentPreflightService,
)
from kodi_lab.domain import DevelopmentSourceDeploymentConfidence
from kodi_lab.storage.managed_activity import timestamp
from kodi_lab.storage.models import (
    AgentWorkspaceRow,
    DevelopmentSourceDeploymentRow,
    McpWorkspaceDeploymentOperationRow,
)
from tests.application import test_mcp_workspace_development as fixture
from tests.application.test_workspace_deployment_preflight import operation, reasons
from tests.application.test_workspace_deployment_recovery import inventory_failure


def later_attempt(env: Any, original: Any, **changes: Any) -> None:
    values = {
        column.name: getattr(original, column.name)
        for column in McpWorkspaceDeploymentOperationRow.__table__.columns
    }
    values.update(
        id=str(uuid4()),
        request_id=str(uuid4()),
        requested_at=timestamp(fixture.NOW + timedelta(minutes=1)),
        completed_at=timestamp(fixture.NOW + timedelta(minutes=2)),
        **changes,
    )
    with env.sessions.begin() as session:
        session.add(McpWorkspaceDeploymentOperationRow(**values))


@pytest.mark.parametrize("later_conflict", [False, True])
@pytest.mark.parametrize("baseline_state", ["verified", "stale", "missing"])
def test_retained_verified_failure_guides_review_without_writes_or_copy_advice(
    tmp_path: Path, later_conflict: bool, baseline_state: str
) -> None:
    env, row = inventory_failure(tmp_path, historical=True)
    if later_conflict:
        later_attempt(
            env,
            row,
            sync_completed=False,
            target_started=False,
            failure_phase="sync",
            failure_code="development_source_replacement_conflict",
            failure_cause="development_source_replacement_conflict",
            source_manifest_digest=None,
            verified_destination_digest=None,
            result_classification=None,
        )
    if baseline_state != "verified":
        with env.sessions.begin() as session:
            baseline = session.get(
                DevelopmentSourceDeploymentRow,
                (str(fixture.TARGET_ID), str(fixture.SOURCE_ID)),
            )
            assert baseline is not None
            if baseline_state == "missing":
                session.delete(baseline)
            else:
                baseline.confidence = DevelopmentSourceDeploymentConfidence.STALE
    statements: list[str] = []

    def record(_connection: Any, _cursor: Any, sql: str, *_args: Any) -> None:
        statements.append(sql.lstrip().split()[0].upper())

    engine = env.sessions.kw["bind"]
    counts = (env.broker.stop_count, env.broker.sync_count, env.broker.start_count)
    event.listen(engine, "before_cursor_execute", record)
    try:
        result = WorkspaceDeploymentPreflightService(env.sessions).inspect(
            operation(env)
        )
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert reasons(result)["deployment_provenance"].reason == (
        "deployment_provenance_review_required"
    )
    assert reasons(result)["deployment_provenance"].state == "unknown"
    if baseline_state != "verified":
        assert (
            reasons(result)["source_baseline"].reason
            == f"source_baseline_{baseline_state}"
        )
        assert result.verdict == "blocked"
    assert any(
        item.action == "review_history" and item.authority == "web_administrator"
        for item in result.next_actions
    )
    assert not any(item.action == "copy_source" for item in result.next_actions)
    assert set(statements) <= {"SELECT", "PRAGMA"}
    assert counts == (
        env.broker.stop_count,
        env.broker.sync_count,
        env.broker.start_count,
    )
    with env.sessions() as session:
        current = session.get(McpWorkspaceDeploymentOperationRow, row.id)
        assert current is not None
        assert current.state == "failed" and current.recovery_state is None
        assert current.failure_code == row.failure_code


@pytest.mark.parametrize("superseding", ["baseline", "success", "contained"])
def test_newer_verified_evidence_does_not_reopen_a_historical_failure(
    tmp_path: Path, superseding: str
) -> None:
    env, row = inventory_failure(tmp_path, historical=True)
    if superseding == "baseline":
        with env.sessions.begin() as session:
            baseline = session.get(
                DevelopmentSourceDeploymentRow,
                (str(fixture.TARGET_ID), str(fixture.SOURCE_ID)),
            )
            assert baseline is not None
            baseline.verified_at = timestamp(fixture.NOW + timedelta(minutes=3))
    else:
        later_attempt(
            env,
            row,
            state="succeeded" if superseding == "success" else "failed",
            recovery_state=None
            if superseding == "success"
            else "contained_requested_tree",
            inventory_classification="matched",
            addon_enabled=True,
            failure_code=None if superseding == "success" else row.failure_code,
        )
    result = WorkspaceDeploymentPreflightService(env.sessions).inspect(operation(env))
    assert "deployment_provenance" not in reasons(result)


@pytest.mark.parametrize(
    "defect", ["incomplete_copy", "missing_receipt", "different_workspace"]
)
def test_unrelated_or_incomplete_failures_are_not_reported_as_verified_copies(
    tmp_path: Path, defect: str
) -> None:
    env, row = inventory_failure(tmp_path, historical=True)
    with env.sessions.begin() as session:
        current = session.get(McpWorkspaceDeploymentOperationRow, row.id)
        assert current is not None
        if defect == "incomplete_copy":
            current.sync_completed = False
            current.target_started = False
        elif defect == "missing_receipt":
            current.verified_destination_digest = None
        else:
            # Use a distinct real workspace to keep foreign-key checks enabled.
            original = session.get(AgentWorkspaceRow, str(env.workspace_id))
            assert original is not None
            values = {
                c.name: getattr(original, c.name)
                for c in AgentWorkspaceRow.__table__.columns
            }
            values["id"] = str(uuid4())
            values["baseline_artifact_id"] = str(uuid4())
            session.add(AgentWorkspaceRow(**values))
            session.flush()
            current.workspace_id = values["id"]
    result = WorkspaceDeploymentPreflightService(env.sessions).inspect(operation(env))
    assert "deployment_provenance" not in reasons(result)


@pytest.mark.parametrize("tied", [False, True])
def test_completion_order_and_tied_failure_cannot_be_hidden_by_a_later_request(
    tmp_path: Path, tied: bool
) -> None:
    env, original = inventory_failure(tmp_path, historical=True)
    later_attempt(
        env,
        original,
        state="succeeded",
        inventory_classification="matched",
        addon_enabled=True,
        failure_code=None,
    )
    with env.sessions.begin() as session:
        failed = session.get(McpWorkspaceDeploymentOperationRow, original.id)
        assert failed is not None
        failed.completed_at = timestamp(
            fixture.NOW + timedelta(minutes=2 if tied else 3)
        )
    result = WorkspaceDeploymentPreflightService(env.sessions).inspect(operation(env))
    assert (
        reasons(result)["deployment_provenance"].reason
        == "deployment_provenance_review_required"
    )
    assert not any(item.action == "copy_source" for item in result.next_actions)


@pytest.mark.parametrize(
    "missing",
    ["verified_destination_digest", "original_container_id", "source_manifest_digest"],
)
def test_tied_incomplete_failure_cannot_hide_strict_verified_copy_evidence(
    tmp_path: Path, missing: str
) -> None:
    env, original = inventory_failure(tmp_path, historical=True)
    later_attempt(env, original, **{missing: None})
    with env.sessions.begin() as session:
        failed = session.get(McpWorkspaceDeploymentOperationRow, original.id)
        assert failed is not None
        failed.completed_at = timestamp(fixture.NOW + timedelta(minutes=2))
        baseline = session.get(
            DevelopmentSourceDeploymentRow,
            (str(fixture.TARGET_ID), str(fixture.SOURCE_ID)),
        )
        assert baseline is not None
        baseline.confidence = DevelopmentSourceDeploymentConfidence.STALE
    result = WorkspaceDeploymentPreflightService(env.sessions).inspect(operation(env))
    assert (
        reasons(result)["deployment_provenance"].reason
        == "deployment_provenance_review_required"
    )
    assert reasons(result)["source_baseline"].reason == "source_baseline_stale"
    assert not any(item.action == "copy_source" for item in result.next_actions)
