from uuid import uuid4

import pytest

from kodi_lab.application.screenshots import (
    ScreenshotCaptureCoordinator,
    ScreenshotServiceError,
)


def test_same_instance_capture_is_excluded() -> None:
    coordinator = ScreenshotCaptureCoordinator(global_limit=4)
    instance_id = uuid4()
    coordinator.acquire(instance_id)
    with pytest.raises(ScreenshotServiceError) as raised:
        coordinator.acquire(instance_id)
    assert raised.value.code == "screenshot_capture_in_progress"
    coordinator.release(instance_id)


def test_global_capture_bound_is_enforced() -> None:
    coordinator = ScreenshotCaptureCoordinator(global_limit=1)
    first = uuid4()
    coordinator.acquire(first)
    with pytest.raises(ScreenshotServiceError) as raised:
        coordinator.acquire(uuid4())
    assert raised.value.code == "visual_rate_limited"
    coordinator.release(first)
