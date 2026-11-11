"""Explicit Create intent admission; no runner or lifecycle calls."""

from uuid import UUID

from kodi_lab.domain.create_kodi import CreateAcceptance, CreateIntent, CreateOperation
from kodi_lab.runtime.recipes import load_installation_recipe, recipe_target_id
from kodi_lab.storage.create_kodi import CreateKodiRepository


class CreateKodiService:
    def __init__(self, repository: CreateKodiRepository) -> None:
        self.repository = repository

    def accept(
        self,
        *,
        request_id: UUID,
        generation: str,
        recipe_digest: str,
        owner_approved: bool,
        start_after_setup: bool = True,
        preview_approved: bool = False,
        bridge_required: bool = False,
    ) -> CreateAcceptance:
        if owner_approved is not True:
            raise ValueError("explicit Create approval required")
        if generation not in {"matrix", "nexus", "omega", "piers"}:
            raise ValueError("reviewed generation required")
        if type(start_after_setup) is not bool or type(preview_approved) is not bool:
            raise ValueError("setup options must be booleans")
        if generation == "piers" and not preview_approved:
            raise ValueError("explicit development-only prerelease approval required")

        def resolve() -> CreateIntent:
            reviewed = load_installation_recipe(generation)
            return CreateIntent(
                generation=generation,
                target_id=recipe_target_id(reviewed),
                recipe_id=reviewed.recipe.recipe_id,
                recipe_revision=reviewed.recipe.recipe_revision,
                recipe_digest=reviewed.digest,
                start_after_setup=start_after_setup,
                preview_approved=preview_approved,
            )

        return self.repository.accept(
            request_id=request_id,
            generation=generation,
            recipe_digest=recipe_digest,
            resolve_intent=resolve,
            start_after_setup=start_after_setup,
            preview_approved=preview_approved,
            bridge_required=bridge_required,
        )

    def get(self, operation_id: UUID) -> CreateOperation | None:
        return self.repository.get(operation_id)

    def for_target(self, target_id: UUID) -> CreateOperation | None:
        return self.repository.for_target(target_id)

    def for_request(self, request_id: UUID) -> CreateOperation | None:
        return self.repository.for_request(request_id)
