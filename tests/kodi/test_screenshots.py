import struct
from pathlib import Path
from uuid import uuid4

import pytest

from kodi_lab.kodi.screenshots import (
    EXPECTED_HEIGHT,
    EXPECTED_WIDTH,
    MAX_PNG_BYTES,
    PNG_SIGNATURE,
    X11_FRAME_MAGIC,
    ScreenshotArtifactError,
    cleanup_capture_stage,
    inspect_x11_frame,
    is_supported_screenshot_runtime,
    png_pixel_statistics,
    ppm_to_png,
    prepare_capture_stage,
    publish_png,
    read_published_png,
    remove_instance_screenshot_cache,
    validate_png,
    x11_frame_to_png,
    xwd_to_png,
)


def valid_x11_frame(*, byte_order: int = 0, **changes: int) -> bytes:
    values = {
        "width": 1280,
        "height": 720,
        "depth": 24,
        "bits_per_pixel": 32,
        "image_byte_order": byte_order,
        "scanline_pad": 32,
        "bytes_per_line": 1280 * 4,
        "visual_class": 4,
        "bits_per_rgb": 8,
        "red_mask": 0x00FF0000,
        "green_mask": 0x0000FF00,
        "blue_mask": 0x000000FF,
    }
    values.update(changes)
    header = struct.pack(
        ">8s12I",
        X11_FRAME_MAGIC,
        values["width"],
        values["height"],
        values["depth"],
        values["bits_per_pixel"],
        values["image_byte_order"],
        values["scanline_pad"],
        values["bytes_per_line"],
        values["visual_class"],
        values["bits_per_rgb"],
        values["red_mask"],
        values["green_mask"],
        values["blue_mask"],
    )
    pixel = (
        bytes((0x33, 0x22, 0x11, 0))
        if byte_order == 0
        else bytes((0, 0x11, 0x22, 0x33))
    )
    return header + (pixel * values["width"] * values["height"])


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


@pytest.mark.parametrize("byte_order", [0, 1])
def test_server_observed_x11_layout_converts_to_strict_png(byte_order: int) -> None:
    frame = valid_x11_frame(byte_order=byte_order)
    layout = inspect_x11_frame(frame)
    png = x11_frame_to_png(frame)
    result = validate_png(png)
    statistics = png_pixel_statistics(png)

    assert layout.classification == (
        "truecolor24-bpp32-lsb-rgb888"
        if byte_order == 0
        else "truecolor24-bpp32-msb-rgb888"
    )
    assert (result.width, result.height) == (1280, 720)
    assert (statistics.rgb_min, statistics.rgb_max) == (0x11, 0x33)
    assert statistics.has_pixel_variation is False


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("depth", 16),
        ("bits_per_pixel", 24),
        ("image_byte_order", 2),
        ("scanline_pad", 16),
        ("bytes_per_line", 1280 * 3),
        ("visual_class", 5),
        ("bits_per_rgb", 10),
        ("red_mask", 0x000000FF),
        ("green_mask", 0x00FF0000),
        ("blue_mask", 0x0000FF00),
    ],
)
def test_unfamiliar_x11_visual_layout_is_rejected(field: str, value: int) -> None:
    with pytest.raises(ScreenshotArtifactError, match="unsupported"):
        inspect_x11_frame(valid_x11_frame(**{field: value}))


@pytest.mark.parametrize(
    ("revision_id", "canonical", "version", "tag", "ordinal"),
    [
        ("51950000-0000-4000-8000-000000000005", "19.5", "19.5", "stable", None),
        ("52000000-0000-4000-8000-000000000005", "20.5", "20.5", "stable", None),
        ("3a000000-0000-4000-8000-000000000006", "21.3", "21.3", "stable", None),
        (
            "52200000-0000-4000-8000-000000000005",
            "22.0-beta.2",
            "22.0",
            "beta",
            2,
        ),
    ],
)
def test_exact_curated_runtime_profiles_support_screenshots(
    revision_id: str,
    canonical: str,
    version: str,
    tag: str,
    ordinal: int | None,
) -> None:
    assert is_supported_screenshot_runtime(
        runtime_revision_id=revision_id,
        canonical_release=canonical,
        expected_application_name="Kodi",
        expected_kodi_version=version,
        expected_version_tag=tag,
        expected_prerelease_ordinal=ordinal,
    )


def test_nearby_runtime_profile_is_not_broadly_admitted() -> None:
    assert not is_supported_screenshot_runtime(
        runtime_revision_id="52200000-0000-4000-8000-000000000005",
        canonical_release="22.0-beta.1",
        expected_application_name="Kodi",
        expected_kodi_version="22.0",
        expected_version_tag="beta",
        expected_prerelease_ordinal=1,
    )
    assert not is_supported_screenshot_runtime(
        runtime_revision_id="52200000-0000-4000-8000-000000000004",
        canonical_release="22.0-beta.2",
        expected_application_name="Kodi",
        expected_kodi_version="22.0",
        expected_version_tag="beta",
        expected_prerelease_ordinal=2,
    )


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
