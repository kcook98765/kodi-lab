"""Closed reviewed recipe inputs and fixed package acquisition authority."""

import hashlib
import json
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from kodi_lab.runtime.assembly import (
    RecipeAssemblyError,
    _apt_download_report,
    _apt_sources,
    _dockerfile,
    assemble_reviewed_image,
    build_context,
)
from kodi_lab.runtime.recipes import (
    RuntimeRecipe,
    load_recorded_reviewed_recipe,
    load_reviewed_recipe,
    supported_generations,
)


def test_matrix_recipe_is_exact_reviewed_binary_package_input() -> None:
    reviewed = load_reviewed_recipe("matrix")
    recipe = reviewed.recipe
    assert supported_generations() == ("matrix", "nexus", "omega")
    assert (recipe.generation, recipe.kodi_version, recipe.platform) == (
        "matrix",
        "19.4",
        "linux/amd64",
    )
    assert recipe.provider.type == "signed_distro_apt"
    assert recipe.provider.suite == "jammy"
    assert recipe.base_image_reference.endswith(recipe.base_image_digest)
    assert reviewed.digest == "sha256:" + hashlib.sha256(reviewed.raw).hexdigest()
    assert {item.name for item in recipe.critical_packages} == {
        "kodi",
        "kodi-bin",
        "kodi-data",
        "kodi-repository-kodi",
    }
    assert all(item.version == "2:19.4+dfsg1-2" for item in recipe.critical_packages)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("platform", "linux/arm64"),
        ("kodi_version", "19.5"),
        ("base_image_reference", "ubuntu:22.04"),
        ("support_packages", ["curl; rm -rf /"]),
        ("arbitrary_dockerfile", "FROM attacker"),
    ],
)
def test_recipe_rejects_unreviewed_runtime_inputs(field: str, value: object) -> None:
    data = json.loads(load_reviewed_recipe("matrix").raw)
    data[field] = value
    with pytest.raises(ValidationError):
        RuntimeRecipe.model_validate(data)


def test_assembly_uses_only_reviewed_sources_and_exact_package_checks() -> None:
    reviewed = load_reviewed_recipe("matrix")
    dockerfile = _dockerfile(reviewed)
    sources = _apt_sources(reviewed)
    assert "jammy main universe" in sources
    assert "jammy-updates main universe" in sources
    assert "jammy-security main universe" in sources
    assert "signed-by=/usr/share/keyrings/ubuntu-archive-keyring.gpg" in sources
    assert "apt-get install -y --download-only --no-install-recommends" in dockerfile
    assert "sha256sum /var/cache/apt/archives/kodi_" in dockerfile
    assert "kodi=2:19.4+dfsg1-2" in dockerfile
    assert "USER kodi" in dockerfile
    assert 'ENTRYPOINT ["/usr/local/bin/kodi-lab-entrypoint"]' in dockerfile
    assert "git clone" not in dockerfile
    assert "make install" not in dockerfile
    assert len(build_context(reviewed)) < 100_000


def test_reviewed_asset_tamper_is_detected(monkeypatch: pytest.MonkeyPatch) -> None:
    from importlib import resources

    original = resources.files

    class Changed:
        def read_bytes(self) -> bytes:
            return b"#!/bin/sh\nexit 0\n"

    class AssetDirectory:
        def joinpath(self, *parts: str) -> Any:
            if parts == ("entrypoint.sh",):
                return Changed()
            return original("kodi_lab.runtime").joinpath("assembly_assets", *parts)

    class ChangedAssets:
        def joinpath(self, *parts: str) -> Any:
            if parts == ("assembly_assets",):
                return AssetDirectory()
            return original("kodi_lab.runtime").joinpath(*parts)

    monkeypatch.setattr(resources, "files", lambda _name: ChangedAssets())
    with pytest.raises(ValueError, match="asset differs"):
        load_reviewed_recipe("matrix")


def test_reviewed_recipe_byte_change_requires_new_pinned_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from importlib import resources

    original = resources.files

    class Changed:
        def read_bytes(self) -> bytes:
            return (
                original("kodi_lab.runtime")
                .joinpath("recipes", "matrix-19.4-r1.json")
                .read_bytes()
                + b"\n"
            )

    class ChangedPackage:
        def joinpath(self, *parts: str) -> Any:
            if parts == ("recipes", "matrix-19.4-r1.json"):
                return Changed()
            return original("kodi_lab.runtime").joinpath(*parts)

    monkeypatch.setattr(resources, "files", lambda _name: ChangedPackage())
    with pytest.raises(ValueError, match="pinned digest"):
        load_reviewed_recipe("matrix")


def test_duplicate_reviewed_id_and_revision_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kodi_lab.runtime import recipes

    monkeypatch.setattr(recipes, "_RECIPES", {"matrix": recipes._RECIPES["matrix"] * 2})
    with pytest.raises(ValueError, match="identity is duplicated"):
        load_reviewed_recipe("matrix")


def test_historical_recipe_resolution_requires_exact_reviewed_identity() -> None:
    reviewed = load_reviewed_recipe("matrix")
    assert (
        load_recorded_reviewed_recipe(
            "matrix",
            reviewed.recipe.recipe_id,
            reviewed.recipe.recipe_revision,
            reviewed.digest,
        )
        == reviewed
    )
    with pytest.raises(ValueError, match="no longer reviewed"):
        load_recorded_reviewed_recipe(
            "matrix", reviewed.recipe.recipe_id, 2, reviewed.digest
        )


def test_apt_download_report_excludes_raw_build_output() -> None:
    assert _apt_download_report(
        "Fetched 33.1 MB in 2s\nprivate line\nFetched 142 MB in 10s\n"
    ) == ("33.1 MB", "142 MB")
    assert _apt_download_report("Docker cache hit") == (None, None)


def test_assembly_rejects_wrong_base_digest_before_build() -> None:
    reviewed = load_reviewed_recipe("matrix")

    def forbidden_build(**_kwargs: object) -> None:
        raise AssertionError("an unreviewed base reached assembly")

    client = SimpleNamespace(
        ping=lambda: True,
        images=SimpleNamespace(pull=lambda *_args, **_kwargs: None),
        api=SimpleNamespace(
            inspect_image=lambda _image: {
                "Os": "linux",
                "Architecture": "amd64",
                "RepoDigests": ["ubuntu:22.04@sha256:" + "f" * 64],
            },
            build=forbidden_build,
        ),
    )
    with pytest.raises(RecipeAssemblyError, match="base image identity differs"):
        assemble_reviewed_image(reviewed, operation_id=uuid4(), client=client)


def test_assembly_redacts_package_acquisition_failure() -> None:
    reviewed = load_reviewed_recipe("matrix")
    operation_id = uuid4()
    network_name = f"kodi-lab-recipe-acquisition-{operation_id.hex}"
    removed: list[bool] = []
    build_networks: list[str] = []

    def failed_build(**kwargs: object) -> Any:
        build_networks.append(str(kwargs["network_mode"]))
        return iter([{"error": "private upstream details"}])

    client = SimpleNamespace(
        ping=lambda: True,
        images=SimpleNamespace(pull=lambda *_args, **_kwargs: None),
        networks=SimpleNamespace(
            create=lambda *_args, **_kwargs: SimpleNamespace(
                id="isolated-network", remove=lambda: removed.append(True)
            )
        ),
        api=SimpleNamespace(
            inspect_image=lambda _image: {
                "Os": "linux",
                "Architecture": "amd64",
                "RepoDigests": [reviewed.recipe.base_image_reference],
            },
            inspect_network=lambda _id: {
                "Id": "isolated-network",
                "Name": network_name,
                "Driver": "bridge",
                "Internal": False,
                "Attachable": False,
                "EnableIPv6": False,
                "Labels": {
                    "io.kodi-lab.resource": "recipe-acquisition-network",
                    "io.kodi-lab.operation-id": str(operation_id),
                },
            },
            build=failed_build,
        ),
    )
    with pytest.raises(
        RecipeAssemblyError, match="acquisition or assembly failed"
    ) as error:
        assemble_reviewed_image(reviewed, operation_id=operation_id, client=client)
    assert "private upstream details" not in str(error.value)
    assert build_networks == [network_name]
    assert removed == [True]


def test_assembly_rejects_substituted_acquisition_network_before_build() -> None:
    reviewed = load_reviewed_recipe("matrix")
    removed: list[bool] = []

    def forbidden_build(**_kwargs: object) -> None:
        raise AssertionError("unreviewed network reached Docker build")

    client = SimpleNamespace(
        ping=lambda: True,
        images=SimpleNamespace(pull=lambda *_args, **_kwargs: None),
        networks=SimpleNamespace(
            create=lambda *_args, **_kwargs: SimpleNamespace(
                id="substituted", remove=lambda: removed.append(True)
            )
        ),
        api=SimpleNamespace(
            inspect_image=lambda _image: {
                "Os": "linux",
                "Architecture": "amd64",
                "RepoDigests": [reviewed.recipe.base_image_reference],
            },
            inspect_network=lambda _id: {
                "Id": "substituted",
                "Name": "attacker-network",
                "Driver": "bridge",
                "Internal": False,
                "Attachable": False,
                "EnableIPv6": False,
                "Labels": {},
            },
            build=forbidden_build,
        ),
    )
    with pytest.raises(RecipeAssemblyError, match="network identity differs"):
        assemble_reviewed_image(reviewed, operation_id=uuid4(), client=client)
    assert removed == [True]


def test_new_generation_is_recipe_data_not_catalog_schema() -> None:
    reviewed = load_reviewed_recipe("matrix")
    hypothetical = reviewed.recipe.model_copy(update={"generation": "nexus"})
    assert hypothetical.generation == "nexus"
    assert hypothetical.provider.type == reviewed.recipe.provider.type
