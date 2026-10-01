"""Controlled local image assembly from one wheel-owned signed-package recipe."""

from __future__ import annotations

import hashlib
import io
import re
import shlex
import tarfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import docker  # type: ignore[import-untyped]

from kodi_lab.config import Settings
from kodi_lab.runtime.recipes import ReviewedRecipe
from kodi_lab.runtime.temporary_networks import (
    TemporaryNetwork,
    TemporaryNetworkError,
    create_temporary_network,
    remove_exact_temporary_network,
)

_IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}\Z")
_MAX_BUILD_EVENT_BYTES = 2 * 1024 * 1024
_MAX_INVENTORY_BYTES = 64 * 1024


class RecipeAssemblyError(RuntimeError):
    """A reviewed package or Docker image invariant failed closed."""


@dataclass(frozen=True, slots=True)
class AssemblyResult:
    image_id: str
    image_size: int
    elapsed_seconds: float
    build_log_digest: str
    build_log_bytes: int
    apt_index_download: str | None
    apt_package_download: str | None
    package_inventory: tuple[tuple[str, str, str], ...]
    apt_metadata_digests: tuple[tuple[str, str], ...]


def _apt_sources(reviewed: ReviewedRecipe) -> str:
    provider = reviewed.recipe.provider
    options = f"[arch=amd64 signed-by={provider.keyring_path}]"
    components = " ".join(provider.components)
    lines = []
    for pocket in provider.pockets:
        url = (
            provider.security_repository_url
            if pocket.endswith("-security")
            else provider.repository_url
        )
        lines.append(f"deb {options} {url} {pocket} {components}\n")
    return "".join(lines)


def _dockerfile(reviewed: ReviewedRecipe) -> str:
    recipe = reviewed.recipe
    packages = [
        f"{item.name}={item.version}" for item in recipe.critical_packages
    ] + list(recipe.support_packages)
    package_args = shlex.join(packages)
    checks: list[str] = []
    for item in recipe.critical_packages:
        archive = f"/var/cache/apt/archives/{item.name}_*_{item.architecture}.deb"
        checks.extend(
            (
                f"test \"$(sha256sum {archive} | cut -d' ' -f1)\" = "
                f"{shlex.quote(item.sha256.removeprefix('sha256:'))}",
                "test \"$(dpkg-query -W -f='${Version}' "
                f'{shlex.quote(item.name)})" = {shlex.quote(item.version)}',
                "test \"$(dpkg-query -W -f='${Architecture}' "
                f'{shlex.quote(item.name)})" = {shlex.quote(item.architecture)}',
            )
        )
    # The package archives are verified before installation too. Post-install
    # version and architecture checks below bind the installed state.
    prechecks = [check for index, check in enumerate(checks) if index % 3 == 0]
    postchecks = [check for index, check in enumerate(checks) if index % 3 != 0]
    commands = [
        "set -eu",
        (
            'test "$(sha256sum /usr/share/keyrings/'
            "ubuntu-archive-keyring.gpg | cut -d' ' -f1)\" = "
            + shlex.quote(recipe.provider.keyring_sha256.removeprefix("sha256:"))
        ),
        "rm -f /etc/apt/sources.list.d/*",
        "apt-get update -o Acquire::Languages=none",
        "apt-get install -y --download-only --no-install-recommends " + package_args,
        *prechecks,
        "apt-get install -y --no-install-recommends " + package_args,
        *postchecks,
        "kodi --version | grep -F " + shlex.quote(recipe.kodi_version),
        "install -d -m 0755 /usr/share/kodi-lab",
        (
            "dpkg-query -W -f='${binary:Package}\\t${Version}\\t"
            "${Architecture}\\n' | sort > "
            "/usr/share/kodi-lab/installed-packages.tsv"
        ),
        (
            "sha256sum /var/lib/apt/lists/*_InRelease > "
            "/usr/share/kodi-lab/apt-metadata.sha256"
        ),
        "rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*.deb",
    ]
    joined = " \\\n    && ".join(commands)
    display_guard_copy = (
        "COPY --chown=kodi:kodi xvfb_guard.py /usr/local/bin/kodi-lab-xvfb-guard\n"
        if "xvfb_guard.py" in reviewed.assets
        else ""
    )
    return (
        f"FROM {recipe.base_image_reference}\n"
        "ENV DEBIAN_FRONTEND=noninteractive\n"
        f"LABEL io.kodi-lab.recipe-id={recipe.recipe_id} "
        f"io.kodi-lab.recipe-revision={recipe.recipe_revision} "
        f"io.kodi-lab.recipe-digest={reviewed.digest}\n"
        "COPY apt-sources.list /etc/apt/sources.list\n"
        f"RUN {joined}\n"
        "RUN if getent passwd 1000 >/dev/null; then \\\n"
        '        test "$(getent passwd 1000)" = '
        "'ubuntu:x:1000:1000:Ubuntu:/home/ubuntu:/bin/bash' \\\n"
        "        && test \"$(getent group 1000)\" = 'ubuntu:x:1000:' \\\n"
        "        && usermod -l kodi -d /home/kodi -m ubuntu \\\n"
        "        && groupmod -n kodi ubuntu \\\n"
        "        && usermod -G '' kodi; \\\n"
        "    else groupadd --gid 1000 kodi \\\n"
        "        && useradd --uid 1000 --gid 1000 --create-home "
        "--shell /bin/sh kodi; fi \\\n"
        '    && test "$(id -G kodi)" = 1000 \\\n'
        "    && install -d -o kodi -g kodi /home/kodi/.kodi "
        "/home/kodi/.kodi/userdata /run/kodi \\\n"
        "    && install -d -m 1777 -o root -g root /tmp/.X11-unix\n"
        "COPY --chown=kodi:kodi entrypoint.sh /usr/local/bin/kodi-lab-entrypoint\n"
        f"{display_guard_copy}"
        "COPY --chown=kodi:kodi healthcheck.sh "
        "/usr/local/bin/kodi-lab-kodi-healthcheck\n"
        "COPY --chown=kodi:kodi profile/guisettings.xml "
        "/home/kodi/.kodi/userdata/guisettings.xml\n"
        "RUN chmod 0755 /usr/local/bin/kodi-lab-entrypoint "
        "/usr/local/bin/kodi-lab-kodi-healthcheck\n"
        "USER kodi\n"
        "ENV HOME=/home/kodi DISPLAY=:99 XDG_RUNTIME_DIR=/run/kodi "
        "LIBGL_ALWAYS_SOFTWARE=1 MESA_LOADER_DRIVER_OVERRIDE=llvmpipe "
        "GALLIUM_DRIVER=llvmpipe PULSE_SERVER=none\n"
        "HEALTHCHECK --interval=1s --timeout=2s --start-period=10s --retries=3 "
        'CMD ["/usr/local/bin/kodi-lab-kodi-healthcheck"]\n'
        'ENTRYPOINT ["/usr/local/bin/kodi-lab-entrypoint"]\n'
    )


def build_context(reviewed: ReviewedRecipe) -> bytes:
    """Create a tiny exact file allowlist; no caller path enters Docker build."""

    entries = {
        "Dockerfile": _dockerfile(reviewed).encode(),
        "apt-sources.list": _apt_sources(reviewed).encode(),
        **reviewed.assets,
    }
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for name, payload in sorted(entries.items()):
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            info.mode = 0o755 if name.endswith(".sh") else 0o644
            info.mtime = 0
            archive.addfile(info, io.BytesIO(payload))
    return output.getvalue()


def _inspect_image(
    client: Any, image: str, reviewed: ReviewedRecipe
) -> Mapping[str, Any]:
    inspected = client.api.inspect_image(image)
    if not isinstance(inspected, dict):
        raise RecipeAssemblyError("Docker image inspection is invalid")
    image_id = inspected.get("Id")
    config = inspected.get("Config")
    labels = config.get("Labels") if isinstance(config, dict) else None
    if (
        not isinstance(image_id, str)
        or _IMAGE_ID.fullmatch(image_id) is None
        or inspected.get("Os") != "linux"
        or inspected.get("Architecture") != "amd64"
        or not isinstance(labels, dict)
        or labels.get("io.kodi-lab.recipe-id") != reviewed.recipe.recipe_id
        or labels.get("io.kodi-lab.recipe-revision")
        != str(reviewed.recipe.recipe_revision)
        or labels.get("io.kodi-lab.recipe-digest") != reviewed.digest
        or not isinstance(config, dict)
        or config.get("User") != "kodi"
        or not isinstance(inspected.get("Size"), int)
    ):
        raise RecipeAssemblyError("assembled image identity or configuration differs")
    return inspected


def _inventory(client: Any, image_id: str, path: str) -> bytes:
    # This exact read-only command runs only in the trusted installation worker.
    # It is never registered as a broker RPC or made caller configurable.
    output = client.containers.run(
        image_id,
        path,
        entrypoint="/bin/cat",
        network_mode="none",
        remove=True,
        stdout=True,
        stderr=False,
        labels={"io.kodi-lab.resource": "recipe-inspection"},
    )
    if not isinstance(output, bytes) or not 1 <= len(output) <= _MAX_INVENTORY_BYTES:
        raise RecipeAssemblyError("assembled package inventory is invalid")
    return output


def _package_inventory(
    payload: bytes, reviewed: ReviewedRecipe
) -> tuple[tuple[str, str, str], ...]:
    try:
        rows = tuple(tuple(line.split("\t")) for line in payload.decode().splitlines())
    except UnicodeDecodeError as error:
        raise RecipeAssemblyError("assembled package inventory is invalid") from error
    if not rows or any(len(row) != 3 for row in rows):
        raise RecipeAssemblyError("assembled package inventory is invalid")
    installed = {row[0]: (row[1], row[2]) for row in rows}
    if len(installed) != len(rows):
        raise RecipeAssemblyError("assembled package inventory has duplicates")
    for item in reviewed.recipe.critical_packages:
        if installed.get(item.name) != (item.version, item.architecture):
            raise RecipeAssemblyError("assembled critical package identity differs")
    return rows  # type: ignore[return-value]


def _metadata_digests(
    payload: bytes, reviewed: ReviewedRecipe
) -> tuple[tuple[str, str], ...]:
    try:
        rows = tuple(line.split("  ", 1) for line in payload.decode().splitlines())
    except UnicodeDecodeError as error:
        raise RecipeAssemblyError("APT metadata inventory is invalid") from error
    if len(rows) != len(reviewed.recipe.provider.pockets) or any(
        len(row) != 2 or re.fullmatch(r"[0-9a-f]{64}", row[0]) is None for row in rows
    ):
        raise RecipeAssemblyError("APT signed metadata inventory is invalid")
    named = tuple((row[1].rsplit("/", 1)[-1], "sha256:" + row[0]) for row in rows)
    for pocket in reviewed.recipe.provider.pockets:
        if sum(name.endswith(f"_dists_{pocket}_InRelease") for name, _ in named) != 1:
            raise RecipeAssemblyError("APT signed metadata pocket identity differs")
    return named


def _apt_download_report(log: str) -> tuple[str | None, str | None]:
    """Keep only APT's bounded, rounded index and package download amounts."""

    amounts = re.findall(r"\bFetched ([0-9]+(?:\.[0-9]+)?) ([kMGT]?B) in ", log)
    if len(amounts) < 2:
        return None, None
    return " ".join(amounts[-2]), " ".join(amounts[-1])


def assemble_reviewed_image(
    reviewed: ReviewedRecipe,
    *,
    operation_id: UUID,
    client: Any | None = None,
    on_base_ready: Callable[[], None] | None = None,
    settings: Settings | None = None,
) -> AssemblyResult:
    """Pull a pinned base, assemble fixed inputs, and attest the exact image."""

    owned_client = client is None
    client = client or docker.from_env(timeout=600)
    tag = (
        f"kodi-lab-reviewed-{reviewed.recipe.generation}:"
        f"r{reviewed.recipe.recipe_revision}-{operation_id.hex}"
    )
    started = time.monotonic()
    log_digest = hashlib.sha256()
    log_size = 0
    log_parts: list[str] = []
    network: TemporaryNetwork | None = None
    try:
        client.ping()
        client.images.pull(
            reviewed.recipe.base_image_reference,
            platform=reviewed.recipe.platform,
        )
        base = client.api.inspect_image(reviewed.recipe.base_image_reference)
        if (
            base.get("Os") != "linux"
            or base.get("Architecture") != "amd64"
            or not any(
                isinstance(value, str)
                and value.endswith("@" + reviewed.recipe.base_image_digest)
                for value in base.get("RepoDigests", [])
            )
        ):
            raise RecipeAssemblyError("reviewed base image identity differs")
        if on_base_ready is not None:
            on_base_ready()
        network = create_temporary_network(
            client,
            settings or Settings(),
            operation_id=operation_id,
            purpose="acquisition",
        )
        stream = client.api.build(
            fileobj=io.BytesIO(build_context(reviewed)),
            custom_context=True,
            tag=tag,
            rm=True,
            forcerm=True,
            pull=False,
            nocache=True,
            decode=True,
            platform=reviewed.recipe.platform,
            network_mode=network.name,
        )
        for event in stream:
            if not isinstance(event, dict):
                raise RecipeAssemblyError("Docker assembly response is invalid")
            if "error" in event:
                raise RecipeAssemblyError(
                    "reviewed package acquisition or assembly failed"
                )
            chunk = event.get("stream", "")
            if not isinstance(chunk, str):
                raise RecipeAssemblyError("Docker assembly output is invalid")
            encoded = chunk.encode("utf-8", errors="replace")
            log_digest.update(encoded)
            log_size += len(encoded)
            if log_size > _MAX_BUILD_EVENT_BYTES:
                raise RecipeAssemblyError("Docker assembly output exceeded its bound")
            log_parts.append(chunk)
        image = _inspect_image(client, tag, reviewed)
        image_id = image["Id"]
        packages = _package_inventory(
            _inventory(client, image_id, "/usr/share/kodi-lab/installed-packages.tsv"),
            reviewed,
        )
        metadata = _metadata_digests(
            _inventory(client, image_id, "/usr/share/kodi-lab/apt-metadata.sha256"),
            reviewed,
        )
        index_download, package_download = _apt_download_report("".join(log_parts))
        return AssemblyResult(
            image_id=image_id,
            image_size=image["Size"],
            elapsed_seconds=time.monotonic() - started,
            build_log_digest="sha256:" + log_digest.hexdigest(),
            build_log_bytes=log_size,
            apt_index_download=index_download,
            apt_package_download=package_download,
            package_inventory=packages,
            apt_metadata_digests=metadata,
        )
    except RecipeAssemblyError:
        raise
    except TemporaryNetworkError as error:
        raise RecipeAssemblyError("acquisition network failed") from error
    except Exception as error:
        raise RecipeAssemblyError(
            "Docker could not assemble the reviewed recipe"
        ) from error
    finally:
        try:
            if network is not None:
                remove_exact_temporary_network(client, network)
        except Exception as error:
            raise RecipeAssemblyError("acquisition network cleanup failed") from error
        finally:
            if owned_client:
                client.close()
