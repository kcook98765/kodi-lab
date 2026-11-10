"""Narrow selected-project input uses the original remote receipt/controller."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from threading import Event
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from kodi_lab.application.browser_remote import BrowserRemoteError
from kodi_lab.application.development_navigation import (
    DevelopmentNavigationError,
    DevelopmentNavigationService,
    require_menu,
)
from kodi_lab.application.mcp_administration import (
    ADDON_DEVELOPMENT_PRESET_ID,
    READ_ONLY_INSPECTION_PRESET_ID,
    McpAdministrationError,
    McpAuthenticationService,
)
from kodi_lab.application.mcp_remote_actions import (
    McpRemoteActionReceiptError,
    McpRemoteActionReceiptService,
)
from kodi_lab.domain import TargetType
from kodi_lab.kodi.remote import BrowserRemoteAction
from kodi_lab.kodi.ui_state import UiObservation
from kodi_lab.mcp.authorization import McpAuthorizationError
from kodi_lab.mcp.tools import DevelopmentNavigateInput
from kodi_lab.storage.models import (
    AgentWorkspaceRow,
    DevelopmentSessionRow,
    McpDevelopmentNavigationBindingRow,
    McpRemoteActionReceiptRow,
    McpWorkspaceDeploymentOperationRow,
    TargetRow,
)
from tests.application import test_mcp_workspace_development as fixtures
from tests.application.test_development_launch import PERMISSIONS, LaunchEnvironment
from tests.application.test_development_launch import environment as launch_environment
from tests.application.test_mcp_remote_actions import FakeLimiter, FakeRemote


def menu(**changes: Any) -> UiObservation:
    return UiObservation.model_validate(
        {
            "window": {"id": 10001},
            "dialog": {"active": False},
            "focus": {"control_id": 50, "is_container": True},
            "container": {"associated": True, "id": 50},
            "playback": {"active": False},
            "ui": {
                "busy": False,
                "fullscreen_video": False,
                "screensaver": False,
                "text_input": False,
            },
            **changes,
        }
    )


def environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[LaunchEnvironment, FakeRemote, FakeLimiter, McpRemoteActionReceiptService]:
    e = launch_environment(tmp_path)
    preset = e.admin.create_custom_preset(
        display_name="Explicit menu navigation",
        template_preset_id=ADDON_DEVELOPMENT_PRESET_ID,
        permissions=PERMISSIONS | {"development.navigate"},
    )
    e.admin.assign_client_preset(e.env.client_id, preset.id)
    e.admin.assign_target_preset(fixtures.TARGET_ID, preset.id)
    remote, limiter = FakeRemote(), FakeLimiter()
    receipts = McpRemoteActionReceiptService(
        e.env.sessions, remote, limiter, clock=lambda: fixtures.NOW
    )
    e.tools._development_navigation = DevelopmentNavigationService(
        e.env.sessions, e.launch, receipts, data_root=tmp_path / "data"
    )
    monkeypatch.setattr(e.tools, "inspect_ui_state", lambda *_a, **_kw: menu())
    return e, remote, limiter, receipts


def request(e: LaunchEnvironment, **changes: Any) -> DevelopmentNavigateInput:
    return DevelopmentNavigateInput.model_validate(
        {
            "request_id": str(uuid4()),
            "workspace_id": str(e.env.workspace_id),
            "target_id": str(fixtures.TARGET_ID),
            "expected_generation": 1,
            "action": "down",
            **changes,
        }
    )


@pytest.mark.parametrize("action", ["up", "down", "left", "right", "back"])
def test_exact_project_inputs_and_replay_preserve_original_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    e, remote, limiter, _receipts = environment(tmp_path, monkeypatch)
    operation = request(e, action=action)
    first = e.tools.development_navigate(e.principal, operation)
    # UI movement after dispatch must not force a replay to inspect or send again.
    monkeypatch.setattr(
        e.tools,
        "inspect_ui_state",
        lambda *_a, **_kw: (_ for _ in ()).throw(
            AssertionError("replayed input must not inspect")
        ),
    )
    assert e.tools.development_navigate(e.principal, operation) == first
    assert len(remote.calls) == len(limiter.calls) == 1
    with e.env.sessions() as db:
        binding = db.get(
            McpDevelopmentNavigationBindingRow, str(first.action_receipt.invocation_id)
        )
        assert binding is not None
        assert binding.workspace_id == str(e.env.workspace_id)
        assert binding.source_id == str(fixtures.SOURCE_ID)
        assert binding.container_id == fixtures.CONTAINER_ID
        assert (
            binding.session_generation
            == binding.workspace_generation
            == binding.bridge_generation
            == 1
        )
        assert (
            binding.deployment_id and binding.tree_digest and binding.destination_digest
        )
    assert not {
        "remote_action",
        "managed_reset",
        "workspace_execution_recover",
        "addon_settings_set",
        "promotion_request",
    } & set(e.auth.discoverable_tools(e.principal))


@pytest.mark.parametrize(
    "action",
    ["select", "home", "context_menu", "info", "stop", "volume_up", "text", "reset"],
)
def test_activating_or_general_control_inputs_are_not_in_schema(action: str) -> None:
    with pytest.raises(ValidationError):
        DevelopmentNavigateInput.model_validate(
            {
                "request_id": str(uuid4()),
                "workspace_id": str(uuid4()),
                "target_id": str(uuid4()),
                "expected_generation": 0,
                "action": action,
            }
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"window": {"id": 10004}},
        {"window": {"id": 13000}},
        {"dialog": {"active": True}},
        {"dialog": {}},
        {"focus": {"control_id": 50, "is_container": False}},
        {"container": {"associated": True, "id": 51}},
        {"playback": {"active": True}},
        {"ui": {"text_input": True}},
        {"ui": {}},
        {"changed_during_collection": True},
    ],
)
def test_settings_custom_windows_dialogs_and_incomplete_ui_are_refused(
    changes: dict[str, Any],
) -> None:
    with pytest.raises(DevelopmentNavigationError, match="menu_required"):
        require_menu(menu(**changes))


@pytest.mark.parametrize(
    "denial",
    [
        "source",
        "target",
        "credential",
        "client_permission",
        "target_permission",
        "disabled",
        "other_client",
        "other_workspace",
        "uncertain",
        "container",
        "wrong_target",
        "stale_generation",
        "stale_deployment",
        "external_production",
        "external_development",
        "disabled_addon",
        "bridge",
    ],
)
def test_navigation_scope_and_installation_refusals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, denial: str
) -> None:
    e, remote, _limiter, _receipts = environment(tmp_path, monkeypatch)
    principal, operation = e.principal, request(e)
    if denial == "source":
        e.admin.remove_development_source_grant(e.env.client_id, fixtures.SOURCE_ID)
    elif denial == "target":
        e.admin.remove_target_grant(e.env.client_id, fixtures.TARGET_ID)
    elif denial == "credential":
        e.admin.revoke_credential(e.env.client_id, principal.credential_id)
    elif denial == "client_permission":
        e.admin.assign_client_preset(e.env.client_id, READ_ONLY_INSPECTION_PRESET_ID)
    elif denial == "target_permission":
        e.admin.assign_target_preset(fixtures.TARGET_ID, ADDON_DEVELOPMENT_PRESET_ID)
    elif denial == "disabled":
        e.admin.set_client_enabled(e.env.client_id, False)
    elif denial == "other_client":
        other = e.admin.create_client(
            display_name="Other", preset_id=e.admin.list_clients()[0].preset.id
        )
        e.admin.grant_target(other.id, fixtures.TARGET_ID)
        e.admin.grant_development_source(other.id, fixtures.SOURCE_ID)
        other_principal = McpAuthenticationService(e.env.sessions).authenticate(
            e.admin.issue_credential(other.id).token
        )
        assert other_principal is not None
        principal = other_principal
    elif denial == "wrong_target":
        operation = request(e, target_id=str(uuid4()))
    elif denial == "stale_generation":
        operation = request(e, expected_generation=0)
    elif denial == "stale_deployment":
        with e.env.sessions.begin() as db:
            deployment = db.scalar(select(McpWorkspaceDeploymentOperationRow))
            assert deployment is not None
            deployment.source_manifest_digest = "sha256:" + "0" * 64
    elif denial == "bridge":

        def missing(*_a: Any, **_kw: Any) -> Any:
            from kodi_lab.kodi.bridge import ExternalBridgeError

            raise ExternalBridgeError("bridge_not_paired")

        monkeypatch.setattr(e.bridge, "binding", missing)
    elif denial == "disabled_addon":
        original = fixtures.InventoryAdapter().get_inventory()
        disabled = replace(original, items=(replace(original.items[0], enabled=False),))
        monkeypatch.setattr(
            e.env.development,
            "_inventory_factory",
            lambda *_: type("Inventory", (), {"get_inventory": lambda _: disabled})(),
        )
    elif denial.startswith("external"):
        with e.env.sessions.begin() as db:
            target = db.get(TargetRow, str(fixtures.TARGET_ID))
            assert target is not None
            target.target_type = TargetType(denial)
    else:
        other_workspace = None
        if denial == "other_workspace":
            created = e.env.workspaces.queue_creation(fixtures.SOURCE_ID)
            lease = e.env.workspaces.acquire(created.operation_id)
            assert lease is not None
            e.env.workspaces.run(lease)
            other_workspace = created.workspace_id
        with e.env.sessions.begin() as db:
            current = db.get(DevelopmentSessionRow, 1)
            assert current is not None
            if denial == "other_workspace":
                current.workspace_id = str(other_workspace)
            elif denial == "container":
                current.container_id = "b" * 64
            else:
                current.phase = "uncertain"
    with pytest.raises(McpAuthorizationError):
        e.tools.development_navigate(principal, operation)
    assert not remote.calls


@pytest.mark.parametrize(
    "change",
    [
        "permission",
        "target",
        "source",
        "credential",
        "selection",
        "container",
        "generation",
        "deployment",
        "bridge",
        "menu",
    ],
)
def test_rechecks_after_receipt_claim_before_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    e, remote, _limiter, _receipts = environment(tmp_path, monkeypatch)
    invoke = remote.invoke

    def change_then_invoke(*args: Any, **kwargs: Any) -> Any:
        if change == "permission":
            e.admin.assign_client_preset(e.env.client_id, ADDON_DEVELOPMENT_PRESET_ID)
        elif change == "source":
            e.admin.remove_development_source_grant(e.env.client_id, fixtures.SOURCE_ID)
        elif change == "target":
            e.admin.remove_target_grant(e.env.client_id, fixtures.TARGET_ID)
        elif change == "credential":
            e.admin.revoke_credential(e.env.client_id, e.principal.credential_id)
        elif change == "menu":
            monkeypatch.setattr(
                e.tools,
                "inspect_ui_state",
                lambda *_a, **_kw: menu(window={"id": 10004}),
            )
        elif change == "bridge":
            monkeypatch.setattr(
                e.bridge,
                "binding",
                lambda *_: (_ for _ in ()).throw(RuntimeError("trust revoked")),
            )
        else:
            with e.env.sessions.begin() as db:
                current = db.get(DevelopmentSessionRow, 1)
                workspace = db.get(AgentWorkspaceRow, str(e.env.workspace_id))
                deployment = db.scalar(select(McpWorkspaceDeploymentOperationRow))
                assert (
                    current is not None
                    and workspace is not None
                    and deployment is not None
                )
                if change == "selection":
                    current.phase = "uncertain"
                elif change == "container":
                    current.container_id = "b" * 64
                elif change == "generation":
                    workspace.generation += 1
                else:
                    deployment.source_manifest_digest = "sha256:" + "0" * 64
        return invoke(*args, **kwargs)

    monkeypatch.setattr(remote, "invoke", change_then_invoke)
    with pytest.raises(McpAuthorizationError, match="context_changed"):
        e.tools.development_navigate(e.principal, request(e))
    assert not remote.calls
    with e.env.sessions() as db:
        row = db.scalar(select(McpRemoteActionReceiptRow))
        assert row is not None and row.state == "completed"
        assert row.final_result == "development_navigation_context_changed"
        assert db.get(McpDevelopmentNavigationBindingRow, row.id) is not None


def test_ambiguous_replay_preserves_original_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    e, remote, _limiter, _receipts = environment(tmp_path, monkeypatch)
    operation = request(e)
    remote.error = BrowserRemoteError("remote_action_ambiguous", "lost acknowledgment")
    for _ in range(2):
        with pytest.raises(McpAuthorizationError, match="remote_action_ambiguous"):
            e.tools.development_navigate(e.principal, operation)
    assert len(remote.calls) == 1
    with e.env.sessions() as db:
        row = db.scalar(select(McpRemoteActionReceiptRow))
        assert row is not None and row.request_id == str(operation.request_id)
        assert row.id == str(remote.calls[0][3]) and row.state == "ambiguous"


def test_navigation_rate_limit_has_a_replayable_denial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    e, remote, limiter, _receipts = environment(tmp_path, monkeypatch)
    limiter.denied = True
    operation = request(e)
    for _ in range(2):
        with pytest.raises(McpAuthorizationError, match="rate_limited"):
            e.tools.development_navigate(e.principal, operation)
    assert not remote.calls and len(limiter.calls) == 1


def test_namespaces_and_retained_immutable_receipts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    e, remote, _limiter, receipts = environment(tmp_path, monkeypatch)
    operation = request(e)
    result = e.tools.development_navigate(e.principal, operation)
    with pytest.raises(McpRemoteActionReceiptError, match="request_id_conflict"):
        receipts.invoke(
            client_id=e.env.client_id,
            credential_id=e.principal.credential_id,
            target_id=fixtures.TARGET_ID,
            request_id=operation.request_id,
            action=BrowserRemoteAction.DOWN,
        )
    other_request = uuid4()
    receipts.invoke(
        client_id=e.env.client_id,
        credential_id=e.principal.credential_id,
        target_id=fixtures.TARGET_ID,
        request_id=other_request,
        action=BrowserRemoteAction.DOWN,
    )
    with pytest.raises(McpAuthorizationError, match="request_id_conflict"):
        e.tools.development_navigate(
            e.principal, request(e, request_id=str(other_request))
        )
    for mutate in ("update", "delete", "parent_delete"):
        with pytest.raises(IntegrityError), e.env.sessions.begin() as db:
            binding = db.get(
                McpDevelopmentNavigationBindingRow,
                str(result.action_receipt.invocation_id),
            )
            assert binding is not None
            if mutate == "update":
                binding.bridge_generation += 1
            elif mutate == "delete":
                db.delete(binding)
            else:
                db.delete(db.get(McpRemoteActionReceiptRow, binding.receipt_id))
            db.flush()
    receipts._clock = lambda: fixtures.NOW + timedelta(days=100)
    receipts.invoke(
        client_id=e.env.client_id,
        credential_id=e.principal.credential_id,
        target_id=fixtures.TARGET_ID,
        request_id=uuid4(),
        action=BrowserRemoteAction.BACK,
    )
    assert e.tools.development_navigate(e.principal, operation) == result
    assert len(remote.calls) == 3


def test_concurrent_same_request_has_one_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    e, remote, _limiter, _receipts = environment(tmp_path, monkeypatch)
    remote.entered, remote.release = Event(), Event()
    operation = request(e)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(e.tools.development_navigate, e.principal, operation)
        assert remote.entered.wait(5)
        try:
            with pytest.raises(McpAuthorizationError, match="remote_action_ambiguous"):
                e.tools.development_navigate(e.principal, operation)
        finally:
            remote.release.set()
        result = first.result(timeout=5)
    assert e.tools.development_navigate(e.principal, operation) == result
    assert len(remote.calls) == 1


def test_no_preset_gains_navigation_implicitly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    e, _remote, _limiter, _receipts = environment(tmp_path, monkeypatch)
    for preset in e.admin.list_presets():
        if preset.builtin:
            assert "development.navigate" not in preset.permissions
    copy = e.admin.create_custom_preset(
        display_name="Default copy", template_preset_id=ADDON_DEVELOPMENT_PRESET_ID
    )
    assert "development.navigate" not in copy.permissions
    with pytest.raises(McpAdministrationError):
        e.admin.create_custom_preset(
            display_name="Invalid",
            template_preset_id=READ_ONLY_INSPECTION_PRESET_ID,
            permissions=frozenset({"development.navigate"}),
        )


def test_lost_receipt_finalization_fences_fresh_requests_until_runtime_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    e, remote, _limiter, receipts = environment(tmp_path, monkeypatch)
    operation = request(e)
    finalize = receipts._finalize_success

    def lost(*_args: Any) -> Any:
        raise SQLAlchemyError("receipt write interrupted")

    monkeypatch.setattr(receipts, "_finalize_success", lost)
    with pytest.raises(McpAuthorizationError, match="remote_action_ambiguous"):
        e.tools.development_navigate(e.principal, operation)
    assert len(remote.calls) == 1
    monkeypatch.setattr(receipts, "_finalize_success", finalize)
    with pytest.raises(
        McpAuthorizationError, match="development_navigation_requires_review"
    ):
        e.tools.development_navigate(e.principal, request(e))
    assert len(remote.calls) == 1
    # Only a separately authorized lifecycle recovery advances runtime identity.
    # Original evidence stays pending; advancing context does not replay it.
    from kodi_lab.storage.models import ManagedInstanceRow

    with e.env.sessions.begin() as db:
        instance = db.get(ManagedInstanceRow, str(fixtures.TARGET_ID))
        assert instance is not None
        instance.runtime_state_revision += 1
    e.tools.development_navigate(e.principal, request(e))
    assert len(remote.calls) == 2
    with pytest.raises(McpAuthorizationError, match="remote_action_ambiguous"):
        e.tools.development_navigate(e.principal, operation)
