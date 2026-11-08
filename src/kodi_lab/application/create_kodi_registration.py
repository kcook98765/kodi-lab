"""Exact canonical registration only; never installs or provisions a runtime."""

from typing import cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.runtime_installations import (
    Generation,
    PreviewActionApproval,
    RuntimeInstallationService,
)
from kodi_lab.domain import InstanceType, TargetType
from kodi_lab.domain.create_kodi import (
    CreateCheckpoint,
    CreateOperation,
    CreateStage,
    CreateTransitionError,
)
from kodi_lab.runtime.install import bootstrap_recipe_instance
from kodi_lab.runtime.recipes import (
    ReviewedRecipe,
    load_recorded_reviewed_recipe,
    recipe_target_id,
)
from kodi_lab.storage.create_kodi import CreateKodiRepository
from kodi_lab.storage.models import (
    ManagedInstanceRow,
    RuntimeRecipeInstallationRow,
    TargetRow,
)


def require_create_runtime(
    session: Session,
    op: CreateOperation,
    installer: RuntimeInstallationService,
) -> ReviewedRecipe:
    """Revalidate immutable approved intent and authoritative ready runtime evidence."""
    reviewed = load_recorded_reviewed_recipe(
        op.generation, op.recipe_id, op.recipe_revision, op.recipe_digest
    )
    if (
        recipe_target_id(reviewed) != op.target_id
        or op.runtime_revision_id is None
        or op.image_id is None
    ):
        raise CreateTransitionError("create_runtime_binding_missing")
    source = session.scalar(
        select(RuntimeRecipeInstallationRow)
        .where(
            RuntimeRecipeInstallationRow.generation == op.generation,
            RuntimeRecipeInstallationRow.recipe_id == op.recipe_id,
            RuntimeRecipeInstallationRow.recipe_revision == op.recipe_revision,
            RuntimeRecipeInstallationRow.recipe_digest == op.recipe_digest,
            RuntimeRecipeInstallationRow.runtime_revision_id
            == str(op.runtime_revision_id),
            RuntimeRecipeInstallationRow.image_id == op.image_id,
            RuntimeRecipeInstallationRow.state == "ready",
        )
        .order_by(RuntimeRecipeInstallationRow.id)
        .limit(1)
    )
    if source is None:
        raise CreateTransitionError("create_runtime_receipt_missing")
    observed = installer.observe_create(
        cast(Generation, op.generation), UUID(source.id)
    )
    if (
        observed.status != "ready"
        or observed.busy
        or (
            observed.recipe_id,
            observed.recipe_revision,
            observed.recipe_digest,
            observed.runtime_revision_id,
            observed.image_id,
        )
        != (
            op.recipe_id,
            op.recipe_revision,
            op.recipe_digest,
            op.runtime_revision_id,
            op.image_id,
        )
    ):
        raise CreateTransitionError("create_runtime_not_verified")
    return reviewed


def registration_matches(session: Session, op: CreateOperation) -> bool:
    target = session.get(TargetRow, str(op.target_id))
    instance = session.get(ManagedInstanceRow, str(op.target_id))
    reviewed = load_recorded_reviewed_recipe(
        op.generation, op.recipe_id, op.recipe_revision, op.recipe_digest
    )
    if target is not None and (
        target.target_type is not TargetType.MANAGED
        or target.kodi_version != reviewed.recipe.kodi_version
    ):
        raise CreateTransitionError("canonical_target_conflict")
    if instance is None:
        return False
    if (
        target is None
        or instance.runtime_revision_id != str(op.runtime_revision_id)
        or instance.instance_type is not InstanceType.DISPOSABLE
    ):
        raise CreateTransitionError("canonical_pin_conflict")
    return True


class CreateKodiRegistrationService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        repository: CreateKodiRepository,
        installer: RuntimeInstallationService,
    ) -> None:
        self._sessions, self._repository, self._installer = (
            sessions,
            repository,
            installer,
        )

    def _verify(self, session: Session, op: CreateOperation) -> bool:
        require_create_runtime(session, op, self._installer)
        return registration_matches(session, op)

    def register(self, operation_id: UUID, *, version: int) -> CreateOperation:
        op = self._repository.get(operation_id)
        if (
            op is None
            or op.version != version
            or op.stage is not CreateStage.REGISTRATION
        ):
            raise CreateTransitionError("stale_registration_context")
        if op.checkpoint not in {CreateCheckpoint.ACCEPTED, CreateCheckpoint.PLANNED}:
            return op
        with self._sessions() as session:
            reviewed = require_create_runtime(session, op, self._installer)
            exists = registration_matches(session, op)
        if exists:
            return self._repository.confirm_registration(
                op.id, version=version, verify=self._verify
            )
        if op.checkpoint is CreateCheckpoint.ACCEPTED:
            op = self._repository.plan(op.id, version=version)
        claimed = self._repository.claim_dispatch(op.id, version=op.version).operation
        assert op.runtime_revision_id is not None
        try:
            # Already committed before either of the helper's local transactions.
            if op.generation == "piers":
                from kodi_lab.runtime.preview import PreviewAcknowledgement

                if not op.preview_approved:
                    raise CreateTransitionError("preview_approval_required")
                self._installer.create_preview_instance(
                    # This is the original exact Install-and-prepare intent,
                    # checked above for stage/version/runtime and Preview approval.
                    # It is not an approval inherited from a runtime-admin Install.
                    approval=PreviewActionApproval.model_validate(
                        {
                            "action": "create",
                            "approved": op.preview_approved,
                            "acknowledgement": PreviewAcknowledgement.model_validate(
                                {
                                    "recipe_id": op.recipe_id,
                                    "recipe_revision": op.recipe_revision,
                                    "recipe_digest": op.recipe_digest,
                                    "release": "22.0-rc.1",
                                    "support_tier": "preview",
                                    "acknowledgement": "development-only-prerelease",
                                }
                            ),
                        }
                    ),
                )
            else:
                result = bootstrap_recipe_instance(
                    self._sessions, reviewed, op.runtime_revision_id
                )
                if result.get("target_id") != str(op.target_id):
                    raise CreateTransitionError(
                        "canonical_registration_result_mismatch"
                    )
            return self._repository.confirm_registration(
                op.id,
                version=claimed.version,
                verify=self._verify,
            )
        except BaseException as error:
            current = self._repository.get(op.id)
            assert current is not None
            if current.checkpoint is CreateCheckpoint.DISPATCHED:
                current = self._repository.note_uncertain(
                    op.id, version=current.version, reason="registration_unconfirmed"
                )
            if not isinstance(error, Exception):
                raise
            return current

    def confirm(self, operation_id: UUID, *, version: int) -> CreateOperation:
        """Explicit read-only goal confirmation after uncertain local mutation.

        An absent/partial/conflicting registration cannot trigger the helper.
        """
        return self._repository.confirm_registration(
            operation_id, version=version, verify=self._verify
        )
