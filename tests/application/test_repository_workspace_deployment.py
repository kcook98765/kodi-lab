"""Repository publication is pending until native and bridge observations agree."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from kodi_lab.application.addon_inventory import (
    AddonInventoryQueryStatus,
    KodiAddonInventoryItem,
    KodiAddonProvenance,
    KodiAddonProvenanceClassification,
    KodiAddonProvenanceEvidenceStatus,
    ManagedInstanceAddonInventory,
)
from kodi_lab.application.agent_workspace_promotions import (
    AgentWorkspacePromotionService,
)
from kodi_lab.application.external_bridges import ExternalBridgeServiceError
from kodi_lab.application.mcp_administration import (
    McpAdministrationService,
    McpAuthenticationService,
)
from kodi_lab.application.mcp_development_requests import (
    prune_expired_safe_development_requests,
)
from kodi_lab.application.mcp_promotion_requests import (
    McpPromotionRequestError,
    McpPromotionRequestService,
)
from kodi_lab.application.mcp_workspace_development import McpWorkspaceDevelopmentError
from kodi_lab.domain import TargetType
from kodi_lab.kodi.bridge import BridgeAddonTree
from kodi_lab.mcp.authorization import McpAuthorizationService
from kodi_lab.mcp.registry import build_mcp_tool_registry
from kodi_lab.mcp.tools import McpTargetToolService
from kodi_lab.repository.development import (
    DevelopmentRepositoryService,
    bootstrap_zip,
    initialize,
    load_current,
)
from kodi_lab.repository.stable import generate as generate_stable
from kodi_lab.repository.workspace_package import bridge_manifest_for_zip
from kodi_lab.storage.models import (
    DevelopmentPublicationHoldRow,
    ExternalBridgePairingRow,
    ExternalKodiTargetRow,
    McpWorkspaceDeploymentOperationRow,
    RepositoryWorkspaceDeploymentRow,
    RepositoryWorkspaceValidationSnapshotRow,
    TargetRow,
)
from tests.application.test_mcp_workspace_development import (
    EXTERNAL_TARGET_ID,
    NOW,
    SOURCE_ID,
    _configure_external_environment,
    _environment,
    _restarted_development,
)
from tests.repository.test_development import BASE, phase2_candidate

ADDON = "plugin.video.slice4"
REPOSITORY = "repository.kodi-lab-dev"


def _item(addon_id: str, version: str, enabled: bool) -> KodiAddonInventoryItem:
    return KodiAddonInventoryItem(
        addon_id=addon_id,
        name=addon_id,
        version=version,
        enabled=enabled,
        kodi_type=(
            "xbmc.addon.repository"
            if addon_id == REPOSITORY
            else "xbmc.python.pluginsource"
        ),
        provenance=KodiAddonProvenance(
            classification=KodiAddonProvenanceClassification.UNKNOWN,
            evidence_status=KodiAddonProvenanceEvidenceStatus.UNKNOWN,
        ),
    )


@dataclass
class RepositoryTargetFixture:
    repository_digest: str
    repository_files: int
    repository_bytes: int
    kodi_release: str = "21.3"
    installed_version: str | None = None
    installed_digest: str | None = None
    installed_files: int = 0
    installed_bytes: int = 0
    enabled: bool = True
    offline: bool = False
    bridge_offline: bool = False
    bridge_error: str | None = None
    executions: int = 0

    def addon_inventory(self, target_id: UUID) -> ManagedInstanceAddonInventory:
        assert target_id == EXTERNAL_TARGET_ID
        if self.offline:
            raise OSError("disposable Kodi offline")
        items = [_item(REPOSITORY, "1.0.0", True)]
        if self.installed_version is not None:
            items.append(_item(ADDON, self.installed_version, self.enabled))
        return ManagedInstanceAddonInventory(
            instance_id=target_id,
            observed_at=NOW,
            kodi_release=self.kodi_release,
            query_status=AddonInventoryQueryStatus.AVAILABLE,
            complete=True,
            items=items,
        )

    def inspect_addon(self, target_id: UUID, addon_id: str) -> BridgeAddonTree:
        assert target_id == EXTERNAL_TARGET_ID
        if self.bridge_error is not None:
            raise ExternalBridgeServiceError(self.bridge_error)
        if self.bridge_offline:
            raise ExternalBridgeServiceError("bridge_unreachable")
        if self.offline:
            raise OSError("disposable Kodi offline")
        if addon_id == REPOSITORY:
            return BridgeAddonTree(
                addon_id=REPOSITORY,
                addon_version="1.0.0",
                manifest_digest=self.repository_digest,
                file_count=self.repository_files,
                total_bytes=self.repository_bytes,
                enabled=True,
            )
        assert addon_id == ADDON and self.installed_version is not None
        assert self.installed_digest is not None
        return BridgeAddonTree(
            addon_id=ADDON,
            addon_version=self.installed_version,
            manifest_digest=self.installed_digest,
            file_count=self.installed_files,
            total_bytes=self.installed_bytes,
            enabled=self.enabled,
        )

    def fixed_execute(self, target_id: UUID, addon_id: str, invocation_id: UUID) -> Any:
        from kodi_lab.kodi.addons import KodiAddonSmokeResult

        assert target_id == EXTERNAL_TARGET_ID and addon_id == ADDON and invocation_id
        self.executions += 1
        return KodiAddonSmokeResult(item_count=1, duration_ms=2)


def _setup(
    tmp_path: Path,
    *,
    database_path: Path | None = None,
    kodi_version: str = "21.3",
    configured_capabilities: list[str] | None = None,
) -> tuple[Any, DevelopmentRepositoryService, RepositoryTargetFixture]:
    env = _environment(tmp_path, database_path=database_path)
    _configure_external_environment(
        env, configured_capabilities=configured_capabilities
    )
    bundle = phase2_candidate(tmp_path / "candidate")
    root = tmp_path / "repository"
    root.mkdir()
    generate_stable(bundle, root, BASE)
    initialize(root / "development-data", BASE, bundle)
    repository = DevelopmentRepositoryService(env.sessions, root, BASE)
    repository.reconcile()
    digest, files, size = bridge_manifest_for_zip(bootstrap_zip(BASE), REPOSITORY)
    target = RepositoryTargetFixture(digest, files, size, kodi_release=kodi_version)
    if kodi_version != "21.3":
        with env.sessions.begin() as session:
            common = session.get(TargetRow, str(EXTERNAL_TARGET_ID))
            external = session.get(ExternalKodiTargetRow, str(EXTERNAL_TARGET_ID))
            pairing = session.get(ExternalBridgePairingRow, str(EXTERNAL_TARGET_ID))
            assert common is not None and external is not None and pairing is not None
            common.kodi_version = kodi_version
            external.verified_kodi_version = kodi_version
            external.observed_kodi_version = kodi_version
            pairing.pinned_kodi_version = kodi_version
            pairing.observed_kodi_version = kodi_version
    env.development.configure_repository(repository)
    env.development.configure_external(
        bridges=cast(Any, target), targets=cast(Any, target)
    )
    env.development.confirm_development_repository(
        target_id=EXTERNAL_TARGET_ID,
        base_url=BASE,
        reachable_in_kodi=True,
        manual_updates_confirmed=True,
    )
    return env, repository, target


def _version(
    env: Any, generation: int, version: str, *, python_version: str = "3.0.1"
) -> int:
    content = (
        f'<addon id="{ADDON}" name="Slice 4" version="{version}">'
        f'<requires><import addon="xbmc.python" version="{python_version}"/></requires>'
        '<extension point="xbmc.python.pluginsource" library="main.py">'
        "<provides>video</provides></extension></addon>\n"
    )
    edit = env.workspaces.replace_text_file(
        env.workspace_id,
        expected_generation=generation,
        relative_path="addon.xml",
        content=content,
    )
    env.development.record_validation(
        workspace_id=env.workspace_id,
        source_id=SOURCE_ID,
        generation=edit.generation,
        addon_id=ADDON,
        addon_version=version,
        extension_types=("xbmc.python.pluginsource",),
        validated_at=NOW,
    )
    return cast(int, edit.generation)


def _approve(env: Any, pending: Any) -> Any:
    return env.development.approve_repository_deployment(
        pending.deployment_id,
        workspace_id=env.workspace_id,
        target_id=EXTERNAL_TARGET_ID,
        generation=pending.generation,
        addon_id=ADDON,
        addon_version=pending.addon_version,
        zip_sha256=pending.zip_sha256,
        expected_manifest_digest=pending.expected_manifest_digest,
    )


def test_workspace_status_resumes_repository_deployment_from_bounded_projection(
    tmp_path: Path,
) -> None:
    env, repository, target = _setup(tmp_path)
    administration = McpAdministrationService(env.sessions)
    administration.grant_development_source(env.client_id, SOURCE_ID)
    credential = administration.issue_credential(env.client_id)
    principal = McpAuthenticationService(env.sessions).authenticate(credential.token)
    assert principal is not None
    tools = McpTargetToolService(
        env.sessions,
        McpAuthorizationService(env.sessions, build_mcp_tool_registry()),
        cast(Any, None),
        cast(Any, None),
        cast(Any, None),
        workspaces=env.workspaces,
        workspace_development=env.development,
    )

    def summary() -> Any:
        return tools.workspace_status(
            principal, workspace_id=env.workspace_id
        ).workspaces[0]

    initial = summary()
    assert initial.latest_deployment_id is None
    assert initial.latest_deployment_mode is None
    assert initial.latest_deployment_next_action is None

    generation = _version(env, 0, "1.0.0")
    pending = env.development.deploy(
        client_id=env.client_id,
        request_id=uuid4(),
        workspace_id=env.workspace_id,
        expected_generation=generation,
        target_id=EXTERNAL_TARGET_ID,
    )
    approval = summary()
    assert approval.creation_state == "ready"
    assert approval.generation == generation
    assert approval.latest_deployment_id == pending.deployment_id
    assert approval.latest_deployment_generation == generation
    assert approval.latest_deployment_target_id == EXTERNAL_TARGET_ID
    assert approval.latest_deployment_state == "running"
    assert approval.latest_deployment_mode == "repository"
    assert approval.latest_deployment_phase == "publication_approval"
    assert approval.latest_deployment_publication_id == pending.publication_id
    assert approval.latest_deployment_installation_kind == "first_install"
    assert approval.latest_deployment_next_action == (
        "Approve exact publication in Kodi Lab Web"
    )
    publication = repository.get_publication(pending.publication_id)
    assert publication is not None and publication.status == "prepared"

    _approve(env, pending)
    install = summary()
    assert install.latest_deployment_phase == "awaiting_kodi_update"
    assert install.latest_deployment_next_action == (
        "Install from Kodi Lab Development, then recheck"
    )
    with env.sessions.begin() as session:
        linked = session.get(
            RepositoryWorkspaceDeploymentRow, str(pending.deployment_id)
        )
        assert linked is not None
        linked.installed_before_version = "0.9.0"
    update = summary()
    assert update.latest_deployment_installation_kind == "update"
    assert update.latest_deployment_next_action == (
        "Update in Kodi Add-ons, then recheck"
    )
    with env.sessions.begin() as session:
        linked = session.get(
            RepositoryWorkspaceDeploymentRow, str(pending.deployment_id)
        )
        operation = session.get(
            McpWorkspaceDeploymentOperationRow, str(pending.deployment_id)
        )
        assert linked is not None and operation is not None
        linked.state = "observed_identity"
        linked.installed_before_version = None
        operation.phase = "verify_tree"
    recheck = summary()
    assert recheck.latest_deployment_phase == "verify_tree"
    assert recheck.latest_deployment_next_action == (
        "Reconnect the bridge, then recheck the installed tree"
    )
    with env.sessions.begin() as session:
        linked = session.get(
            RepositoryWorkspaceDeploymentRow, str(pending.deployment_id)
        )
        operation = session.get(
            McpWorkspaceDeploymentOperationRow, str(pending.deployment_id)
        )
        assert linked is not None and operation is not None
        linked.state = "awaiting_kodi_update"
        operation.phase = "awaiting_kodi_update"
        target.installed_version = "1.0.0"
        target.installed_digest = linked.expected_manifest_digest
        target.installed_files = linked.file_count
        target.installed_bytes = linked.total_bytes
    completed = env.development.recheck_repository_deployment(pending.deployment_id)
    assert completed.state == "succeeded"
    done = summary()
    assert done.latest_deployment_state == "succeeded"
    assert done.latest_deployment_next_action is None
    assert done.latest_deployment_id == pending.deployment_id
    assert done.latest_deployment_publication_id == pending.publication_id
    edited = env.workspaces.replace_text_file(
        env.workspace_id,
        expected_generation=generation,
        relative_path="main.py",
        content="pass\n# next generation\n",
    )
    assert edited.generation == generation + 1
    stale = summary()
    assert stale.generation == generation + 1
    assert stale.latest_deployment_generation == generation

    with env.sessions.begin() as session:
        linked = session.get(
            RepositoryWorkspaceDeploymentRow, str(pending.deployment_id)
        )
        operation = session.get(
            McpWorkspaceDeploymentOperationRow, str(pending.deployment_id)
        )
        assert linked is not None and operation is not None
        linked.state = "failed"
        operation.state = "failed"
        operation.failure_code = "private_path_/tmp/internal"
        operation.phase = "verify_tree"
    failed = summary()
    assert failed.latest_deployment_state == "failed"
    assert failed.latest_deployment_next_action is None
    assert "private_path_/tmp/internal" not in failed.model_dump_json()
    with env.sessions.begin() as session:
        operation = session.get(
            McpWorkspaceDeploymentOperationRow, str(pending.deployment_id)
        )
        assert operation is not None
        operation.state = "reconciliation_required"
    recovery = summary()
    assert recovery.latest_deployment_state == "reconciliation_required"
    assert recovery.latest_deployment_next_action is None


def test_first_install_approval_pending_observation_and_exact_tree(
    tmp_path: Path,
) -> None:
    env, repository, target = _setup(tmp_path)
    generation = _version(env, 0, "1.0.0")
    pending = env.development.deploy(
        client_id=env.client_id,
        request_id=uuid4(),
        workspace_id=env.workspace_id,
        expected_generation=generation,
        target_id=EXTERNAL_TARGET_ID,
    )
    assert pending.deployment_mode == "repository"
    assert pending.repository_id == REPOSITORY
    assert pending.installation_kind == "first_install"
    assert pending.state == "running" and pending.phase == "publication_approval"
    assert repository.list_publications()[0].status == "prepared"
    with pytest.raises(McpWorkspaceDevelopmentError, match="execution_not_deployed"):
        env.development.execute(
            client_id=env.client_id,
            request_id=uuid4(),
            workspace_id=env.workspace_id,
            expected_generation=generation,
            target_id=EXTERNAL_TARGET_ID,
        )
    promotion_service = McpPromotionRequestService(
        env.sessions,
        AgentWorkspacePromotionService(
            env.sessions,
            data_root=tmp_path / "data",
            development_root=tmp_path / "sources",
            verification_publisher=lambda _session, _operation: uuid4(),
            clock=lambda: NOW,
        ),
        clock=lambda: NOW,
    )
    with pytest.raises(McpPromotionRequestError, match="deployment_required"):
        promotion_service.request(
            client_id=env.client_id,
            request_id=uuid4(),
            workspace_id=env.workspace_id,
            expected_generation=generation,
            target_id=EXTERNAL_TARGET_ID,
        )
    with env.sessions() as session:
        assert (
            session.scalar(
                select(DevelopmentPublicationHoldRow).where(
                    DevelopmentPublicationHoldRow.reference_id
                    == str(pending.deployment_id)
                )
            )
            is not None
        )
    with pytest.raises(
        McpWorkspaceDevelopmentError, match="publication_approval_mismatch"
    ):
        env.development.approve_repository_deployment(
            pending.deployment_id,
            workspace_id=env.workspace_id,
            target_id=EXTERNAL_TARGET_ID,
            generation=generation,
            addon_id=ADDON,
            addon_version="9.9.9",
            zip_sha256=pending.zip_sha256,
            expected_manifest_digest=pending.expected_manifest_digest,
        )
    published = _approve(env, pending)
    assert published.state == "running" and published.phase == "awaiting_kodi_update"
    with pytest.raises(McpWorkspaceDevelopmentError, match="execution_not_deployed"):
        env.development.execute(
            client_id=env.client_id,
            request_id=uuid4(),
            workspace_id=env.workspace_id,
            expected_generation=generation,
            target_id=EXTERNAL_TARGET_ID,
        )
    with pytest.raises(McpPromotionRequestError, match="deployment_required"):
        promotion_service.request(
            client_id=env.client_id,
            request_id=uuid4(),
            workspace_id=env.workspace_id,
            expected_generation=generation,
            target_id=EXTERNAL_TARGET_ID,
        )
    existing = repository.get_publication(pending.publication_id)
    assert existing is not None and existing.validation_receipt is not None
    assert existing.source_tree_sha256 is not None
    package_bytes = (
        repository.root / "packages" / ADDON / f"{ADDON}-1.0.0.zip"
    ).read_bytes()
    shared = repository.prepare_workspace(
        package_bytes,
        ("omega",),
        operation_id=uuid4(),
        workspace_id=env.workspace_id,
        workspace_generation=generation,
        target_id=uuid4(),
        validation_receipt=UUID(existing.validation_receipt),
        source_tree_sha256=existing.source_tree_sha256,
    )
    assert shared.id == str(pending.publication_id)
    assert (
        b' version="1.0.0"'
        in (load_current(repository.root)[0] / "development/addons.xml").read_bytes()
    )
    target.offline = True
    assert (
        env.development.recheck_repository_deployment(pending.deployment_id).state
        == "running"
    )
    target.offline = False
    restarted = _restarted_development(env, tmp_path)
    restarted.configure_external(bridges=cast(Any, target), targets=cast(Any, target))
    restarted.configure_repository(repository)
    restarted.sweep_startup()
    assert (
        restarted.recheck_repository_deployment(pending.deployment_id).state
        == "running"
    )
    with env.sessions() as session:
        linked = session.get(
            RepositoryWorkspaceDeploymentRow, str(pending.deployment_id)
        )
        assert linked is not None
        target.installed_version = "1.0.0"
        target.installed_digest = linked.expected_manifest_digest
        target.installed_files = linked.file_count
        target.installed_bytes = linked.total_bytes
    completed = restarted.recheck_repository_deployment(pending.deployment_id)
    assert completed.state == "succeeded"
    assert completed.result_classification == "repository_verified"
    assert completed.origin == "planned"
    with env.sessions() as session:
        operation = session.get(
            McpWorkspaceDeploymentOperationRow, str(pending.deployment_id)
        )
        linked = session.get(
            RepositoryWorkspaceDeploymentRow, str(pending.deployment_id)
        )
        assert operation is not None and linked is not None
        assert operation.initiator_kind == "mcp"
        assert operation.client_id == str(env.client_id)
        assert linked.origin == "planned" and linked.verified_by is None
        assert linked.approved_at is not None
    with pytest.raises(
        McpWorkspaceDevelopmentError, match="deployment_already_completed"
    ):
        restarted.recheck_repository_deployment(pending.deployment_id)
    with env.sessions() as session:
        assert (
            session.scalar(
                select(DevelopmentPublicationHoldRow).where(
                    DevelopmentPublicationHoldRow.reference_id
                    == str(pending.deployment_id)
                )
            )
            is None
        )
    executed = restarted.execute(
        client_id=env.client_id,
        request_id=uuid4(),
        workspace_id=env.workspace_id,
        expected_generation=generation,
        target_id=EXTERNAL_TARGET_ID,
    )
    assert executed.state == "succeeded" and target.executions == 1
    promotion = promotion_service.request(
        client_id=env.client_id,
        request_id=uuid4(),
        workspace_id=env.workspace_id,
        expected_generation=generation,
        target_id=EXTERNAL_TARGET_ID,
    )
    assert promotion.review_state == "reviewable"
    assert promotion.web_review_required


def test_correct_version_wrong_tree_fails_closed(tmp_path: Path) -> None:
    env, _, target = _setup(tmp_path)
    generation = _version(env, 0, "1.0.0")
    pending = env.development.deploy(
        client_id=env.client_id,
        request_id=uuid4(),
        workspace_id=env.workspace_id,
        expected_generation=generation,
        target_id=EXTERNAL_TARGET_ID,
    )
    _approve(env, pending)
    target.installed_version = "1.0.0"
    target.installed_digest = "sha256:" + "f" * 64
    with pytest.raises(McpWorkspaceDevelopmentError, match="installed_tree_mismatch"):
        env.development.recheck_repository_deployment(pending.deployment_id)
    with env.sessions.begin() as session:
        pruned = prune_expired_safe_development_requests(
            session, now=NOW + timedelta(days=31)
        )
    assert pruned.deleted >= 1


def test_native_version_without_bridge_tree_remains_pending(tmp_path: Path) -> None:
    env, _, target = _setup(tmp_path)
    generation = _version(env, 0, "1.0.0")
    pending = env.development.deploy(
        client_id=env.client_id,
        request_id=uuid4(),
        workspace_id=env.workspace_id,
        expected_generation=generation,
        target_id=EXTERNAL_TARGET_ID,
    )
    _approve(env, pending)
    target.installed_version = "1.0.0"
    target.bridge_offline = True
    observed = env.development.recheck_repository_deployment(pending.deployment_id)
    assert observed.state == "running" and observed.next_action is not None
    with env.sessions() as session:
        linked = session.get(
            RepositoryWorkspaceDeploymentRow, str(pending.deployment_id)
        )
        assert linked is not None and linked.state == "observed_identity"
    with pytest.raises(McpWorkspaceDevelopmentError, match="execution_not_deployed"):
        env.development.execute(
            client_id=env.client_id,
            request_id=uuid4(),
            workspace_id=env.workspace_id,
            expected_generation=generation,
            target_id=EXTERNAL_TARGET_ID,
        )


def test_explicit_update_retains_old_artifact_until_verified(tmp_path: Path) -> None:
    env, repository, target = _setup(tmp_path)
    first_generation = _version(env, 0, "1.0.0")
    first = env.development.deploy(
        client_id=env.client_id,
        request_id=uuid4(),
        workspace_id=env.workspace_id,
        expected_generation=first_generation,
        target_id=EXTERNAL_TARGET_ID,
    )
    _approve(env, first)
    with env.sessions() as session:
        first_link = session.get(
            RepositoryWorkspaceDeploymentRow, str(first.deployment_id)
        )
        assert first_link is not None
        target.installed_version = first.addon_version
        target.installed_digest = first_link.expected_manifest_digest
        target.installed_files = first_link.file_count
        target.installed_bytes = first_link.total_bytes
    assert (
        env.development.recheck_repository_deployment(first.deployment_id).state
        == "succeeded"
    )
    old_bytes = (
        repository.root / "packages" / ADDON / f"{ADDON}-1.0.0.zip"
    ).read_bytes()

    second_generation = _version(env, first_generation, "1.0.1")
    with env.sessions() as session:
        snapshot = session.get(
            RepositoryWorkspaceValidationSnapshotRow, first_link.validation_receipt
        )
        assert snapshot is not None
        assert snapshot.generation == first_generation
        assert snapshot.addon_version == "1.0.0"
    second = env.development.deploy(
        client_id=env.client_id,
        request_id=uuid4(),
        workspace_id=env.workspace_id,
        expected_generation=second_generation,
        target_id=EXTERNAL_TARGET_ID,
    )
    assert second.state == "running"
    assert second.installation_kind == "update"
    _approve(env, second)
    assert (
        env.development.recheck_repository_deployment(second.deployment_id).state
        == "running"
    )
    index = (load_current(repository.root)[0] / "development/addons.xml").read_bytes()
    assert b' version="1.0.1"' in index
    assert b' version="1.0.0"' not in index
    assert (
        repository.root / "packages" / ADDON / f"{ADDON}-1.0.0.zip"
    ).read_bytes() == old_bytes
    with env.sessions() as session:
        second_link = session.get(
            RepositoryWorkspaceDeploymentRow, str(second.deployment_id)
        )
        assert second_link is not None
        target.installed_version = second.addon_version
        target.installed_digest = second_link.expected_manifest_digest
        target.installed_files = second_link.file_count
        target.installed_bytes = second_link.total_bytes
    assert (
        env.development.recheck_repository_deployment(second.deployment_id).state
        == "succeeded"
    )


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("0.9.0", "repository_version_not_newer"),
        ("1.0.0", "repository_same_version_conflict"),
    ],
)
def test_repository_update_requires_new_version_and_immutable_bytes(
    tmp_path: Path, version: str, expected: str
) -> None:
    env, repository, target = _setup(tmp_path)
    first_generation = _version(env, 0, "1.0.0")
    first = env.development.deploy(
        client_id=env.client_id,
        request_id=uuid4(),
        workspace_id=env.workspace_id,
        expected_generation=first_generation,
        target_id=EXTERNAL_TARGET_ID,
    )
    _approve(env, first)
    with env.sessions() as session:
        linked = session.get(RepositoryWorkspaceDeploymentRow, str(first.deployment_id))
        assert linked is not None
        target.installed_version = "1.0.0"
        target.installed_digest = linked.expected_manifest_digest
        target.installed_files = linked.file_count
        target.installed_bytes = linked.total_bytes
    assert (
        env.development.recheck_repository_deployment(first.deployment_id).state
        == "succeeded"
    )
    original = (
        repository.root / "packages" / ADDON / f"{ADDON}-1.0.0.zip"
    ).read_bytes()
    generation = _version(env, first_generation, version)
    if version == "1.0.0":
        target.installed_version = None
        changed = env.workspaces.replace_text_file(
            env.workspace_id,
            expected_generation=generation,
            relative_path="main.py",
            content="def harmless_change():\n    return []\n",
        )
        generation = changed.generation
        env.development.record_validation(
            workspace_id=env.workspace_id,
            source_id=SOURCE_ID,
            generation=generation,
            addon_id=ADDON,
            addon_version=version,
            extension_types=("xbmc.python.pluginsource",),
            validated_at=NOW,
        )
    with pytest.raises(McpWorkspaceDevelopmentError, match=expected):
        env.development.deploy(
            client_id=env.client_id,
            request_id=uuid4(),
            workspace_id=env.workspace_id,
            expected_generation=generation,
            target_id=EXTERNAL_TARGET_ID,
        )
    assert (
        repository.root / "packages" / ADDON / f"{ADDON}-1.0.0.zip"
    ).read_bytes() == original


def test_stale_approval_and_exact_request_replay(tmp_path: Path) -> None:
    env, repository, _ = _setup(tmp_path)
    generation = _version(env, 0, "1.0.0")
    request = uuid4()
    pending = env.development.deploy(
        client_id=env.client_id,
        request_id=request,
        workspace_id=env.workspace_id,
        expected_generation=generation,
        target_id=EXTERNAL_TARGET_ID,
    )
    replay = env.development.deploy(
        client_id=env.client_id,
        request_id=request,
        workspace_id=env.workspace_id,
        expected_generation=generation,
        target_id=EXTERNAL_TARGET_ID,
    )
    assert replay == pending
    with pytest.raises(McpWorkspaceDevelopmentError, match="deployment_conflict"):
        env.development.deploy(
            client_id=env.client_id,
            request_id=uuid4(),
            workspace_id=env.workspace_id,
            expected_generation=generation,
            target_id=EXTERNAL_TARGET_ID,
        )
    assert len(repository.list_publications()) == 1
    _version(env, generation, "1.0.1")
    with pytest.raises(
        McpWorkspaceDevelopmentError, match="workspace_generation_conflict"
    ):
        _approve(env, pending)
    with env.sessions() as session:
        operation = session.get(
            McpWorkspaceDeploymentOperationRow, str(pending.deployment_id)
        )
        assert operation is not None and operation.state == "failed"


def test_published_shared_artifact_does_not_approve_another_request(
    tmp_path: Path,
) -> None:
    env, repository, _ = _setup(tmp_path)
    generation = _version(env, 0, "1.0.0")
    pending = env.development.deploy(
        client_id=env.client_id,
        request_id=uuid4(),
        workspace_id=env.workspace_id,
        expected_generation=generation,
        target_id=EXTERNAL_TARGET_ID,
    )
    _approve(env, pending)
    publication = repository.get_publication(pending.publication_id)
    assert publication is not None and publication.status == "current"
    with env.sessions.begin() as session:
        link = session.get(RepositoryWorkspaceDeploymentRow, str(pending.deployment_id))
        assert link is not None
        link.state = "awaiting_approval"
        link.approved_at = None
    restarted = _restarted_development(env, tmp_path)
    restarted.configure_repository(repository)
    restarted.sweep_startup()
    with env.sessions() as session:
        link = session.get(RepositoryWorkspaceDeploymentRow, str(pending.deployment_id))
        assert link is not None and link.state == "awaiting_approval"


def test_workspace_edit_after_publication_keeps_old_verified_generation(
    tmp_path: Path,
) -> None:
    env, _, target = _setup(tmp_path)
    old_generation = _version(env, 0, "1.0.0")
    pending = env.development.deploy(
        client_id=env.client_id,
        request_id=uuid4(),
        workspace_id=env.workspace_id,
        expected_generation=old_generation,
        target_id=EXTERNAL_TARGET_ID,
    )
    _approve(env, pending)
    new_generation = _version(env, old_generation, "1.0.1")
    with env.sessions() as session:
        linked = session.get(
            RepositoryWorkspaceDeploymentRow, str(pending.deployment_id)
        )
        assert linked is not None
        target.installed_version = "1.0.0"
        target.installed_digest = linked.expected_manifest_digest
        target.installed_files = linked.file_count
        target.installed_bytes = linked.total_bytes
    completed = env.development.recheck_repository_deployment(pending.deployment_id)
    assert completed.state == "succeeded" and completed.generation == old_generation
    assert new_generation > completed.generation
    with pytest.raises(McpWorkspaceDevelopmentError, match="execution_not_deployed"):
        env.development.execute(
            client_id=env.client_id,
            request_id=uuid4(),
            workspace_id=env.workspace_id,
            expected_generation=new_generation,
            target_id=EXTERNAL_TARGET_ID,
        )


@pytest.mark.parametrize(
    ("kodi_version", "python_version", "expected_generation"),
    [("19.4", "3.0.0", "matrix"), ("20.5", "3.0.1", "nexus")],
)
def test_matrix_nexus_repository_package_preparation(
    tmp_path: Path,
    kodi_version: str,
    python_version: str,
    expected_generation: str,
) -> None:
    env, repository, _ = _setup(tmp_path, kodi_version=kodi_version)
    generation = _version(env, 0, "1.0.0", python_version=python_version)
    pending = env.development.deploy(
        client_id=env.client_id,
        request_id=uuid4(),
        workspace_id=env.workspace_id,
        expected_generation=generation,
        target_id=EXTERNAL_TARGET_ID,
    )
    assert pending.state == "running" and pending.phase == "publication_approval"
    publication = repository.get_publication(pending.publication_id)
    assert publication is not None
    assert publication.compatible_generations == expected_generation


@pytest.mark.parametrize(
    ("version", "enabled", "expected"),
    [("9.9.9", True, "installed_version_mismatch"), ("1.0.0", False, "addon_disabled")],
)
def test_native_identity_or_enablement_fails_closed(
    tmp_path: Path, version: str, enabled: bool, expected: str
) -> None:
    env, _, target = _setup(tmp_path)
    generation = _version(env, 0, "1.0.0")
    pending = env.development.deploy(
        client_id=env.client_id,
        request_id=uuid4(),
        workspace_id=env.workspace_id,
        expected_generation=generation,
        target_id=EXTERNAL_TARGET_ID,
    )
    _approve(env, pending)
    target.installed_version = version
    target.enabled = enabled
    with pytest.raises(McpWorkspaceDevelopmentError, match=expected):
        env.development.recheck_repository_deployment(pending.deployment_id)


def test_target_classification_change_and_production_denial(tmp_path: Path) -> None:
    env, _, _ = _setup(tmp_path)
    generation = _version(env, 0, "1.0.0")
    pending = env.development.deploy(
        client_id=env.client_id,
        request_id=uuid4(),
        workspace_id=env.workspace_id,
        expected_generation=generation,
        target_id=EXTERNAL_TARGET_ID,
    )
    _approve(env, pending)
    with env.sessions.begin() as session:
        target = session.get(TargetRow, str(EXTERNAL_TARGET_ID))
        assert target is not None
        target.target_type = TargetType.EXTERNAL_PRODUCTION
    with pytest.raises(McpWorkspaceDevelopmentError, match="target_identity_changed"):
        env.development.recheck_repository_deployment(pending.deployment_id)
    with pytest.raises(McpWorkspaceDevelopmentError, match="deployment_not_allowed"):
        env.development.deploy(
            client_id=env.client_id,
            request_id=uuid4(),
            workspace_id=env.workspace_id,
            expected_generation=generation,
            target_id=EXTERNAL_TARGET_ID,
            deployment_mode="repository",
        )


def test_target_classification_change_before_approval_fails_request(
    tmp_path: Path,
) -> None:
    env, _, _ = _setup(tmp_path)
    generation = _version(env, 0, "1.0.0")
    pending = env.development.deploy(
        client_id=env.client_id,
        request_id=uuid4(),
        workspace_id=env.workspace_id,
        expected_generation=generation,
        target_id=EXTERNAL_TARGET_ID,
    )
    with env.sessions.begin() as session:
        target = session.get(TargetRow, str(EXTERNAL_TARGET_ID))
        assert target is not None
        target.target_type = TargetType.EXTERNAL_PRODUCTION
    with pytest.raises(McpWorkspaceDevelopmentError, match="target_identity_changed"):
        _approve(env, pending)
    with env.sessions() as session:
        link = session.get(RepositoryWorkspaceDeploymentRow, str(pending.deployment_id))
        assert link is not None and link.state == "failed"


def test_concurrent_deploy_requests_have_one_pending_publication(
    tmp_path: Path,
) -> None:
    env, repository, _ = _setup(tmp_path)
    generation = _version(env, 0, "1.0.0")

    def attempt(_: int) -> str:
        try:
            result = env.development.deploy(
                client_id=env.client_id,
                request_id=uuid4(),
                workspace_id=env.workspace_id,
                expected_generation=generation,
                target_id=EXTERNAL_TARGET_ID,
            )
            return cast(str, result.state)
        except McpWorkspaceDevelopmentError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(attempt, (0, 1)))
    assert outcomes.count("running") == 1
    assert set(outcomes) <= {"running", "deployment_conflict"}
    assert len(repository.list_publications()) == 1
    with env.sessions() as session:
        assert len(session.query(RepositoryWorkspaceDeploymentRow).all()) == 1


def test_repository_tampering_stays_pending_and_native_mismatch_fails(
    tmp_path: Path,
) -> None:
    env, repository, _target = _setup(tmp_path)
    generation = _version(env, 0, "1.0.0")
    pending = env.development.deploy(
        client_id=env.client_id,
        request_id=uuid4(),
        workspace_id=env.workspace_id,
        expected_generation=generation,
        target_id=EXTERNAL_TARGET_ID,
    )
    _approve(env, pending)
    package = repository.root / "packages" / ADDON / f"{ADDON}-1.0.0.zip"
    expected = package.read_bytes()
    package.unlink()
    package.write_bytes(b"tampered")
    assert (
        env.development.recheck_repository_deployment(pending.deployment_id).state
        == "running"
    )
    package.unlink()
    package.write_bytes(expected)
    with env.sessions.begin() as session:
        native = session.get(ExternalKodiTargetRow, str(EXTERNAL_TARGET_ID))
        assert native is not None
        native.verified_kodi_version = "20.5"
    with pytest.raises(McpWorkspaceDevelopmentError, match="target_identity_changed"):
        env.development.recheck_repository_deployment(pending.deployment_id)
