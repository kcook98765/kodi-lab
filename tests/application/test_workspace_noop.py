"""Actual admission/receipts and private source verification with native doubles."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session

from kodi_lab.application.mcp_workspace_development import (
    BoundedIntervalRateLimiter,
    McpWorkspaceDevelopmentError,
    WorkspaceDeploymentResult,
)
from kodi_lab.development.deployment import expected_destination_digest
from kodi_lab.storage.models import (
    ManagedBridgeTrustRow,
    ManagedInstanceMutationLeaseRow,
    ManagedInstanceRow,
    McpWorkspaceDeploymentOperationRow,
)
from kodi_lab.workspace import capture_workspace_deployment_tree
from tests.application import test_mcp_workspace_development as fixture


def environment(
    tmp_path: Path, database_path: Path | None = None
) -> fixture.Environment:
    env = fixture._environment(tmp_path, database_path=database_path)
    tick = [0]

    def now() -> datetime:
        tick[0] += 10
        return fixture.NOW + timedelta(seconds=tick[0])

    env.development._clock = now
    env.development._deployment_rate = BoundedIntervalRateLimiter(0)
    env.development.configure_managed_noop_verifier(
        lambda observed, release: SimpleNamespace(generation=1)
    )
    with env.sessions.begin() as session:
        session.add(
            ManagedBridgeTrustRow(
                target_id=str(fixture.TARGET_ID),
                runtime_revision_id=str(fixture.RUNTIME_REVISION_ID),
                container_id=fixture.CONTAINER_ID,
                bridge_id=str(uuid4()),
                bridge_port=8765,
                secret_reference=str(uuid4()),
                identity_json='{"fixture":"reviewed"}',
                state="active",
                generation=1,
                prepared_at=fixture.NOW_TEXT,
                established_at=fixture.NOW_TEXT,
            )
        )
    return env


def deploy(
    env: fixture.Environment,
    request: UUID | None = None,
    *,
    generation: int = 0,
) -> WorkspaceDeploymentResult:
    return env.development.deploy(
        client_id=env.client_id,
        request_id=request or uuid4(),
        workspace_id=env.workspace_id,
        expected_generation=generation,
        target_id=fixture.TARGET_ID,
        recheck_authority=lambda _session: None,
    )


def test_exact_existing_target_reuses_verified_bytes_and_durable_receipt(
    tmp_path: Path,
) -> None:
    env = environment(tmp_path)
    first = deploy(env)
    assert not first.deployment_work_skipped
    assert first.managed_verification is not None
    request = uuid4()
    second = deploy(env, request)
    assert second.deployment_work_skipped
    assert second.result_classification == "verified_existing"
    assert second.managed_verification is not None
    assert second.managed_verification.reused_deployment_id == first.deployment_id
    assert second.managed_verification.native_restart_count == 0
    assert (
        second.managed_verification.bytes_staged
        == second.managed_verification.bytes_transferred
        == 0
    )
    assert second.iteration_evidence is not None
    assert (
        second.iteration_evidence.file_application == "verified_existing_at_operation"
    )
    assert second.iteration_evidence.kodi_restart == "not_observed"
    assert second.iteration_evidence.fresh_code == "not_observed"
    assert deploy(env, request) == second
    third = deploy(env)
    assert third.deployment_work_skipped
    assert (env.broker.stop_count, env.broker.sync_count, env.broker.start_count) == (
        1,
        1,
        1,
    )
    assert env.broker.verify_count == 2
    with env.sessions() as session:
        row = session.get(McpWorkspaceDeploymentOperationRow, str(second.deployment_id))
        assert row is not None
        assert not any(
            [
                row.mutation_started,
                row.target_stopped,
                row.sync_started,
                row.sync_completed,
                row.target_started,
            ]
        )
    executed = fixture._execute_workspace(env)
    assert executed.deployment_id == third.deployment_id
    assert executed.state == "succeeded" and env.smoke.count == 1


@pytest.mark.parametrize(
    "drift",
    [
        "runtime_revision",
        "bridge_trust",
        "missing_proof",
        "missing_receipt",
        "source_bytes",
        "addon_xml",
    ],
)
def test_insufficient_proof_retains_full_deployment(tmp_path: Path, drift: str) -> None:
    env = environment(tmp_path)
    first = deploy(env)
    with env.sessions.begin() as session:
        row = session.get(McpWorkspaceDeploymentOperationRow, str(first.deployment_id))
        assert row is not None
        if drift == "runtime_revision":
            instance = session.get(ManagedInstanceRow, str(fixture.TARGET_ID))
            assert instance is not None
            instance.runtime_state_revision += 10
        elif drift == "bridge_trust":
            trust = session.get(ManagedBridgeTrustRow, str(fixture.TARGET_ID))
            assert trust is not None
            trust.state = "invalidated"
            trust.secret_reference = None
        elif drift == "missing_proof":
            row.managed_verification_json = None
        elif drift == "missing_receipt":
            # Keep durable request history; a newer definitive failure must
            # never allow older success to become the current generation.
            row.state = "failed"
            row.failure_code = "deployment_conflict"
        elif drift in {"source_bytes", "addon_xml"}:
            # Out-of-band private source mutation is detected by full capture.
            candidates = list(
                (tmp_path / "data").rglob(
                    "addon.xml" if drift == "addon_xml" else "main.py"
                )
            )
            assert candidates
            candidates[0].write_text(candidates[0].read_text() + "\n")
    if drift == "missing_receipt":
        with pytest.raises(
            McpWorkspaceDevelopmentError, match="deployment_not_allowed"
        ):
            deploy(env)
        assert env.broker.stop_count == 1
        return
    result = deploy(env)
    assert not result.deployment_work_skipped
    assert env.broker.stop_count == 2


def test_historical_request_replay_does_not_reverify_or_restart(tmp_path: Path) -> None:
    env = environment(tmp_path)
    request = uuid4()
    original = deploy(env, request)
    reused = deploy(env)
    with env.sessions.begin() as session:
        trust = session.get(ManagedBridgeTrustRow, str(fixture.TARGET_ID))
        assert trust is not None
        trust.state = "invalidated"
        trust.secret_reference = None
    before = env.broker.verify_count
    assert deploy(env, request) == original
    assert env.broker.verify_count == before
    assert reused.deployment_work_skipped and env.broker.stop_count == 1


def test_revoked_authority_during_verification_never_falls_back_to_mutation(
    tmp_path: Path,
) -> None:
    env = environment(tmp_path)
    deploy(env)

    def deny(_session: Session) -> None:
        raise RuntimeError("Exact grant was revoked")

    with pytest.raises(McpWorkspaceDevelopmentError):
        env.development.deploy(
            client_id=env.client_id,
            request_id=uuid4(),
            workspace_id=env.workspace_id,
            expected_generation=0,
            target_id=fixture.TARGET_ID,
            recheck_authority=deny,
        )
    assert (env.broker.stop_count, env.broker.sync_count, env.broker.start_count) == (
        1,
        1,
        1,
    )


def test_concurrent_new_request_and_pending_replay_cannot_bypass_noop_lease(
    tmp_path: Path,
) -> None:
    env = environment(tmp_path)
    deploy(env)
    entered, resume = Event(), Event()

    def gate(phase: str) -> None:
        if phase == "noop_after_destination_readback":
            entered.set()
            assert resume.wait(5)

    env.development._failure = gate
    request = uuid4()
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(deploy, env, request)
        assert entered.wait(5)
        try:
            with pytest.raises(
                McpWorkspaceDevelopmentError, match="deployment_conflict"
            ):
                deploy(env)
            with pytest.raises(
                McpWorkspaceDevelopmentError, match="deployment_ambiguous"
            ):
                deploy(env, request)
        finally:
            resume.set()
        result = running.result(timeout=5)
    assert result.deployment_work_skipped
    assert deploy(env, request) == result
    assert env.broker.stop_count == 1


def test_stop_start_revision_race_during_readback_refuses_noop(tmp_path: Path) -> None:
    env = environment(tmp_path)
    deploy(env)

    def changed(phase: str) -> None:
        if phase == "noop_after_destination_readback":
            with env.sessions.begin() as session:
                instance = session.get(ManagedInstanceRow, str(fixture.TARGET_ID))
                assert instance is not None
                instance.runtime_state_revision += 2

    env.development._failure = changed
    assert not deploy(env).deployment_work_skipped
    assert env.broker.stop_count == 2


@pytest.mark.parametrize("changed_bytes", [False, True])
def test_new_workspace_generation_always_uses_full_deployment(
    tmp_path: Path,
    changed_bytes: bool,
) -> None:
    env = environment(tmp_path)
    deploy(env)
    edit = env.workspaces.replace_text_file(
        env.workspace_id,
        expected_generation=0,
        relative_path="main.py",
        content="print('new')\n" if changed_bytes else "pass\n",
    )
    env.development.record_validation(
        workspace_id=env.workspace_id,
        source_id=fixture.SOURCE_ID,
        generation=edit.generation,
        addon_id="plugin.video.slice4",
        addon_version="1.0.0",
        extension_types=("xbmc.python.pluginsource",),
        validated_at=fixture.NOW,
    )
    source = capture_workspace_deployment_tree(tmp_path / "data", env.workspace_id)
    env.broker.sync_source_digest = source.digest
    env.broker.sync_destination_digest = expected_destination_digest(
        source,
        runtime_uid=1000,
        runtime_gid=1000,
    )
    with pytest.raises(
        McpWorkspaceDevelopmentError, match="workspace_generation_conflict"
    ):
        deploy(env)
    result = deploy(env, generation=edit.generation)
    assert not result.deployment_work_skipped
    assert env.broker.stop_count == 2


def test_disabled_native_addon_does_not_qualify_for_noop(tmp_path: Path) -> None:
    env = environment(tmp_path)
    deploy(env)
    native = fixture.InventoryAdapter().get_inventory()
    env.development._inventory_factory = lambda observed, profile: SimpleNamespace(
        get_inventory=lambda: replace(
            native, items=tuple(replace(item, enabled=False) for item in native.items)
        )
    )
    assert not deploy(env).deployment_work_skipped
    assert env.broker.stop_count == 2


def test_stale_generation_history_never_selects_older_success(tmp_path: Path) -> None:
    env = environment(tmp_path)
    deploy(env)
    second = deploy(env)
    with env.sessions.begin() as session:
        latest = session.get(
            McpWorkspaceDeploymentOperationRow, str(second.deployment_id)
        )
        assert latest is not None
        latest.generation += 1
    assert not deploy(env).deployment_work_skipped
    assert env.broker.stop_count == 2


def test_lost_mutation_lease_during_finalization_never_falls_back(
    tmp_path: Path,
) -> None:
    env = environment(tmp_path)
    deploy(env)

    def changed(phase: str) -> None:
        if phase == "noop_after_destination_readback":
            with env.sessions.begin() as session:
                lease = session.get(
                    ManagedInstanceMutationLeaseRow, str(fixture.TARGET_ID)
                )
                assert lease is not None
                lease.lease_token_digest = "e" * 64

    env.development._failure = changed
    with pytest.raises(McpWorkspaceDevelopmentError, match="deployment_conflict"):
        deploy(env)
    assert (env.broker.stop_count, env.broker.sync_count, env.broker.start_count) == (
        1,
        1,
        1,
    )
    with env.sessions() as session:
        assert (
            session.get(ManagedInstanceMutationLeaseRow, str(fixture.TARGET_ID))
            is not None
        )


@pytest.mark.parametrize("mismatch", ["target", "container", "revision", "phase"])
def test_changed_selected_session_refuses_noop_and_native_work(
    tmp_path: Path, mismatch: str
) -> None:
    from tests.application.test_session_workspace_admission import bind

    env = environment(tmp_path)
    deploy(env)
    bind(env, mismatch=mismatch)
    with pytest.raises(McpWorkspaceDevelopmentError):
        deploy(env)
    assert (env.broker.stop_count, env.broker.sync_count, env.broker.start_count) == (
        1,
        1,
        1,
    )


def test_replaced_container_refuses_stale_profile_deployment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.application.test_session_profile_lineage import fresh_binding

    env = environment(tmp_path)
    deploy(env)
    fresh_binding(env, monkeypatch)
    env.development._deployment_rate = BoundedIntervalRateLimiter(0)
    with pytest.raises(McpWorkspaceDevelopmentError, match="deployment_not_allowed"):
        deploy(env)
    assert env.broker.stop_count == 1


@pytest.mark.parametrize("state", ["ambiguous", "reconciliation_required"])
def test_unresolved_previous_operation_never_reuses_older_proof(
    tmp_path: Path, state: str
) -> None:
    env = environment(tmp_path)
    first = deploy(env)
    with env.sessions.begin() as session:
        row = session.get(McpWorkspaceDeploymentOperationRow, str(first.deployment_id))
        assert row is not None
        row.state = state
        row.failure_code = "deployment_ambiguous"
    with pytest.raises(McpWorkspaceDevelopmentError):
        deploy(env)
    assert (env.broker.stop_count, env.broker.sync_count, env.broker.start_count) == (
        1,
        1,
        1,
    )
