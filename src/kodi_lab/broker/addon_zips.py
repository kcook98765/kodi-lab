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
    AddonZipExtensionClassification,
    AddonZipInstallationOperationState,
    InstanceType,
    PythonBytecodePolicy,
)
from kodi_lab.storage.engine import create_database_engine
from kodi_lab.storage.repositories import (
    AddonZipArtifactRepository,
    AddonZipInstallationOperationRepository,
    ManagedInstanceRepository,
    RuntimeExecutionProfileRepository,
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
