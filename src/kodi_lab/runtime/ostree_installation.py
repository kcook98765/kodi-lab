"""Bounded OSTree installation evidence; authorization stays in eligibility."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from kodi_lab.runtime.attestation_integrity import canonical_json
from kodi_lab.runtime.ostree import OstreeVerificationEvidence
from kodi_lab.runtime.ostree_assembly import (
    ASSETS_SHA256,
    OstreeAssemblyEvidence,
    load_assembly_assets,
)
from kodi_lab.runtime.preview import PreviewAcknowledgement
from kodi_lab.runtime.recipes import ReviewedRecipe, load_inactive_ostree_recipe
from kodi_lab.runtime.ubuntu_support import load_support_closure


@dataclass(frozen=True)
class OstreeInstallationAssembly:
    assembly: OstreeAssemblyEvidence
    acquisition: OstreeVerificationEvidence
    acknowledgement: PreviewAcknowledgement

    @property
    def image_id(self) -> str:
        return self.assembly.image_id

    @property
    def build_log_digest(self) -> str:
        return self.assembly.build_log_digest


def ostree_attestation(
    reviewed: ReviewedRecipe, result: OstreeInstallationAssembly
) -> str:
    value = {
        "schema": "kodi-ostree-recipe-acquisition-v1",
        "recipe_id": reviewed.recipe.recipe_id,
        "recipe_revision": reviewed.recipe.recipe_revision,
        "recipe_digest": reviewed.digest,
        "release": reviewed.recipe.kodi_version,
        "support_tier": "preview",
        "provider": reviewed.recipe.provider.model_dump(mode="json"),
        "operation_id": result.assembly.operation_id,
        "image_id": result.image_id,
        "preview_acknowledgement": result.acknowledgement.model_dump(mode="json"),
        "acquisition": result.acquisition.model_dump(mode="json"),
        "assembly": json.loads(result.assembly.canonical_bytes()),
        "validation_contract": reviewed.recipe.validation_contract,
    }
    raw = canonical_json(value)
    if len(raw) > 32768 or not ostree_attestation_matches(
        value, reviewed, result.image_id
    ):
        raise ValueError("OSTree installation provenance differs")
    return raw


def ostree_attestation_matches(
    value: object, reviewed: ReviewedRecipe, image_id: str
) -> bool:
    try:
        if not isinstance(value, dict) or len(canonical_json(value)) > 32768:
            return False
        expected = {
            "schema": "kodi-ostree-recipe-acquisition-v1",
            "recipe_id": reviewed.recipe.recipe_id,
            "recipe_revision": reviewed.recipe.recipe_revision,
            "recipe_digest": reviewed.digest,
            "release": "22.0-rc.1",
            "support_tier": "preview",
            "provider": reviewed.recipe.provider.model_dump(mode="json"),
            "image_id": image_id,
            "validation_contract": reviewed.recipe.validation_contract,
        }
        if any(value.get(k) != v for k, v in expected.items()):
            return False
        ack = PreviewAcknowledgement.model_validate(value["preview_acknowledgement"])
        if ack.recipe_digest != reviewed.digest:
            return False
        acquired = OstreeVerificationEvidence.model_validate(value["acquisition"])
        if value["acquisition"] != acquired.model_dump(mode="json"):
            return False
        inputs = load_inactive_ostree_recipe()
        if acquired.recipe_digest != inputs.digest:
            return False
        for actual, pinned in zip(
            acquired.components, inputs.recipe.provider.components, strict=True
        ):
            if (
                actual.role,
                actual.ref,
                actual.commit,
                actual.collection,
                actual.metadata_sha256,
            ) != (
                pinned.role,
                pinned.ref,
                pinned.commit,
                pinned.collection,
                pinned.metadata_sha256,
            ):
                return False
        assembly = value["assembly"]
        if not isinstance(assembly, dict):
            return False
        assets = load_assembly_assets()
        fixed = {
            "schema_version": "flathub-ostree-assembly-v1",
            "recipe_id": reviewed.recipe.recipe_id,
            "recipe_revision": 1,
            "recipe_digest": inputs.digest,
            "image_id": image_id,
            "operation_id": value["operation_id"],
            "kodi_release": "22.0-rc.1",
            "platform": "linux/amd64",
            "kodi_source_revision": inputs.recipe.kodi_source_revision,
            "acquisition_digest": "sha256:"
            + hashlib.sha256(acquired.canonical_bytes()).hexdigest(),
            "supporting_closure_digest": load_support_closure().digest,
            "assembly_assets_digest": "sha256:" + ASSETS_SHA256,
            "base_image": reviewed.recipe.base_image_reference,
            "asset_hashes": sorted(
                [[k, hashlib.sha256(v).hexdigest()] for k, v in assets.items()]
            ),
            "input_components": [
                [c.role, c.ref, c.commit, c.inventory_sha256]
                for c in acquired.components
            ],
        }
        if any(assembly.get(k) != v for k, v in fixed.items()):
            return False
        return (
            all(
                isinstance(assembly.get(k), str)
                and re.fullmatch(r"sha256:[0-9a-f]{64}", assembly[k])
                for k in (
                    "context_digest",
                    "installed_inventory_digest",
                    "build_log_digest",
                )
            )
            and isinstance(assembly.get("context_bytes"), int)
            and 0 < assembly["context_bytes"] <= 1024**3
        )
    except (ValueError, KeyError, TypeError):
        return False
