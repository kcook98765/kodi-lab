"""The fixed raw JPEG path rejects unfamiliar pixels before codec entry."""

from io import BytesIO

import pytest
from PIL import Image

from kodi_lab.kodi.preview import x11_frame_to_preview
from kodi_lab.kodi.screenshots import ScreenshotArtifactError
from tests.kodi.test_screenshots import valid_x11_frame


@pytest.mark.parametrize("byte_order", [0, 1])
def test_direct_validated_raw_frame_becomes_small_jpeg(byte_order: int) -> None:
    result = x11_frame_to_preview(valid_x11_frame(byte_order=byte_order))
    assert result.startswith(b"\xff\xd8") and result.endswith(b"\xff\xd9")
    assert len(result) < 512 * 1024
    with Image.open(BytesIO(result)) as image:
        assert (
            image.format == "JPEG" and image.size == (640, 360) and image.mode == "RGB"
        )
        pixel = image.getpixel((300, 180))
        assert isinstance(pixel, tuple)
        assert all(
            abs(actual - expected) <= 5
            for actual, expected in zip(pixel, (0x11, 0x22, 0x33), strict=True)
        )


@pytest.mark.parametrize(
    "change", [{"width": 1279}, {"bits_per_pixel": 24}, {"red_mask": 0x000000FF}]
)
def test_raw_visual_drift_is_refused_before_codec(
    monkeypatch: pytest.MonkeyPatch, change: dict[str, int]
) -> None:
    def unexpected(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("The codec must not see an unvalidated layout")

    monkeypatch.setattr(Image, "frombytes", unexpected)
    with pytest.raises(ScreenshotArtifactError):
        x11_frame_to_preview(valid_x11_frame(**change))


def test_encoding_failure_does_not_fall_back_to_another_capture_or_format(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failure(*_args: object, **_kwargs: object) -> None:
        raise OSError("fixed codec failure")

    monkeypatch.setattr(Image.Image, "save", failure)
    with pytest.raises(OSError, match="codec failure"):
        x11_frame_to_preview(valid_x11_frame())
