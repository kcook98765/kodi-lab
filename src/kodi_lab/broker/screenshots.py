"""Fixed broker-owned Xvfb screenshot capture with private artifact handoff."""

from __future__ import annotations

import os
import stat
import time
from collections.abc import Callable, Iterable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast
from uuid import UUID

import docker  # type: ignore[import-untyped]
from docker.errors import APIError, DockerException  # type: ignore[import-untyped]
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.broker.docker_inventory import (
    INSTANCE_ID_LABEL_KEY,
    MANAGED_LABEL_KEY,
    MANAGED_LABEL_VALUE,
)
from kodi_lab.broker.provisioning import RUNTIME_REVISION_LABEL_KEY
from kodi_lab.config import Settings
from kodi_lab.kodi.screenshots import (
    MAX_RAW_CAPTURE_BYTES,
    is_supported_screenshot_runtime,
    raw_capture_path,
)
from kodi_lab.storage.engine import create_database_engine
from kodi_lab.storage.repositories import (
    ManagedInstanceRepository,
    RuntimeDefinitionRepository,
    RuntimeExecutionProfileRepository,
    RuntimeRevisionRepository,
    TargetRepository,
)

CAPTURE_DEADLINE_SECONDS = 5.0
_FIXED_X11_CAPTURE_PROGRAM = r"""
import socket, struct, sys
def exact(sock, count):
    result = bytearray()
    while len(result) < count:
        chunk = sock.recv(count - len(result))
        if not chunk:
            raise SystemExit(21)
        result.extend(chunk)
    return bytes(result)
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.settimeout(4.0)
s.connect('/tmp/.X11-unix/X99')
s.sendall(struct.pack('<BBHHHHH', ord('l'), 0, 11, 0, 0, 0, 0))
setup = exact(s, 8)
status, _reason, _major, _minor, words = struct.unpack('<BBHHH', setup)
body = exact(s, words * 4)
if status != 1 or len(body) < 40:
    raise SystemExit(22)
vendor_length = struct.unpack_from('<H', body, 16)[0]
root_count, format_count = body[20], body[21]
image_byte_order = body[22]
formats_offset = 32 + ((vendor_length + 3) & ~3)
screen_offset = formats_offset + (format_count * 8)
if root_count < 1 or screen_offset + 40 > len(body):
    raise SystemExit(23)
root = struct.unpack_from('<I', body, screen_offset)[0]
width, height = struct.unpack_from('<HH', body, screen_offset + 20)
root_visual = struct.unpack_from('<I', body, screen_offset + 32)[0]
root_depth = body[screen_offset + 38]
depth_count = body[screen_offset + 39]
if (width, height) != (1280, 720):
    raise SystemExit(24)
bits_per_pixel = scanline_pad = None
for index in range(format_count):
    item = formats_offset + (index * 8)
    if item + 8 > len(body):
        raise SystemExit(23)
    depth, bpp, pad = body[item], body[item + 1], body[item + 2]
    if depth == root_depth:
        bits_per_pixel, scanline_pad = bpp, pad
visual_class = bits_per_rgb = None
red_mask = green_mask = blue_mask = None
depth_offset = screen_offset + 40
for _ in range(depth_count):
    if depth_offset + 8 > len(body):
        raise SystemExit(23)
    depth = body[depth_offset]
    visual_count = struct.unpack_from('<H', body, depth_offset + 2)[0]
    visual_offset = depth_offset + 8
    for index in range(visual_count):
        item = visual_offset + (index * 24)
        if item + 24 > len(body):
            raise SystemExit(23)
        visual_id = struct.unpack_from('<I', body, item)[0]
        if depth == root_depth and visual_id == root_visual:
            visual_class = body[item + 4]
            bits_per_rgb = body[item + 5]
            red_mask, green_mask, blue_mask = struct.unpack_from('<III', body, item + 8)
    depth_offset = visual_offset + (visual_count * 24)
if not (
    root_depth == 24
    and bits_per_pixel == 32
    and image_byte_order in (0, 1)
    and scanline_pad == 32
    and visual_class == 4
    and bits_per_rgb == 8
    and red_mask == 0x00ff0000
    and green_mask == 0x0000ff00
    and blue_mask == 0x000000ff
):
    raise SystemExit(26)
bytes_per_line = (
    (width * bits_per_pixel + scanline_pad - 1) // scanline_pad
) * (scanline_pad // 8)
s.sendall(struct.pack('<BBHIhhHHI', 73, 2, 5, root, 0, 0, width, height, 0xffffffff))
reply = exact(s, 32)
kind, depth, _sequence, length, _visual = struct.unpack_from('<BBHII', reply)
if kind != 1 or depth != root_depth or length * 4 != bytes_per_line * height:
    raise SystemExit(25)
pixels = exact(s, length * 4)
sys.stdout.buffer.write(struct.pack(
    '>8s12I', b'KLSX11V1', width, height, root_depth, bits_per_pixel,
    image_byte_order, scanline_pad, bytes_per_line, visual_class, bits_per_rgb,
    red_mask, green_mask, blue_mask,
))
sys.stdout.buffer.write(pixels)
""".strip()
FIXED_SCREENSHOT_COMMAND = (
    "/usr/bin/python3",
    "-I",
    "-c",
    _FIXED_X11_CAPTURE_PROGRAM,
)


class ScreenshotCaptureStateError(RuntimeError):
    pass


class ScreenshotCaptureUnsupportedError(ScreenshotCaptureStateError):
    pass


class ScreenshotCaptureError(RuntimeError):
    pass


class ScreenshotCaptureIdentityError(ScreenshotCaptureError):
    pass


@dataclass(frozen=True, slots=True)
class ManagedScreenshotCaptureSpec:
    managed_instance_id: str
    runtime_revision_id: str
    capture_id: str
    artifact_path: Path
    artifact_owner_uid: int
    artifact_owner_gid: int


@dataclass(frozen=True, slots=True)
class CapturedManagedScreenshotRecord:
    managed_instance_id: str
    runtime_revision_id: str
    container_id: str
    capture_id: str
    backend: str
    raw_byte_size: int


class ScreenshotCaptureResolver(Protocol):
    def resolve(
        self,
        managed_instance_id: str,
        expected_runtime_revision_id: str,
        capture_id: str,
    ) -> ManagedScreenshotCaptureSpec: ...


class ManagedScreenshotCaptureProvider(Protocol):
    def capture_managed_kodi_screenshot(
        self, *, spec: ManagedScreenshotCaptureSpec, expected_container_id: str
    ) -> CapturedManagedScreenshotRecord: ...


class SqliteScreenshotCaptureResolver:
    def __init__(
        self, session_factory: sessionmaker[Session], *, data_root: Path
    ) -> None:
        self._session_factory = session_factory
        self._data_root = data_root

    @classmethod
    def production(cls) -> SqliteScreenshotCaptureResolver:
        settings = Settings()
        engine = create_database_engine(settings.database_url)
        return cls(
            sessionmaker(engine, expire_on_commit=False), data_root=settings.data_root
        )

    def resolve(
        self,
        managed_instance_id: str,
        expected_runtime_revision_id: str,
        capture_id: str,
    ) -> ManagedScreenshotCaptureSpec:
        try:
            instance_id = UUID(managed_instance_id)
            revision_id = UUID(expected_runtime_revision_id)
            capture_uuid = UUID(capture_id)
            if (
                str(instance_id) != managed_instance_id
                or str(revision_id) != expected_runtime_revision_id
                or str(capture_uuid) != capture_id
            ):
                raise ValueError
        except ValueError as error:
            raise ScreenshotCaptureStateError(
                "Screenshot identities are not canonical."
            ) from error
        try:
            with self._session_factory() as session:
                target = TargetRepository(session).get(instance_id)
                instance = ManagedInstanceRepository(session).get(instance_id)
                if target is None or instance is None:
                    raise ScreenshotCaptureStateError(
                        "Managed screenshot instance does not exist."
                    )
                if instance.runtime_revision_id != revision_id:
                    raise ScreenshotCaptureStateError(
                        "Managed screenshot runtime revision changed."
                    )
                revision = RuntimeRevisionRepository(session).get(revision_id)
                if revision is None:
                    raise ScreenshotCaptureStateError(
                        "Managed screenshot runtime revision is unavailable."
                    )
                definition = RuntimeDefinitionRepository(session).get(
                    revision.runtime_definition_id
                )
                profile = RuntimeExecutionProfileRepository(session).get(revision_id)
                if definition is None or profile is None:
                    raise ScreenshotCaptureStateError(
                        "Managed screenshot runtime profile is unavailable."
                    )
                if not is_supported_screenshot_runtime(
                    runtime_revision_id=str(revision_id),
                    canonical_release=definition.kodi_version,
                    expected_application_name=profile.expected_application_name,
                    expected_kodi_version=profile.expected_kodi_version,
                    expected_version_tag=profile.expected_version_tag,
                    expected_prerelease_ordinal=(profile.expected_prerelease_ordinal),
                ):
                    raise ScreenshotCaptureUnsupportedError(
                        "This runtime revision does not support screenshots."
                    )
            stage = raw_capture_path(self._data_root, capture_uuid).parent
            stage_metadata = stage.lstat()
            data_metadata = self._data_root.lstat()
            if (
                not stat.S_ISDIR(stage_metadata.st_mode)
                or stat.S_ISLNK(stage_metadata.st_mode)
                or stat.S_IMODE(stage_metadata.st_mode) != 0o700
            ):
                raise ScreenshotCaptureStateError(
                    "Screenshot capture staging is unsafe."
                )
            return ManagedScreenshotCaptureSpec(
                managed_instance_id=managed_instance_id,
                runtime_revision_id=expected_runtime_revision_id,
                capture_id=capture_id,
                artifact_path=raw_capture_path(self._data_root, capture_uuid),
                artifact_owner_uid=data_metadata.st_uid,
                artifact_owner_gid=data_metadata.st_gid,
            )
        except ScreenshotCaptureStateError:
            raise
        except (FileNotFoundError, OSError, SQLAlchemyError) as error:
            raise ScreenshotCaptureStateError(
                "Canonical screenshot capture state is unavailable."
            ) from error


class _ScreenshotDockerClient(Protocol):
    def containers(
        self, *, all: bool, filters: Mapping[str, list[str]]
    ) -> list[object]: ...

    def inspect_container(self, container: str) -> object: ...

    def exec_create(
        self,
        container: str,
        cmd: list[str],
        *,
        stdout: bool,
        stderr: bool,
    ) -> object: ...

    def exec_start(self, exec_id: str, *, stream: bool) -> object: ...

    def exec_inspect(self, exec_id: str) -> object: ...

    def close(self) -> None: ...


class DockerManagedScreenshotCaptureProvider:
    def __init__(
        self, client_factory: Callable[[], _ScreenshotDockerClient] | None = None
    ) -> None:
        self._client_factory = client_factory or self._production_client

    @staticmethod
    def _production_client() -> _ScreenshotDockerClient:
        return cast(
            _ScreenshotDockerClient,
            docker.APIClient(version="auto", timeout=CAPTURE_DEADLINE_SECONDS),
        )

    def capture_managed_kodi_screenshot(
        self, *, spec: ManagedScreenshotCaptureSpec, expected_container_id: str
    ) -> CapturedManagedScreenshotRecord:
        client: _ScreenshotDockerClient | None = None
        artifact_created = False
        try:
            client = self._client_factory()
            selected_id = self._select_exact(client, spec, expected_container_id)
            created = client.exec_create(
                selected_id,
                list(FIXED_SCREENSHOT_COMMAND),
                stdout=True,
                stderr=False,
            )
            if not isinstance(created, Mapping) or not isinstance(
                created.get("Id"), str
            ):
                raise ScreenshotCaptureError("Screenshot backend did not start safely.")
            exec_id = cast(str, created["Id"])
            descriptor = os.open(
                spec.artifact_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
            )
            artifact_created = True
            total = 0
            started = time.monotonic()
            try:
                with os.fdopen(descriptor, "wb") as output:
                    stream = client.exec_start(exec_id, stream=True)
                    if not isinstance(stream, Iterable):
                        raise ScreenshotCaptureError(
                            "Screenshot backend returned an invalid stream."
                        )
                    for chunk in stream:
                        if time.monotonic() - started > CAPTURE_DEADLINE_SECONDS:
                            raise ScreenshotCaptureError(
                                "Screenshot capture timed out."
                            )
                        if not isinstance(chunk, bytes) or not chunk:
                            raise ScreenshotCaptureError(
                                "Screenshot backend returned invalid bytes."
                            )
                        total += len(chunk)
                        if total > MAX_RAW_CAPTURE_BYTES:
                            raise ScreenshotCaptureError(
                                "Screenshot capture exceeded its size bound."
                            )
                        output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                inspected_exec = client.exec_inspect(exec_id)
                if (
                    not isinstance(inspected_exec, Mapping)
                    or inspected_exec.get("Running") is not False
                    or inspected_exec.get("ExitCode") != 0
                    or total == 0
                ):
                    raise ScreenshotCaptureError("Screenshot capture did not succeed.")
                os.chown(
                    spec.artifact_path,
                    spec.artifact_owner_uid,
                    spec.artifact_owner_gid,
                    follow_symlinks=False,
                )
                os.chmod(spec.artifact_path, 0o600, follow_symlinks=False)
            except Exception:
                spec.artifact_path.unlink(missing_ok=True)
                artifact_created = False
                raise
            final_id = self._select_exact(client, spec, expected_container_id)
            if final_id != selected_id:
                raise ScreenshotCaptureIdentityError(
                    "Managed runtime identity changed during screenshot capture."
                )
            return CapturedManagedScreenshotRecord(
                managed_instance_id=spec.managed_instance_id,
                runtime_revision_id=spec.runtime_revision_id,
                container_id=selected_id,
                capture_id=spec.capture_id,
                backend="x11_get_image",
                raw_byte_size=total,
            )
        except (ScreenshotCaptureError, ScreenshotCaptureIdentityError):
            if artifact_created:
                spec.artifact_path.unlink(missing_ok=True)
            raise
        except (APIError, DockerException, OSError, TimeoutError) as error:
            if artifact_created:
                spec.artifact_path.unlink(missing_ok=True)
            raise ScreenshotCaptureError("Fixed screenshot capture failed.") from error
        finally:
            if client is not None:
                with suppress(DockerException, OSError):
                    client.close()

    @staticmethod
    def _select_exact(
        client: _ScreenshotDockerClient,
        spec: ManagedScreenshotCaptureSpec,
        expected_container_id: str,
    ) -> str:
        summaries = client.containers(
            all=True,
            filters={
                "label": [
                    f"{MANAGED_LABEL_KEY}={MANAGED_LABEL_VALUE}",
                    f"{INSTANCE_ID_LABEL_KEY}={spec.managed_instance_id}",
                ]
            },
        )
        if not isinstance(summaries, list) or len(summaries) != 1:
            raise ScreenshotCaptureIdentityError(
                "Exact managed screenshot container is unavailable."
            )
        summary = summaries[0]
        if (
            not isinstance(summary, Mapping)
            or summary.get("Id") != expected_container_id
        ):
            raise ScreenshotCaptureIdentityError(
                "Managed screenshot container identity changed."
            )
        inspected = client.inspect_container(expected_container_id)
        if not isinstance(inspected, Mapping):
            raise ScreenshotCaptureIdentityError(
                "Managed screenshot container metadata is invalid."
            )
        config = inspected.get("Config")
        state = inspected.get("State")
        if not isinstance(config, Mapping) or not isinstance(state, Mapping):
            raise ScreenshotCaptureIdentityError(
                "Managed screenshot container metadata is invalid."
            )
        labels = config.get("Labels")
        health = state.get("Health")
        if (
            inspected.get("Id") != expected_container_id
            or not isinstance(labels, Mapping)
            or labels.get(MANAGED_LABEL_KEY) != MANAGED_LABEL_VALUE
            or labels.get(INSTANCE_ID_LABEL_KEY) != spec.managed_instance_id
            or labels.get(RUNTIME_REVISION_LABEL_KEY) != spec.runtime_revision_id
            or state.get("Status") != "running"
            or not isinstance(health, Mapping)
            or health.get("Status") != "healthy"
        ):
            raise ScreenshotCaptureIdentityError(
                "Managed screenshot runtime is not the expected healthy runtime."
            )
        return expected_container_id
