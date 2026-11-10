"""Automatic onboarding composes existing source and workspace safeguards."""

from __future__ import annotations

import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.agent_workspaces import AgentWorkspaceService
from kodi_lab.application.development_sources import (
    DevelopmentSourceAdministrationService,
)
from kodi_lab.application.local_projects import (
    LocalProjectOnboardingService,
    LocalProjectSelection,
)
from kodi_lab.application.mcp_administration import (
    ADDON_DEVELOPMENT_PRESET_ID,
    READ_ONLY_INSPECTION_PRESET_ID,
    McpAdministrationService,
)
from kodi_lab.application.mcp_workspace_mutations import McpWorkspaceCreationService
from kodi_lab.development.projects import LocalProjectError, LocalProjectResolver
from kodi_lab.development.sources import DevelopmentSourceValidator
from kodi_lab.domain import AgentWorkspaceCreationState, AgentWorkspaceState
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.storage.metadata import Base
from kodi_lab.storage.models import (
    AgentWorkspaceCreationOperationRow,
    AgentWorkspaceRow,
    DevelopmentSourceRow,
    McpAgentWorkspaceBindingRow,
    McpClientDevelopmentSourceGrantRow,
    McpClientTargetGrantRow,
    McpDevelopmentRequestRow,
    McpSecurityEventRow,
)


def write_project(root: Path, locator: str, addon_id: str = "script.example") -> Path:
    project = root / locator
    project.mkdir(parents=True, exist_ok=True)
    (project / "addon.xml").write_text(
        f'<addon id="{addon_id}" name="Example" version="1.0.0">'
        '<extension point="xbmc.python.script" library="main.py"/></addon>'
    )
    (project / "main.py").write_text("print('original')\n")
    return project


@dataclass
class Environment:
    sessions: sessionmaker[Session]
    root: Path
    service: LocalProjectOnboardingService
    workspaces: AgentWorkspaceService
    creation: McpWorkspaceCreationService
    administration: McpAdministrationService
    client_id: UUID
    other_client_id: UUID
    submitted: list[UUID]

    def choose(self, project: str = "example") -> LocalProjectSelection:
        return LocalProjectSelection(client_id=self.client_id, project=project)

    def finish(self, operation_id: UUID) -> None:
        lease = self.workspaces.acquire(operation_id)
        assert lease is not None
        self.workspaces.run(lease)


@pytest.fixture
def environment(tmp_path: Path) -> Iterator[Environment]:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'onboarding.db'}")
    Base.metadata.create_all(engine)
    sessions = create_session_factory(engine)
    root = tmp_path / "projects"
    root.mkdir()
    write_project(root, "example")
    workspaces = AgentWorkspaceService(
        sessions, data_root=tmp_path / "data", development_root=root
    )
    submitted: list[UUID] = []
    creation = McpWorkspaceCreationService(sessions, workspaces, submitted.append)
    administration = McpAdministrationService(sessions)
    client = administration.create_client(
        display_name="Selected local developer", preset_id=ADDON_DEVELOPMENT_PRESET_ID
    )
    other = administration.create_client(
        display_name="Unrelated developer", preset_id=ADDON_DEVELOPMENT_PRESET_ID
    )
    result = Environment(
        sessions,
        root,
        LocalProjectOnboardingService(
            sessions, development_root=root, workspaces=workspaces, creation=creation
        ),
        workspaces,
        creation,
        administration,
        client.id,
        other.id,
        submitted,
    )
    yield result
    engine.dispose()


def test_first_project_and_exact_client_authority(environment: Environment) -> None:
    env = environment
    result = env.service.onboard(env.choose())
    assert result.state == "preparing"
    assert env.submitted == [result.creation_operation_id]
    with env.sessions() as db:
        source = db.get(DevelopmentSourceRow, str(result.source_id))
        assert source is not None and source.relative_locator == "example"
        grants = db.scalars(select(McpClientDevelopmentSourceGrantRow)).all()
        assert [(g.client_id, g.development_source_id) for g in grants] == [
            (str(env.client_id), str(result.source_id))
        ]
        binding = db.get(McpAgentWorkspaceBindingRow, str(result.workspace_id))
        assert binding is not None and binding.client_id == str(env.client_id)
        assert db.scalar(select(func.count()).select_from(McpClientTargetGrantRow)) == 0
        assert (
            db.scalar(
                select(func.count())
                .select_from(McpSecurityEventRow)
                .where(McpSecurityEventRow.event_kind == "development_source_granted")
            )
            == 1
        )
    env.finish(result.creation_operation_id)
    ready = env.service.status(env.client_id, result.workspace_id)
    assert ready.state == "ready"
    assert "retained?workspace_id=" in ready.next_url
    assert (
        env.workspaces.read_file(result.workspace_id, "main.py").content
        == "print('original')\n"
    )
    with pytest.raises(LocalProjectError, match="not authorized"):
        env.service.status(env.other_client_id, result.workspace_id)


def test_existing_registration_identity_and_original_mcp_request_are_reused(
    environment: Environment,
) -> None:
    env = environment
    registration = DevelopmentSourceAdministrationService(
        env.sessions, DevelopmentSourceValidator(env.root)
    ).register_source(logical_name="Original name", relative_locator="example")
    env.administration.grant_development_source(env.client_id, registration.id)
    request_id = uuid4()
    allocation = env.creation.create(
        client_id=env.client_id, request_id=request_id, source_id=registration.id
    )
    env.finish(allocation.creation_operation_id)
    first = env.service.onboard(env.choose())
    second = env.service.onboard(env.choose())
    assert first == second
    assert first.source_id == registration.id
    assert first.workspace_id == allocation.workspace_id
    assert first.request_id == request_id
    assert first.creation_operation_id == allocation.creation_operation_id
    assert env.submitted == [allocation.creation_operation_id]
    with env.sessions() as db:
        assert db.scalar(select(func.count()).select_from(DevelopmentSourceRow)) == 1
        assert db.scalar(select(func.count()).select_from(AgentWorkspaceRow)) == 1
        assert (
            db.scalar(
                select(func.count()).select_from(McpClientDevelopmentSourceGrantRow)
            )
            == 1
        )


def test_nested_addon_and_bounded_discovery(environment: Environment) -> None:
    env = environment
    write_project(env.root, "checkout/script.nested", "script.nested")
    (env.root / "checkout" / ".git").mkdir()
    result = env.service.onboard(env.choose("checkout"))
    with env.sessions() as db:
        source = db.get(DevelopmentSourceRow, str(result.source_id))
        assert (
            source is not None and source.relative_locator == "checkout/script.nested"
        )
    assert env.service.projects() == ["checkout", "checkout/script.nested", "example"]
    assert (
        env.service.onboard(env.choose("checkout/script.nested")).workspace_id
        == result.workspace_id
    )


@pytest.mark.parametrize(
    "document",
    [
        "<broken",
        '<addon id="../escape" name="Bad" version="1">'
        '<extension point="xbmc.python.script"/></addon>',
        "<!DOCTYPE addon><addon/>",
        '<addon id="script.example" name="Bad" version=""/>',
    ],
)
def test_invalid_manifest_has_no_side_effects(
    environment: Environment, document: str
) -> None:
    env = environment
    (env.root / "example" / "addon.xml").write_text(document)
    with pytest.raises(LocalProjectError):
        env.service.onboard(env.choose())
    with env.sessions() as db:
        assert not db.scalars(select(DevelopmentSourceRow)).all()
        assert not db.scalars(select(McpClientDevelopmentSourceGrantRow)).all()


@pytest.mark.parametrize(
    "path",
    [
        "../outside",
        "/tmp/outside",
        "example/../../outside",
        "example//child",
        "example/./child",
        "example\\child",
        "missing",
    ],
)
def test_path_escapes_and_missing_sources_are_rejected(
    environment: Environment, path: str
) -> None:
    with pytest.raises(LocalProjectError):
        environment.service.onboard(environment.choose(path))
    assert environment.submitted == []


@pytest.mark.parametrize("kind", ["project", "parent", "manifest", "file", "nested"])
def test_symlinks_are_rejected(environment: Environment, kind: str) -> None:
    env = environment
    locator = "example"
    if kind == "manifest":
        source = env.root / "example" / "addon.xml"
        source.rename(source.with_name("real.xml"))
        source.symlink_to("real.xml")
    elif kind == "file":
        (env.root / "example" / "link.py").symlink_to("main.py")
    elif kind == "nested":
        (env.root / "checkout").mkdir()
        (env.root / "checkout" / "linked").symlink_to(
            env.root / "example", target_is_directory=True
        )
        locator = "checkout"
    else:
        (env.root / "linked").symlink_to(env.root / "example", target_is_directory=True)
        locator = "linked" if kind == "project" else "linked/child"
    with pytest.raises(LocalProjectError):
        env.service.onboard(env.choose(locator))
    assert env.submitted == []


def test_manifest_fifo_does_not_block(environment: Environment) -> None:
    env = environment
    manifest = env.root / "example" / "addon.xml"
    manifest.unlink()
    os.mkfifo(manifest)
    with pytest.raises(LocalProjectError):
        env.service.onboard(env.choose())


@pytest.mark.parametrize("change", ["different_checkout", "changed_identity"])
def test_conflicting_source_identity_preserves_original(
    environment: Environment, change: str
) -> None:
    env = environment
    first = env.service.onboard(env.choose())
    locator = "example"
    if change == "different_checkout":
        write_project(env.root, "other")
        locator = "other"
    else:
        write_project(env.root, "example", "script.replacement")
    with pytest.raises(LocalProjectError) as caught:
        env.service.onboard(env.choose(locator))
    assert caught.value.code == "project_source_conflict"
    with env.sessions() as db:
        assert db.scalar(select(func.count()).select_from(DevelopmentSourceRow)) == 1
        assert db.get(DevelopmentSourceRow, str(first.source_id)) is not None
    assert env.submitted == [first.creation_operation_id]


def test_source_changes_and_workspace_edits_are_kept(environment: Environment) -> None:
    env = environment
    first = env.service.onboard(env.choose())
    env.finish(first.creation_operation_id)
    env.workspaces.replace_text_file(
        first.workspace_id,
        expected_generation=0,
        relative_path="main.py",
        content="private edit\n",
    )
    (env.root / "example" / "main.py").write_text("new checkout bytes\n")
    manifest = env.root / "example" / "addon.xml"
    manifest.write_text(
        manifest.read_text().replace('version="1.0.0"', 'version="2.0.0"')
    )
    again = env.service.onboard(env.choose())
    assert again.workspace_id == first.workspace_id and again.generation == 1
    assert again.version == "1.0.0"
    assert (
        env.workspaces.read_file(first.workspace_id, "main.py").content
        == "private edit\n"
    )
    assert "source scan/capture" in again.message


def test_concurrent_and_replayed_onboarding_only_allocates_once(
    environment: Environment,
) -> None:
    env = environment
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: env.service.onboard(env.choose()), range(8)))
    assert len({r.source_id for r in results}) == 1
    assert len({r.workspace_id for r in results}) == 1
    assert len({r.request_id for r in results}) == 1
    assert len({r.creation_operation_id for r in results}) == 1
    assert len(env.submitted) == 1


def test_interrupted_dispatch_and_reconciliation_keep_operation(
    environment: Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = environment

    def interrupted(_operation: UUID) -> None:
        raise RuntimeError("response lost after commit")

    monkeypatch.setattr(env.creation, "_submit_creation", interrupted)
    first = env.service.onboard(env.choose())
    assert first.state == "preparing"
    assert env.service.onboard(env.choose()) == first
    with env.sessions.begin() as db:
        workspace = db.get(AgentWorkspaceRow, str(first.workspace_id))
        operation = db.get(
            AgentWorkspaceCreationOperationRow, str(first.creation_operation_id)
        )
        assert workspace is not None and operation is not None
        workspace.state = AgentWorkspaceState.RECONCILIATION_REQUIRED
        operation.state = AgentWorkspaceCreationState.RECONCILIATION_REQUIRED
        operation.completed_at = (
            datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
        )
        operation.failure_code = "test_uncertain_capture"
    uncertain = env.service.onboard(env.choose())
    assert uncertain.state == "needs_review"
    assert uncertain.request_id == first.request_id
    assert uncertain.creation_operation_id == first.creation_operation_id


def test_workspace_reuse_survives_request_mapping_pruning(
    environment: Environment,
) -> None:
    env = environment
    first = env.service.onboard(env.choose())
    env.finish(first.creation_operation_id)
    with env.sessions.begin() as db:
        request = db.scalar(select(McpDevelopmentRequestRow))
        assert request is not None
        db.delete(request)
    again = env.service.onboard(env.choose())
    assert again.workspace_id == first.workspace_id
    assert env.submitted == [first.creation_operation_id]


def test_another_clients_workspace_is_never_adopted(environment: Environment) -> None:
    env = environment
    first = env.service.onboard(env.choose())
    env.finish(first.creation_operation_id)
    other = env.service.onboard(
        LocalProjectSelection(client_id=env.other_client_id, project="example")
    )
    assert other.workspace_id != first.workspace_id
    assert other.source_id == first.source_id
    with env.sessions() as db:
        binding = db.get(McpAgentWorkspaceBindingRow, str(first.workspace_id))
        assert binding is not None and binding.client_id == str(env.client_id)


@pytest.mark.parametrize("kind", ["readonly", "disabled"])
def test_ineligible_client_receives_no_automatic_grant(
    environment: Environment, kind: str
) -> None:
    env = environment
    client_id = env.client_id
    if kind == "readonly":
        client_id = env.administration.create_client(
            display_name="Read only", preset_id=READ_ONLY_INSPECTION_PRESET_ID
        ).id
    else:
        env.administration.set_client_enabled(client_id, False)
    with pytest.raises(LocalProjectError) as caught:
        env.service.onboard(
            LocalProjectSelection(client_id=client_id, project="example")
        )
    assert caught.value.code == "development_client_ineligible"
    with env.sessions() as db:
        assert not db.scalars(select(McpClientDevelopmentSourceGrantRow)).all()
        assert not db.scalars(select(DevelopmentSourceRow)).all()


def test_ambiguous_layout_and_discovery_bounds(environment: Environment) -> None:
    env = environment
    write_project(env.root, "multi/one", "script.one")
    write_project(env.root, "multi/two", "script.two")
    with pytest.raises(LocalProjectError, match="exactly one"):
        env.service.onboard(env.choose("multi"))
    for index in range(129):
        (env.root / "multi" / f"entry{index}").touch()
    with pytest.raises(LocalProjectError) as caught:
        env.service.onboard(env.choose("multi"))
    assert caught.value.code == "project_discovery_limit"
    assert env.service.onboard(env.choose("multi/one")).addon_id == "script.one"


def test_missing_root_never_guesses_directory(environment: Environment) -> None:
    with pytest.raises(LocalProjectError) as caught:
        LocalProjectResolver(None).resolve("example")
    assert caught.value.code == "development_root_not_configured"


def test_interruption_after_source_grant_recovers_partial_setup(
    environment: Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kodi_lab.application.mcp_workspace_mutations import McpWorkspaceMutationError

    env = environment
    create = env.creation.create

    def unavailable(**_kwargs: object) -> None:
        raise McpWorkspaceMutationError("workspace_creation_ambiguous")

    monkeypatch.setattr(env.creation, "create", unavailable)
    with pytest.raises(LocalProjectError):
        env.service.onboard(env.choose())
    with env.sessions() as db:
        sources = db.scalars(select(DevelopmentSourceRow)).all()
        assert len(sources) == 1
        original_source = sources[0].id
        assert not db.scalars(select(AgentWorkspaceRow)).all()
    monkeypatch.setattr(env.creation, "create", create)
    recovered = env.service.onboard(env.choose())
    assert str(recovered.source_id) == original_source
    with env.sessions() as db:
        assert (
            db.scalar(
                select(func.count()).select_from(McpClientDevelopmentSourceGrantRow)
            )
            == 1
        )
        assert (
            db.scalar(
                select(func.count())
                .select_from(McpSecurityEventRow)
                .where(McpSecurityEventRow.event_kind == "development_source_granted")
            )
            == 1
        )


def test_previously_web_bound_workspace_is_reused(environment: Environment) -> None:
    env = environment
    source = DevelopmentSourceAdministrationService(
        env.sessions, DevelopmentSourceValidator(env.root)
    ).register_source(logical_name="Existing project", relative_locator="example")
    env.administration.grant_development_source(env.client_id, source.id)
    allocation = env.workspaces.queue_creation(source.id)
    env.finish(allocation.operation_id)
    env.administration.bind_workspace(env.client_id, allocation.workspace_id)
    reused = env.service.onboard(env.choose())
    assert reused.workspace_id == allocation.workspace_id
    assert reused.creation_operation_id == allocation.operation_id
    assert env.submitted == []


def test_multiple_owned_workspaces_require_explicit_selection(
    environment: Environment,
) -> None:
    env = environment
    first = env.service.onboard(env.choose())
    env.finish(first.creation_operation_id)
    second = env.creation.create(
        client_id=env.client_id, request_id=uuid4(), source_id=first.source_id
    )
    env.finish(second.creation_operation_id)
    with pytest.raises(LocalProjectError) as caught:
        env.service.onboard(env.choose())
    assert caught.value.code == "project_workspaces_ambiguous"
    assert len(env.workspaces.list_workspaces()) == 2


def test_nested_source_escape_is_fenced_by_existing_capture(
    environment: Environment,
) -> None:
    env = environment
    resources = env.root / "example" / "resources"
    resources.mkdir()
    (resources / "outside").symlink_to(env.root.parent, target_is_directory=True)
    allocated = env.service.onboard(env.choose())
    env.finish(allocated.creation_operation_id)
    fenced = env.service.onboard(env.choose())
    assert fenced.state == "needs_review"
    assert fenced.workspace_id == allocated.workspace_id
    assert fenced.creation_operation_id == allocated.creation_operation_id
    assert env.submitted == [allocated.creation_operation_id]
