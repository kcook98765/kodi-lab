"""Real MCP authorization callback across verification and fallback races."""

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from kodi_lab.application.mcp_administration import (
    FULL_LAB_CONTROL_PRESET_ID,
    McpAdministrationService,
    McpAuthenticationService,
    McpPrincipal,
)
from kodi_lab.mcp.authorization import (
    McpAuthorizationError,
    McpAuthorizationService,
    TargetTechnicalAuthority,
)
from kodi_lab.mcp.registry import TargetClass, build_mcp_tool_registry
from kodi_lab.mcp.tools import McpTargetToolService, WorkspaceDeployInput
from kodi_lab.storage.models import (
    ManagedBridgeTrustRow,
    ManagedInstanceMutationLeaseRow,
    McpClientDevelopmentSourceGrantRow,
    McpClientTargetGrantRow,
    McpCredentialRow,
)
from tests.application import test_mcp_workspace_development as fixture
from tests.application.test_workspace_noop import environment


class NativeAuthority:
    def resolve(self, target_id: UUID) -> TargetTechnicalAuthority:
        assert target_id == fixture.TARGET_ID
        return TargetTechnicalAuthority(
            TargetClass.INTERNAL_LAB,
            frozenset({"workspace_deployment"}),
            frozenset({"ready", "running", "reconciled"}),
        )


def tool_environment(
    tmp_path: Path,
) -> tuple[fixture.Environment, McpTargetToolService, McpPrincipal]:
    env = environment(tmp_path)
    admin = McpAdministrationService(env.sessions)
    admin.assign_target_preset(fixture.TARGET_ID, FULL_LAB_CONTROL_PRESET_ID)
    admin.grant_target(env.client_id, fixture.TARGET_ID)
    admin.grant_development_source(env.client_id, fixture.SOURCE_ID)
    issued = admin.issue_credential(env.client_id)
    principal = McpAuthenticationService(env.sessions).authenticate(issued.token)
    assert principal is not None
    authorization = McpAuthorizationService(
        env.sessions, build_mcp_tool_registry(), targets=NativeAuthority()
    )
    tools = McpTargetToolService(
        env.sessions,
        authorization,
        cast(Any, None),
        cast(Any, None),
        cast(Any, None),
        workspaces=env.workspaces,
        workspace_development=env.development,
    )
    return env, tools, principal


def request(env: fixture.Environment) -> WorkspaceDeployInput:
    return WorkspaceDeployInput(
        request_id=uuid4(),
        workspace_id=env.workspace_id,
        expected_generation=0,
        target_id=fixture.TARGET_ID,
    )


@pytest.mark.parametrize("change", ["revoked", "expired"])
def test_actual_tool_rechecks_inflight_credential_at_noop_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    env, tools, principal = tool_environment(tmp_path)
    tools.workspace_deploy(principal, request(env))

    def race(phase: str) -> None:
        if phase == "noop_after_destination_readback":
            with env.sessions.begin() as session:
                credential = session.get(McpCredentialRow, str(principal.credential_id))
                assert credential is not None
                if change == "revoked":
                    credential.revoked_at = fixture.NOW_TEXT
                else:
                    assert credential.expires_at is not None
                    expired_now = datetime.fromisoformat(
                        credential.expires_at.replace("Z", "+00:00")
                    ) + timedelta(seconds=1)
                    monkeypatch.setattr(
                        tools._authorization, "_clock", lambda: expired_now
                    )

    env.development._failure = race
    with pytest.raises(McpAuthorizationError, match="permission_denied"):
        tools.workspace_deploy(principal, request(env))
    assert (env.broker.stop_count, env.broker.sync_count, env.broker.start_count) == (
        1,
        1,
        1,
    )


@pytest.mark.parametrize(
    "change", ["credential", "target_grant", "source_grant", "lease"]
)
def test_readback_failure_cannot_fall_back_after_authority_or_lease_loss(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    env, tools, principal = tool_environment(tmp_path)
    tools.workspace_deploy(principal, request(env))

    def readback(**_kwargs: Any) -> Any:
        with env.sessions.begin() as session:
            if change == "credential":
                credential = session.get(McpCredentialRow, str(principal.credential_id))
                assert credential is not None
                credential.revoked_at = fixture.NOW_TEXT
            elif change == "lease":
                lease = session.get(
                    ManagedInstanceMutationLeaseRow, str(fixture.TARGET_ID)
                )
                assert lease is not None
                lease.lease_token_digest = "d" * 64
            elif change == "target_grant":
                grant = session.get(
                    McpClientTargetGrantRow,
                    (str(env.client_id), str(fixture.TARGET_ID)),
                )
                assert grant is not None
                session.delete(grant)
            else:
                source = session.get(
                    McpClientDevelopmentSourceGrantRow,
                    (str(env.client_id), str(fixture.SOURCE_ID)),
                )
                assert source is not None
                session.delete(source)
        raise RuntimeError("Readback response unavailable")

    monkeypatch.setattr(env.broker, "verify_mcp_workspace_destination", readback)
    with pytest.raises(McpAuthorizationError):
        tools.workspace_deploy(principal, request(env))
    assert (env.broker.stop_count, env.broker.sync_count, env.broker.start_count) == (
        1,
        1,
        1,
    )


def test_bridge_revocation_after_last_native_check_cannot_commit_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env, tools, principal = tool_environment(tmp_path)
    tools.workspace_deploy(principal, request(env))
    original = env.development._recheck_deployment
    checks = [0]

    def recheck(operation: Any) -> None:
        original(operation)
        checks[0] += 1
        if checks[0] == 2:
            with env.sessions.begin() as session:
                trust = session.get(ManagedBridgeTrustRow, str(fixture.TARGET_ID))
                assert trust is not None
                trust.state = "revoked"
                trust.secret_reference = None
                trust.generation += 1

    monkeypatch.setattr(env.development, "_recheck_deployment", recheck)
    with pytest.raises(McpAuthorizationError):
        tools.workspace_deploy(principal, request(env))
    assert (env.broker.stop_count, env.broker.sync_count, env.broker.start_count) == (
        1,
        1,
        1,
    )


@pytest.mark.parametrize("change", ["target_grant", "source_grant"])
def test_wrong_initial_grant_cannot_reuse_existing_proof(
    tmp_path: Path, change: str
) -> None:
    env, tools, principal = tool_environment(tmp_path)
    tools.workspace_deploy(principal, request(env))
    with env.sessions.begin() as session:
        grant: McpClientTargetGrantRow | McpClientDevelopmentSourceGrantRow | None
        if change == "target_grant":
            grant = session.get(
                McpClientTargetGrantRow, (str(env.client_id), str(fixture.TARGET_ID))
            )
        else:
            grant = session.get(
                McpClientDevelopmentSourceGrantRow,
                (str(env.client_id), str(fixture.SOURCE_ID)),
            )
        assert grant is not None
        session.delete(grant)
    with pytest.raises(McpAuthorizationError):
        tools.workspace_deploy(principal, request(env))
    assert env.broker.verify_count == 0
    assert (env.broker.stop_count, env.broker.sync_count, env.broker.start_count) == (
        1,
        1,
        1,
    )
