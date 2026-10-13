"""Inactive OSTree parsing cannot change install selection or APT identities."""

import json
from copy import deepcopy
from importlib import resources

import pytest
from pydantic import ValidationError

from kodi_lab.runtime.recipes import (
    FlathubOstreeProvider,
    InactiveOstreeRecipe,
    RuntimeRecipe,
    load_inactive_ostree_recipe,
    load_reviewed_recipe,
    parse_acquisition_provider,
    parse_runtime_recipe,
    supported_generations,
)


def recipe_data() -> dict[str, object]:
    return load_inactive_ostree_recipe().recipe.model_dump(mode="json")


def test_rc1_definition_is_parseable_but_not_installable() -> None:
    recipe = load_inactive_ostree_recipe().recipe
    assert recipe.kodi_version == "22.0-rc.1"
    assert recipe.support_tier == "preview"
    assert recipe.development_only is True
    assert recipe.installation_status == "inactive"
    assert recipe.acquisition_trigger == "explicit-user-install"
    assert recipe.support_dependencies_status == "unqualified"
    assert recipe.image_assets_status == "unqualified"
    assert recipe.validation_status == "unqualified"
    assert isinstance(
        parse_runtime_recipe(recipe.model_dump_json().encode()), InactiveOstreeRecipe
    )
    assert isinstance(
        parse_acquisition_provider(recipe.provider.model_dump()), FlathubOstreeProvider
    )
    assert supported_generations() == ("matrix", "nexus", "omega")
    with pytest.raises(ValueError, match="no reviewed"):
        load_reviewed_recipe("piers")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("platform", "linux/arm64"),
        ("kodi_version", "22.0-rc.2"),
        ("kodi_version", "22.0"),
        ("kodi_source_revision", "a" * 40),
        ("support_tier", "current"),
        ("development_only", False),
        ("installation_status", "ready"),
        ("validation_status", "passed"),
        ("acquisition_trigger", "automatic"),
        ("dockerfile", "FROM attacker"),
        ("checkout_path", "/home/kodi/.kodi"),
    ],
)
def test_recipe_rejects_changed_scope_or_identity(field: str, value: object) -> None:
    data = recipe_data()
    data[field] = value
    with pytest.raises(ValidationError):
        parse_runtime_recipe(json.dumps(data).encode())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("type", "untrusted"),
        ("architecture", "aarch64"),
        ("primary_key_fingerprint", "A" * 40),
        ("signing_subkey_fingerprint", "B" * 40),
        ("key_sha256", "c" * 64),
        ("verification_policy", "unsigned"),
        ("download_url", "https://attacker.invalid"),
        ("command", "touch /tmp/attacker"),
    ],
)
def test_provider_rejects_unreviewed_configuration(field: str, value: object) -> None:
    provider = load_inactive_ostree_recipe().recipe.provider.model_dump(mode="json")
    provider[field] = value
    with pytest.raises(ValidationError):
        parse_acquisition_provider(provider)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("commit", "0" * 63),
        ("commit", "0" * 65),
        ("commit", "G" * 64),
        ("commit", "a" * 64),
        ("commit", "0; touch /tmp/attacker"),
        ("ref", "app/tv.kodi.Kodi/x86_64/stable"),
        ("ref", "app/tv.kodi.Kodi/aarch64/beta"),
        ("repository_url", "https://attacker.invalid/"),
        ("collection", "org.attacker"),
        ("metadata_sha256", "b" * 64),
    ],
)
def test_component_requires_exact_reviewed_pin(field: str, value: object) -> None:
    provider = load_inactive_ostree_recipe().recipe.provider.model_dump(mode="json")
    provider["components"][0][field] = value
    with pytest.raises(ValidationError):
        parse_acquisition_provider(provider)


@pytest.mark.parametrize("change", ["missing", "additional", "duplicate", "reordered"])
def test_component_closure_is_exact(change: str) -> None:
    provider = load_inactive_ostree_recipe().recipe.provider.model_dump(mode="json")
    components = deepcopy(provider["components"])
    if change == "missing":
        components.pop()
    elif change == "additional":
        components.append(deepcopy(components[0]))
    elif change == "duplicate":
        components[2] = deepcopy(components[0])
    else:
        components.reverse()
    provider["components"] = components
    with pytest.raises(ValidationError):
        parse_acquisition_provider(provider)


@pytest.mark.parametrize("generation", ["matrix", "nexus", "omega"])
def test_apt_parser_and_serialization_remain_exact(generation: str) -> None:
    reviewed = load_reviewed_recipe(generation)
    original = RuntimeRecipe.model_validate_json(reviewed.raw)
    parsed = parse_runtime_recipe(reviewed.raw)
    assert isinstance(parsed, RuntimeRecipe)
    assert parsed.model_dump_json() == original.model_dump_json()
    assert parsed.provider.model_dump() == original.provider.model_dump()
    assert (
        parse_acquisition_provider(original.provider.model_dump()) == original.provider
    )


def test_inactive_recipe_tamper_does_not_load(monkeypatch: pytest.MonkeyPatch) -> None:
    original = resources.files

    class Changed:
        def read_bytes(self) -> bytes:
            return b"{}"

    class Package:
        def joinpath(self, *parts: str) -> object:
            if parts == ("recipes", "piers-22.0-rc1-r1.json"):
                return Changed()
            return original("kodi_lab.runtime").joinpath(*parts)

    monkeypatch.setattr(resources, "files", lambda _name: Package())
    with pytest.raises(ValueError, match="pinned digest"):
        load_inactive_ostree_recipe()
