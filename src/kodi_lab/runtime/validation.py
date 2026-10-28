"""Fixed two-cycle validation of a locally assembled Kodi runtime image."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import docker  # type: ignore[import-untyped]

from kodi_lab.broker.screenshots import (
    _EXPERIMENTAL_X11_CAPTURE_PROGRAM,
    FIXED_SCREENSHOT_COMMAND,
)
from kodi_lab.config import Settings
from kodi_lab.kodi.screenshots import png_pixel_statistics, x11_frame_to_png
from kodi_lab.runtime.recipes import ReviewedRecipe
from kodi_lab.runtime.temporary_networks import (
    TemporaryNetwork,
    TemporaryNetworkError,
    create_temporary_network,
    remove_exact_temporary_network,
)

_RPC_PROGRAM = """
import json, sys, urllib.request
method = sys.argv[1]
params = json.loads(sys.argv[2])
body = json.dumps({'jsonrpc': '2.0', 'id': 'kodi-lab-recipe-validation',
                   'method': method, 'params': params}).encode()
request = urllib.request.Request('http://127.0.0.1:8080/jsonrpc', body,
                                 {'Content-Type': 'application/json'})
with urllib.request.urlopen(request, timeout=3) as response:
    payload = response.read(65537)
if len(payload) > 65536:
    raise SystemExit(21)
sys.stdout.buffer.write(payload)
""".strip()


class RecipeValidationError(RuntimeError):
    """The exact assembled image did not satisfy the runtime contract."""


@dataclass(frozen=True, slots=True)
class ValidationResult:
    started_at: datetime
    completed_at: datetime
    report: dict[str, object]


def _exec(
    container: Any, command: list[str] | tuple[str, ...], *, limit: int = 65536
) -> bytes:
    result = container.exec_run(list(command), stdout=True, stderr=False)
    output = result.output
    if result.exit_code != 0 or not isinstance(output, bytes) or len(output) > limit:
        raise RecipeValidationError("reviewed runtime validation command failed")
    return output


def _rpc(
    container: Any, method: str, params: dict[str, object] | None = None
) -> object:
    payload = _exec(
        container,
        (
            "/usr/bin/python3",
            "-I",
            "-c",
            _RPC_PROGRAM,
            method,
            json.dumps(params or {}, separators=(",", ":")),
        ),
    )
    try:
        document = json.loads(payload)
    except (ValueError, UnicodeDecodeError) as error:
        raise RecipeValidationError("reviewed runtime JSON-RPC is invalid") from error
    if (
        not isinstance(document, dict)
        or document.get("jsonrpc") != "2.0"
        or document.get("id") != "kodi-lab-recipe-validation"
        or "error" in document
        or "result" not in document
    ):
        raise RecipeValidationError("reviewed runtime JSON-RPC is invalid")
    return document["result"]


def _readiness(container: Any, *, timeout_seconds: int) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        container.reload()
        state = container.attrs.get("State")
        if not isinstance(state, dict) or state.get("Running") is not True:
            raise RecipeValidationError("reviewed Kodi process exited before readiness")
        health = state.get("Health")
        if isinstance(health, dict) and health.get("Status") == "healthy":
            try:
                if _rpc(container, "JSONRPC.Ping") == "pong":
                    return
            except RecipeValidationError:
                pass
        time.sleep(1)
    raise RecipeValidationError("reviewed Kodi readiness timed out")


def _identity(container: Any, reviewed: ReviewedRecipe) -> None:
    result = _rpc(
        container, "Application.GetProperties", {"properties": ["name", "version"]}
    )
    if not isinstance(result, dict):
        raise RecipeValidationError("reviewed Kodi identity is invalid")
    version = result.get("version")
    expected_major, expected_minor = (
        int(part) for part in reviewed.recipe.kodi_version.split(".")
    )
    if (
        result.get("name") != "Kodi"
        or not isinstance(version, dict)
        or version.get("major") != expected_major
        or version.get("minor") != expected_minor
        or version.get("tag") != "stable"
    ):
        raise RecipeValidationError("reviewed Kodi version differs")
    if reviewed.recipe.kodi_version.encode() not in _exec(
        container, ("/usr/bin/kodi", "--version"), limit=4096
    ):
        raise RecipeValidationError("reviewed Kodi binary version differs")


def _capture(container: Any, *, experimental: bool = False) -> dict[str, object]:
    command = (
        ("/usr/bin/python3", "-I", "-c", _EXPERIMENTAL_X11_CAPTURE_PROGRAM)
        if experimental
        else FIXED_SCREENSHOT_COMMAND
    )
    raw = _exec(container, command, limit=8 * 1024 * 1024)
    png = x11_frame_to_png(raw)
    stats = png_pixel_statistics(png)
    if not stats.has_pixel_variation:
        raise RecipeValidationError("reviewed GUI screenshot has no visual content")
    return {
        "width": 1280,
        "height": 720,
        "png_sha256": "sha256:" + hashlib.sha256(png).hexdigest(),
        "png_bytes": len(png),
        "software_renderer": "llvmpipe",
    }


def validate_reviewed_image(
    reviewed: ReviewedRecipe,
    *,
    image_id: str,
    operation_id: UUID,
    client: Any | None = None,
    settings: Settings | None = None,
) -> ValidationResult:
    """Require exact identity, non-root GUI, screenshot, RPC, and two cycles."""

    owned_client = client is None
    client = client or docker.from_env(timeout=15)
    started = datetime.now(UTC)
    container: Any | None = None
    network: TemporaryNetwork | None = None
    cycles: list[dict[str, object]] = []
    screenshot: dict[str, object] | None = None
    try:
        network = create_temporary_network(
            client,
            settings or Settings(),
            operation_id=operation_id,
            purpose="validation",
        )
        container = client.containers.create(
            image_id,
            name=f"kodi-lab-recipe-validation-{operation_id.hex}",
            detach=True,
            network=network.name,
            labels={
                "io.kodi-lab.resource": "recipe-validation",
                "io.kodi-lab.recipe-id": reviewed.recipe.recipe_id,
            },
        )
        inspected = client.api.inspect_container(container.id)
        host = inspected.get("HostConfig") if isinstance(inspected, dict) else None
        network_settings = (
            inspected.get("NetworkSettings") if isinstance(inspected, dict) else None
        )
        attached = (
            network_settings.get("Networks")
            if isinstance(network_settings, dict)
            else None
        )
        if (
            not isinstance(inspected, dict)
            or inspected.get("Image") != image_id
            or not isinstance(host, dict)
            or host.get("NetworkMode") not in (network.name, network.id)
            or not isinstance(attached, dict)
            or set(attached) != {network.name}
            or inspected.get("Mounts") != []
        ):
            raise RecipeValidationError("validation container isolation differs")
        for cycle in range(2):
            cycle_started = datetime.now(UTC)
            container.start()
            if cycle == 0:
                attached_now = (
                    client.api.inspect_container(container.id)
                    .get("NetworkSettings", {})
                    .get("Networks", {})
                )
                if (
                    not isinstance(attached_now, dict)
                    or set(attached_now) != {network.name}
                    or attached_now[network.name].get("NetworkID") != network.id
                ):
                    raise RecipeValidationError("validation network attachment differs")
            _readiness(
                container,
                timeout_seconds=reviewed.recipe.execution_profile.readiness_timeout_seconds,
            )
            _identity(container, reviewed)
            if _exec(container, ("/usr/bin/id", "-u"), limit=16).strip() != b"1000":
                raise RecipeValidationError("reviewed runtime is not non-root")
            if _exec(container, ("/usr/bin/id", "-g"), limit=16).strip() != b"1000":
                raise RecipeValidationError("reviewed runtime group differs")
            _exec(container, ("/usr/bin/pgrep", "-x", "Xvfb"), limit=128)
            _exec(container, ("/usr/bin/pgrep", "-x", "kodi.bin"), limit=128)
            renderer = _exec(container, ("/usr/bin/glxinfo", "-B"), limit=8192)
            if b"llvmpipe" not in renderer.lower():
                raise RecipeValidationError("reviewed GUI renderer is not software")
            if (
                _rpc(container, "Input.Home") != "OK"
                or _rpc(container, "Input.Up") != "OK"
            ):
                raise RecipeValidationError("reviewed Kodi navigation failed")
            library = _rpc(
                container, "VideoLibrary.GetMovies", {"properties": ["title"]}
            )
            if not isinstance(library, dict):
                raise RecipeValidationError("reviewed Kodi library query failed")
            if cycle == 0:
                screenshot = _capture(
                    container,
                    experimental=reviewed.recipe.recipe_id
                    == "ubuntu-resolute-omega-21.3"
                    and reviewed.recipe.recipe_revision == 4,
                )
            container.stop(timeout=10)
            container.reload()
            state = container.attrs.get("State")
            if not isinstance(state, dict) or state.get("Running") is not False:
                raise RecipeValidationError("reviewed Kodi did not stop cleanly")
            cycles.append(
                {
                    "cycle": cycle + 1,
                    "started_at": cycle_started.isoformat(),
                    "completed_at": datetime.now(UTC).isoformat(),
                    "readiness": True,
                    "clean_stop": True,
                }
            )
        completed = datetime.now(UTC)
        return ValidationResult(
            started_at=started,
            completed_at=completed,
            report={
                "schema": reviewed.recipe.validation_contract,
                "result": "passed",
                "recipe_digest": reviewed.digest,
                "release": reviewed.recipe.kodi_version,
                "image_id": image_id,
                "network_mode": "isolated_internal_bridge",
                "non_root_uid": 1000,
                "jsonrpc": True,
                "navigation": True,
                "library_query": True,
                "screenshot": screenshot,
                "cycles": cycles,
            },
        )
    except RecipeValidationError:
        raise
    except TemporaryNetworkError as error:
        raise RecipeValidationError("validation network failed") from error
    except Exception as error:
        raise RecipeValidationError("reviewed runtime validation failed") from error
    finally:
        cleanup_failed = False
        if container is not None:
            try:
                container.reload()
                if container.attrs.get("State", {}).get("Running") is True:
                    container.stop(timeout=10)
                container.remove(v=False, force=False)
            except Exception:
                cleanup_failed = True
        if network is not None:
            try:
                remove_exact_temporary_network(client, network)
            except Exception:
                cleanup_failed = True
        if owned_client:
            client.close()
        if cleanup_failed:
            raise RecipeValidationError("validation resource cleanup failed")
