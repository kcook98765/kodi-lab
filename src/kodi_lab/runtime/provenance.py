"""Display the binary source sealed by an existing reviewed runtime recipe."""

from dataclasses import dataclass

from kodi_lab.runtime.recipes import (
    FlathubOstreeProvider,
    MatrixCandidateRecipe,
    ReviewedRecipe,
    load_inactive_ostree_recipe,
)


@dataclass(frozen=True, slots=True)
class RuntimeSource:
    provider: str
    distribution: str
    url: str
    support: str
    package_version: str = ""
    source_revision: str = ""
    source_url: str = ""
    binary_sha256: str = ""
    acquisition_checksum: str = ""
    checksum_label: str = "Kodi binary package SHA-256"


def runtime_source(reviewed: ReviewedRecipe) -> RuntimeSource:
    recipe = reviewed.recipe
    provider = recipe.provider
    if isinstance(provider, FlathubOstreeProvider):
        app = next(
            component for component in provider.components if component.role == "app"
        )
        upstream = load_inactive_ostree_recipe(recipe.kodi_version).recipe
        return RuntimeSource(
            provider="Team Kodi Flatpak",
            distribution=f"Flathub Beta · {app.ref}",
            url=next(url for url in recipe.source_references if "/flathub/" in url),
            support="Preview",
            source_revision=upstream.kodi_source_revision,
            source_url=f"https://github.com/xbmc/xbmc/commit/{upstream.kodi_source_revision}",
            acquisition_checksum=f"sha256:{app.commit}",
            checksum_label="Flathub app OSTree commit",
        )
    binary = next(
        package for package in recipe.critical_packages if package.name == "kodi-bin"
    )
    if isinstance(recipe, MatrixCandidateRecipe):
        ppa, _, source = recipe.source_references
        return RuntimeSource(
            provider="Official Team Kodi binaries",
            distribution=f"Team Kodi PPA for Ubuntu {provider.suite.capitalize()}",
            url=ppa,
            support="Legacy",
            package_version=binary.version,
            source_revision=recipe.kodi_source_revision,
            source_url=source,
            binary_sha256=binary.sha256,
            acquisition_checksum=f"sha256:{recipe.closure_sha256}",
            checksum_label="Package closure SHA-256",
        )
    archive = provider.repository_url.replace("http://", "https://", 1).rstrip("/")
    return RuntimeSource(
        provider=f"{provider.distribution} distribution packages",
        distribution=(
            f"{provider.distribution} {provider.suite.capitalize()} APT archive"
        ),
        url=f"{archive}/dists/{provider.suite}/",
        support="Legacy" if recipe.generation == "matrix" else "Reviewed",
        package_version=binary.version,
        binary_sha256=binary.sha256,
        acquisition_checksum=binary.sha256,
    )
