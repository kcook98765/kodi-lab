"""Fixed Web-side raw-frame conversion; the broker has no codec dependency."""

from io import BytesIO

from kodi_lab.kodi.screenshots import ScreenshotArtifactError, inspect_x11_frame

JPEG_MIME_TYPE = "image/jpeg"
PREVIEW_WIDTH = 640
PREVIEW_HEIGHT = 360
PREVIEW_QUALITY = 68
MAX_PREVIEW_BYTES = 512 * 1024


def x11_frame_to_preview(payload: bytes) -> bytes:
    """Validate first, then decode raw pixels directly; no intermediate PNG."""
    layout = inspect_x11_frame(payload)
    # Pillow is an optional Web extra and a pinned test dependency. Do not import
    # it from the shared broker/module-import path or decode arbitrary images.
    from PIL import Image

    raw_mode = "BGRX" if layout.image_byte_order == 0 else "XRGB"
    pixel_offset = len(payload) - layout.bytes_per_line * layout.height
    with (
        Image.frombytes(
            "RGB",
            (layout.width, layout.height),
            payload[pixel_offset:],
            "raw",
            raw_mode,
        ) as full,
        full.resize((PREVIEW_WIDTH, PREVIEW_HEIGHT), Image.Resampling.BOX) as small,
    ):
        output = BytesIO()
        small.save(
            output,
            format="JPEG",
            quality=PREVIEW_QUALITY,
            subsampling=2,
            optimize=False,
            progressive=False,
        )
        result = output.getvalue()
    if (
        not 0 < len(result) <= MAX_PREVIEW_BYTES
        or not result.startswith(b"\xff\xd8")
        or not result.endswith(b"\xff\xd9")
    ):
        raise ScreenshotArtifactError("Preview encoding failed safely.")
    return result
