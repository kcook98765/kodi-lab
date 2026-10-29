"""Fresh profiles keep old receipts as history, not current deployment lineage."""

from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select

from kodi_lab.application.mcp_workspace_development import (
    BoundedIntervalRateLimiter,
    McpWorkspaceDevelopmentError,
)
from kodi_lab.application.workspace_deployment_preflight import (
    DeploymentPreflightInput,
    WorkspaceDeploymentPreflightService,
)
from kodi_lab.application.workspace_launch import WorkspaceLaunchError
from kodi_lab.broker.development_sources import (
    SqliteDevelopmentSourceDeploymentResolver,
)
from kodi_lab.broker.smoke_diagnostics import SqliteSmokeLogAuthorityResolver
from kodi_lab.domain import DevelopmentSourceDeploymentConfidence
from kodi_lab.storage.models import (
    DevelopmentSessionPolicyRow,
    DevelopmentSessionRow,
    DevelopmentSourceDeploymentOperationRow,
    DevelopmentSourceDeploymentRow,
    ManagedInstanceRow,
    McpWorkspaceDeploymentOperationRow,
)
from tests.application import test_mcp_workspace_development as fixtures
from tests.application import test_workspace_launch as launches
from tests.application.test_session_workspace_admission import bind

NEW_CONTAINER = "b" * 64


def fresh_binding(env: fixtures.Environment, monkeypatch: pytest.MonkeyPatch) -> None:
    """Seed the already-tested atomic handoff's DB outcome; no live resources.

    These tests isolate receipt selection using real stored history and fake
    positive transports. They do not establish native baseline/copy acceptance.
    """
    bind(env)
    with env.sessions.begin() as session:
        old = session.get(DevelopmentSessionRow, 1)
        policy = session.get(DevelopmentSessionPolicyRow, 1)
        instance = session.get(ManagedInstanceRow, str(fixtures.TARGET_ID))
        assert old is not None and policy is not None and instance is not None
        old.phase = "closed"
        session.add(
            DevelopmentSessionRow(
                generation=2,
                request_id=str(uuid4()),
                target_id=old.target_id,
                runtime_revision_id=old.runtime_revision_id,
                workspace_id=old.workspace_id,
                source_id=old.source_id,
                container_id=NEW_CONTAINER,
                phase="bound",
                created_at=fixtures.NOW_TEXT,
                updated_at=fixtures.NOW_TEXT,
            )
        )
        session.flush()
        policy.active_generation = 2
        instance.runtime_state_revision += 1
    # The existing fake transport reports exact identity from this module's
    # constant. Persisted old rows retain their original container strings.
    monkeypatch.setattr(fixtures, "CONTAINER_ID", NEW_CONTAINER)
    env.development._deployment_rate = BoundedIntervalRateLimiter(
        300, clock=lambda: 2000.0
    )
    env.development._execution_rate = BoundedIntervalRateLimiter(
        60, clock=lambda: 2000.0
    )


def counts(env: fixtures.Environment) -> tuple[int, ...]:
    return (
        env.broker.stop_count,
        env.broker.sync_count,
        env.broker.start_count,
        env.smoke.count,
    )


@pytest.mark.parametrize("legacy_unscoped", [False, True])
def test_fresh_same_version_cannot_execute_using_only_the_old_profile_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    legacy_unscoped: bool,
) -> None:
    env = fixtures._environment(tmp_path)
    fixtures._deploy_workspace(env)
    if legacy_unscoped:
        with env.sessions.begin() as session:
            previous = session.scalar(select(McpWorkspaceDeploymentOperationRow))
            assert previous is not None
            previous.original_container_id = None
            previous.original_runtime_state_revision = None
    fresh_binding(env, monkeypatch)
    before = counts(env)
    with pytest.raises(McpWorkspaceDevelopmentError, match="execution_not_deployed"):
        fixtures._execute_workspace(env)
    assert counts(env) == before


def test_current_profile_receipt_is_not_ambiguous_with_retained_same_generation_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = fixtures._environment(tmp_path)
    fixtures._deploy_workspace(env)
    fresh_binding(env, monkeypatch)
    with env.sessions.begin() as session:
        old = session.scalar(select(McpWorkspaceDeploymentOperationRow))
        assert old is not None and old.original_container_id != NEW_CONTAINER
        values: dict[str, Any] = {
            column.name: getattr(old, column.name) for column in old.__table__.columns
        }
        values.update(
            id=str(uuid4()),
            request_id=str(uuid4()),
            original_container_id=NEW_CONTAINER,
            original_runtime_state_revision=1,
        )
        session.add(McpWorkspaceDeploymentOperationRow(**values))

    class Docker:
        def containers(self, **_kwargs: object) -> list[dict[str, str]]:
            return [{"Id": NEW_CONTAINER}]

        def inspect_container(self, _container_id: str) -> dict[str, object]:
            return {
                "Id": NEW_CONTAINER,
                "State": {"Running": True},
                "Config": {
                    "Labels": {
                        "io.kodi-lab.managed": "true",
                        "io.kodi-lab.instance-id": str(fixtures.TARGET_ID),
                        "io.kodi-lab.runtime-revision-id": str(
                            fixtures.RUNTIME_REVISION_ID
                        ),
                    }
                },
            }

        def close(self) -> None:
            pass

    resolver = SqliteSmokeLogAuthorityResolver(env.sessions, Docker)
    observed: list[str] = []

    def capture(execution_id: str) -> str:
        authority = resolver.resolve(execution_id)
        observed.append(authority.container_id)
        return authority.container_id

    env.broker.capture_hook = capture
    result = fixtures._execute_workspace(env)
    assert observed == [NEW_CONTAINER]
    assert result.state == "succeeded" and env.smoke.count == 1
    with env.sessions() as session:
        assert (
            len(list(session.scalars(select(McpWorkspaceDeploymentOperationRow)))) == 2
        )


def test_old_exact_execution_request_still_replays_as_history_after_fresh_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = fixtures._environment(tmp_path)
    fixtures._deploy_workspace(env)
    request = uuid4()
    first = fixtures._execute_workspace(env, request_id=request)
    fresh_binding(env, monkeypatch)
    before = counts(env)
    assert fixtures._execute_workspace(env, request_id=request) == first
    assert counts(env) == before


def test_gui_launch_requires_a_receipt_from_the_current_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env, bridge, service = launches.environment(tmp_path)
    fresh_binding(env, monkeypatch)
    with pytest.raises(WorkspaceLaunchError, match="launch_not_deployed"):
        launches.launch(env, service)
    assert bridge.calls == []


def unknown_baseline(env: fixtures.Environment) -> None:
    with env.sessions.begin() as session:
        baseline = session.get(
            DevelopmentSourceDeploymentRow,
            (str(fixtures.TARGET_ID), str(fixtures.SOURCE_ID)),
        )
        assert baseline is not None
        baseline.confidence = DevelopmentSourceDeploymentConfidence.UNKNOWN


def test_preflight_does_not_relabel_an_old_profile_copy_as_current_workspace_lineage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = fixtures._environment(tmp_path)
    fixtures._deploy_workspace(env)
    fresh_binding(env, monkeypatch)
    unknown_baseline(env)
    result = WorkspaceDeploymentPreflightService(env.sessions).inspect(
        DeploymentPreflightInput(
            workspace_id=env.workspace_id,
            expected_generation=0,
            target_id=fixtures.TARGET_ID,
        )
    )
    baseline = next(item for item in result.checks if item.name == "source_baseline")
    assert baseline.reason == "source_baseline_unknown" and baseline.state == "blocked"


def test_old_profile_copy_cannot_bypass_new_profile_baseline_before_stop_or_sync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = fixtures._environment(tmp_path)
    fixtures._deploy_workspace(env)
    fresh_binding(env, monkeypatch)
    unknown_baseline(env)
    before = counts(env)
    with pytest.raises(
        McpWorkspaceDevelopmentError, match="deployment_not_allowed"
    ) as caught:
        fixtures._deploy_workspace(env)
    assert caught.value.reason == "source_baseline_unknown"
    assert counts(env) == before


def test_legacy_unscoped_receipt_requires_container_evidence_without_active_session(
    tmp_path: Path,
) -> None:
    env = fixtures._environment(tmp_path)
    fixtures._deploy_workspace(env)
    with env.sessions.begin() as session:
        previous = session.scalar(select(McpWorkspaceDeploymentOperationRow))
        assert previous is not None
        previous.original_container_id = None
        previous.original_runtime_state_revision = None
    with pytest.raises(McpWorkspaceDevelopmentError, match="execution_not_deployed"):
        fixtures._execute_workspace(env)
    assert env.smoke.count == 0


def test_broker_uses_new_profile_source_baseline_instead_of_old_workspace_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = fixtures._environment(tmp_path)
    fixtures._deploy_workspace(env)
    fresh_binding(env, monkeypatch)
    operation_id = str(uuid4())
    with env.sessions.begin() as session:
        source_operation = session.get(
            DevelopmentSourceDeploymentOperationRow, str(fixtures.BASELINE_OPERATION_ID)
        )
        baseline = session.get(
            DevelopmentSourceDeploymentRow,
            (str(fixtures.TARGET_ID), str(fixtures.SOURCE_ID)),
        )
        old = session.scalar(select(McpWorkspaceDeploymentOperationRow))
        assert source_operation is not None and baseline is not None and old is not None
        values = {
            column.name: getattr(source_operation, column.name)
            for column in source_operation.__table__.columns
        }
        source_id = str(uuid4())
        values.update(id=source_id, expected_container_id=NEW_CONTAINER)
        session.add(DevelopmentSourceDeploymentOperationRow(**values))
        session.flush()
        baseline.last_successful_operation_id = source_id
        baseline.trusted_container_id = NEW_CONTAINER
        values = {
            column.name: getattr(old, column.name) for column in old.__table__.columns
        }
        values.update(
            id=operation_id,
            request_id=str(uuid4()),
            state="running",
            phase="sync",
            completed_at=None,
            source_manifest_digest=None,
            verified_destination_digest=None,
            original_container_id=NEW_CONTAINER,
            original_runtime_state_revision=1,
            mutation_started=True,
            target_stopped=True,
            sync_started=True,
            sync_completed=False,
            target_started=False,
            addon_enabled=None,
            result_classification=None,
            inventory_classification="not_checked",
        )
        session.add(McpWorkspaceDeploymentOperationRow(**values))
        expected = baseline.source_manifest_digest
    resolver = SqliteDevelopmentSourceDeploymentResolver(
        env.sessions, development_root=tmp_path / "sources", data_root=tmp_path / "data"
    )
    result = resolver.resolve_workspace_deployment(
        managed_instance_id=str(fixtures.TARGET_ID),
        workspace_id=str(env.workspace_id),
        expected_generation=0,
        deployment_operation_id=operation_id,
        expected_container_id=NEW_CONTAINER,
    )
    assert result.prior_evidence is not None
    assert result.prior_evidence.source_manifest_digest == expected
    assert result.prior_evidence.trusted_container_id == NEW_CONTAINER
