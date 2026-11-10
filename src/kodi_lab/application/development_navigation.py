"""Project-bound admission to the existing managed remote-action controller."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import fields
from pathlib import Path
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.browser_remote import BrowserRemoteError
from kodi_lab.application.mcp_remote_actions import (
    McpRemoteActionReceiptError,
    McpRemoteActionReceiptService,
    McpRemoteActionResult,
)
from kodi_lab.application.workspace_launch import WorkspaceLaunchService
from kodi_lab.development.navigation import DevelopmentNavigationBinding
from kodi_lab.kodi.remote import BrowserRemoteAction
from kodi_lab.kodi.ui_state import UiObservation
from kodi_lab.storage.development_sessions import active_session
from kodi_lab.storage.models import (
    AgentWorkspaceRow,
    McpDevelopmentNavigationBindingRow,
)
from kodi_lab.workspace.storage import workspace_coordination_lock

# Native list menus only: Home, Programs, Pictures, Videos, Add-on browser, Music.
# No settings, dialogs, editors, players or arbitrary/custom add-on windows.
DEVELOPMENT_MENU_WINDOWS = frozenset({10000, 10001, 10002, 10025, 10040, 10502})
DEVELOPMENT_NAVIGATION_ACTIONS = frozenset(
    BrowserRemoteAction(value) for value in ("up", "down", "left", "right", "back")
)


class DevelopmentNavigationError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class DevelopmentNavigateResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    workspace_id: UUID
    source_id: UUID
    generation: int = Field(ge=0)
    session_generation: int = Field(ge=1)
    action_receipt: McpRemoteActionResult


def require_menu(ui: UiObservation) -> tuple[int, int]:
    """Fail closed when input could reach a setting, text field or dialog."""
    if (
        ui.changed_during_collection
        or ui.window.id not in DEVELOPMENT_MENU_WINDOWS
        or ui.dialog.active is not False
        or ui.playback.active is not False
        or any(
            flag is not False
            for flag in (
                ui.ui.busy,
                ui.ui.fullscreen_video,
                ui.ui.screensaver,
                ui.ui.text_input,
            )
        )
        or ui.focus.is_container is not True
        or ui.focus.control_id is None
        or ui.focus.control_id < 0
        or not ui.container.associated
        or ui.container.id != ui.focus.control_id
    ):
        raise DevelopmentNavigationError("development_navigation_menu_required")
    assert ui.window.id is not None and ui.focus.control_id is not None
    return ui.window.id, ui.focus.control_id


class DevelopmentNavigationService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        launch: WorkspaceLaunchService,
        receipts: McpRemoteActionReceiptService,
        *,
        data_root: Path,
    ) -> None:
        self._sessions, self._launch, self._receipts = sessions, launch, receipts
        self._data_root = data_root

    def navigate(
        self,
        *,
        client_id: UUID,
        credential_id: UUID,
        request_id: UUID,
        workspace_id: UUID,
        expected_generation: int,
        target_id: UUID,
        action: BrowserRemoteAction,
        authorize: Callable[[Session], None],
        inspect: Callable[[], UiObservation],
    ) -> DevelopmentNavigateResult:
        if action not in DEVELOPMENT_NAVIGATION_ACTIONS:
            raise DevelopmentNavigationError("development_navigation_action_refused")

        def selection(session: Session) -> tuple[int, str, str, str]:
            authorize(session)
            current = active_session(session)
            workspace = session.get(AgentWorkspaceRow, str(workspace_id))
            if (
                current is None
                or current.phase != "bound"
                or current.workspace_id != str(workspace_id)
                or current.target_id != str(target_id)
                or workspace is None
                or workspace.generation != expected_generation
                or current.source_id != workspace.development_source_id
                or current.container_id is None
            ):
                raise DevelopmentNavigationError(
                    "development_navigation_context_changed"
                )
            return (
                current.generation,
                current.source_id,
                current.runtime_revision_id,
                current.container_id,
            )

        with workspace_coordination_lock(
            self._data_root, workspace_id, exclusive=False, blocking=False
        ):
            with self._sessions() as session:
                selected = selection(session)

            existing = self._receipts.existing(client_id, request_id)
            if existing is not None:
                with self._sessions() as session:
                    stored = session.get(
                        McpDevelopmentNavigationBindingRow, existing.id
                    )
                    if stored is None:
                        raise McpRemoteActionReceiptError("request_id_conflict")
                    binding = DevelopmentNavigationBinding(
                        **{
                            field.name: getattr(stored, field.name)
                            for field in fields(DevelopmentNavigationBinding)
                        }
                    )
                if (
                    binding.workspace_id != str(workspace_id)
                    or binding.workspace_generation != expected_generation
                    or selected
                    != (
                        binding.session_generation,
                        binding.source_id,
                        binding.runtime_revision_id,
                        binding.container_id,
                    )
                ):
                    raise McpRemoteActionReceiptError("request_id_conflict")
                # Return original retained evidence; never inspect/re-dispatch an
                # acknowledged or uncertain input to manufacture a fresh result.
                result = self._receipts.replay(
                    existing,
                    target_id=target_id,
                    action=action,
                    development_binding=binding,
                )
            else:

                def proof() -> DevelopmentNavigationBinding:
                    deployment, state_revision, bridge = (
                        self._launch.navigation_installation(
                            workspace_id, expected_generation, target_id
                        )
                    )
                    window_id, control_id = require_menu(inspect())
                    with self._sessions() as session:
                        current = selection(session)
                    if current != selected or current[2:] != (
                        deployment.runtime_revision_id,
                        bridge.container_id,
                    ):
                        raise DevelopmentNavigationError(
                            "development_navigation_context_changed"
                        )
                    if (
                        not deployment.source_manifest_digest
                        or not deployment.verified_destination_digest
                    ):
                        raise DevelopmentNavigationError(
                            "development_navigation_not_deployed"
                        )
                    return DevelopmentNavigationBinding(
                        session_generation=selected[0],
                        workspace_id=str(workspace_id),
                        source_id=selected[1],
                        workspace_generation=expected_generation,
                        deployment_id=deployment.id,
                        addon_id=deployment.addon_id,
                        addon_version=deployment.addon_version,
                        tree_digest=deployment.source_manifest_digest,
                        destination_digest=deployment.verified_destination_digest,
                        runtime_revision_id=bridge.runtime_revision_id,
                        runtime_state_revision=state_revision,
                        container_id=bridge.container_id,
                        bridge_generation=bridge.generation,
                        window_id=window_id,
                        control_id=control_id,
                    )

                binding = proof()

                def reauthorize() -> None:
                    try:
                        if proof() != binding:
                            raise DevelopmentNavigationError(
                                "development_navigation_context_changed"
                            )
                    except Exception as error:
                        raise BrowserRemoteError(
                            "development_navigation_context_changed",
                            "Selected development project or menu authority changed.",
                        ) from error

                def authorize_claim(session: Session) -> None:
                    if selection(session) != selected:
                        raise DevelopmentNavigationError(
                            "development_navigation_context_changed"
                        )

                result = self._receipts.invoke(
                    client_id=client_id,
                    credential_id=credential_id,
                    target_id=target_id,
                    request_id=request_id,
                    action=action,
                    before_dispatch=reauthorize,
                    development_binding=binding,
                    authorize_claim=authorize_claim,
                )
            return DevelopmentNavigateResult(
                workspace_id=workspace_id,
                source_id=UUID(binding.source_id),
                generation=expected_generation,
                session_generation=binding.session_generation,
                action_receipt=result,
            )
