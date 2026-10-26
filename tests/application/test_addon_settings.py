"""Settings writes retain durable typed ownership and never replay uncertain work."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select

from kodi_lab.application.addon_settings import AddonSettingsError, AddonSettingsService
from kodi_lab.application.reconciliation import (
    ManagedInstanceReconciliationService,
    ReconciliationAction,
    StaleOperationReconciliationDispatcher,
)
from kodi_lab.kodi.addon_settings import AddonSettingOutcome, AddonSettingsSetInput
from kodi_lab.kodi.launch import ManagedLaunchRuntimeBinding
from kodi_lab.storage.models import (
    DevelopmentSessionPolicyRow,
    DevelopmentSessionRow,
    ManagedInstanceMutationLeaseRow,
    McpAddonSettingsOperationRow,
    McpWorkspaceDeploymentOperationRow,
)
from tests.application import test_mcp_workspace_development as fixtures
from tests.application.test_workspace_launch import Bridge as LaunchBridge
from tests.application.test_workspace_launch import environment


class Bridge(LaunchBridge):
    def __init__(self) -> None:
        super().__init__()
        self.count = 0
        self.uncertain = False

    def settings_binding(
        self, observed: Any, kodi_version: str
    ) -> ManagedLaunchRuntimeBinding:
        return self.binding(observed, kodi_version)

    def settings(
        self, observed: Any, kodi_version: str, payload: dict[str, object]
    ) -> tuple[AddonSettingOutcome, ...]:
        self.count += 1
        assert payload["addon_id"] == "plugin.video.slice4"
        assert kodi_version == "21.3"
        if self.lost:
            raise RuntimeError("private-secret-in-exception")
        return (
            AddonSettingOutcome(
                id="debug", status="write_unconfirmed" if self.uncertain else "verified"
            ),
        )


def fixture(
    tmp_path: Path,
    seeded: fixtures.Environment | None = None,
) -> tuple[fixtures.Environment, Bridge, AddonSettingsService]:
    if seeded is None:
        env, _unused, launch = environment(tmp_path)
    else:
        # Exercise coordinators against a genuinely migrated database. The
        # caller populated its exact predecessor without ORM schema creation.
        from kodi_lab.application.workspace_launch import WorkspaceLaunchService
        from kodi_lab.development.deployment import expected_destination_digest
        from kodi_lab.storage.models import AgentWorkspaceRow
        from kodi_lab.workspace import capture_workspace_deployment_tree

        env = seeded
        fixtures._deploy_workspace(env)
        manifest = capture_workspace_deployment_tree(
            tmp_path / "data", env.workspace_id
        )
        with env.sessions.begin() as session:
            workspace = session.get(AgentWorkspaceRow, str(env.workspace_id))
            deployment = session.scalar(select(McpWorkspaceDeploymentOperationRow))
            assert workspace is not None and deployment is not None
            deployment.source_manifest_digest = workspace.current_tree_digest
            deployment.verified_destination_digest = expected_destination_digest(
                manifest, runtime_uid=1000, runtime_gid=1000
            )
        launch = WorkspaceLaunchService(
            env.sessions, env.development, LaunchBridge(), data_root=tmp_path / "data"
        )
    with env.sessions.begin() as session:
        deployment = session.scalar(select(McpWorkspaceDeploymentOperationRow))
        assert deployment is not None
        current = DevelopmentSessionRow(
            generation=1,
            request_id=str(uuid4()),
            target_id=str(fixtures.TARGET_ID),
            runtime_revision_id=deployment.runtime_revision_id,
            workspace_id=str(env.workspace_id),
            source_id=deployment.development_source_id,
            container_id=deployment.original_container_id or fixtures.CONTAINER_ID,
            phase="bound",
            created_at=fixtures.NOW_TEXT,
            updated_at=fixtures.NOW_TEXT,
        )
        session.add(current)
        session.flush()
        policy = session.get(DevelopmentSessionPolicyRow, 1)
        if policy is None:
            session.add(DevelopmentSessionPolicyRow(id=1, active_generation=1))
        else:
            policy.active_generation = 1
        # Make the exact deployment belong to the positively selected profile.
        deployment.original_container_id = current.container_id
        from kodi_lab.storage.repositories import ManagedRuntimeProjectionRepository

        token = ManagedRuntimeProjectionRepository(session).observation_token(
            fixtures.TARGET_ID
        )
        assert token is not None
        deployment.original_runtime_state_revision = token.runtime_state_revision
    bridge = Bridge()
    return (
        env,
        bridge,
        AddonSettingsService(
            env.sessions,
            env.development,
            launch,
            bridge,
            clock=lambda: fixtures.NOW + timedelta(minutes=1),
        ),
    )


def request() -> AddonSettingsSetInput:
    return AddonSettingsSetInput.model_validate(
        {
            "request_id": str(uuid4()),
            "expected_session_generation": 1,
            "expected_workspace_generation": 0,
            "settings": [{"id": "debug", "value": True}],
        }
    )


def test_one_write_has_exact_selection_and_no_plaintext_receipt(tmp_path: Path) -> None:
    env, bridge, service = fixture(tmp_path)
    req = request()
    result = service.set(
        client_id=env.client_id, request=req, authorize=lambda *_: None
    )
    assert result.state == "completed" and result.results[0].status == "verified"
    assert bridge.count == 1
    assert (
        result.target_id == fixtures.TARGET_ID
        and result.workspace_id == env.workspace_id
    )
    with env.sessions() as session:
        row = session.scalar(select(McpAddonSettingsOperationRow))
        assert (
            row is not None
            and row.results_json == '[{"id":"debug","status":"verified"}]'
        )
        assert (
            session.get(ManagedInstanceMutationLeaseRow, str(fixtures.TARGET_ID))
            is None
        )
    with pytest.raises(AddonSettingsError, match="already_used"):
        service.set(client_id=env.client_id, request=req, authorize=lambda *_: None)
    assert bridge.count == 1


@pytest.mark.parametrize("failure", ["transport", "setter"])
def test_ambiguous_write_fences_other_work_without_retry(
    tmp_path: Path, failure: str
) -> None:
    env, bridge, service = fixture(tmp_path)
    bridge.lost = failure == "transport"
    bridge.uncertain = failure == "setter"
    result = service.set(
        client_id=env.client_id, request=request(), authorize=lambda *_: None
    )
    assert result.state == "ambiguous"
    assert "private-secret" not in result.model_dump_json()
    status = ManagedInstanceReconciliationService(env.sessions).status(
        fixtures.TARGET_ID
    )
    assert status.blocking and ReconciliationAction.STOP not in status.blocked_actions
    with pytest.raises(AddonSettingsError, match="reconciliation_required"):
        service.set(
            client_id=env.client_id, request=request(), authorize=lambda *_: None
        )
    assert bridge.count == 1


@pytest.mark.parametrize(
    "change", ["no_selection", "stale_session", "workspace_changed", "denied"]
)
def test_preconditions_refuse_before_bridge_dispatch(
    tmp_path: Path, change: str
) -> None:
    env, bridge, service = fixture(tmp_path)
    req = request()
    if change == "no_selection":
        with env.sessions.begin() as session:
            policy = session.get(DevelopmentSessionPolicyRow, 1)
            assert policy is not None
            policy.active_generation = None
    elif change == "stale_session":
        req = req.model_copy(update={"expected_session_generation": 2})
    elif change == "workspace_changed":
        req = req.model_copy(update={"expected_workspace_generation": 1})

    def authorize(*_args: object) -> None:
        if change == "denied":
            raise PermissionError("denied")

    with pytest.raises((AddonSettingsError, PermissionError)):
        service.set(client_id=env.client_id, request=req, authorize=authorize)
    assert bridge.count == 0


def test_expired_settings_dispatch_becomes_durable_ambiguity(tmp_path: Path) -> None:
    env, bridge, service = fixture(tmp_path)
    # Create a completed real operation, then model a crash immediately after
    # its committed dispatch marker with the exact real typed lease ownership.
    result = service.set(
        client_id=env.client_id, request=request(), authorize=lambda *_: None
    )
    from kodi_lab.domain import ManagedInstanceMutationKind
    from kodi_lab.storage.repositories import ManagedInstanceMutationLeaseRepository

    with env.sessions.begin() as session:
        row = session.get(McpAddonSettingsOperationRow, str(result.operation_id))
        assert row is not None
        row.state = "dispatching"
        row.results_json = None
        row.completed_at = None
        leases = ManagedInstanceMutationLeaseRepository(session)
        leases.reserve(
            fixtures.TARGET_ID,
            kind=ManagedInstanceMutationKind.MCP_ADDON_SETTINGS,
            operation_id=result.operation_id,
        )
        assert leases.claim(
            fixtures.TARGET_ID,
            operation_id=result.operation_id,
            token_digest="a" * 64,
            now=fixtures.NOW,
            expires_at=fixtures.NOW + timedelta(seconds=1),
        )
    dispatcher = StaleOperationReconciliationDispatcher(
        env.sessions, clock=lambda: fixtures.NOW + timedelta(minutes=2)
    )
    dispatcher.sweep_instance(fixtures.TARGET_ID)
    status = ManagedInstanceReconciliationService(env.sessions).status(
        fixtures.TARGET_ID
    )
    assert status.blocking and status.operation_state == "ambiguous"
    assert bridge.count == 1


@pytest.mark.parametrize("ending", ["swept", "contained_success", "contained_error"])
def test_late_worker_cannot_erase_sweep_or_containment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ending: str
) -> None:
    env, bridge, service = fixture(tmp_path)
    original = bridge.settings

    def delayed(
        observed: Any, kodi_version: str, payload: dict[str, object]
    ) -> tuple[AddonSettingOutcome, ...]:
        results = original(observed, kodi_version, payload)
        StaleOperationReconciliationDispatcher(
            env.sessions, clock=lambda: fixtures.NOW + timedelta(hours=1)
        ).sweep_instance(fixtures.TARGET_ID)
        if ending.startswith("contained"):
            from uuid import UUID

            from kodi_lab.broker.docker_inventory import ContainerRuntimeState

            env.broker.state = ContainerRuntimeState.STOPPED
            service.contain(fixtures.TARGET_ID, UUID(str(payload["operation_id"])))
        if ending == "contained_error":
            raise RuntimeError("private late error")
        return results

    monkeypatch.setattr(bridge, "settings", delayed)
    result = service.set(
        client_id=env.client_id, request=request(), authorize=lambda *_: None
    )
    assert result.state == ("ambiguous" if ending == "swept" else "contained")
    assert bridge.count == 1
    status = ManagedInstanceReconciliationService(env.sessions).status(
        fixtures.TARGET_ID
    )
    assert status.blocking == (ending == "swept")


@pytest.mark.parametrize("release", ["19.5", "22.0-rc.1"])
def test_unsupported_release_refuses_before_claim_or_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, release: str
) -> None:
    from dataclasses import replace

    env, bridge, service = fixture(tmp_path)
    original = env.development._runtime_context

    def unsupported(target: Any) -> Any:
        observed, profile, container = original(target)
        if release == "19.5":
            profile = replace(profile, expected_kodi_version="19.5")
        else:
            profile = replace(
                profile,
                expected_kodi_version="22.0",
                expected_version_tag="rc",
                expected_prerelease_ordinal=1,
            )
        return observed, profile, container

    monkeypatch.setattr(env.development, "_runtime_context", unsupported)
    with pytest.raises(AddonSettingsError, match="settings_kodi_unsupported"):
        service.set(
            client_id=env.client_id, request=request(), authorize=lambda *_: None
        )
    with env.sessions() as session:
        assert session.scalar(select(McpAddonSettingsOperationRow)) is None
        assert (
            session.get(ManagedInstanceMutationLeaseRow, str(fixtures.TARGET_ID))
            is None
        )
    assert bridge.count == 0


def test_mixed_partial_outcomes_survive_postwrite_identity_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kodi_lab.broker.docker_inventory import ContainerRuntimeState

    env, bridge, service = fixture(tmp_path)
    req = request().model_copy(
        update={
            "settings": AddonSettingsSetInput.model_validate(
                {
                    "request_id": str(uuid4()),
                    "expected_session_generation": 1,
                    "expected_workspace_generation": 0,
                    "settings": [
                        {"id": "debug", "value": True},
                        {"id": "page_size", "value": 30},
                        {"id": "password", "value": "secret-canary"},
                    ],
                }
            ).settings
        }
    )
    outcomes = (
        AddonSettingOutcome(id="debug", status="verified"),
        AddonSettingOutcome(id="page_size", status="write_unconfirmed"),
        AddonSettingOutcome(id="password", status="denied"),
    )

    def partial(
        _observed: object, _version: str, _payload: dict[str, object]
    ) -> tuple[AddonSettingOutcome, ...]:
        bridge.count += 1
        env.broker.state = ContainerRuntimeState.STOPPED
        return outcomes

    monkeypatch.setattr(bridge, "settings", partial)
    result = service.set(
        client_id=env.client_id, request=req, authorize=lambda *_: None
    )
    assert result.state == "ambiguous" and result.results == outcomes
    assert "secret-canary" not in result.model_dump_json()
    with env.sessions() as session:
        row = session.get(McpAddonSettingsOperationRow, str(result.operation_id))
        assert row is not None and "secret-canary" not in (row.results_json or "")
    contained = service.contain(fixtures.TARGET_ID, result.operation_id)
    assert contained.state == "contained" and contained.results == outcomes
    assert bridge.count == 1


@pytest.mark.parametrize("lost", [False, True])
def test_receipt_lookup_after_response_loss_never_calls_kodi_or_writes_again(
    tmp_path: Path,
    lost: bool,
) -> None:
    env, bridge, service = fixture(tmp_path)
    req = request()
    bridge.lost = lost
    original = service.set(
        client_id=env.client_id, request=req, authorize=lambda *_: None
    )
    recovered = service.result(
        client_id=env.client_id, request_id=req.request_id, authorize=lambda *_: None
    )
    assert recovered.replayed and recovered.operation_id == original.operation_id
    assert recovered.state == original.state and recovered.results == original.results
    assert bridge.count == 1
    with pytest.raises(AddonSettingsError, match="not_found"):
        service.result(
            client_id=uuid4(), request_id=req.request_id, authorize=lambda *_: None
        )
    with env.sessions.begin() as session:
        policy = session.get(DevelopmentSessionPolicyRow, 1)
        assert policy is not None
        policy.active_generation = None
    with pytest.raises(AddonSettingsError, match="stale_session"):
        service.result(
            client_id=env.client_id,
            request_id=req.request_id,
            authorize=lambda *_: None,
        )
    assert bridge.count == 1
