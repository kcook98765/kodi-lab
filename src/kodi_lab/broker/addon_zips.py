"""Broker-trusted uploaded-ZIP installation resolution."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.config import Settings
from kodi_lab.development import PayloadManifestEntry, revalidate_addon_zip_artifact
from kodi_lab.domain import (
    AddonZipEnablementOperationState,
    AddonZipExtensionClassification,
    AddonZipInstallationConfidence,
    AddonZipInstallationOperationState,
    AddonZipInstallationResult,
    AddonZipOrigin,
    AddonZipSmokeTestOperationState,
    DevelopmentSourceDeploymentConfidence,
    DevelopmentSourceDeploymentOperationState,
    InstanceType,
    PythonBytecodePolicy,
    RepositoryReplacementIncumbentClass,
    RepositoryReplacementOperationState,
    RepositoryReplacementPlanState,
    SnapshotState,
)
from kodi_lab.storage.engine import create_database_engine
from kodi_lab.storage.repositories import (
    AddonRepositoryMetadataRepository,
    AddonRepositorySourceRepository,
    AddonZipArtifactRepository,
    AddonZipEnablementOperationRepository,
    AddonZipInstallationEvidenceRepository,
    AddonZipInstallationOperationRepository,
    AddonZipSmokeTestOperationRepository,
    DevelopmentSourceDeploymentOperationRepository,
    DevelopmentSourceDeploymentRepository,
    ManagedInstanceAddonRepositorySourceRepository,
    ManagedInstanceDevelopmentSourceRepository,
    ManagedInstanceRepository,
    RepositoryReplacementOperationRepository,
    RepositoryReplacementPlanRepository,
    RuntimeExecutionProfileRepository,
    SnapshotRepository,
)


class AddonZipInstallationStateError(RuntimeError):
    code = "addon_zip_installation_state_invalid"


@dataclass(frozen=True, slots=True)
class AddonZipInstallationSpec:
    managed_instance_id: str
    artifact_id: str
    installation_operation_id: str
    runtime_revision_id: str
    runtime_uid: int
    runtime_gid: int
    addon_id: str
    addon_version: str
    payload_manifest_digest: str
    payload_root: Path
    staging_root: Path
    entries: tuple[PayloadManifestEntry, ...]
    application_started: bool


@dataclass(frozen=True, slots=True)
class AddonZipEnablementVerificationSpec(AddonZipInstallationSpec):
    enablement_operation_id: str


@dataclass(frozen=True, slots=True)
class AddonZipSmokeVerificationSpec(AddonZipInstallationSpec):
    smoke_operation_id: str


@dataclass(frozen=True, slots=True)
class RepositoryReplacementSpec:
    managed_instance_id: str
    approval_id: str
    replacement_operation_id: str | None
    artifact_id: str
    runtime_revision_id: str
    runtime_uid: int
    runtime_gid: int
    addon_id: str
    addon_version: str
    development_source_id: str
    incumbent_class: RepositoryReplacementIncumbentClass
    verified_destination_digest: str | None
    payload_root: Path
    staging_root: Path
    entries: tuple[PayloadManifestEntry, ...]
    application_started: bool
    approval_consumed: bool


class SqliteAddonZipInstallationResolver:
    def __init__(
        self, session_factory: sessionmaker[Session], *, data_root: Path
    ) -> None:
        self._session_factory = session_factory
        self._data_root = data_root

    @classmethod
    def production(cls) -> SqliteAddonZipInstallationResolver:
        settings = Settings()
        engine = create_database_engine(settings.database_url)
        return cls(
            sessionmaker(engine, expire_on_commit=False), data_root=settings.data_root
        )

    def resolve(
        self,
        managed_instance_id: str,
        artifact_id: str,
        installation_operation_id: str,
        expected_container_id: str,
    ) -> AddonZipInstallationSpec:
        return self._resolve(
            managed_instance_id,
            artifact_id,
            installation_operation_id,
            expected_container_id,
            allowed_states={AddonZipInstallationOperationState.RUNNING},
        )

    def resolve_observation(
        self,
        managed_instance_id: str,
        artifact_id: str,
        installation_operation_id: str,
        expected_container_id: str,
    ) -> AddonZipInstallationSpec:
        """Resolve the same immutable authority for bounded reconciliation readback."""

        return self._resolve(
            managed_instance_id,
            artifact_id,
            installation_operation_id,
            expected_container_id,
            allowed_states={
                AddonZipInstallationOperationState.RUNNING,
                AddonZipInstallationOperationState.RECONCILIATION_REQUIRED,
            },
        )

    def resolve_replacement_plan(
        self,
        managed_instance_id: str,
        approval_id: str,
        expected_container_id: str,
    ) -> RepositoryReplacementSpec:
        return self._resolve_replacement(
            managed_instance_id,
            approval_id,
            None,
            expected_container_id,
        )

    def resolve_replacement_execution(
        self,
        managed_instance_id: str,
        approval_id: str,
        replacement_operation_id: str,
        expected_container_id: str,
    ) -> RepositoryReplacementSpec:
        return self._resolve_replacement(
            managed_instance_id,
            approval_id,
            replacement_operation_id,
            expected_container_id,
        )

    def _resolve_replacement(
        self,
        managed_instance_id: str,
        approval_id: str,
        replacement_operation_id: str | None,
        expected_container_id: str,
    ) -> RepositoryReplacementSpec:
        try:
            instance_uuid = UUID(managed_instance_id)
            approval_uuid = UUID(approval_id)
            operation_uuid = (
                None
                if replacement_operation_id is None
                else UUID(replacement_operation_id)
            )
            if (
                str(instance_uuid) != managed_instance_id
                or str(approval_uuid) != approval_id
                or (
                    operation_uuid is not None
                    and str(operation_uuid) != replacement_operation_id
                )
                or len(expected_container_id) != 64
                or any(c not in "0123456789abcdef" for c in expected_container_id)
            ):
                raise ValueError
        except ValueError as error:
            raise AddonZipInstallationStateError(
                "Repository replacement identities are not canonical."
            ) from error
        try:
            with self._session_factory() as session:
                plans = RepositoryReplacementPlanRepository(session)
                plan = plans.get(approval_uuid)
                instance = ManagedInstanceRepository(session).get(instance_uuid)
                artifact = (
                    None
                    if plan is None
                    else AddonZipArtifactRepository(session).get(
                        plan.incoming_artifact_id
                    )
                )
                repository = (
                    None
                    if plan is None
                    else AddonRepositorySourceRepository(session).get(
                        plan.repository_source_id
                    )
                )
                candidate = (
                    None
                    if plan is None
                    else AddonRepositoryMetadataRepository(session).get_candidate(
                        plan.repository_candidate_id
                    )
                )
                profile = (
                    None
                    if instance is None
                    else RuntimeExecutionProfileRepository(session).get(
                        instance.runtime_revision_id
                    )
                )
                operation = (
                    None
                    if operation_uuid is None
                    else RepositoryReplacementOperationRepository(session).get(
                        operation_uuid
                    )
                )
                source_attachment = (
                    None
                    if plan is None
                    else ManagedInstanceDevelopmentSourceRepository(session).get(
                        instance_uuid, plan.development_source_id
                    )
                )
                repository_attachment = (
                    None
                    if plan is None
                    else ManagedInstanceAddonRepositorySourceRepository(session).get(
                        instance_uuid, plan.repository_source_id
                    )
                )
                evidence = (
                    None
                    if plan is None
                    else DevelopmentSourceDeploymentRepository(session).get(
                        instance_uuid, plan.development_source_id
                    )
                )
                evidence_operation = (
                    None
                    if evidence is None
                    else DevelopmentSourceDeploymentOperationRepository(session).get(
                        evidence.last_successful_operation_id
                    )
                )
                snapshot = (
                    None
                    if plan is None or plan.qualifying_snapshot_id is None
                    else SnapshotRepository(session).get(plan.qualifying_snapshot_id)
                )
            if (
                plan is None
                or instance is None
                or artifact is None
                or repository is None
                or candidate is None
                or profile is None
                or source_attachment is None
                or repository_attachment is None
                or plan.managed_instance_id != instance_uuid
                or plan.expected_container_id != expected_container_id
                or plan.runtime_revision_id != instance.runtime_revision_id
                or plan.incoming_artifact_id != artifact.id
                or artifact.origin is not AddonZipOrigin.REPOSITORY_DOWNLOAD
                or artifact.addon_id != plan.addon_id
                or artifact.addon_version != plan.incoming_version
                or artifact.payload_manifest_digest
                != plan.incoming_payload_manifest_digest
                or artifact.repository_source_id != plan.repository_source_id
                or artifact.repository_snapshot_id != plan.repository_snapshot_id
                or artifact.repository_candidate_id != plan.repository_candidate_id
                or candidate.repository_source_id != repository.id
                or candidate.snapshot_id != plan.repository_snapshot_id
                or candidate.addon_id != plan.addon_id
                or candidate.version != plan.incoming_version
                or source_attachment.attached_at
                != plan.development_source_attachment_at
                or repository_attachment.attached_at != plan.repository_attachment_at
                or profile.python_bytecode_policy is not PythonBytecodePolicy.SUPPRESS
            ):
                raise AddonZipInstallationStateError(
                    "Canonical repository replacement state is inconsistent."
                )
            if operation_uuid is None:
                if plan.state not in {
                    RepositoryReplacementPlanState.PENDING,
                    RepositoryReplacementPlanState.APPROVED,
                    RepositoryReplacementPlanState.CONSUMED,
                }:
                    raise AddonZipInstallationStateError(
                        "Repository replacement plan is not pending."
                    )
            elif (
                operation is None
                or operation.state is not RepositoryReplacementOperationState.RUNNING
                or operation.plan_id != plan.id
                or operation.managed_instance_id != instance_uuid
                or operation.incoming_artifact_id != artifact.id
                or operation.development_source_id != plan.development_source_id
                or operation.expected_container_id != expected_container_id
                or plan.consumed_execution_operation_id != operation.id
                or plan.state
                not in {
                    RepositoryReplacementPlanState.APPROVED,
                    RepositoryReplacementPlanState.CONSUMED,
                }
            ):
                raise AddonZipInstallationStateError(
                    "Repository replacement execution is inconsistent."
                )
            copied = (
                plan.incumbent_class
                is RepositoryReplacementIncumbentClass.VERIFIED_COPIED
            )
            if copied:
                if (
                    evidence is None
                    or evidence_operation is None
                    or evidence_operation.state
                    is not DevelopmentSourceDeploymentOperationState.SUCCEEDED
                    or evidence.last_successful_operation_id
                    != plan.development_deployment_operation_id
                    or evidence.source_manifest_digest
                    != plan.development_source_manifest_digest
                    or evidence.verified_destination_digest
                    != plan.development_destination_digest
                    or evidence.runtime_revision_id != plan.runtime_revision_id
                    or evidence.trusted_container_id != expected_container_id
                    or (
                        plan.state is not RepositoryReplacementPlanState.CONSUMED
                        and evidence.confidence
                        is not (
                            DevelopmentSourceDeploymentConfidence.VERIFIED_AT_LAST_OPERATION
                        )
                    )
                    or (
                        plan.state is RepositoryReplacementPlanState.CONSUMED
                        and evidence.confidence
                        is not DevelopmentSourceDeploymentConfidence.UNKNOWN
                    )
                    or snapshot is None
                    or snapshot.state is not SnapshotState.AVAILABLE
                    or snapshot.source_managed_instance_id != instance_uuid
                    or snapshot.runtime_revision_id != plan.runtime_revision_id
                ):
                    raise AddonZipInstallationStateError(
                        "Verified copied incumbent state is inconsistent."
                    )
            elif evidence is not None:
                raise AddonZipInstallationStateError(
                    "Unmaterialized incumbent gained deployment evidence."
                )
            validated = revalidate_addon_zip_artifact(self._data_root, artifact)
            return RepositoryReplacementSpec(
                managed_instance_id=managed_instance_id,
                approval_id=approval_id,
                replacement_operation_id=replacement_operation_id,
                artifact_id=str(artifact.id),
                runtime_revision_id=str(instance.runtime_revision_id),
                runtime_uid=profile.runtime_uid,
                runtime_gid=profile.runtime_gid,
                addon_id=artifact.addon_id,
                addon_version=artifact.addon_version,
                development_source_id=str(plan.development_source_id),
                incumbent_class=plan.incumbent_class,
                verified_destination_digest=plan.development_destination_digest,
                payload_root=self._data_root
                / "addon-zips"
                / "artifacts"
                / str(artifact.id)
                / "payload",
                staging_root=self._data_root / "addon-zips" / "replacement-staging",
                entries=validated.entries,
                application_started=(
                    plan.state is RepositoryReplacementPlanState.CONSUMED
                    or (operation is not None and operation.application_started)
                ),
                approval_consumed=(
                    plan.state is RepositoryReplacementPlanState.CONSUMED
                ),
            )
        except AddonZipInstallationStateError:
            raise
        except (SQLAlchemyError, OSError, ValueError) as error:
            raise AddonZipInstallationStateError(
                "Canonical repository replacement state is unavailable."
            ) from error

    def resolve_enablement_verification(
        self,
        managed_instance_id: str,
        artifact_id: str,
        enablement_operation_id: str,
        expected_container_id: str,
    ) -> AddonZipEnablementVerificationSpec:
        """Resolve fresh readback solely from ZIP enablement semantic identity."""

        try:
            instance_uuid = UUID(managed_instance_id)
            artifact_uuid = UUID(artifact_id)
            enablement_uuid = UUID(enablement_operation_id)
            if (
                str(instance_uuid) != managed_instance_id
                or str(artifact_uuid) != artifact_id
                or str(enablement_uuid) != enablement_operation_id
            ):
                raise ValueError
        except ValueError as error:
            raise AddonZipInstallationStateError(
                "ZIP enablement identities are not canonical."
            ) from error
        try:
            with self._session_factory() as session:
                artifact = AddonZipArtifactRepository(session).get(artifact_uuid)
                enablement = AddonZipEnablementOperationRepository(session).get(
                    enablement_uuid
                )
                evidence = (
                    None
                    if artifact is None
                    else AddonZipInstallationEvidenceRepository(session).get(
                        instance_uuid, artifact.addon_id
                    )
                )
                installation = (
                    None
                    if evidence is None or evidence.installation_operation_id is None
                    else AddonZipInstallationOperationRepository(session).get(
                        evidence.installation_operation_id
                    )
                )
            if (
                artifact is None
                or enablement is None
                or evidence is None
                or installation is None
                or artifact.origin is not AddonZipOrigin.UPLOADED_ZIP
                or evidence.artifact_origin is not AddonZipOrigin.UPLOADED_ZIP
                or enablement.state is not AddonZipEnablementOperationState.RUNNING
                or enablement.managed_instance_id != instance_uuid
                or enablement.artifact_id != artifact_uuid
                or enablement.installation_operation_id
                != evidence.installation_operation_id
                or enablement.expected_container_id != expected_container_id
                or enablement.runtime_revision_id != evidence.runtime_revision_id
                or enablement.addon_id != artifact.addon_id
                or enablement.addon_version != artifact.addon_version
                or evidence.artifact_id != artifact_uuid
                or evidence.trusted_container_id != expected_container_id
                or evidence.confidence
                is not AddonZipInstallationConfidence.VERIFIED_AT_LAST_OPERATION
                or evidence.result
                is not AddonZipInstallationResult.INSTALLED_RECOGNIZED_DISABLED
                or evidence.original_managed_instance_id != instance_uuid
                or evidence.addon_id != artifact.addon_id
                or evidence.addon_version != artifact.addon_version
                or evidence.payload_manifest_digest != artifact.payload_manifest_digest
                or installation.state
                is not AddonZipInstallationOperationState.SUCCEEDED
                or installation.original_managed_instance_id != instance_uuid
                or installation.managed_instance_id != instance_uuid
                or installation.artifact_id != artifact_uuid
                or installation.addon_id != artifact.addon_id
                or installation.addon_version != artifact.addon_version
                or installation.runtime_revision_id != evidence.runtime_revision_id
                or installation.expected_container_id != expected_container_id
                or not installation.application_started
                or not installation.stopped_readback_matched
                or not installation.final_readback_matched
                or installation.recognition_enabled is not False
            ):
                raise AddonZipInstallationStateError(
                    "Canonical ZIP enablement state is inconsistent."
                )
            base = self._resolve(
                managed_instance_id,
                artifact_id,
                str(evidence.installation_operation_id),
                expected_container_id,
                allowed_states={AddonZipInstallationOperationState.SUCCEEDED},
            )
            return AddonZipEnablementVerificationSpec(
                **{
                    field: getattr(base, field)
                    for field in AddonZipInstallationSpec.__dataclass_fields__
                },
                enablement_operation_id=enablement_operation_id,
            )
        except AddonZipInstallationStateError:
            raise
        except (SQLAlchemyError, OSError, ValueError) as error:
            raise AddonZipInstallationStateError(
                "Canonical ZIP enablement verification state is unavailable."
            ) from error

    def resolve_smoke_verification(
        self,
        managed_instance_id: str,
        artifact_id: str,
        smoke_operation_id: str,
        expected_container_id: str,
    ) -> AddonZipSmokeVerificationSpec:
        """Resolve immutable installed-tree authority for one running ZIP smoke."""

        try:
            instance_uuid = UUID(managed_instance_id)
            artifact_uuid = UUID(artifact_id)
            smoke_uuid = UUID(smoke_operation_id)
            if (
                str(instance_uuid) != managed_instance_id
                or str(artifact_uuid) != artifact_id
                or str(smoke_uuid) != smoke_operation_id
            ):
                raise ValueError
        except ValueError as error:
            raise AddonZipInstallationStateError(
                "ZIP smoke identities are not canonical."
            ) from error
        try:
            with self._session_factory() as session:
                artifact = AddonZipArtifactRepository(session).get(artifact_uuid)
                smoke = AddonZipSmokeTestOperationRepository(session).get(smoke_uuid)
                evidence = (
                    None
                    if artifact is None
                    else AddonZipInstallationEvidenceRepository(session).get(
                        instance_uuid, artifact.addon_id
                    )
                )
                installation = (
                    None
                    if evidence is None or evidence.installation_operation_id is None
                    else AddonZipInstallationOperationRepository(session).get(
                        evidence.installation_operation_id
                    )
                )
            if (
                artifact is None
                or smoke is None
                or evidence is None
                or installation is None
                or artifact.origin is not AddonZipOrigin.UPLOADED_ZIP
                or evidence.artifact_origin is not AddonZipOrigin.UPLOADED_ZIP
                or smoke.state is not AddonZipSmokeTestOperationState.RUNNING
                or smoke.managed_instance_id != instance_uuid
                or smoke.artifact_id != artifact_uuid
                or smoke.installation_operation_id != evidence.installation_operation_id
                or smoke.expected_container_id != expected_container_id
                or smoke.runtime_revision_id != evidence.runtime_revision_id
                or smoke.addon_id != artifact.addon_id
                or smoke.addon_version != artifact.addon_version
                or evidence.artifact_id != artifact_uuid
                or evidence.trusted_container_id != expected_container_id
                or evidence.confidence
                is not AddonZipInstallationConfidence.VERIFIED_AT_LAST_OPERATION
                or evidence.result
                is not AddonZipInstallationResult.INSTALLED_RECOGNIZED_DISABLED
                or evidence.original_managed_instance_id != instance_uuid
                or evidence.addon_id != artifact.addon_id
                or evidence.addon_version != artifact.addon_version
                or evidence.payload_manifest_digest != artifact.payload_manifest_digest
                or installation.state
                is not AddonZipInstallationOperationState.SUCCEEDED
                or installation.original_managed_instance_id != instance_uuid
                or installation.managed_instance_id != instance_uuid
                or installation.artifact_id != artifact_uuid
                or installation.id != smoke.installation_operation_id
                or installation.runtime_revision_id != evidence.runtime_revision_id
                or installation.expected_container_id != expected_container_id
                or not installation.application_started
                or not installation.stopped_readback_matched
                or not installation.final_readback_matched
                or installation.recognition_enabled is not False
            ):
                raise AddonZipInstallationStateError(
                    "Canonical ZIP smoke state is inconsistent."
                )
            base = self._resolve(
                managed_instance_id,
                artifact_id,
                str(evidence.installation_operation_id),
                expected_container_id,
                allowed_states={AddonZipInstallationOperationState.SUCCEEDED},
            )
            return AddonZipSmokeVerificationSpec(
                **{
                    field: getattr(base, field)
                    for field in AddonZipInstallationSpec.__dataclass_fields__
                },
                smoke_operation_id=smoke_operation_id,
            )
        except AddonZipInstallationStateError:
            raise
        except (SQLAlchemyError, OSError, ValueError) as error:
            raise AddonZipInstallationStateError(
                "Canonical ZIP smoke verification state is unavailable."
            ) from error

    def _resolve(
        self,
        managed_instance_id: str,
        artifact_id: str,
        installation_operation_id: str,
        expected_container_id: str,
        *,
        allowed_states: set[AddonZipInstallationOperationState],
    ) -> AddonZipInstallationSpec:
        try:
            instance_uuid = UUID(managed_instance_id)
            artifact_uuid = UUID(artifact_id)
            operation_uuid = UUID(installation_operation_id)
            if (
                str(instance_uuid) != managed_instance_id
                or str(artifact_uuid) != artifact_id
                or str(operation_uuid) != installation_operation_id
                or len(expected_container_id) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in expected_container_id
                )
            ):
                raise ValueError
        except ValueError as error:
            raise AddonZipInstallationStateError(
                "ZIP installation identities are not canonical."
            ) from error
        try:
            with self._session_factory() as session:
                instance = ManagedInstanceRepository(session).get(instance_uuid)
                artifact = AddonZipArtifactRepository(session).get(artifact_uuid)
                operation = AddonZipInstallationOperationRepository(session).get(
                    operation_uuid
                )
                profile = (
                    None
                    if instance is None
                    else RuntimeExecutionProfileRepository(session).get(
                        instance.runtime_revision_id
                    )
                )
            if (
                instance is None
                or artifact is None
                or operation is None
                or profile is None
                or instance.instance_type is not InstanceType.DISPOSABLE
                or profile.python_bytecode_policy is not PythonBytecodePolicy.SUPPRESS
                or artifact.extension_classification
                is not AddonZipExtensionClassification.PYTHON_VIDEO_PLUGIN
                or operation.state not in allowed_states
                or operation.managed_instance_id != instance_uuid
                or operation.artifact_id != artifact_uuid
                or operation.runtime_revision_id != instance.runtime_revision_id
                or operation.addon_id != artifact.addon_id
                or operation.addon_version != artifact.addon_version
                or operation.expected_container_id != expected_container_id
            ):
                raise AddonZipInstallationStateError(
                    "Canonical ZIP installation state is inconsistent."
                )
            validated = revalidate_addon_zip_artifact(self._data_root, artifact)
            return AddonZipInstallationSpec(
                managed_instance_id=managed_instance_id,
                artifact_id=artifact_id,
                installation_operation_id=installation_operation_id,
                runtime_revision_id=str(instance.runtime_revision_id),
                runtime_uid=profile.runtime_uid,
                runtime_gid=profile.runtime_gid,
                addon_id=artifact.addon_id,
                addon_version=artifact.addon_version,
                payload_manifest_digest=artifact.payload_manifest_digest,
                payload_root=self._data_root
                / "addon-zips"
                / "artifacts"
                / artifact_id
                / "payload",
                staging_root=self._data_root / "addon-zips" / "install-staging",
                entries=validated.entries,
                application_started=operation.application_started,
            )
        except AddonZipInstallationStateError:
            raise
        except (SQLAlchemyError, OSError, ValueError) as error:
            raise AddonZipInstallationStateError(
                "Canonical ZIP artifact state could not be verified."
            ) from error
