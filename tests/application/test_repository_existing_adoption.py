"""An existing install earns provenance only from the exact approved bytes."""

from __future__ import annotations

import tempfile
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select

from kodi_lab.application.mcp_administration import (
    ADDON_DEVELOPMENT_PRESET_ID,
    McpAdministrationService,
    McpAuthenticationService,
)
from kodi_lab.application.mcp_workspace_development import McpWorkspaceDevelopmentError
from kodi_lab.mcp.authorization import (
    McpAuthorizationError,
    McpAuthorizationService,
    TargetTechnicalAuthority,
)
from kodi_lab.mcp.registry import TargetClass, build_mcp_tool_registry
from kodi_lab.repository.development import load_current
from kodi_lab.repository.workspace_package import build_workspace_package
from kodi_lab.storage.models import (
    AgentWorkspaceRow,
    DevelopmentPublicationRow,
    ExternalBridgePairingRow,
    ExternalDevelopmentRepositoryReadinessRow,
    ExternalKodiTargetRow,
    McpClientTargetGrantRow,
    McpTargetPresetAssignmentRow,
    McpWorkspaceDeploymentOperationRow,
    McpWorkspaceExecutionOperationRow,
    McpWorkspaceValidationRow,
    RepositoryWorkspaceDeploymentRow,
    RepositoryWorkspaceValidationSnapshotRow,
    TargetRow,
)
from kodi_lab.workspace import capture_workspace_deployment_tree
from tests.application.test_mcp_workspace_development import (
    EXTERNAL_TARGET_ID,
    NOW,
    SOURCE_ID,
)
from tests.application.test_repository_workspace_deployment import (
    ADDON,
    _approve,
    _setup,
    _version,
)


def _approved_existing(
    tmp_path: Path,
    *,
    missing_capability: bool = False,
    without_execution_capability: bool = False,
    database_path: Path | None = None,
) -> tuple[Any, Any, Any, DevelopmentPublicationRow, dict[str, str]]:
    env, repository, target = _setup(
        tmp_path,
        database_path=database_path,
        configured_capabilities=(
            ["target_inspect"]
            if missing_capability
            else [
                "addon_inventory",
                *([] if without_execution_capability else ["fixed_execution"]),
                "target_inspect",
                "workspace_deployment",
            ]
        ),
    )
    generation = _version(env, 0, "1.0.1")
    with tempfile.TemporaryDirectory(dir=tmp_path) as directory:
        stage = Path(directory)
        manifest = capture_workspace_deployment_tree(
            tmp_path / "data", env.workspace_id, stage_root=stage
        )
        package = build_workspace_package(ADDON, manifest, stage)
    with env.sessions.begin() as session:
        validation = session.scalar(
            select(McpWorkspaceValidationRow).where(
                McpWorkspaceValidationRow.workspace_id == str(env.workspace_id)
            )
        )
        assert validation is not None
        session.add(
            RepositoryWorkspaceValidationSnapshotRow(
                id=validation.id,
                workspace_id=str(env.workspace_id),
                generation=generation,
                addon_id=ADDON,
                addon_version="1.0.1",
                extension_types_json=validation.extension_types_json,
                source_tree_digest=manifest.digest,
                validated_at=validation.validated_at,
            )
        )
        receipt = UUID(validation.id)
    publication = repository.prepare_workspace(
        package.content,
        ("omega",),
        operation_id=uuid4(),
        workspace_id=env.workspace_id,
        workspace_generation=generation,
        target_id=EXTERNAL_TARGET_ID,
        validation_receipt=receipt,
        source_tree_sha256=manifest.digest,
    )
    publication = repository.approve(
        UUID(publication.id), observed_sha256=publication.zip_sha256
    )
    target.installed_version = "1.0.1"
    target.installed_digest = package.installed_manifest_digest
    target.installed_files = package.file_count
    target.installed_bytes = package.total_bytes
    identity = {
        "addon_id": ADDON,
        "addon_version": "1.0.1",
        "zip_sha256": publication.zip_sha256,
        "expected_manifest_digest": package.installed_manifest_digest,
    }
    return env, repository, target, publication, identity


def _adopt(
    env: Any, publication: DevelopmentPublicationRow, identity: dict[str, str]
) -> Any:
    return env.development.verify_existing_repository_installation(
        target_id=EXTERNAL_TARGET_ID,
        publication_id=UUID(publication.id),
        **identity,
    )


def test_exact_existing_adoption_records_web_origin_and_reuses_result(
    tmp_path: Path,
) -> None:
    env, repository, target, publication, identity = _approved_existing(tmp_path)
    _, before_feed = load_current(repository.root)
    first = _adopt(env, publication, identity)
    second = _adopt(env, publication, identity)
    _, after_feed = load_current(repository.root)
    assert after_feed == before_feed
    assert first.deployment_id == second.deployment_id
    assert first.state == "succeeded"
    assert first.origin == "verified_existing"
    assert first.installation_kind == "existing"
    assert target.executions == 0
    with env.sessions() as session:
        operations = list(session.scalars(select(McpWorkspaceDeploymentOperationRow)))
        assert len(operations) == 1
        operation = operations[0]
        linked = session.get(RepositoryWorkspaceDeploymentRow, operation.id)
        assert operation.client_id is None and operation.initiator_kind == "web"
        assert operation.deployment_mode == "repository"
        assert operation.mutation_started is False
        assert operation.sync_started is False and operation.sync_completed is False
        assert operation.result_classification == "repository_verified"
        assert linked is not None
        assert linked.origin == "verified_existing"
        assert linked.verified_by == "local_admin"
        assert linked.approved_at is None
        assert linked.observed_at is not None and linked.verified_at is not None
        assert linked.validation_receipt == publication.validation_receipt
        assert session.scalar(select(McpClientTargetGrantRow)) is None
        assert session.scalar(select(McpTargetPresetAssignmentRow)) is None
        assert session.scalar(select(McpWorkspaceExecutionOperationRow)) is None
        assert len(list(session.scalars(select(DevelopmentPublicationRow)))) == 1


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ("kind", "adoption_prerequisite_unconfirmed"),
        ("disabled", "adoption_prerequisite_unconfirmed"),
        ("readiness", "adoption_prerequisite_unconfirmed"),
        ("bridge", "adoption_prerequisite_unconfirmed"),
        ("kodi", "adoption_prerequisite_unconfirmed"),
        ("bridge_identity", "adoption_target_unverified"),
        ("native_kodi", "adoption_installed_state_mismatch"),
        ("capability", "adoption_capability_unavailable"),
        ("publication_missing", "adoption_publication_ineligible"),
        ("publication_unapproved", "adoption_publication_ineligible"),
        ("web_import", "adoption_publication_ineligible"),
        ("generation", "adoption_publication_ineligible"),
        ("validation", "adoption_publication_ineligible"),
        ("feed", "adoption_artifact_unverified"),
        ("artifact", "adoption_artifact_unverified"),
        ("addon_missing", "adoption_installed_state_mismatch"),
        ("version", "adoption_installed_state_mismatch"),
        ("addon_disabled", "adoption_installed_state_mismatch"),
        ("tree", "adoption_installed_state_mismatch"),
        ("files", "adoption_installed_state_mismatch"),
        ("bytes", "adoption_installed_state_mismatch"),
        ("confirmation", "adoption_confirmation_stale"),
        ("confirmation_tree", "adoption_confirmation_stale"),
        ("confirmation_version", "adoption_confirmation_stale"),
    ],
)
def test_existing_adoption_fails_closed(
    tmp_path: Path, change: str, expected: str
) -> None:
    env, repository, target, publication, identity = _approved_existing(
        tmp_path, missing_capability=change == "capability"
    )
    with env.sessions.begin() as session:
        common = session.get(TargetRow, str(EXTERNAL_TARGET_ID))
        external = session.get(ExternalKodiTargetRow, str(EXTERNAL_TARGET_ID))
        pairing = session.get(ExternalBridgePairingRow, str(EXTERNAL_TARGET_ID))
        readiness = session.get(
            ExternalDevelopmentRepositoryReadinessRow, str(EXTERNAL_TARGET_ID)
        )
        workspace = session.get(AgentWorkspaceRow, str(env.workspace_id))
        current = session.get(DevelopmentPublicationRow, publication.id)
        snapshot = session.get(
            RepositoryWorkspaceValidationSnapshotRow, publication.validation_receipt
        )
        assert all(
            item is not None
            for item in (
                common,
                external,
                pairing,
                readiness,
                workspace,
                current,
                snapshot,
            )
        )
        assert (
            common
            and external
            and pairing
            and readiness
            and workspace
            and current
            and snapshot
        )
        if change == "kind":
            common.target_type = "external_production"
        elif change == "disabled":
            external.administrative_state = "disabled"
        elif change == "readiness":
            readiness.base_url = "http://stale.invalid"
        elif change == "bridge":
            readiness.bridge_id = str(uuid4())
        elif change == "kodi":
            readiness.native_kodi_version = "20.5"
        elif change == "publication_unapproved":
            current.approval_identity = None
        elif change == "web_import":
            current.source_kind = "web_import"
            current.workspace_id = None
            current.workspace_generation = None
            current.validation_receipt = None
            current.source_tree_sha256 = None
        elif change == "generation":
            workspace.generation += 1
        elif change == "validation":
            snapshot.source_tree_digest = "sha256:" + "0" * 64
        elif change == "publication_missing":
            pass
    if change == "publication_missing":
        publication.id = str(uuid4())
    elif change == "feed":
        pointer = repository.root / "CURRENT"
        pointer.write_text("0" * 64 + "\n")
    elif change == "artifact":
        artifact = repository.root / "packages" / ADDON / f"{ADDON}-1.0.1.zip"
        altered = artifact.read_bytes() + b"tampered"
        artifact.unlink()
        artifact.write_bytes(altered)
    elif change == "addon_missing":
        target.installed_version = None
    elif change == "bridge_identity":
        target.bridge_error = "bridge_identity_mismatch"
    elif change == "native_kodi":
        target.kodi_release = "20.5"
    elif change == "version":
        target.installed_version = "1.0.0"
    elif change == "addon_disabled":
        target.enabled = False
    elif change == "tree":
        target.installed_digest = "sha256:" + "0" * 64
    elif change == "files":
        target.installed_files += 1
    elif change == "bytes":
        target.installed_bytes += 1
    elif change == "confirmation":
        identity["zip_sha256"] = "0" * 64
    elif change == "confirmation_tree":
        identity["expected_manifest_digest"] = "sha256:" + "0" * 64
    elif change == "confirmation_version":
        identity["addon_version"] = "9.9.9"
    with pytest.raises(McpWorkspaceDevelopmentError, match=expected):
        _adopt(env, publication, identity)
    with env.sessions() as session:
        assert session.scalar(select(McpWorkspaceDeploymentOperationRow)) is None


@pytest.mark.parametrize("conflict", ["artifact", "version"])
def test_conflicting_completed_result_rejected(tmp_path: Path, conflict: str) -> None:
    env, _, _, publication, identity = _approved_existing(tmp_path)
    _adopt(env, publication, identity)
    with env.sessions.begin() as session:
        linked = session.scalar(select(RepositoryWorkspaceDeploymentRow))
        operation = session.scalar(select(McpWorkspaceDeploymentOperationRow))
        assert linked is not None and operation is not None
        if conflict == "artifact":
            linked.zip_sha256 = "0" * 64
        else:
            operation.addon_version = "9.9.9"
    with pytest.raises(McpWorkspaceDevelopmentError, match="adoption_conflict"):
        _adopt(env, publication, identity)


def test_pending_target_operation_blocks_exact_repeat(tmp_path: Path) -> None:
    env, _, _, publication, identity = _approved_existing(tmp_path)
    _adopt(env, publication, identity)
    with env.sessions.begin() as session:
        completed = session.scalar(select(McpWorkspaceDeploymentOperationRow))
        assert completed is not None
        values = {
            column.key: getattr(completed, column.key)
            for column in McpWorkspaceDeploymentOperationRow.__table__.columns
        }
        values.update(
            id=str(uuid4()),
            client_id=str(env.client_id),
            initiator_kind="mcp",
            request_id=str(uuid4()),
            state="pending",
            phase="claimed",
            file_count=None,
            total_bytes=None,
            inventory_classification="not_checked",
            addon_enabled=None,
            result_classification=None,
            source_manifest_digest=None,
            verified_destination_digest=None,
            started_at=None,
            completed_at=None,
        )
        session.add(McpWorkspaceDeploymentOperationRow(**values))
    with pytest.raises(McpWorkspaceDevelopmentError, match="adoption_conflict"):
        _adopt(env, publication, identity)


def test_adopted_history_survives_operation_retention(tmp_path: Path) -> None:
    env, _, _, publication, identity = _approved_existing(tmp_path)
    adopted = _adopt(env, publication, identity)
    with env.sessions.begin() as session:
        workspace = session.get(AgentWorkspaceRow, str(env.workspace_id))
        assert workspace is not None
        workspace.generation += 1
    with env.sessions.begin() as session:
        env.development._prune_and_require_capacity(
            session, env.client_id, NOW + timedelta(days=365)
        )
    with env.sessions() as session:
        assert (
            session.get(McpWorkspaceDeploymentOperationRow, str(adopted.deployment_id))
            is not None
        )
        assert (
            session.get(RepositoryWorkspaceDeploymentRow, str(adopted.deployment_id))
            is not None
        )


def test_adopted_result_needs_separate_execution_authority(tmp_path: Path) -> None:
    env, _, target, publication, identity = _approved_existing(
        tmp_path, without_execution_capability=True
    )
    _adopt(env, publication, identity)
    administration = McpAdministrationService(env.sessions)
    administration.assign_target_preset(EXTERNAL_TARGET_ID, ADDON_DEVELOPMENT_PRESET_ID)
    administration.grant_target(env.client_id, EXTERNAL_TARGET_ID)
    administration.grant_development_source(env.client_id, SOURCE_ID)
    credential = administration.issue_credential(env.client_id)
    principal = McpAuthenticationService(env.sessions).authenticate(credential.token)
    assert principal is not None

    class Authority:
        allow_execution = False

        def resolve(self, target_id: UUID) -> TargetTechnicalAuthority:
            assert target_id == EXTERNAL_TARGET_ID
            return TargetTechnicalAuthority(
                target_class=TargetClass.EXTERNAL_DEVELOPMENT,
                capabilities=frozenset(
                    {"workspace_deployment", "workspace_execution"}
                    if self.allow_execution
                    else {"workspace_deployment"}
                ),
                current_states=frozenset({"running", "ready", "reconciled"}),
            )

    technical = Authority()
    authorization = McpAuthorizationService(
        env.sessions, build_mcp_tool_registry(), targets=technical
    )
    with pytest.raises(McpAuthorizationError, match="deployment_not_allowed"):
        authorization.authorize_workspace_target(
            principal, "workspace_execute", env.workspace_id, EXTERNAL_TARGET_ID
        )
    assert target.executions == 0


def test_adopted_result_can_execute_after_separate_authority_and_live_check(
    tmp_path: Path,
) -> None:
    env, _, target, publication, identity = _approved_existing(tmp_path)
    _adopt(env, publication, identity)
    administration = McpAdministrationService(env.sessions)
    administration.assign_target_preset(EXTERNAL_TARGET_ID, ADDON_DEVELOPMENT_PRESET_ID)
    administration.grant_target(env.client_id, EXTERNAL_TARGET_ID)
    administration.grant_development_source(env.client_id, SOURCE_ID)
    credential = administration.issue_credential(env.client_id)
    principal = McpAuthenticationService(env.sessions).authenticate(credential.token)
    assert principal is not None

    class Authority:
        def resolve(self, target_id: UUID) -> TargetTechnicalAuthority:
            assert target_id == EXTERNAL_TARGET_ID
            return TargetTechnicalAuthority(
                target_class=TargetClass.EXTERNAL_DEVELOPMENT,
                capabilities=frozenset({"workspace_deployment", "workspace_execution"}),
                current_states=frozenset({"running", "ready", "reconciled"}),
            )

    authorization = McpAuthorizationService(
        env.sessions, build_mcp_tool_registry(), targets=Authority()
    )
    authorization.authorize_workspace_target(
        principal, "workspace_execute", env.workspace_id, EXTERNAL_TARGET_ID
    )
    executed = env.development.execute(
        client_id=env.client_id,
        request_id=uuid4(),
        workspace_id=env.workspace_id,
        expected_generation=publication.workspace_generation,
        target_id=EXTERNAL_TARGET_ID,
    )
    assert executed.state == "succeeded" and target.executions == 1


def test_0059_planned_record_upgrades_without_rewriting_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_root = tmp_path / "data"
    database_path = data_root / "database" / "kodi-lab.db"
    database_path.parent.mkdir(parents=True)
    monkeypatch.setenv("KODI_LAB_DATA_ROOT", str(data_root))
    monkeypatch.delenv("KODI_LAB_RUNTIME_ROOT", raising=False)
    config = Config(str(Path(__file__).parents[2] / "alembic.ini"))
    command.upgrade(config, "0060_repository_existing_verify")
    env, _, target = _setup(tmp_path, database_path=database_path)
    generation = _version(env, 0, "1.0.1")
    pending = env.development.deploy(
        client_id=env.client_id,
        request_id=uuid4(),
        workspace_id=env.workspace_id,
        expected_generation=generation,
        target_id=EXTERNAL_TARGET_ID,
    )
    _approve(env, pending)
    with env.sessions() as session:
        linked = session.get(
            RepositoryWorkspaceDeploymentRow, str(pending.deployment_id)
        )
        assert linked is not None
        target.installed_version = linked.addon_version
        target.installed_digest = linked.expected_manifest_digest
        target.installed_files = linked.file_count
        target.installed_bytes = linked.total_bytes
    assert (
        env.development.recheck_repository_deployment(pending.deployment_id).state
        == "succeeded"
    )
    with env.sessions() as session:
        operation = session.get(
            McpWorkspaceDeploymentOperationRow, str(pending.deployment_id)
        )
        linked = session.get(
            RepositoryWorkspaceDeploymentRow, str(pending.deployment_id)
        )
        assert operation is not None and linked is not None
        before = (
            operation.client_id,
            operation.request_id,
            linked.approved_at,
            linked.observed_at,
            linked.verified_at,
            linked.publication_id,
        )
        assert operation.initiator_kind == "mcp" and linked.origin == "planned"
    env.sessions.kw["bind"].dispose()
    command.downgrade(config, "0059_workspace_validation_digest")
    command.upgrade(config, "0060_repository_existing_verify")
    with env.sessions() as session:
        operation = session.get(
            McpWorkspaceDeploymentOperationRow, str(pending.deployment_id)
        )
        linked = session.get(
            RepositoryWorkspaceDeploymentRow, str(pending.deployment_id)
        )
        assert operation is not None and linked is not None
        assert operation.initiator_kind == "mcp"
        assert linked.origin == "planned" and linked.verified_by is None
        assert (
            operation.client_id,
            operation.request_id,
            linked.approved_at,
            linked.observed_at,
            linked.verified_at,
            linked.publication_id,
        ) == before


def test_0060_migrated_schema_accepts_exact_adoption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_root = tmp_path / "data"
    database_path = data_root / "database" / "kodi-lab.db"
    database_path.parent.mkdir(parents=True)
    monkeypatch.setenv("KODI_LAB_DATA_ROOT", str(data_root))
    monkeypatch.delenv("KODI_LAB_RUNTIME_ROOT", raising=False)
    config = Config(str(Path(__file__).parents[2] / "alembic.ini"))
    command.upgrade(config, "0060_repository_existing_verify")
    command.check(config)
    env, _, _, publication, identity = _approved_existing(
        tmp_path, database_path=database_path
    )
    result = _adopt(env, publication, identity)
    assert result.origin == "verified_existing" and result.state == "succeeded"
    env.sessions.kw["bind"].dispose()
    with pytest.raises(RuntimeError, match="cannot be downgraded"):
        command.downgrade(config, "0059_workspace_validation_digest")
