"""RC1 revision 2 changes four source locations and preserves immutable history."""

from __future__ import annotations

import json
from dataclasses import asdict
from uuid import uuid4

import pytest
from pydantic import ValidationError

from kodi_lab.runtime.ostree_assembly import load_assembly_assets
from kodi_lab.runtime.preview import PreviewAcknowledgement, admission_evidence
from kodi_lab.runtime.recipes import (
    HISTORICAL_PREVIEW_INSTALLATION_DIGEST,
    load_installation_recipe,
    load_recorded_reviewed_recipe,
)
from kodi_lab.runtime.ubuntu_support import (
    SNAPSHOT_PACKAGES,
    SupportError,
    _parse,
    load_support_closure,
)


def test_revision_changes_only_four_source_locations_and_preserves_dependencies() -> (
    None
):
    old, new = load_support_closure(revision=1), load_support_closure()
    assert old.digest != new.digest
    assert old.base_inventory == new.base_inventory
    assert old.final_inventory == new.final_inventory
    assert len(new.packages) == len(old.packages) == 97
    changed = set()
    for before, after in zip(old.packages, new.packages, strict=True):
        a, b = asdict(before), asdict(after)
        assert {k: v for k, v in a.items() if k not in {"url", "index"}} == {
            k: v for k, v in b.items() if k not in {"url", "index"}
        }
        if a != b:
            changed.add(after.name)
            assert after.url.startswith(
                "https://snapshot.ubuntu.com/ubuntu/20261001T000000Z/pool/"
            )
    assert changed == SNAPSHOT_PACKAGES
    old_assets, new_assets = load_assembly_assets(revision=1), load_assembly_assets()
    assert old_assets.keys() == new_assets.keys()
    assert {n for n in old_assets if old_assets[n] != new_assets[n]} == {"Dockerfile"}
    assert b'io.kodi-lab.support-recipe-revision="2"' in new_assets["Dockerfile"]


@pytest.mark.parametrize(
    "url",
    [
        "https://snapshot.ubuntu.com/ubuntu/latest/pool/main/c/ca-certificates/FILE",
        "https://snapshot.ubuntu.com/ubuntu/20261002T000000Z/pool/main/c/ca-certificates/FILE",
        "http://snapshot.ubuntu.com/ubuntu/20261001T000000Z/pool/main/c/ca-certificates/FILE",
        "https://snapshot.ubuntu.com:443/ubuntu/20261001T000000Z/pool/main/c/ca-certificates/FILE",
        "https://archive.ubuntu.com.attacker.invalid/ubuntu/pool/main/c/ca-certificates/FILE",
        "https://snapshot.ubuntu.com/ubuntu/20261001T000000Z/pool/../FILE",
    ],
)
def test_moving_or_changed_snapshot_origin_is_rejected(url: str) -> None:
    data = json.loads(load_support_closure().raw)
    package = data["packages"][0]
    package["url"] = url.replace("FILE", package["archive_name"])
    with pytest.raises(SupportError):
        _parse(json.dumps(data).encode())


def test_historical_recipe_and_acknowledgement_do_not_authorize_new_install() -> None:
    old = load_recorded_reviewed_recipe(
        "piers", "piers-22.0-rc1-r1", 1, HISTORICAL_PREVIEW_INSTALLATION_DIGEST
    )
    new = load_installation_recipe("piers")
    assert (old.recipe.recipe_revision, new.recipe.recipe_revision) == (1, 2)
    assert old.recipe.recipe_id == "piers-22.0-rc1-r1"
    assert new.recipe.recipe_id == "piers-22.0-rc1-r2"
    assert old.recipe.provider == new.recipe.provider
    ack = PreviewAcknowledgement(
        recipe_id=old.recipe.recipe_id,  # type: ignore[arg-type]
        recipe_revision=1,
        recipe_digest=old.digest,  # type: ignore[arg-type]
        release="22.0-rc.1",
        support_tier="preview",
        acknowledgement="development-only-prerelease",
    )
    with pytest.raises(ValueError, match="Preview recipe differs"):
        admission_evidence(ack, uuid4())
    for field, value in [
        ("recipe_id", new.recipe.recipe_id),
        ("recipe_revision", 2),
        ("recipe_digest", new.digest),
    ]:
        data = ack.model_dump()
        data[field] = value
        with pytest.raises(ValidationError):
            PreviewAcknowledgement.model_validate(data)


@pytest.mark.parametrize("revision", [0, 3, True])
def test_support_and_assembly_revision_selection_remains_closed(revision: int) -> None:
    from kodi_lab.runtime.ostree_assembly import OstreeAssemblyError

    with pytest.raises(SupportError):
        load_support_closure(revision=revision)
    with pytest.raises(OstreeAssemblyError):
        load_assembly_assets(revision=revision)


def test_original_attestation_remains_historical_and_cannot_be_relabelled() -> None:
    from pathlib import Path

    from kodi_lab.runtime.ostree_installation import ostree_attestation_matches

    fixture = json.loads(
        (Path(__file__).parent / "fixtures/rc1-installation-evidence.json").read_text()
    )
    old = load_recorded_reviewed_recipe(
        "piers", "piers-22.0-rc1-r1", 1, HISTORICAL_PREVIEW_INSTALLATION_DIGEST
    )
    raw = fixture["assembly"]
    value = {
        "schema": "kodi-ostree-recipe-acquisition-v1",
        "recipe_id": old.recipe.recipe_id,
        "recipe_revision": old.recipe.recipe_revision,
        "recipe_digest": old.digest,
        "release": "22.0-rc.1",
        "support_tier": "preview",
        "provider": old.recipe.provider.model_dump(mode="json"),
        "image_id": raw["image_id"],
        "operation_id": raw["operation_id"],
        "validation_contract": old.recipe.validation_contract,
        "preview_acknowledgement": {
            "recipe_id": old.recipe.recipe_id,
            "recipe_revision": 1,
            "recipe_digest": old.digest,
            "release": "22.0-rc.1",
            "support_tier": "preview",
            "acknowledgement": "development-only-prerelease",
        },
        "acquisition": fixture["acquisition"],
        "assembly": raw,
    }
    assert ostree_attestation_matches(value, old, raw["image_id"])
    new = load_installation_recipe("piers")
    assert not ostree_attestation_matches(value, new, raw["image_id"])
    value.update(
        recipe_id=new.recipe.recipe_id,
        recipe_revision=2,
        recipe_digest=new.digest,
    )
    value["preview_acknowledgement"] = {
        **value["preview_acknowledgement"],
        "recipe_id": new.recipe.recipe_id,
        "recipe_revision": 2,
        "recipe_digest": new.digest,
    }
    assert not ostree_attestation_matches(value, new, raw["image_id"])
