"""Explicit, bounded installation handoff; never executes the next Create stage."""

from datetime import UTC, datetime
from typing import cast
from uuid import UUID

from sqlalchemy.orm import Session

from kodi_lab.application.runtime_installations import (
    CreateInstallationObservation,
    Generation,
    RuntimeInstallationService,
)
from kodi_lab.domain.create_kodi import (
    CreateCheckpoint,
    CreateChildResult,
    CreateOperation,
    CreateOutcome,
    CreateStage,
    CreateTransitionError,
)
from kodi_lab.runtime.preview import PreviewAcknowledgement, admission_evidence
from kodi_lab.runtime.recipes import load_installation_recipe, recipe_target_id
from kodi_lab.storage.create_kodi import CreateKodiRepository
from kodi_lab.storage.models import RuntimeRecipeInstallationRow
from kodi_lab.storage.repositories import RuntimeRecipeInstallationRepository


class CreateKodiInstallationService:
    def __init__(
        self,
        repository: CreateKodiRepository,
        installer: RuntimeInstallationService,
    ) -> None:
        self._repository = repository
        self._installer = installer

    def _get(self, operation_id: UUID) -> CreateOperation:
        op = self._repository.get(operation_id)
        if op is None or op.stage is not CreateStage.INSTALLATION:
            raise CreateTransitionError("not_at_installation_stage")
        return op

    @staticmethod
    def _matches(op: CreateOperation, evidence: CreateInstallationObservation) -> bool:
        return (op.generation, op.recipe_id, op.recipe_revision, op.recipe_digest) == (
            evidence.generation,
            evidence.recipe_id,
            evidence.recipe_revision,
            evidence.recipe_digest,
        )

    def install(self, operation_id: UUID, *, version: int) -> CreateOperation:
        """One explicit invocation; duplicates can inspect but never redispatch."""
        op = self._get(operation_id)
        if op.version != version:
            raise CreateTransitionError("stale_version")
        if op.checkpoint not in {CreateCheckpoint.ACCEPTED, CreateCheckpoint.PLANNED}:
            return op
        generation = cast(Generation, op.generation)
        reviewed = load_installation_recipe(generation)
        if (reviewed.digest, recipe_target_id(reviewed)) != (
            op.recipe_digest,
            op.target_id,
        ):
            return self._repository.refuse(
                op.id, version=op.version, reason="reviewed_recipe_changed"
            )
        current = self._installer.observe_create(generation)
        if (
            self._matches(op, current)
            and current.status == "ready"
            and not current.busy
        ):
            assert (
                current.operation_id
                and current.runtime_revision_id
                and current.image_id
            )

            def verify_cache() -> bool:
                if op.installation_operation_id is not None:
                    planned = self._installer.observe_create(
                        generation, op.installation_operation_id
                    )
                    if planned.status != "absent" or planned.busy:
                        return False
                return (
                    self._installer.observe_create(generation, current.operation_id)
                    == current
                )

            return self._repository.reuse_installation(
                op.id,
                version=op.version,
                source_operation_id=current.operation_id,
                runtime_revision_id=current.runtime_revision_id,
                image_id=current.image_id,
                verify_runtime=verify_cache,
            )
        if current.status != "absent" or current.busy or not self._matches(op, current):
            return self._repository.refuse(
                op.id, version=op.version, reason="installation_not_available"
            )
        if op.checkpoint is CreateCheckpoint.ACCEPTED:
            op = self._repository.plan(op.id, version=op.version)
        assert op.installation_operation_id is not None
        claimed = False

        def checkpoint(child_id: UUID) -> None:
            nonlocal claimed
            if child_id != op.installation_operation_id:
                raise CreateTransitionError("child_identity_mismatch")
            if op.generation == "piers":
                self._repository.claim_dispatch(
                    op.id, version=op.version, prepare=self._prepare_preview_admission
                )
            else:
                self._repository.claim_dispatch(op.id, version=op.version)
            claimed = True

        try:
            if op.generation == "piers":
                if not op.preview_approved:
                    raise CreateTransitionError("preview_approval_required")
                consent = PreviewAcknowledgement.model_validate(
                    {
                        "recipe_id": op.recipe_id,
                        "recipe_revision": op.recipe_revision,
                        "recipe_digest": op.recipe_digest,
                        "release": "22.0-rc.2"
                        if op.recipe_id == "piers-22.0-rc2-r4"
                        else "22.0-rc.1",
                        "support_tier": "preview",
                        "acknowledgement": "development-only-prerelease",
                    }
                )
                self._installer.start(
                    generation,
                    operation_id=op.installation_operation_id,
                    before_dispatch=checkpoint,
                    preview_consent=consent,
                )
            else:
                self._installer.start(
                    generation,
                    operation_id=op.installation_operation_id,
                    before_dispatch=checkpoint,
                )

        except BaseException as error:
            latest = self._get(op.id)
            if claimed and latest.checkpoint is CreateCheckpoint.DISPATCHED:
                latest = self._repository.note_uncertain(
                    op.id,
                    version=latest.version,
                    reason="installation_launch_unconfirmed",
                )
            elif latest.version == op.version:
                latest = self._repository.refuse(
                    op.id, version=op.version, reason="installation_admission_refused"
                )
            if not isinstance(error, Exception):
                raise
            return latest
        return self.resolve(op.id, version=self._get(op.id).version)

    @staticmethod
    def _prepare_preview_admission(session: Session, op: CreateOperation) -> None:
        """Commit application-owned consent with the exact dispatch checkpoint."""
        if (
            op.generation != "piers"
            or not op.preview_approved
            or op.stage is not CreateStage.INSTALLATION
            or op.installation_operation_id is None
            or session.get(
                RuntimeRecipeInstallationRow, str(op.installation_operation_id)
            )
            is not None
        ):
            raise CreateTransitionError("preview_admission_not_available")
        consent = PreviewAcknowledgement.model_validate(
            {
                "recipe_id": op.recipe_id,
                "recipe_revision": op.recipe_revision,
                "recipe_digest": op.recipe_digest,
                "release": "22.0-rc.2"
                if op.recipe_id == "piers-22.0-rc2-r4"
                else "22.0-rc.1",
                "support_tier": "preview",
                "acknowledgement": "development-only-prerelease",
            }
        )
        row = RuntimeRecipeInstallationRepository(session).create(
            operation_id=op.installation_operation_id,
            recipe_id=op.recipe_id,
            recipe_revision=op.recipe_revision,
            recipe_digest=op.recipe_digest,
            generation=op.generation,
            now=datetime.now(UTC),
        )
        row.attestation_json = admission_evidence(consent, op.installation_operation_id)

    def resume_refused(self, operation_id: UUID, *, version: int) -> CreateOperation:
        """Explicit continuation only; no automatic reset after refusal/failure."""
        op = self._get(operation_id)
        if op.version != version:
            raise CreateTransitionError("stale_version")
        if op.installation_operation_id is not None:
            evidence = self._installer.observe_create(
                cast(Generation, op.generation), op.installation_operation_id
            )
            if evidence.status != "absent" or evidence.busy:
                raise CreateTransitionError("undispatched_installation_not_verified")
        resumed = self._repository.resume_refused_installation(op.id, version=version)
        return self.install(resumed.id, version=resumed.version)

    def resolve(self, operation_id: UUID, *, version: int) -> CreateOperation:
        """Explicitly record exact evidence. This method NEVER calls launch."""
        op = self._get(operation_id)
        if op.version != version:
            raise CreateTransitionError("stale_version")
        recover = (
            op.checkpoint is CreateCheckpoint.NEEDS_ATTENTION
            and op.outcome is CreateOutcome.UNCERTAIN
        )
        if op.checkpoint is not CreateCheckpoint.DISPATCHED and not recover:
            return op
        assert op.installation_operation_id is not None
        observed = self._installer.observe_create(
            cast(Generation, op.generation), op.installation_operation_id
        )
        evidence = (
            observed
            if observed.operation_id == op.installation_operation_id
            and self._matches(op, observed)
            else None
        )
        if evidence is not None and evidence.status == "active":
            return op
        if (
            evidence is not None
            and evidence.status in {"ready", "failed"}
            and not evidence.busy
        ):
            result = CreateChildResult(
                stage=CreateStage.INSTALLATION,
                child_id=op.installation_operation_id,
                target_id=op.target_id,
                recipe_digest=op.recipe_digest,
                outcome=(
                    CreateOutcome.SUCCEEDED
                    if evidence.status == "ready"
                    else CreateOutcome.FAILED
                ),
                runtime_revision_id=evidence.runtime_revision_id,
                image_id=evidence.image_id,
                reason="installation_failed" if evidence.status == "failed" else None,
            )
            if recover:
                return self._repository.resolve_installation_result(
                    op.id,
                    version=version,
                    result=result,
                    verify_result=lambda: (
                        self._installer.observe_create(
                            cast(Generation, op.generation),
                            op.installation_operation_id,
                        )
                        == evidence
                    ),
                )
            return self._repository.record_result(op.id, version=version, result=result)
        if recover:
            return op
        return self._repository.note_uncertain(
            op.id, version=version, reason="installation_outcome_unknown"
        )
