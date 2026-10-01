"""Nexus package identity is a separately pinned reviewed runtime input."""

import hashlib
import json
from importlib import resources
from typing import Any

import pytest

from kodi_lab.runtime.assembly import _apt_sources, _dockerfile
from kodi_lab.runtime.recipes import load_reviewed_recipe


def test_nexus_recipe_binds_signed_noble_packages_and_assets() -> None:
    reviewed = load_reviewed_recipe("nexus")
    recipe = reviewed.recipe
    assert (recipe.recipe_id, recipe.recipe_revision) == (
        "ubuntu-noble-nexus-20.5",
        2,
    )
    assert (recipe.generation, recipe.kodi_version, recipe.platform) == (
        "nexus",
        "20.5",
        "linux/amd64",
    )
    assert recipe.base_image_digest == (
        "sha256:a61567bd31828687156d735ea8eb01ba4e37636e225dd6a48ba94136a70d9d61"
    )
    assert recipe.provider.suite == "noble"
    assert recipe.provider.keyring_sha256 == (
        "sha256:80a36b0a6de2f69f49d2df75ef473ccde121e9e190b9ea01d20a4f63778d5c31"
    )
    assert {item.name: item.sha256 for item in recipe.critical_packages} == {
        "kodi": "sha256:49a33fde1cde10521c7db8746bfe785381a3498a53f162"
        "a2c6b4a863cb007893",
        "kodi-bin": "sha256:8c696a01b924ac83761b9dd02d37e30419c413f0cd7949"
        "1b11924336363eef77",
        "kodi-data": "sha256:e1bb8b75e1a437120511a2e7a8d94edea8e6352290c2d96"
        "b8f8f6e23487a4f1b",
        "kodi-repository-kodi": "sha256:308611c4a23e5efa98036494ebe6ff5e76ad6"
        "a12ef31f92f953be4808890e4e8",
    }
    assert all(
        item.version == "2:20.5+dfsg-1ubuntu1" for item in recipe.critical_packages
    )
    assert reviewed.digest == "sha256:" + hashlib.sha256(reviewed.raw).hexdigest()
    sources = _apt_sources(reviewed)
    for pocket in ("noble", "noble-updates", "noble-security"):
        assert f"{pocket} main universe" in sources
    assert (
        sources.count("signed-by=/usr/share/keyrings/ubuntu-archive-keyring.gpg") == 3
    )
    dockerfile = _dockerfile(reviewed)
    for item in recipe.critical_packages:
        assert f"{item.name}={item.version}" in dockerfile
        assert item.sha256.removeprefix("sha256:") in dockerfile
    assert (
        'test "$(getent passwd 1000)" = '
        "'ubuntu:x:1000:1000:Ubuntu:/home/ubuntu:/bin/bash'"
    ) in dockerfile
    assert "usermod -G '' kodi" in dockerfile
    assert 'test "$(id -G kodi)" = 1000' in dockerfile


def test_nexus_recipe_bytes_are_digest_protected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = resources.files

    class Changed:
        def read_bytes(self) -> bytes:
            return (
                original("kodi_lab.runtime")
                .joinpath("recipes", "nexus-20.5-r1.json")
                .read_bytes()
                + b"\n"
            )

    class ChangedPackage:
        def joinpath(self, *parts: str) -> Any:
            if parts == ("recipes", "nexus-20.5-r1.json"):
                return Changed()
            return original("kodi_lab.runtime").joinpath(*parts)

    monkeypatch.setattr(resources, "files", lambda _name: ChangedPackage())
    with pytest.raises(ValueError, match="pinned digest"):
        load_reviewed_recipe("nexus")


def test_nexus_recipe_identity_is_separate_from_matrix() -> None:
    nexus = load_reviewed_recipe("nexus")
    matrix = load_reviewed_recipe("matrix")
    assert nexus.recipe.definition_id != matrix.recipe.definition_id
    assert nexus.recipe.recipe_id != matrix.recipe.recipe_id
    assert nexus.digest != matrix.digest
    assert json.loads(nexus.raw)["display_generation"] == "Nexus 20"
