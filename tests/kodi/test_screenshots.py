import struct
from pathlib import Path
from uuid import uuid4

import pytest

from kodi_lab.kodi.screenshots import (
    EXPECTED_HEIGHT,
    EXPECTED_WIDTH,
    MAX_PNG_BYTES,
    PNG_SIGNATURE,
    ScreenshotArtifactError,
    cleanup_capture_stage,
    ppm_to_png,
    prepare_capture_stage,
    publish_png,
    read_published_png,
    remove_instance_screenshot_cache,
    validate_png,
    xwd_to_png,
)


def valid_xwd(*, width: int = EXPECTED_WIDTH, height: int = EXPECTED_HEIGHT) -> bytes:
    header = struct.pack(
        ">25I",
        100,
        7,
        2,
        24,
        width,
        height,
        0,
        0,
        32,
        0,
        32,
        32,
        width * 4,
        4,
        0x00FF0000,
        0x0000FF00,
        0x000000FF,
        8,
        256,
        0,
        width,
        height,
        0,
        0,
        0,
    )
    pixel = bytes((0x33, 0x22, 0x11, 0x00))
    return header + (pixel * width * height)


def test_exact_xvfb_xwd_converts_to_strict_png() -> None:
    png = xwd_to_png(valid_xwd())
    result = validate_png(png)

    assert png.startswith(PNG_SIGNATURE)
    assert result.width == EXPECTED_WIDTH
    assert result.height == EXPECTED_HEIGHT
    assert result.byte_size == len(png)


def test_fixed_x11_rgb_capture_converts_and_rejects_format_confusion() -> None:
    raw = b"P6\n1280 720\n255\n" + (bytes((0x11, 0x22, 0x33)) * 1280 * 720)
    result = validate_png(ppm_to_png(raw))
    assert (result.width, result.height) == (1280, 720)
    with pytest.raises(ScreenshotArtifactError):
        ppm_to_png(b"P3\n1280 720\n255\n" + raw[17:])


@pytest.mark.parametrize(
    "payload",
    [
        b"not-xwd",
        valid_xwd()[:-1],
        valid_xwd(width=0, height=EXPECTED_HEIGHT),
        bytearray(valid_xwd()),
    ],
)
def test_invalid_xwd_is_rejected(payload: bytes | bytearray) -> None:
    if isinstance(payload, bytearray):
        payload[11] = 1  # pixmap format differs from fixed ZPixmap.
    with pytest.raises(ScreenshotArtifactError):
        xwd_to_png(bytes(payload))


def test_png_rejects_wrong_signature_truncation_trailing_and_oversize() -> None:
    png = xwd_to_png(valid_xwd())
    candidates = (
        b"GIF89a" + png[6:],
        png[:-1],
        png + b"unexpected",
        PNG_SIGNATURE + (b"x" * MAX_PNG_BYTES),
    )
    for candidate in candidates:
        with pytest.raises(ScreenshotArtifactError):
            validate_png(candidate)


def test_private_cache_round_trip_disappearance_and_instance_cleanup(
    tmp_path: Path,
) -> None:
    instance_id = uuid4()
    capture_id = uuid4()
    stage_id = uuid4()
    png = xwd_to_png(valid_xwd())

    stage = prepare_capture_stage(tmp_path, stage_id)
    assert stage.stat().st_mode & 0o777 == 0o700
    cleanup_capture_stage(tmp_path, stage_id)
    publish_png(
        data_root=tmp_path,
        instance_id=instance_id,
        capture_id=capture_id,
        png=png,
    )
    assert read_published_png(tmp_path, instance_id, capture_id) == png

    remove_instance_screenshot_cache(tmp_path, instance_id)
    with pytest.raises(ScreenshotArtifactError, match="unavailable"):
        read_published_png(tmp_path, instance_id, capture_id)


def test_instance_cleanup_leaves_sibling_cache_untouched(tmp_path: Path) -> None:
    first = uuid4()
    sibling = uuid4()
    first_capture = uuid4()
    sibling_capture = uuid4()
    png = xwd_to_png(valid_xwd())
    publish_png(
        data_root=tmp_path,
        instance_id=first,
        capture_id=first_capture,
        png=png,
    )
    publish_png(
        data_root=tmp_path,
        instance_id=sibling,
        capture_id=sibling_capture,
        png=png,
    )

    remove_instance_screenshot_cache(tmp_path, first)

    assert read_published_png(tmp_path, sibling, sibling_capture) == png
