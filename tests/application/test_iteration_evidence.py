from pathlib import Path
from uuid import uuid4

from kodi_lab.application.mcp_workspace_development import WorkspaceDeploymentResult
from kodi_lab.broker.docker_inventory import ContainerRuntimeState
from tests.application import test_mcp_workspace_development as fixture


def test_deployment_receipt_separates_copied_bytes_from_addon_execution_and_replays(
    tmp_path: Path,
) -> None:
    env = fixture._environment(tmp_path)
    request_id = uuid4()

    def deploy() -> WorkspaceDeploymentResult:
        return env.development.deploy(
            client_id=env.client_id,
            request_id=request_id,
            workspace_id=env.workspace_id,
            expected_generation=0,
            target_id=fixture.TARGET_ID,
        )

    result = deploy()
    proof = result.iteration_evidence
    assert proof is not None
    assert proof.file_application == "verified_at_operation"
    assert proof.kodi_restart == "observed_at_operation"
    assert proof.scope == "historical_operation"
    assert proof.tree_policy == "retain_destination_only"
    assert proof.exact_mirror == "not_established"
    assert proof.addon_launch == "not_dispatched"
    assert proof.fresh_code == "not_observed" and proof.functional_smoke == "not_run"
    env.broker.state = ContainerRuntimeState.STOPPED
    replay = deploy()
    assert replay.iteration_evidence == proof
    assert env.broker.stop_count == env.broker.sync_count == env.broker.start_count == 1


def test_external_receipts_do_not_inherit_managed_restart_claims(
    tmp_path: Path,
) -> None:
    env = fixture._environment(tmp_path)
    fixture._configure_external_environment(env)
    result = env.development.deploy(
        client_id=env.client_id,
        request_id=uuid4(),
        workspace_id=env.workspace_id,
        expected_generation=0,
        target_id=fixture.EXTERNAL_TARGET_ID,
    )
    assert result.iteration_evidence is None
