"""Omega has its own immutable signed-package lineage and fixture."""

import hashlib
import json
from importlib import resources

from kodi_lab.fixtures.package import load_packaged_fixture
from kodi_lab.runtime.assembly import _apt_sources, _dockerfile, build_context
from kodi_lab.runtime.recipes import load_reviewed_recipe


def test_omega_recipe_binds_reviewed_resolute_inputs() -> None:
    reviewed = load_reviewed_recipe("omega")
    recipe = reviewed.recipe
    assert (recipe.recipe_id, recipe.recipe_revision) == (
        "ubuntu-resolute-omega-21.3",
        1,
    )
    assert (recipe.generation, recipe.kodi_version, recipe.platform) == (
        "omega",
        "21.3",
        "linux/amd64",
    )
    assert recipe.base_image_reference == (
        "ubuntu:26.04@sha256:da6fc2be547864451aa253836dd926da33623312df4a9a243e35dc877c378a78"
    )
    assert recipe.provider.pockets == (
        "resolute",
        "resolute-updates",
        "resolute-security",
    )
    assert recipe.provider.keyring_sha256 == (
        "sha256:80a36b0a6de2f69f49d2df75ef473ccde121e9e190b9ea01d20a4f63778d5c31"
    )
    assert {item.name: item.sha256 for item in recipe.critical_packages} == {
        "kodi": "sha256:d7e6d9763765e8c37888ce54519e9aaa208f136303b3e0a55222"
        "e087daa0bf77",
        "kodi-bin": "sha256:04081a6eda59cbb247932d59cf4149cf5ed6dd1f75fe82f3f80c8"
        "eb330004e33",
        "kodi-data": "sha256:2c6ffd487fc83399bc8304401f31bef3779c7744a9e2c1aaf4505"
        "c19c2d08eff",
        "kodi-repository-kodi": "sha256:a53f3a178dd3e2eafed14ef42153fb2335b08915235649"
        "5713995c3c06880927",
    }
    assert all(
        item.version == "2:21.3+dfsg-1ubuntu1" for item in recipe.critical_packages
    )
    assert reviewed.digest == "sha256:" + hashlib.sha256(reviewed.raw).hexdigest()
    sources = _apt_sources(reviewed)
    assert (
        sources.count("signed-by=/usr/share/keyrings/ubuntu-archive-keyring.gpg") == 3
    )
    for pocket in recipe.provider.pockets:
        assert f"{pocket} main universe" in sources
    dockerfile = _dockerfile(reviewed)
    for item in recipe.critical_packages:
        assert f"{item.name}={item.version}" in dockerfile
        assert item.sha256.removeprefix("sha256:") in dockerfile
    assert "USER kodi" in dockerfile
    assert len(build_context(reviewed)) < 100_000


def test_omega_definition_is_distinct_from_private_reference_and_other_recipes() -> (
    None
):
    omega = load_reviewed_recipe("omega")
    private = json.loads(
        resources.files("kodi_lab.runtime")
        .joinpath("catalog", "kodi-21.3.json")
        .read_bytes()
    )
    assert omega.recipe.definition_id != private["catalog"]["definition_id"]
    assert omega.recipe.definition_slug != private["catalog"]["slug"]
    for generation in ("matrix", "nexus"):
        other = load_reviewed_recipe(generation)
        assert omega.recipe.definition_id != other.recipe.definition_id
        assert omega.digest != other.digest


def test_existing_mixed_fixture_matches_omega_release_and_database() -> None:
    fixture = load_packaged_fixture("small-mixed-v2")
    assert str(fixture.definition.id) == "f7bd24c7-bccc-44ad-b286-999979b59d9e"
    assert fixture.definition.compatible_kodi_version == "21.3"
    assert fixture.seed_audit is not None
    assert fixture.seed_audit.schema_version == 131
