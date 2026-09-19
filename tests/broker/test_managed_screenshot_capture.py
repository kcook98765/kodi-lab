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
