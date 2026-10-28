from pathlib import Path

import pytest

from kodi_lab.broker.docker_inventory import (
    INSTANCE_ID_LABEL_KEY,
    MANAGED_LABEL_KEY,
    MANAGED_LABEL_VALUE,
)
from kodi_lab.broker.provisioning import RUNTIME_REVISION_LABEL_KEY
from kodi_lab.broker.screenshots import (
    FIXED_SCREENSHOT_COMMAND,
    DockerManagedScreenshotCaptureProvider,
    ManagedScreenshotCaptureSpec,
    ScreenshotCaptureError,
    ScreenshotCaptureIdentityError,
)

INSTANCE_ID = "00000000-0000-4000-8000-000000000001"
REVISION_ID = "00000000-0000-4000-8000-000000000002"
CAPTURE_ID = "00000000-0000-4000-8000-000000000003"
CONTAINER_ID = "a" * 64


class FakeDocker:
    def __init__(self, *, exit_code: int = 0, container_id: str = CONTAINER_ID) -> None:
        self.exit_code = exit_code
        self.container_id = container_id
        self.command: list[str] | None = None
        self.closed = False
        self.selection_count = 0
        self.drift_after_capture = False

    def containers(self, **_kwargs: object) -> list[object]:
        self.selection_count += 1
        if self.drift_after_capture and self.selection_count > 1:
            return [{"Id": "b" * 64}]
        return [{"Id": self.container_id}]

    def inspect_container(self, container: str) -> object:
        return {
            "Id": container,
            "Config": {
                "Labels": {
                    MANAGED_LABEL_KEY: MANAGED_LABEL_VALUE,
                    INSTANCE_ID_LABEL_KEY: INSTANCE_ID,
                    RUNTIME_REVISION_LABEL_KEY: REVISION_ID,
                }
            },
            "State": {"Status": "running", "Health": {"Status": "healthy"}},
        }

    def exec_create(
        self,
        _container: str,
        cmd: list[str],
        *,
        stdout: bool,
        stderr: bool,
    ) -> object:
        assert stdout is True and stderr is False
        self.command = cmd
        return {"Id": "exec-id"}

    def exec_start(self, _exec_id: str, *, stream: bool) -> object:
        assert stream is True
        return iter((b"fixed-", b"xwd-bytes"))

    def exec_inspect(self, _exec_id: str) -> object:
        return {"Running": False, "ExitCode": self.exit_code}

    def close(self) -> None:
        self.closed = True


def spec(path: Path) -> ManagedScreenshotCaptureSpec:
    return ManagedScreenshotCaptureSpec(
        managed_instance_id=INSTANCE_ID,
        runtime_revision_id=REVISION_ID,
        capture_id=CAPTURE_ID,
        artifact_path=path,
        artifact_owner_uid=path.parent.stat().st_uid,
        artifact_owner_gid=path.parent.stat().st_gid,
    )


def test_provider_runs_only_fixed_xwd_and_writes_private_artifact(
    tmp_path: Path,
) -> None:
    fake = FakeDocker()
    path = tmp_path / "frame.raw"
    result = DockerManagedScreenshotCaptureProvider(
        lambda: fake
    ).capture_managed_kodi_screenshot(
        spec=spec(path), expected_container_id=CONTAINER_ID
    )

    assert fake.command == list(FIXED_SCREENSHOT_COMMAND)
    assert path.read_bytes() == b"fixed-xwd-bytes"
    assert path.stat().st_mode & 0o777 == 0o600
    assert result.raw_byte_size == len(b"fixed-xwd-bytes")
    assert result.backend == "x11_get_image"
    assert fake.closed is True


def test_wrong_container_is_rejected_before_exec(tmp_path: Path) -> None:
    fake = FakeDocker(container_id="b" * 64)
    path = tmp_path / "frame.raw"
    with pytest.raises(ScreenshotCaptureIdentityError):
        DockerManagedScreenshotCaptureProvider(
            lambda: fake
        ).capture_managed_kodi_screenshot(
            spec=spec(path), expected_container_id=CONTAINER_ID
        )
    assert fake.command is None
    assert not path.exists()


def test_backend_failure_removes_partial_staging_artifact(tmp_path: Path) -> None:
    fake = FakeDocker(exit_code=1)
    path = tmp_path / "frame.raw"
    with pytest.raises(ScreenshotCaptureError):
        DockerManagedScreenshotCaptureProvider(
            lambda: fake
        ).capture_managed_kodi_screenshot(
            spec=spec(path), expected_container_id=CONTAINER_ID
        )
    assert not path.exists()


def test_runtime_identity_drift_removes_completed_raw_artifact(tmp_path: Path) -> None:
    fake = FakeDocker()
    fake.drift_after_capture = True
    path = tmp_path / "frame.raw"
    with pytest.raises(ScreenshotCaptureIdentityError):
        DockerManagedScreenshotCaptureProvider(
            lambda: fake
        ).capture_managed_kodi_screenshot(
            spec=spec(path), expected_container_id=CONTAINER_ID
        )
    assert not path.exists()


def test_capture_deadline_removes_partial_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeDocker()
    ticks = iter((0.0, 6.0))
    monkeypatch.setattr(
        "kodi_lab.broker.screenshots.time.monotonic", lambda: next(ticks)
    )
    path = tmp_path / "frame.raw"
    with pytest.raises(ScreenshotCaptureError, match="timed out"):
        DockerManagedScreenshotCaptureProvider(
            lambda: fake
        ).capture_managed_kodi_screenshot(
            spec=spec(path), expected_container_id=CONTAINER_ID
        )
    assert not path.exists()


@pytest.mark.parametrize("forbidden", ["sh", "/tmp", ":0", "jpeg"])
def test_fixed_command_has_no_generic_primitive(forbidden: str) -> None:
    assert forbidden not in FIXED_SCREENSHOT_COMMAND


def test_experimental_capture_requires_exact_pin_and_keeps_normal_command(
    tmp_path: Path,
) -> None:
    class ExperimentalDocker(FakeDocker):
        def inspect_container(self, container: str) -> object:
            result = super().inspect_container(container)
            assert isinstance(result, dict)
            result["Image"] = "sha256:" + "b" * 64
            result["State"]["StartedAt"] = "start-1"
            return result

    fake = ExperimentalDocker()
    from dataclasses import replace

    experimental = replace(
        spec(tmp_path / "experimental.raw"), experimental_image_id="sha256:" + "b" * 64
    )
    provider = DockerManagedScreenshotCaptureProvider(lambda: fake)
    provider.capture_managed_kodi_screenshot(
        spec=experimental, expected_container_id=CONTAINER_ID
    )
    assert (
        fake.command is not None and "(width, height) != (640, 360)" in fake.command[-1]
    )
    assert "(width, height) != (1280, 720)" in FIXED_SCREENSHOT_COMMAND[-1]
    with pytest.raises(ScreenshotCaptureIdentityError):
        provider.capture_managed_kodi_screenshot(
            spec=replace(
                experimental,
                artifact_path=tmp_path / "drift.raw",
                experimental_image_id="sha256:" + "c" * 64,
            ),
            expected_container_id=CONTAINER_ID,
        )


@pytest.mark.parametrize(
    "events,accepted,event_type",
    [(1, True, 34), (16, True, 34), (17, False, 34), (1, False, 2)],
)
def test_experimental_x11_capture_drains_only_bounded_mapping_notifications(
    monkeypatch: pytest.MonkeyPatch, events: int, accepted: bool, event_type: int
) -> None:
    import io
    import socket
    import struct
    import sys
    from types import SimpleNamespace

    from kodi_lab.broker.screenshots import _EXPERIMENTAL_X11_CAPTURE_PROGRAM
    from kodi_lab.kodi.screenshots import inspect_x11_frame

    body = bytearray(112)
    body[20:23] = bytes((1, 1, 0))
    body[32:35] = bytes((24, 32, 32))
    struct.pack_into("<I", body, 40, 1)
    struct.pack_into("<HH", body, 60, 640, 360)
    struct.pack_into("<I", body, 72, 2)
    body[78:80] = bytes((24, 1))
    body[80] = 24
    struct.pack_into("<H", body, 82, 1)
    struct.pack_into("<I", body, 88, 2)
    body[92:94] = bytes((4, 8))
    struct.pack_into("<III", body, 96, 0xFF0000, 0xFF00, 0xFF)
    reply = bytearray(32)
    struct.pack_into("<BBHII", reply, 0, 1, 24, 1, 640 * 360, 2)
    pixel = bytes((1, 2, 3, 0))
    wire = (
        struct.pack("<BBHHH", 1, 0, 11, 0, len(body) // 4)
        + body
        + bytes((34,))
        + bytes(31)
    )
    wire = (
        wire[:-32]
        + (bytes((event_type,)) + bytes(31)) * events
        + reply
        + pixel * (640 * 360)
    )

    class CaptureSocket:
        def __init__(self) -> None:
            self.remaining = bytearray(wire)

        def settimeout(self, value: float) -> None:
            pass

        def connect(self, path: str) -> None:
            assert path == "/tmp/.X11-unix/X99"

        def sendall(self, data: bytes) -> None:
            pass

        def recv(self, size: int) -> bytes:
            part = bytes(self.remaining[:size])
            del self.remaining[:size]
            return part

    fake = CaptureSocket()
    output = io.BytesIO()
    with monkeypatch.context() as patch:
        patch.setattr(socket, "socket", lambda *args: fake)
        patch.setattr(sys, "stdout", SimpleNamespace(buffer=output))
        if accepted:
            exec(_EXPERIMENTAL_X11_CAPTURE_PROGRAM, {})
        else:
            with pytest.raises(SystemExit) as failure:
                exec(_EXPERIMENTAL_X11_CAPTURE_PROGRAM, {})
            assert failure.value.code == 25
    if accepted:
        layout = inspect_x11_frame(output.getvalue())
        assert (layout.width, layout.height) == (1280, 720)
        assert output.getvalue()[56:64] == pixel * 2
    else:
        assert not output.getvalue()
