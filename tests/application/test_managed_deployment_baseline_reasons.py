"""Technical baseline refusals remain distinct without changing admission."""

from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

from kodi_lab.application.mcp_workspace_development import McpWorkspaceDevelopmentError
from kodi_lab.domain import DevelopmentSourceDeploymentConfidence
from kodi_lab.storage.models import (
    DevelopmentSourceDeploymentRow,
    McpDevelopmentRequestRow,
    McpWorkspaceDeploymentOperationRow,
    RuntimeRevisionRow,
)
from tests.application import test_mcp_workspace_development as fixture


@pytest.mark.parametrize("defect", ["missing", "stale", "unknown", "runtime_mismatch"])
def test_baseline_refusal_has_reason_before_any_deployment_is_queued(
    tmp_path: Path, defect: str
) -> None:
    env = fixture._environment(tmp_path)
    with env.sessions.begin() as session:
        baseline = session.get(
            DevelopmentSourceDeploymentRow,
            (str(fixture.TARGET_ID), str(fixture.SOURCE_ID)),
        )
        assert baseline is not None
        if defect == "missing":
            session.delete(baseline)
        elif defect == "runtime_mismatch":
            original = session.get(RuntimeRevisionRow, str(fixture.RUNTIME_REVISION_ID))
            assert original is not None
            values = {
                column.name: getattr(original, column.name)
                for column in RuntimeRevisionRow.__table__.columns
            }
            identifier = str(uuid4())
            values.update(id=identifier, revision_number=original.revision_number + 1)
            session.add(RuntimeRevisionRow(**values))
            session.flush()
            baseline.runtime_revision_id = identifier
        else:
            baseline.confidence = DevelopmentSourceDeploymentConfidence(defect)
    with pytest.raises(McpWorkspaceDevelopmentError) as refused:
        fixture._deploy_workspace(env)
    assert refused.value.code == "deployment_not_allowed"
    assert refused.value.reason == f"source_baseline_{defect}"
    assert env.broker.stop_count == env.broker.sync_count == env.broker.start_count == 0
    with env.sessions() as session:
        assert session.scalar(select(McpWorkspaceDeploymentOperationRow)) is None
        assert session.scalar(select(McpDevelopmentRequestRow)) is None


def test_verified_prior_workspace_copy_still_satisfies_existing_admission(
    tmp_path: Path,
) -> None:
    env = fixture._environment(tmp_path)
    fixture._deploy_workspace(env)
    with env.sessions() as session:
        baseline = session.get(
            DevelopmentSourceDeploymentRow,
            (str(fixture.TARGET_ID), str(fixture.SOURCE_ID)),
        )
        assert baseline is not None and baseline.confidence == "stale"
        workspace, validation, target = env.development._deployment_preflight_rows(
            session, env.workspace_id, 0, fixture.TARGET_ID
        )
        assert workspace.id == env.workspace_id
        assert validation.generation == 0
        assert target.target_class == "internal_lab"
