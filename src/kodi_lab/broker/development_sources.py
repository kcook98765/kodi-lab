"""Broker-trusted resolution for stopped DevelopmentSource copy/sync."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.config import Settings
from kodi_lab.domain import (
    AddonZipInstallationConfidence,
    AddonZipInstallationResult,
    AddonZipOrigin,
    DevelopmentSourceDeploymentConfidence,
    DevelopmentSourceDeploymentMode,
    DevelopmentSourceDeploymentOperationState,
    DevelopmentSourceDeploymentResult,
    InstanceType,
    PromotionVerificationPhase,
    PromotionVerificationState,
    RepositoryReplacementOperationState,
)
from kodi_lab.storage.engine import create_database_engine
from kodi_lab.storage.repositories import (
    AddonZipInstallationEvidenceRepository,
    AgentWorkspacePromotionHandoffRepository,
    AgentWorkspacePromotionOperationRepository,
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


class DevelopmentSourceDeploymentStateError(RuntimeError):
    code = "development_source_deployment_state_invalid"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


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
    ) -> None:
        self._session_factory = session_factory
        self._development_root = development_root
        self._data_root = data_root

    @classmethod
    def production(cls) -> SqliteDevelopmentSourceDeploymentResolver:
        settings = Settings()
        engine = create_database_engine(settings.database_url)
        return cls(
            sessionmaker(engine, expire_on_commit=False),
            development_root=settings.development_root,
            data_root=settings.data_root,
        )

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
        if self._development_root is None:
            raise DevelopmentSourceDeploymentStateError(
                "Development root is not configured."
            )
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
                if instance is None or source is None or attachment is None:
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
                    development_root=self._development_root,
                    relative_locator=source.relative_locator,
                    addon_id=source.addon_id,
                    observed_name=source.observed_name,
                    observed_version=source.observed_version,
                    staging_root=self._data_root / "development-source-staging",
                    prior_evidence=prior,
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
        if self._development_root is None:
            raise DevelopmentSourceDeploymentStateError(
                "Development root is not configured."
            )
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
        if (
            handoff is None
            or promotion is None
            or source is None
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
            or evidence.source_manifest_digest != handoff.prior_source_digest
            or evidence.verified_destination_digest != handoff.prior_destination_digest
            or evidence.last_successful_operation_id
            != handoff.prior_deployment_operation_id
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
            development_root=self._development_root,
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
        )
