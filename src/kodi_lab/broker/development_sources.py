"""Broker-trusted resolution for stopped DevelopmentSource copy/sync."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.config import Settings
from kodi_lab.development.confined_fs import open_development_root
from kodi_lab.domain import (
    AddonZipInstallationConfidence,
    AddonZipInstallationResult,
    AddonZipOrigin,
    DevelopmentSourceDeploymentConfidence,
    DevelopmentSourceDeploymentMode,
    DevelopmentSourceDeploymentOperationState,
    DevelopmentSourceDeploymentResult,
    DevelopmentSourceKind,
    InstanceType,
    PromotionVerificationPhase,
    PromotionVerificationState,
    RepositoryReplacementOperationState,
)
from kodi_lab.storage.engine import create_database_engine
from kodi_lab.storage.models import (
    McpWorkspaceDeploymentOperationRow,
    McpWorkspaceExecutionOperationRow,
)
from kodi_lab.storage.repositories import (
    AddonZipInstallationEvidenceRepository,
    AgentWorkspacePromotionHandoffRepository,
    AgentWorkspacePromotionOperationRepository,
    AgentWorkspacePromotionPlanRepository,
    AgentWorkspaceRepository,
    DevelopmentSourceDeploymentOperationRepository,
    DevelopmentSourceDeploymentRepository,
    DevelopmentSourceRepository,
    ManagedInstanceDevelopmentSourceRepository,
    ManagedInstanceRepository,
    PromotionVerificationOperationRepository,
    RepositoryReplacementOperationRepository,
    RuntimeExecutionProfileRepository,
    RuntimeRevisionRepository,
)

BROKER_DEVELOPMENT_ROOT = Path("/mnt/kodi-lab-development")


class DevelopmentSourceDeploymentStateError(RuntimeError):
    code = "development_source_deployment_state_invalid"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


def require_readonly_source_mount(
    root: Path | None,
    *,
    expected_root: Path = BROKER_DEVELOPMENT_ROOT,
    mountinfo: Path = Path("/proc/self/mountinfo"),
) -> None:
    """Require the fixed read-only bind before broker reads a host source."""

    if root is None or root != expected_root:
        raise DevelopmentSourceDeploymentStateError(
            "Broker development root is absent or not the fixed mount path."
        )
    try:
        root_fd = open_development_root(root)
        os.close(root_fd)
        entries = [
            fields
            for line in mountinfo.read_text().splitlines()
            if len(fields := line.split()) >= 6 and fields[4] == str(expected_root)
        ]
    except (OSError, ValueError) as error:
        raise DevelopmentSourceDeploymentStateError(
            "Broker development root or mount state is unavailable."
        ) from error
    if len(entries) != 1 or "ro" not in entries[0][5].split(","):
        raise DevelopmentSourceDeploymentStateError(
            "Broker development root must be a single read-only mount."
        )


@dataclass(frozen=True, slots=True)
class PriorDevelopmentSourceDeploymentEvidence:
    source_manifest_digest: str
    verified_destination_digest: str
    runtime_revision_id: str
    trusted_container_id: str
    confidence: DevelopmentSourceDeploymentConfidence


@dataclass(frozen=True, slots=True)
class DevelopmentSourceDeploymentSpec:
    managed_instance_id: str
    development_source_id: str
    deployment_operation_id: str
    runtime_revision_id: str
    runtime_uid: int
    runtime_gid: int
    development_root: Path
    relative_locator: str
    addon_id: str
    observed_name: str
    observed_version: str
    staging_root: Path
    prior_evidence: PriorDevelopmentSourceDeploymentEvidence | None
    expected_source_digest: str | None = None


@dataclass(frozen=True, slots=True)
class DevelopmentSourceDestinationVerificationSpec:
    managed_instance_id: str
    development_source_id: str
    runtime_revision_id: str
    runtime_uid: int
    runtime_gid: int
    addon_id: str
    addon_version: str
    verified_destination_digest: str
    trusted_container_id: str


class DevelopmentSourceDeploymentResolver(Protocol):
    def resolve(
        self,
        managed_instance_id: str,
        development_source_id: str,
        deployment_operation_id: str,
        expected_container_id: str,
    ) -> DevelopmentSourceDeploymentSpec: ...

    def resolve_destination_verification(
        self,
        managed_instance_id: str,
        development_source_id: str,
        expected_container_id: str,
    ) -> DevelopmentSourceDestinationVerificationSpec: ...

    def resolve_workspace_deployment(
        self,
        managed_instance_id: str,
        workspace_id: str,
        expected_generation: int,
        deployment_operation_id: str,
        expected_container_id: str,
    ) -> DevelopmentSourceDeploymentSpec: ...

    def resolve_workspace_destination_verification(
        self,
        deployment_operation_id: str,
        managed_instance_id: str,
        expected_container_id: str,
    ) -> DevelopmentSourceDestinationVerificationSpec: ...


class PromotionDevelopmentSourceDeploymentResolver(Protocol):
    def resolve_promotion_verification(
        self,
        verification_operation_id: str,
        managed_instance_id: str,
        expected_container_id: str,
    ) -> DevelopmentSourceDeploymentSpec: ...


class SqliteDevelopmentSourceDeploymentResolver:
    """Resolve all path, ownership, association, and evidence authority."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        development_root: Path | None,
        data_root: Path,
        enforce_source_mount: bool = False,
    ) -> None:
        self._session_factory = session_factory
        self._development_root = development_root
        self._data_root = data_root
        self._enforce_source_mount = enforce_source_mount

    @classmethod
    def production(cls) -> SqliteDevelopmentSourceDeploymentResolver:
        settings = Settings()
        engine = create_database_engine(settings.database_url)
        return cls(
            sessionmaker(engine, expire_on_commit=False),
            development_root=settings.development_root,
            data_root=settings.data_root,
            enforce_source_mount=True,
        )

    def _require_source_root(self) -> Path:
        root = self._development_root
        if self._enforce_source_mount:
            require_readonly_source_mount(root)
        if root is None:
            raise DevelopmentSourceDeploymentStateError(
                "Development root is not configured."
            )
        return root

    def resolve(
        self,
        managed_instance_id: str,
        development_source_id: str,
        deployment_operation_id: str,
        expected_container_id: str,
    ) -> DevelopmentSourceDeploymentSpec:
        try:
            instance_id = UUID(managed_instance_id)
            source_id = UUID(development_source_id)
            operation_id = UUID(deployment_operation_id)
            if (
                str(instance_id) != managed_instance_id
                or str(source_id) != development_source_id
                or str(operation_id) != deployment_operation_id
                or len(expected_container_id) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in expected_container_id
                )
            ):
                raise ValueError
        except ValueError as error:
            raise DevelopmentSourceDeploymentStateError(
                "Deployment identities are not canonical."
            ) from error
        development_root = self._require_source_root()
        try:
            with self._session_factory() as session:
                instance = ManagedInstanceRepository(session).get(instance_id)
                source = DevelopmentSourceRepository(session).get(source_id)
                attachment = ManagedInstanceDevelopmentSourceRepository(session).get(
                    instance_id, source_id
                )
                operation = DevelopmentSourceDeploymentOperationRepository(session).get(
                    operation_id
                )
                if (
                    instance is None
                    or source is None
                    or source.source_kind is not DevelopmentSourceKind.LOCAL_DIRECTORY
                    or attachment is None
                ):
                    raise DevelopmentSourceDeploymentStateError(
                        "Attached development-source state does not exist."
                    )
                replacement_evidence = AddonZipInstallationEvidenceRepository(
                    session
                ).get(instance_id, source.addon_id)
                replacement_operation = (
                    None
                    if replacement_evidence is None
                    or replacement_evidence.replacement_operation_id is None
                    else RepositoryReplacementOperationRepository(session).get(
                        replacement_evidence.replacement_operation_id
                    )
                )
                if (
                    replacement_evidence is not None
                    and replacement_operation is not None
                    and replacement_evidence.confidence
                    is AddonZipInstallationConfidence.VERIFIED_AT_LAST_OPERATION
                    and replacement_evidence.result
                    is AddonZipInstallationResult.REPOSITORY_REPLACEMENT_RECOGNIZED
                    and replacement_evidence.artifact_origin
                    is AddonZipOrigin.REPOSITORY_DOWNLOAD
                    and replacement_evidence.runtime_revision_id
                    == instance.runtime_revision_id
                    and replacement_evidence.trusted_container_id
                    == expected_container_id
                    and replacement_operation.state
                    is RepositoryReplacementOperationState.SUCCEEDED
                    and replacement_operation.managed_instance_id == instance_id
                    and replacement_operation.addon_id == source.addon_id
                    and replacement_operation.stopped_readback_matched
                    and replacement_operation.final_readback_matched
                ):
                    raise DevelopmentSourceDeploymentStateError(
                        "Attached DevelopmentSource is currently overridden.",
                        code="repository_override_active",
                    )
                if instance.instance_type is not InstanceType.DISPOSABLE:
                    raise DevelopmentSourceDeploymentStateError(
                        "Development-source deployment requires a disposable instance."
                    )
                if (
                    operation is None
                    or operation.state
                    is not DevelopmentSourceDeploymentOperationState.RUNNING
                    or operation.managed_instance_id != instance_id
                    or operation.development_source_id != source_id
                    or operation.addon_id != source.addon_id
                    or operation.runtime_revision_id != instance.runtime_revision_id
                    or operation.expected_container_id != expected_container_id
                ):
                    raise DevelopmentSourceDeploymentStateError(
                        "Deployment operation ownership is inconsistent."
                    )
                revision = RuntimeRevisionRepository(session).get(
                    instance.runtime_revision_id
                )
                profile = RuntimeExecutionProfileRepository(session).get(
                    instance.runtime_revision_id
                )
                if revision is None or profile is None:
                    raise DevelopmentSourceDeploymentStateError(
                        "Runtime ownership state is unavailable."
                    )
                evidence = DevelopmentSourceDeploymentRepository(session).get(
                    instance_id, source_id
                )
                if evidence is not None:
                    evidence_operation = DevelopmentSourceDeploymentOperationRepository(
                        session
                    ).get(evidence.last_successful_operation_id)
                    if (
                        evidence.mode is not DevelopmentSourceDeploymentMode.COPY
                        or evidence.addon_id != source.addon_id
                        or evidence.runtime_revision_id != revision.id
                        or evidence_operation is None
                        or evidence_operation.state
                        is not DevelopmentSourceDeploymentOperationState.SUCCEEDED
                        or evidence_operation.result_classification
                        not in {
                            DevelopmentSourceDeploymentResult.INITIAL_DEPLOYMENT,
                            DevelopmentSourceDeploymentResult.SYNCHRONIZED,
                            DevelopmentSourceDeploymentResult.UNCHANGED,
                        }
                        or evidence_operation.original_managed_instance_id
                        != instance_id
                        or evidence_operation.development_source_id != source_id
                        or evidence_operation.addon_id != source.addon_id
                        or evidence_operation.runtime_revision_id != revision.id
                        or evidence_operation.expected_container_id
                        != evidence.trusted_container_id
                    ):
                        raise DevelopmentSourceDeploymentStateError(
                            "Retained deployment evidence is inconsistent."
                        )
                prior = (
                    None
                    if evidence is None
                    else PriorDevelopmentSourceDeploymentEvidence(
                        source_manifest_digest=evidence.source_manifest_digest,
                        verified_destination_digest=(
                            evidence.verified_destination_digest
                        ),
                        runtime_revision_id=str(evidence.runtime_revision_id),
                        trusted_container_id=evidence.trusted_container_id,
                        confidence=evidence.confidence,
                    )
                )
                return DevelopmentSourceDeploymentSpec(
                    managed_instance_id=managed_instance_id,
                    development_source_id=development_source_id,
                    deployment_operation_id=deployment_operation_id,
                    runtime_revision_id=str(revision.id),
                    runtime_uid=profile.runtime_uid,
                    runtime_gid=profile.runtime_gid,
                    development_root=development_root,
                    relative_locator=source.relative_locator,
                    addon_id=source.addon_id,
                    observed_name=source.observed_name,
                    observed_version=source.observed_version,
                    staging_root=self._data_root / "development-source-staging",
                    prior_evidence=prior,
                    expected_source_digest=None,
                )
        except DevelopmentSourceDeploymentStateError:
            raise
        except (SQLAlchemyError, ValueError) as error:
            raise DevelopmentSourceDeploymentStateError(
                "Canonical deployment state could not be read."
            ) from error

    def resolve_destination_verification(
        self,
        managed_instance_id: str,
        development_source_id: str,
        expected_container_id: str,
    ) -> DevelopmentSourceDestinationVerificationSpec:
        try:
            instance_id = UUID(managed_instance_id)
            source_id = UUID(development_source_id)
            if (
                str(instance_id) != managed_instance_id
                or str(source_id) != development_source_id
                or len(expected_container_id) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in expected_container_id
                )
            ):
                raise ValueError
        except ValueError as error:
            raise DevelopmentSourceDeploymentStateError(
                "Destination-verification identities are not canonical."
            ) from error
        try:
            with self._session_factory() as session:
                instance = ManagedInstanceRepository(session).get(instance_id)
                source = DevelopmentSourceRepository(session).get(source_id)
                attachment = ManagedInstanceDevelopmentSourceRepository(session).get(
                    instance_id, source_id
                )
                evidence = DevelopmentSourceDeploymentRepository(session).get(
                    instance_id, source_id
                )
                if instance is None or source is None or attachment is None:
                    raise DevelopmentSourceDeploymentStateError(
                        "Attached development-source state does not exist."
                    )
                profile = RuntimeExecutionProfileRepository(session).get(
                    instance.runtime_revision_id
                )
                if profile is None or evidence is None:
                    raise DevelopmentSourceDeploymentStateError(
                        "Verified deployment evidence is unavailable."
                    )
                operation = DevelopmentSourceDeploymentOperationRepository(session).get(
                    evidence.last_successful_operation_id
                )
                if (
                    evidence.confidence
                    is not (
                        DevelopmentSourceDeploymentConfidence.VERIFIED_AT_LAST_OPERATION
                    )
                    or evidence.mode is not DevelopmentSourceDeploymentMode.COPY
                    or evidence.managed_instance_id != instance_id
                    or evidence.development_source_id != source_id
                    or evidence.addon_id != source.addon_id
                    or evidence.runtime_revision_id != instance.runtime_revision_id
                    or evidence.trusted_container_id != expected_container_id
                    or operation is None
                    or operation.state
                    is not DevelopmentSourceDeploymentOperationState.SUCCEEDED
                    or operation.managed_instance_id != instance_id
                    or operation.development_source_id != source_id
                    or operation.addon_id != source.addon_id
                    or operation.runtime_revision_id != instance.runtime_revision_id
                    or operation.expected_container_id != expected_container_id
                ):
                    raise DevelopmentSourceDeploymentStateError(
                        "Verified deployment evidence is stale or inconsistent."
                    )
                return DevelopmentSourceDestinationVerificationSpec(
                    managed_instance_id=managed_instance_id,
                    development_source_id=development_source_id,
                    runtime_revision_id=str(instance.runtime_revision_id),
                    runtime_uid=profile.runtime_uid,
                    runtime_gid=profile.runtime_gid,
                    addon_id=source.addon_id,
                    addon_version=evidence.observed_source_version,
                    verified_destination_digest=evidence.verified_destination_digest,
                    trusted_container_id=evidence.trusted_container_id,
                )
        except DevelopmentSourceDeploymentStateError:
            raise
        except (SQLAlchemyError, ValueError) as error:
            raise DevelopmentSourceDeploymentStateError(
                "Canonical destination-verification state could not be read."
            ) from error

    def resolve_promotion_verification(
        self,
        verification_operation_id: str,
        managed_instance_id: str,
        expected_container_id: str,
    ) -> DevelopmentSourceDeploymentSpec:
        try:
            verification_id = UUID(verification_operation_id)
            instance_id = UUID(managed_instance_id)
            if (
                str(verification_id) != verification_operation_id
                or str(instance_id) != managed_instance_id
            ):
                raise ValueError
        except ValueError as error:
            raise DevelopmentSourceDeploymentStateError(
                "Promotion verification identities are not canonical."
            ) from error
        development_root = self._require_source_root()
        with self._session_factory() as session:
            verification = PromotionVerificationOperationRepository(session).get(
                verification_id
            )
            instance = ManagedInstanceRepository(session).get(instance_id)
            if verification is None or instance is None:
                raise DevelopmentSourceDeploymentStateError(
                    "Promotion verification state is unavailable."
                )
            handoff = AgentWorkspacePromotionHandoffRepository(session).get(
                verification.handoff_id
            )
            promotion = AgentWorkspacePromotionOperationRepository(session).get(
                verification.source_promotion_id
            )
            plan = (
                None
                if promotion is None
                else AgentWorkspacePromotionPlanRepository(session).get(
                    promotion.plan_id
                )
            )
            source = DevelopmentSourceRepository(session).get(
                verification.development_source_id
            )
            attachment = ManagedInstanceDevelopmentSourceRepository(session).get(
                instance_id, verification.development_source_id
            )
            evidence = DevelopmentSourceDeploymentRepository(session).get(
                instance_id, verification.development_source_id
            )
            deployment = DevelopmentSourceDeploymentOperationRepository(session).get(
                verification.deployment_operation_id
            )
            profile = RuntimeExecutionProfileRepository(session).get(
                instance.runtime_revision_id
            )
            mcp_deployment = (
                None
                if plan is None or plan.originating_mcp_deployment_id is None
                else session.get(
                    McpWorkspaceDeploymentOperationRow,
                    str(plan.originating_mcp_deployment_id),
                )
            )
            mcp_execution = (
                None
                if plan is None or plan.originating_mcp_execution_id is None
                else session.get(
                    McpWorkspaceExecutionOperationRow,
                    str(plan.originating_mcp_execution_id),
                )
            )
        canonical_anchor_valid = (
            evidence is not None
            and evidence.last_successful_operation_id
            == (None if handoff is None else handoff.prior_deployment_operation_id)
        )
        if plan is None or plan.originating_mcp_client_id is None:
            prior_evidence_valid = canonical_anchor_valid and (
                handoff is not None
                and evidence is not None
                and evidence.source_manifest_digest == handoff.prior_source_digest
                and evidence.verified_destination_digest
                == handoff.prior_destination_digest
            )
        else:
            prior_evidence_valid = canonical_anchor_valid and (
                handoff is not None
                and mcp_deployment is not None
                and mcp_execution is not None
                and mcp_deployment.client_id == str(plan.originating_mcp_client_id)
                and mcp_deployment.workspace_id == str(verification.workspace_id)
                and mcp_deployment.development_source_id
                == str(verification.development_source_id)
                and mcp_deployment.target_id == str(instance_id)
                and mcp_deployment.generation == plan.workspace_generation
                and mcp_deployment.state == "succeeded"
                and mcp_deployment.source_manifest_digest == handoff.prior_source_digest
                and mcp_deployment.verified_destination_digest
                == handoff.prior_destination_digest
                and mcp_execution.client_id == str(plan.originating_mcp_client_id)
                and mcp_execution.deployment_id == mcp_deployment.id
                and mcp_execution.workspace_id == str(verification.workspace_id)
                and mcp_execution.development_source_id
                == str(verification.development_source_id)
                and mcp_execution.target_id == str(instance_id)
                and mcp_execution.generation == plan.workspace_generation
                and mcp_execution.state == "succeeded"
            )
        if (
            handoff is None
            or promotion is None
            or plan is None
            or source is None
            or source.source_kind is not DevelopmentSourceKind.LOCAL_DIRECTORY
            or attachment is None
            or evidence is None
            or deployment is None
            or profile is None
            or verification.state is not PromotionVerificationState.RUNNING
            or verification.phase is not PromotionVerificationPhase.SYNC
            or not verification.stop_completed
            or not verification.sync_started
            or verification.sync_completed
            or verification.handoff_consumed
            or handoff.verification_operation_id != verification.id
            or handoff.consumed_at is not None
            or handoff.operation_id != promotion.id
            or promotion.state.value != "succeeded"
            or verification.target_instance_id != instance_id
            or verification.runtime_revision_id != instance.runtime_revision_id
            or handoff.runtime_revision_id != instance.runtime_revision_id
            or handoff.trusted_container_id != expected_container_id
            or handoff.expected_addon_id != verification.addon_id
            or handoff.expected_name != verification.expected_name
            or handoff.expected_version != verification.expected_version
            or handoff.expected_enabled is not verification.expected_enabled
            or not prior_evidence_valid
            or deployment.state is not DevelopmentSourceDeploymentOperationState.RUNNING
            or deployment.expected_container_id != expected_container_id
        ):
            raise DevelopmentSourceDeploymentStateError(
                "Promotion verification authority drifted.",
                code="promotion_verification_handoff_invalid",
            )
        return DevelopmentSourceDeploymentSpec(
            managed_instance_id=managed_instance_id,
            development_source_id=str(source.id),
            deployment_operation_id=str(deployment.id),
            runtime_revision_id=str(instance.runtime_revision_id),
            runtime_uid=profile.runtime_uid,
            runtime_gid=profile.runtime_gid,
            development_root=development_root,
            relative_locator=source.relative_locator,
            addon_id=source.addon_id,
            observed_name=verification.expected_name,
            observed_version=verification.expected_version,
            staging_root=self._data_root / "development-source-staging",
            prior_evidence=PriorDevelopmentSourceDeploymentEvidence(
                source_manifest_digest=handoff.prior_source_digest,
                verified_destination_digest=handoff.prior_destination_digest,
                runtime_revision_id=str(handoff.runtime_revision_id),
                trusted_container_id=handoff.trusted_container_id,
                confidence=DevelopmentSourceDeploymentConfidence.VERIFIED_AT_LAST_OPERATION,
            ),
            expected_source_digest=None,
        )

    def resolve_workspace_deployment(
        self,
        managed_instance_id: str,
        workspace_id: str,
        expected_generation: int,
        deployment_operation_id: str,
        expected_container_id: str,
    ) -> DevelopmentSourceDeploymentSpec:
        try:
            instance_id = UUID(managed_instance_id)
            workspace_uuid = UUID(workspace_id)
            operation_id = UUID(deployment_operation_id)
            if (
                str(instance_id) != managed_instance_id
                or str(workspace_uuid) != workspace_id
                or str(operation_id) != deployment_operation_id
                or expected_generation < 0
                or len(expected_container_id) != 64
            ):
                raise ValueError
        except ValueError as error:
            raise DevelopmentSourceDeploymentStateError(
                "Workspace deployment identities are not canonical."
            ) from error
        try:
            with self._session_factory() as session:
                instance = ManagedInstanceRepository(session).get(instance_id)
                workspace = AgentWorkspaceRepository(session).get(workspace_uuid)
                operation = session.get(
                    McpWorkspaceDeploymentOperationRow, deployment_operation_id
                )
                source = (
                    None
                    if workspace is None
                    else DevelopmentSourceRepository(session).get(
                        workspace.development_source_id
                    )
                )
                attachment = (
                    None
                    if workspace is None
                    else ManagedInstanceDevelopmentSourceRepository(session).get(
                        instance_id, workspace.development_source_id
                    )
                )
                profile = (
                    None
                    if instance is None
                    else RuntimeExecutionProfileRepository(session).get(
                        instance.runtime_revision_id
                    )
                )
                baseline = (
                    None
                    if workspace is None
                    else DevelopmentSourceDeploymentRepository(session).get(
                        instance_id, workspace.development_source_id
                    )
                )
                prior_workspace = (
                    None
                    if workspace is None
                    else session.scalar(
                        select(McpWorkspaceDeploymentOperationRow)
                        .where(
                            McpWorkspaceDeploymentOperationRow.id
                            != deployment_operation_id,
                            McpWorkspaceDeploymentOperationRow.workspace_id
                            == workspace_id,
                            McpWorkspaceDeploymentOperationRow.target_id
                            == managed_instance_id,
                            McpWorkspaceDeploymentOperationRow.development_source_id
                            == str(workspace.development_source_id),
                            McpWorkspaceDeploymentOperationRow.state == "succeeded",
                            McpWorkspaceDeploymentOperationRow.runtime_revision_id
                            == str(instance.runtime_revision_id)
                            if instance is not None
                            else sa.false(),
                        )
                        .order_by(
                            McpWorkspaceDeploymentOperationRow.completed_at.desc()
                        )
                    )
                )
            if (
                instance is None
                or workspace is None
                or source is None
                or attachment is None
                or profile is None
                or operation is None
                or instance.instance_type is not InstanceType.DISPOSABLE
                or workspace.state.value != "ready"
                or workspace.bytes_state.value != "present"
                or workspace.generation != expected_generation
                or workspace.current_tree_digest is None
                or workspace.source_addon_id != source.addon_id
                or operation.state != "running"
                or operation.phase != "sync"
                or not operation.mutation_started
                or not operation.target_stopped
                or not operation.sync_started
                or operation.sync_completed
                or operation.workspace_id != workspace_id
                or operation.target_id != managed_instance_id
                or operation.generation != expected_generation
                or operation.runtime_revision_id != str(instance.runtime_revision_id)
                or operation.addon_id != source.addon_id
            ):
                raise DevelopmentSourceDeploymentStateError(
                    "Workspace deployment authority drifted.",
                    code="workspace_deployment_state_invalid",
                )
            prior: PriorDevelopmentSourceDeploymentEvidence | None = None
            if prior_workspace is not None:
                if (
                    prior_workspace.source_manifest_digest is None
                    or prior_workspace.verified_destination_digest is None
                    or baseline is None
                    or baseline.runtime_revision_id != instance.runtime_revision_id
                    or baseline.trusted_container_id != expected_container_id
                ):
                    raise DevelopmentSourceDeploymentStateError(
                        "Prior workspace deployment evidence is incomplete."
                    )
                prior = PriorDevelopmentSourceDeploymentEvidence(
                    source_manifest_digest=prior_workspace.source_manifest_digest,
                    verified_destination_digest=prior_workspace.verified_destination_digest,
                    runtime_revision_id=str(instance.runtime_revision_id),
                    trusted_container_id=expected_container_id,
                    confidence=(
                        DevelopmentSourceDeploymentConfidence.VERIFIED_AT_LAST_OPERATION
                    ),
                )
            elif baseline is not None:
                prior = PriorDevelopmentSourceDeploymentEvidence(
                    source_manifest_digest=baseline.source_manifest_digest,
                    verified_destination_digest=baseline.verified_destination_digest,
                    runtime_revision_id=str(baseline.runtime_revision_id),
                    trusted_container_id=baseline.trusted_container_id,
                    confidence=baseline.confidence,
                )
            return DevelopmentSourceDeploymentSpec(
                managed_instance_id=managed_instance_id,
                development_source_id=str(source.id),
                deployment_operation_id=deployment_operation_id,
                runtime_revision_id=str(instance.runtime_revision_id),
                runtime_uid=profile.runtime_uid,
                runtime_gid=profile.runtime_gid,
                development_root=self._data_root / "agent-workspaces" / workspace_id,
                relative_locator="tree",
                addon_id=source.addon_id,
                observed_name=source.observed_name,
                observed_version=operation.addon_version,
                staging_root=self._data_root / "development-source-staging",
                prior_evidence=prior,
                expected_source_digest=workspace.current_tree_digest,
            )
        except DevelopmentSourceDeploymentStateError:
            raise
        except (SQLAlchemyError, ValueError) as error:
            raise DevelopmentSourceDeploymentStateError(
                "Canonical workspace deployment state could not be read."
            ) from error

    def resolve_workspace_destination_verification(
        self,
        deployment_operation_id: str,
        managed_instance_id: str,
        expected_container_id: str,
    ) -> DevelopmentSourceDestinationVerificationSpec:
        try:
            operation_id = UUID(deployment_operation_id)
            instance_id = UUID(managed_instance_id)
            if (
                str(operation_id) != deployment_operation_id
                or str(instance_id) != managed_instance_id
            ):
                raise ValueError
        except ValueError as error:
            raise DevelopmentSourceDeploymentStateError(
                "Workspace destination verification identities are invalid."
            ) from error
        with self._session_factory() as session:
            operation = session.get(
                McpWorkspaceDeploymentOperationRow, deployment_operation_id
            )
            instance = ManagedInstanceRepository(session).get(instance_id)
            profile = (
                None
                if instance is None
                else RuntimeExecutionProfileRepository(session).get(
                    instance.runtime_revision_id
                )
            )
        if (
            operation is None
            or instance is None
            or profile is None
            or operation.state != "succeeded"
            or operation.target_id != managed_instance_id
            or operation.runtime_revision_id != str(instance.runtime_revision_id)
            or operation.verified_destination_digest is None
        ):
            raise DevelopmentSourceDeploymentStateError(
                "Completed workspace deployment evidence is unavailable.",
                code="workspace_deployment_state_invalid",
            )
        return DevelopmentSourceDestinationVerificationSpec(
            managed_instance_id=managed_instance_id,
            development_source_id=operation.development_source_id,
            runtime_revision_id=operation.runtime_revision_id,
            runtime_uid=profile.runtime_uid,
            runtime_gid=profile.runtime_gid,
            addon_id=operation.addon_id,
            addon_version=operation.addon_version,
            verified_destination_digest=operation.verified_destination_digest,
            trusted_container_id=expected_container_id,
        )
