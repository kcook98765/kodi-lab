"""The Web composition rechecks generation/bindings inside existing admission."""

from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy.orm import Session

from kodi_lab.application.development_session_workflow import DevelopmentSessionWorkflow
from kodi_lab.storage.development_sessions import DevelopmentSessionConflict
from tests.application import test_mcp_workspace_development as fixture
from tests.application.test_development_sessions import environment


class Lifecycle:
    def __init__(self, state: Any) -> None:
        self.state = state
        self.calls: list[tuple[str, Any]] = []

    def provision(self, target_id: Any, *, precondition: Any) -> Any:
        with self.state.env.sessions() as session:
            precondition(session)
        self.calls.append(("prepare", target_id))
        return self.state.provision()

    def start(self, target_id: Any, *, precondition: Any, before_dispatch: Any) -> Any:
        with self.state.env.sessions() as session:
            precondition(session)
        before_dispatch()
        self.calls.append(("resume", target_id))
        return None

    def stop(self, target_id: Any, *, precondition: Any, before_dispatch: Any) -> Any:
        with self.state.env.sessions() as session:
            precondition(session)
        before_dispatch()
        self.calls.append(("stop", target_id))
        return None


def test_prepare_resume_stop_keep_the_same_exact_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = environment(tmp_path, monkeypatch)
    lifecycle = Lifecycle(state)
    workflow = DevelopmentSessionWorkflow(state.service, cast(Any, lifecycle))
    assert workflow.view().current is None and workflow.view().latest_generation == 0
    state.service.reserve(state.request)
    workflow.act("prepare", 1)
    workflow.act("resume", 1)
    workflow.act("stop", 1)
    assert lifecycle.calls == [
        (action, fixture.TARGET_ID) for action in ("prepare", "resume", "stop")
    ]
    current = workflow.view().current
    assert current is not None and current.container_id == fixture.CONTAINER_ID
    assert state.env.workspaces.get(state.env.workspace_id).generation == 0
    with pytest.raises(DevelopmentSessionConflict, match="identity_changed"):
        workflow.act("prepare", 1)


@pytest.mark.parametrize(
    "defect", ["generation", "container", "closed_between_load_and_claim"]
)
def test_changed_binding_never_dispatches_an_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, defect: str
) -> None:
    state = environment(tmp_path, monkeypatch)
    state.service.reserve(state.request)
    state.provision()
    lifecycle = Lifecycle(state)
    workflow = DevelopmentSessionWorkflow(state.service, cast(Any, lifecycle))
    if defect == "container":
        from dataclasses import replace

        state.records[0] = replace(state.records[0], container_id="e" * 64)
    elif defect == "closed_between_load_and_claim":

        def changed(_target: Any, *, precondition: Any, **_kwargs: Any) -> Any:
            state.records.clear()
            state.service.close_absent(1)
            with state.env.sessions() as session:
                precondition(session)
            raise AssertionError("stale request must not dispatch")

        monkeypatch.setattr(lifecycle, "start", changed)
    with pytest.raises(DevelopmentSessionConflict):
        workflow.act("resume", 2 if defect == "generation" else 1)
    assert lifecycle.calls == []


def test_prepare_rechecks_runtime_under_lifecycle_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = environment(tmp_path, monkeypatch)
    state.service.reserve(state.request)
    lifecycle = Lifecycle(state)

    def revoked(_session: Session, *_args: Any) -> None:
        raise DevelopmentSessionConflict("development_session_runtime_ineligible")

    monkeypatch.setattr(state.service, "_runtime", revoked)
    with pytest.raises(DevelopmentSessionConflict, match="runtime_ineligible"):
        DevelopmentSessionWorkflow(state.service, cast(Any, lifecycle)).act(
            "prepare", 1
        )
    assert state.records == [] and lifecycle.calls == []
