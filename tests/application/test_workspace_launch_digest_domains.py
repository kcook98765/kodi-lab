"""Managed launch consumes real source and ownership-bound copy evidence."""

from pathlib import Path

import pytest
from sqlalchemy import select

from kodi_lab.application.workspace_launch import (
    WorkspaceLaunchError,
    WorkspaceLaunchService,
)
from kodi_lab.broker.operations import VerifyAttachedDevelopmentSourceDestinationResult
from kodi_lab.development.deployment import (
    DevelopmentTreeManifest,
    build_delta_archive,
    capture_destination_archive,
    classify_delta,
    expected_destination_digest,
    verify_destination,
)
from kodi_lab.storage.models import (
    AgentWorkspaceRow,
    ManagedInstanceMutationLeaseRow,
    McpWorkspaceDeploymentOperationRow,
    McpWorkspaceLaunchRow,
    RuntimeExecutionProfileRow,
)
from kodi_lab.workspace import capture_workspace_deployment_tree
from tests.application import test_mcp_workspace_development as fixtures
from tests.application import test_workspace_launch as launches


def real_copy_receipt(
    tmp_path: Path, *, uid: int = 1000, gid: int = 1000
) -> tuple[
    fixtures.Environment,
    launches.Bridge,
    WorkspaceLaunchService,
    DevelopmentTreeManifest,
]:
    env, bridge, service = launches.environment(tmp_path)
    stage = tmp_path / "normalized-copy"
    stage.mkdir()
    source = capture_workspace_deployment_tree(
        tmp_path / "data", env.workspace_id, stage_root=stage
    )
    archive = tmp_path / "installed-copy.tar"
    build_delta_archive(
        stage,
        archive,
        source,
        classify_delta(source, None),
        runtime_uid=uid,
        runtime_gid=gid,
        root_name="plugin.video.slice4",
    )
    installed = capture_destination_archive(
        [archive.read_bytes()], root_name="plugin.video.slice4"
    )
    verify_destination(source, None, installed, runtime_uid=uid, runtime_gid=gid)
    assert source.digest != installed.digest
    assert installed.digest == expected_destination_digest(
        source, runtime_uid=uid, runtime_gid=gid
    )
    with env.sessions.begin() as session:
        workspace = session.get(AgentWorkspaceRow, str(env.workspace_id))
        deployment = session.scalar(select(McpWorkspaceDeploymentOperationRow))
        profile = session.get(
            RuntimeExecutionProfileRow, str(fixtures.RUNTIME_REVISION_ID)
        )
        assert workspace is not None and deployment is not None and profile is not None
        assert source.digest == workspace.current_tree_digest
        profile.runtime_uid, profile.runtime_gid = uid, gid
        deployment.source_manifest_digest = source.digest
        deployment.verified_destination_digest = installed.digest
    return env, bridge, service, source


@pytest.mark.parametrize(("uid", "gid"), [(1000, 1000), (42, 43)])
def test_real_normalized_copy_is_launchable_and_replays_once(
    tmp_path: Path, uid: int, gid: int
) -> None:
    env, bridge, service, source = real_copy_receipt(tmp_path, uid=uid, gid=gid)
    result = launches.launch(env, service)
    assert result.state == "accepted" and result.dispatch_accepted
    assert launches.launch(env, service, request_id=result.request_id).replayed
    assert bridge.calls == [False]
    assert bridge.last is not None
    assert bridge.last.intent.manifest_digest == source.digest


@pytest.mark.parametrize("invalid", ["source_domain", "wrong_uid", "wrong_gid"])
def test_launch_rejects_wrong_ownership_digest_before_dispatch(
    tmp_path: Path, invalid: str
) -> None:
    env, bridge, service, source = real_copy_receipt(tmp_path)
    digests = {
        "source_domain": source.digest,
        "wrong_uid": expected_destination_digest(
            source, runtime_uid=42, runtime_gid=1000
        ),
        "wrong_gid": expected_destination_digest(
            source, runtime_uid=1000, runtime_gid=43
        ),
    }
    with env.sessions.begin() as session:
        deployment = session.scalar(select(McpWorkspaceDeploymentOperationRow))
        assert deployment is not None
        deployment.verified_destination_digest = digests[invalid]
    with pytest.raises(WorkspaceLaunchError, match="launch_not_deployed"):
        launches.launch(env, service)
    assert bridge.calls == []
    with env.sessions() as session:
        assert session.scalar(select(McpWorkspaceLaunchRow)) is None


def test_launch_rejects_unrecorded_workspace_byte_drift_before_dispatch(
    tmp_path: Path,
) -> None:
    env, bridge, service, _ = real_copy_receipt(tmp_path)
    current = (
        tmp_path
        / "data"
        / "agent-workspaces"
        / str(env.workspace_id)
        / "tree"
        / "main.py"
    )
    current.write_text("# unrecorded byte drift\n", encoding="utf-8")
    with pytest.raises(WorkspaceLaunchError, match="launch_not_deployed"):
        launches.launch(env, service)
    assert bridge.calls == []


def test_claim_time_recapture_rejects_drift_and_releases_lease(tmp_path: Path) -> None:
    env, bridge, service, _ = real_copy_receipt(tmp_path)

    def change_after_claim(phase: str) -> None:
        if phase == "claimed":
            current = (
                tmp_path
                / "data"
                / "agent-workspaces"
                / str(env.workspace_id)
                / "tree"
                / "main.py"
            )
            current.write_text("# drift after initial admission\n", encoding="utf-8")

    service._failure = change_after_claim
    result = launches.launch(env, service)
    assert result.state == "failed" and result.code == "launch_not_deployed"
    assert bridge.calls == []
    with env.sessions() as session:
        assert session.get(McpWorkspaceLaunchRow, str(result.launch_id)) is not None
        assert (
            session.get(ManagedInstanceMutationLeaseRow, str(fixtures.TARGET_ID))
            is None
        )


def test_unsafe_workspace_capture_refuses_before_claim(tmp_path: Path) -> None:
    env, bridge, service, _ = real_copy_receipt(tmp_path)
    current = (
        tmp_path
        / "data"
        / "agent-workspaces"
        / str(env.workspace_id)
        / "tree"
        / "main.py"
    )
    external = tmp_path / "outside-workspace.py"
    external.write_text("# outside confined workspace\n", encoding="utf-8")
    current.unlink()
    current.symlink_to(external)
    with pytest.raises(WorkspaceLaunchError, match="launch_not_deployed"):
        launches.launch(env, service)
    assert bridge.calls == []
    with env.sessions() as session:
        assert session.scalar(select(McpWorkspaceLaunchRow)) is None


def test_live_destination_verification_remains_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env, bridge, service, _ = real_copy_receipt(tmp_path)
    verify = env.broker.verify_mcp_workspace_destination

    def mismatch(**kwargs: str) -> VerifyAttachedDevelopmentSourceDestinationResult:
        return verify(**kwargs).model_copy(update={"matched": False})

    monkeypatch.setattr(env.broker, "verify_mcp_workspace_destination", mismatch)
    before = env.broker.verify_count
    result = launches.launch(env, service)
    assert result.state == "failed" and result.code == "launch_deployment_changed"
    assert env.broker.verify_count == before + 1
    assert bridge.calls == []
