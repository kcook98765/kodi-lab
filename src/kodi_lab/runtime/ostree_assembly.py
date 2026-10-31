"""Inactive RC1 assembly from a live acquisition lease and fixed wheel assets.

There is no install registry, broker selector, database write, attestation signing,
or eligibility transition here. The legacy signed-APT assembler is unchanged.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import tarfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from importlib import resources
from typing import Any
from uuid import UUID

import docker  # type: ignore[import-untyped]

from kodi_lab.runtime.ostree import AcquiredOstree
from kodi_lab.runtime.ostree_context import verified_context
from kodi_lab.runtime.ubuntu_support import (
    BASE_IMAGE,
    download_support,
    load_support_closure,
    validate_support_entries,
)

ASSETS_SHA256 = "397ced6ae67887b97b0c80fe35c0ace858d0f2722907ed5c31228fa68b7e6589"
HISTORICAL_ASSETS_SHA256 = (
    "267deb5e56837d9190ba02e7a00a98b48dfac7e8e035d608c6b6990e85d01b0b"
)
_ASSET_NAMES = frozenset(
    (
        "Dockerfile",
        "launcher.sh",
        "xrandr.sh",
        "supervisor.py",
        "profile_init.py",
        "healthcheck.py",
        "xvfb_guard.py",
        "guisettings.xml",
    )
)
_SHARED_PYTHON_ASSETS = frozenset(
    {"healthcheck.py", "profile_init.py", "supervisor.py", "xvfb_guard.py"}
)
DISPLAY_ASSETS_SHA256 = (
    "6d8e034d94137b7b060c82c9cd131f5149f1ee22dfa5398fe87e46dfb426d873"
)
_MAX_LOG = 2 * 1024**2
_MAX_BUILD_SECONDS = 900
_IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}\Z")


class OstreeAssemblyError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class OstreeAssemblyEvidence:
    image_id: str
    image_size: int
    operation_id: str
    recipe_digest: str
    acquisition_digest: str
    supporting_closure_digest: str
    assembly_assets_digest: str
    asset_hashes: tuple[tuple[str, str], ...]
    context_digest: str
    context_bytes: int
    input_components: tuple[tuple[str, str, str, str], ...]
    build_log_digest: str
    build_log_bytes: int
    installed_inventory_digest: str
    recipe_revision: int = 2

    def canonical_bytes(self) -> bytes:
        from dataclasses import asdict

        if type(self.recipe_revision) is not int or self.recipe_revision not in (2, 3):
            raise OstreeAssemblyError("unsupported_assembly_revision")
        data = {
            "schema_version": "flathub-ostree-assembly-v1",
            "recipe_id": f"piers-22.0-rc1-r{self.recipe_revision}",
            "recipe_revision": self.recipe_revision,
            "kodi_release": "22.0-rc.1",
            "kodi_source_revision": "28ea2eac1eb7af8fdcbd2672d933ce87f594be79",
            "platform": "linux/amd64",
            "installation_status": "inactive",
            "production_approval": False,
            "runtime_validation": "separate-evidence-required",
            "base_image": BASE_IMAGE,
            "native_addon_path": (
                "/opt/fdo/lib/x86_64-linux-gnu/addons -> /app/lib/kodi/addons"
            ),
            "taglib": "stock 2.3.1; Kodi Matroska fast-scan patch absent",
            **asdict(self),
        }
        encoded = json.dumps(
            data, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
        if len(encoded) > 16384:
            raise OstreeAssemblyError("assembly_evidence_exceeded")
        return encoded


def assembly_assets_digest(*, revision: int = 2) -> str:
    if type(revision) is not int or revision not in (1, 2, 3):
        raise OstreeAssemblyError("unsupported_assembly_revision")
    return {1: HISTORICAL_ASSETS_SHA256, 2: ASSETS_SHA256, 3: DISPLAY_ASSETS_SHA256}[
        revision
    ]


def load_assembly_assets(*, revision: int = 2) -> dict[str, bytes]:
    expected = assembly_assets_digest(revision=revision)
    directory = f"assembly_assets/piers-22.0-rc1-r{revision}"
    root = resources.files("kodi_lab.runtime").joinpath(directory)
    raw = root.joinpath("assembly-assets.json").read_bytes()
    if len(raw) > 8192 or hashlib.sha256(raw).hexdigest() != expected:
        raise OstreeAssemblyError("assembly_manifest_mismatch")
    manifest = json.loads(raw)
    if set(manifest) != _ASSET_NAMES | (
        {"novnc_view_helper.py"} if revision == 3 else set()
    ):
        raise OstreeAssemblyError("unexpected_assembly_asset")
    entries = {}
    for name, expected in manifest.items():
        # Both support revisions execute these exact unchanged frozen helpers.
        # Keep one canonical Python resource so source checks see each module once.
        source = (
            resources.files("kodi_lab.runtime").joinpath(
                "assembly_assets/piers-22.0-rc1-r1"
            )
            if revision in (2, 3)
            and name in _SHARED_PYTHON_ASSETS
            and not (revision == 3 and name == "supervisor.py")
            else root
        )
        payload = source.joinpath(name).read_bytes()
        if len(payload) > 64 * 1024 or hashlib.sha256(payload).hexdigest() != expected:
            raise OstreeAssemblyError("assembly_asset_mismatch")
        entries[name] = payload
    return entries


def _entries(
    acquired: AcquiredOstree,
    operation_id: UUID,
    support_entries: dict[str, bytes] | None = None,
    *,
    revision: int = 2,
) -> dict[str, bytes]:
    assets = load_assembly_assets(revision=revision)
    closure = load_support_closure(revision=revision)
    support = (
        download_support(acquired, operation_id, revision=revision)
        if support_entries is None
        else support_entries
    )
    validate_support_entries(support, revision=revision)
    tokens = {
        "@@OPERATION@@": operation_id.hex,
        "@@RECIPE_DIGEST@@": acquired.evidence.recipe_digest,
        "@@ACQUISITION_DIGEST@@": "sha256:"
        + hashlib.sha256(acquired.evidence.canonical_bytes()).hexdigest(),
        "@@SUPPORT_DIGEST@@": closure.digest,
        "@@ASSET_DIGEST@@": "sha256:" + assembly_assets_digest(revision=revision),
    }
    dockerfile = assets.pop("Dockerfile").decode()
    for token, value in tokens.items():
        dockerfile = dockerfile.replace(token, value)
    if "@@" in dockerfile:
        raise OstreeAssemblyError("unresolved_assembly_asset")
    return {
        **support,
        "Dockerfile": dockerfile.encode(),
        **{"assets/" + name: data for name, data in assets.items()},
        "assets/support-manifest.json": closure.raw,
    }


def validate_image_config(
    image: Mapping[str, Any], operation_id: UUID, expected_labels: Mapping[str, str]
) -> None:
    config = image.get("Config")
    if not isinstance(config, dict):
        raise OstreeAssemblyError("invalid_image_config")
    labels = config.get("Labels")
    environment = config.get("Env")
    if (
        not isinstance(image.get("Id"), str)
        or not _IMAGE_ID.fullmatch(image["Id"])
        or image.get("Os") != "linux"
        or image.get("Architecture") != "amd64"
        or config.get("User") != "1000:1000"
        or config.get("Volumes")
        or config.get("Entrypoint")
        != ["/usr/bin/python3", "/usr/local/lib/kodi-lab/supervisor.py"]
        or config.get("Cmd") not in ([], None)
        or config.get("ExposedPorts")
        or not isinstance(labels, dict)
        or any(labels.get(k) != v for k, v in expected_labels.items())
        or labels.get("io.kodi-lab.operation-id") != operation_id.hex
        or labels.get("io.kodi-lab.experimental") != "true"
        or not isinstance(environment, list)
        or not {
            "HOME=/home/kodi",
            "DISPLAY=:99",
            "XDG_RUNTIME_DIR=/run/kodi",
            "LIBGL_ALWAYS_SOFTWARE=1",
            "MESA_LOADER_DRIVER_OVERRIDE=llvmpipe",
            "GALLIUM_DRIVER=llvmpipe",
            "PULSE_SERVER=none",
        }
        <= set(environment)
        or config.get("Healthcheck", {}).get("Test")
        != ["CMD", "/usr/bin/python3", "-I", "/usr/local/lib/kodi-lab/healthcheck.py"]
    ):
        raise OstreeAssemblyError("final_image_profile_mismatch")


def _image_file(
    client: Any,
    image_id: str,
    path: str,
    operation_id: UUID,
    on_resource: Callable[[str, str], None] | None,
) -> bytes:
    container = client.containers.create(
        image_id,
        entrypoint=["/bin/true"],
        user="1000:1000",
        network_mode="none",
        cap_drop=["ALL"],
        security_opt=["no-new-privileges:true"],
        labels={
            "io.kodi-lab.experimental": "true",
            "io.kodi-lab.assembly-inspection": "true",
            "io.kodi-lab.operation-id": operation_id.hex,
        },
    )
    try:
        if on_resource is not None:
            on_resource("container", container.id)
        chunks, _ = container.get_archive(path)
        payload = bytearray()
        for chunk in chunks:
            payload.extend(chunk)
            if len(payload) > 256 * 1024:
                raise OstreeAssemblyError("image_inventory_exceeded")
        with tarfile.open(fileobj=io.BytesIO(payload)) as archive:
            members = archive.getmembers()
            if (
                len(members) != 1
                or not members[0].isfile()
                or members[0].size > 128 * 1024
            ):
                raise OstreeAssemblyError("unsafe_image_inventory")
            stream = archive.extractfile(members[0])
            if stream is None:
                raise OstreeAssemblyError("missing_image_inventory")
            return stream.read()
    finally:
        container.remove(force=False)


def _validate_inventory(raw: bytes, *, revision: int = 2) -> None:
    inventory = []
    for line in raw.decode("utf-8").splitlines():
        fields = line.split("\t")
        if len(fields) != 3:
            raise OstreeAssemblyError("invalid_image_inventory")
        name, version, architecture = fields
        inventory.append((name.removesuffix(":amd64"), version, architecture))
    if (
        tuple(sorted(inventory))
        != load_support_closure(revision=revision).final_inventory
    ):
        raise OstreeAssemblyError("installed_dependency_closure_mismatch")


def assemble_ostree_image(
    acquired: AcquiredOstree,
    *,
    operation_id: UUID,
    client: Any | None = None,
    on_log: Callable[[bytes], None] | None = None,
    on_resource: Callable[[str, str], None] | None = None,
    support_entries: dict[str, bytes] | None = None,
    support_revision: int = 2,
) -> OstreeAssemblyEvidence:
    """Local, offline Docker assembly only. Acquirer owns staging until return."""
    assets_digest = assembly_assets_digest(revision=support_revision)
    acquired.claim_assembly(operation_id)
    owned = client is None
    client = client or docker.from_env(timeout=600)
    tag = "kodi-lab-experimental-piers:phase3-" + operation_id.hex
    log = hashlib.sha256()
    log_bytes = 0
    stream = None
    succeeded = False
    build_started = False
    try:
        try:
            client.api.inspect_image(tag)
        except docker.errors.ImageNotFound:
            pass
        else:
            raise OstreeAssemblyError("assembly_operation_already_exists")
        client.images.pull(BASE_IMAGE, platform="linux/amd64")
        base = client.api.inspect_image(BASE_IMAGE)
        if (
            base.get("Os") != "linux"
            or base.get("Architecture") != "amd64"
            or not any(
                v.endswith("@" + BASE_IMAGE.split("@")[1])
                for v in base.get("RepoDigests", [])
            )
        ):
            raise OstreeAssemblyError("wrong_base_image")
        with verified_context(
            acquired,
            operation_id,
            entries=_entries(
                acquired, operation_id, support_entries, revision=support_revision
            ),
        ) as context:
            context.assert_sealed(operation_id)
            deadline = time.monotonic() + _MAX_BUILD_SECONDS
            build_started = True
            stream = client.api.build(
                fileobj=context.stream,
                custom_context=True,
                tag=tag,
                rm=True,
                forcerm=True,
                pull=False,
                nocache=True,
                decode=True,
                platform="linux/amd64",
                network_mode="none",
            )
            for event in stream:
                if time.monotonic() > deadline:
                    raise OstreeAssemblyError("assembly_timeout")
                if not isinstance(event, dict) or "error" in event:
                    raise OstreeAssemblyError("image_assembly_failed")
                chunk = event.get("stream", "")
                if not isinstance(chunk, str):
                    raise OstreeAssemblyError("invalid_build_response")
                payload = chunk.encode()
                log_bytes += len(payload)
                if log_bytes > _MAX_LOG:
                    raise OstreeAssemblyError("build_output_exceeded")
                log.update(payload)
                if on_log is not None:
                    on_log(payload)
            inspected = client.api.inspect_image(tag)
            if on_resource is not None:
                on_resource("image", inspected["Id"])
            labels = {
                "io.kodi-lab.recipe-digest": acquired.evidence.recipe_digest,
                "io.kodi-lab.acquisition-digest": context.acquisition_digest,
                "io.kodi-lab.support-digest": load_support_closure(
                    revision=support_revision
                ).digest,
                "io.kodi-lab.assets-digest": "sha256:" + assets_digest,
                "io.kodi-lab.installation-status": "inactive",
                "org.opencontainers.image.revision": (
                    acquired.evidence.kodi_source_revision
                ),
                "org.opencontainers.image.version": "22.0-rc.1",
            }
            validate_image_config(inspected, operation_id, labels)
            inventory = _image_file(
                client,
                inspected["Id"],
                "/usr/share/kodi-lab/installed-packages.tsv",
                operation_id,
                on_resource,
            )
            _validate_inventory(inventory, revision=support_revision)
            result = OstreeAssemblyEvidence(
                image_id=inspected["Id"],
                image_size=inspected["Size"],
                operation_id=operation_id.hex,
                recipe_digest=context.recipe_digest,
                acquisition_digest=context.acquisition_digest,
                supporting_closure_digest=load_support_closure(
                    revision=support_revision
                ).digest,
                assembly_assets_digest="sha256:" + assets_digest,
                asset_hashes=tuple(
                    sorted(
                        (k, hashlib.sha256(v).hexdigest())
                        for k, v in load_assembly_assets(
                            revision=support_revision
                        ).items()
                    )
                ),
                context_digest=context.digest,
                context_bytes=context.size,
                input_components=tuple(
                    (x.role, x.ref, x.commit, x.inventory_sha256)
                    for x in acquired.evidence.components
                ),
                build_log_digest="sha256:" + log.hexdigest(),
                build_log_bytes=log_bytes,
                recipe_revision=support_revision,
                installed_inventory_digest="sha256:"
                + hashlib.sha256(inventory).hexdigest(),
            )
            result.canonical_bytes()
            succeeded = True
            return result
    except OstreeAssemblyError:
        raise
    except Exception as error:
        raise OstreeAssemblyError("assembly_failed_closed") from error
    finally:
        try:
            if stream is not None and hasattr(stream, "close"):
                stream.close()
            if build_started and not succeeded:
                # Only this operation's uniquely tagged image, if created here.
                try:
                    image = client.api.inspect_image(tag)
                except docker.errors.ImageNotFound:
                    pass
                else:
                    if (
                        image.get("Config", {})
                        .get("Labels", {})
                        .get("io.kodi-lab.operation-id")
                        == operation_id.hex
                    ):
                        client.images.remove(tag, force=False)
        except Exception as error:
            raise OstreeAssemblyError("assembly_cleanup_failed") from error
        finally:
            if owned:
                client.close()
