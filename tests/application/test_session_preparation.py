"""Stored checklist composes existing projections in one read-only snapshot."""

from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import event, select, text

from kodi_lab.application.session_preparation import SessionPreparationService
from kodi_lab.storage.development_sessions import DevelopmentSessionConflict
from kodi_lab.storage.models import (
    AgentWorkspaceRow,
    DevelopmentSessionPolicyRow,
    DevelopmentSessionRow,
    ManagedInstanceRow,
    McpWorkspaceValidationRow,
    RuntimeRevisionRow,
)
from tests.application import test_mcp_workspace_development as fixtures
from tests.application.test_session_workspace_admission import bind
from tests.storage.test_migrations import (  # noqa: F401
    migration_setup,
    migration_templates,
)


@pytest.mark.parametrize("journal", ["DELETE", "WAL"])
@pytest.mark.parametrize("phase", ["bound", "reserved", "uncertain"])
def test_checklist_reads_only_and_keeps_live_and_agent_authority_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    journal: str,
    phase: str,
) -> None:
    env = fixtures._environment(tmp_path)
    bind(env)
    with env.sessions.begin() as session:
        current = session.get(DevelopmentSessionRow, 1)
        assert current is not None
        current.phase = phase
    engine = env.sessions.kw["bind"]
    with engine.connect() as connection:
        assert (
            connection.exec_driver_sql(f"PRAGMA journal_mode={journal}")
            .scalar()
            .upper()
            == journal
        )

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("stored checklist cannot invoke runtime machinery")

    for name in (
        "managed_container_inventory",
        "observe_managed_kodi_runtime",
        "start_managed_container",
        "stop_managed_container",
        "copy_sync_mcp_workspace",
    ):
        monkeypatch.setattr(env.broker, name, forbidden)
    statements: list[str] = []
    connections: set[int] = set()

    def record(connection: Any, _cursor: Any, sql: str, *_args: Any) -> None:
        command = sql.lstrip().split()[0].upper()
        statements.append(command)
        connections.add(id(connection))
        assert command in {"BEGIN", "SELECT", "PRAGMA"}

    event.listen(engine, "before_cursor_execute", record)
    try:
        result = SessionPreparationService(env.sessions).inspect(expected_generation=1)
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert result is not None and result.session.phase == phase
    assert statements[0] == "BEGIN" and len(connections) == 1
    assert result.session.workspace_id == env.workspace_id
    assert result.addon_id == "plugin.video.slice4" and result.workspace_generation == 0
    assert result.native_readiness == result.addon_enabled == "unknown"
    assert result.agent_authority == "not_assessed" and not result.reservation_created
    assert result.current_container == "not_observed"
    assert result.capabilities.current_container_binding == "not_observed"
    assert result.capabilities.native_readiness == "unknown"
    assert result.deployment.workspace_id == env.workspace_id
    assert result.deployment.verdict in {"blocked", "unknown"}
    assert (
        env.broker.stop_count
        == env.broker.sync_count
        == env.broker.start_count
        == env.smoke.count
        == 0
    )
    # Read completion leaves the connection usable for ordinary authorized writes.
    with env.sessions.begin() as session:
        current = session.get(DevelopmentSessionRow, 1)
        assert current is not None
        current.phase = "bound"


def test_no_session_and_stale_or_invalid_generation_are_explicit(
    tmp_path: Path,
) -> None:
    env = fixtures._environment(tmp_path)
    service = SessionPreparationService(env.sessions)
    assert service.inspect() is None
    with pytest.raises(DevelopmentSessionConflict, match="generation_conflict"):
        service.inspect(expected_generation=1)
    bind(env)
    for generation in (0, -1, True, 2):
        with pytest.raises(DevelopmentSessionConflict, match="generation_conflict"):
            service.inspect(expected_generation=generation)


@pytest.mark.parametrize("defect", ["workspace_source", "runtime", "closed_policy"])
def test_partial_identity_restore_refuses_a_mixed_checklist(
    tmp_path: Path, defect: str
) -> None:
    env = fixtures._environment(tmp_path)
    bind(
        env,
        mismatch="source"
        if defect == "workspace_source"
        else "revision"
        if defect == "runtime"
        else "",
    )
    if defect == "closed_policy":
        with env.sessions.begin() as session:
            current = session.get(DevelopmentSessionRow, 1)
            assert current is not None
            current.phase = "closed"
    with pytest.raises(
        DevelopmentSessionConflict, match=r"identity_changed|requires_review"
    ):
        SessionPreparationService(env.sessions).inspect(expected_generation=1)


def test_workspace_edit_changes_validation_facts_without_a_runtime_probe(
    tmp_path: Path,
) -> None:
    env = fixtures._environment(tmp_path)
    bind(env)
    with env.sessions.begin() as session:
        workspace = session.get(AgentWorkspaceRow, str(env.workspace_id))
        validation = session.scalar(
            select(McpWorkspaceValidationRow).where(
                McpWorkspaceValidationRow.workspace_id == str(env.workspace_id)
            )
        )
        assert workspace is not None and validation is not None
        validation.tree_digest = workspace.current_tree_digest
    service = SessionPreparationService(env.sessions)
    first = service.inspect()
    assert first is not None
    assert (
        next(c for c in first.deployment.checks if c.name == "validation").state
        == "satisfied"
    )
    env.workspaces.replace_text_file(
        env.workspace_id,
        expected_generation=0,
        relative_path="main.py",
        content="print('changed')\n",
    )
    edited = service.inspect(expected_generation=1)
    assert edited is not None and edited.workspace_generation == 1
    assert (
        next(c for c in edited.deployment.checks if c.name == "validation").reason
        == "validation_generation_mismatch"
    )
    assert env.broker.stop_count == env.broker.sync_count == 0


def test_concurrent_version_switch_cannot_mix_session_and_capability_snapshots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = fixtures._environment(tmp_path)
    bind(env)
    engine = env.sessions.kw["bind"]
    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA journal_mode=WAL").scalar() == "wal"
    service = SessionPreparationService(env.sessions)
    original = service._preflight.inspect
    new_revision = str(uuid4())

    def inspect(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        with env.sessions.begin() as writer:
            old = writer.get(RuntimeRevisionRow, str(fixtures.RUNTIME_REVISION_ID))
            current = writer.get(DevelopmentSessionRow, 1)
            policy = writer.get(DevelopmentSessionPolicyRow, 1)
            instance = writer.get(ManagedInstanceRow, str(fixtures.TARGET_ID))
            assert (
                old is not None
                and current is not None
                and policy is not None
                and instance is not None
            )
            values = {
                column.name: getattr(old, column.name)
                for column in old.__table__.columns
            }
            values.update(id=new_revision, revision_number=2)
            writer.add(RuntimeRevisionRow(**values))
            writer.flush()
            # Only temporary SQLite data changes: simulate an atomic generation
            # transition to test read isolation, with no resource dispatch.
            current.phase = "closed"
            writer.add(
                DevelopmentSessionRow(
                    generation=2,
                    request_id=str(uuid4()),
                    target_id=str(fixtures.TARGET_ID),
                    runtime_revision_id=new_revision,
                    workspace_id=str(env.workspace_id),
                    source_id=str(fixtures.SOURCE_ID),
                    container_id="b" * 64,
                    phase="bound",
                    created_at=fixtures.NOW_TEXT,
                    updated_at=fixtures.NOW_TEXT,
                )
            )
            writer.flush()
            instance.runtime_revision_id = new_revision
            policy.active_generation = 2
        return result

    monkeypatch.setattr(service._preflight, "inspect", inspect)
    result = service.inspect(expected_generation=1)
    assert result is not None and result.session.generation == 1
    assert str(result.capabilities.runtime_revision_id) == str(
        fixtures.RUNTIME_REVISION_ID
    )
    assert result.capabilities.kodi_release == "21.3"
    monkeypatch.setattr(service._preflight, "inspect", original)
    with pytest.raises(DevelopmentSessionConflict, match="generation_conflict"):
        service.inspect(expected_generation=1)
    current = service.inspect()
    assert current is not None and current.session.generation == 2
    assert str(current.capabilities.runtime_revision_id) == new_revision
    assert current.capabilities.kodi_release is None


def test_projection_runs_on_the_real_upgraded_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _config, url = migration_setup(monkeypatch, tmp_path, start_revision="head")
    env = fixtures._environment(
        tmp_path / "case", database_path=Path(url.removeprefix("sqlite:///"))
    )
    bind(env)
    result = SessionPreparationService(env.sessions).inspect(expected_generation=1)
    assert result is not None and result.session.generation == 1
    with env.sessions() as session:
        assert session.scalar(text("PRAGMA integrity_check")) == "ok"
