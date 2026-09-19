"""Strict fixed-geometry capture conversion and private screenshot cache storage."""

from __future__ import annotations

import binascii
import os
import shutil
import stat
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import UUID

PNG_MIME_TYPE = "image/png"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
EXPECTED_WIDTH = 1280
EXPECTED_HEIGHT = 720
MAX_WIDTH = 4096
MAX_HEIGHT = 2160
MAX_PNG_BYTES = 8 * 1024 * 1024
MAX_RAW_CAPTURE_BYTES = 8 * 1024 * 1024
_XWD_HEADER_WORDS = 25
_XWD_HEADER_SIZE = _XWD_HEADER_WORDS * 4


class ScreenshotArtifactError(RuntimeError):
    """A screenshot artifact was missing, unsafe, or invalid."""


@dataclass(frozen=True, slots=True)
class ValidatedPng:
    width: int
    height: int
    byte_size: int


def screenshot_root(data_root: Path) -> Path:
    return data_root / "screenshots"


def capture_stage_directory(data_root: Path, capture_id: UUID) -> Path:
    return screenshot_root(data_root) / "staging" / str(capture_id)


def raw_capture_path(data_root: Path, capture_id: UUID) -> Path:
    return capture_stage_directory(data_root, capture_id) / "frame.raw"


def published_png_path(data_root: Path, instance_id: UUID, capture_id: UUID) -> Path:
    return (
        screenshot_root(data_root)
        / "instances"
        / str(instance_id)
        / (f"{capture_id}.png")
    )


def prepare_capture_stage(data_root: Path, capture_id: UUID) -> Path:
    root = screenshot_root(data_root)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    root.chmod(0o700)
    staging = root / "staging"
    staging.mkdir(mode=0o700, exist_ok=True)
    staging.chmod(0o700)
    stage = capture_stage_directory(data_root, capture_id)
    stage.mkdir(mode=0o700)
    return stage


def cleanup_capture_stage(data_root: Path, capture_id: UUID) -> None:
    stage = capture_stage_directory(data_root, capture_id)
    try:
        metadata = stage.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise ScreenshotArtifactError("Screenshot staging identity is unsafe.")
    shutil.rmtree(stage)


def publish_png(
    *, data_root: Path, instance_id: UUID, capture_id: UUID, png: bytes
) -> Path:
    validate_png(png)
    directory = screenshot_root(data_root) / "instances" / str(instance_id)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    final_path = published_png_path(data_root, instance_id, capture_id)
    temporary = directory / f".{capture_id}.tmp"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as output:
            output.write(png)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, final_path)
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    finally:
        os.close(descriptor)
    return final_path


def remove_published_png(data_root: Path, instance_id: UUID, capture_id: UUID) -> None:
    path = published_png_path(data_root, instance_id, capture_id)
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise ScreenshotArtifactError("Published screenshot identity is unsafe.")
    path.unlink()


def remove_instance_screenshot_cache(data_root: Path, instance_id: UUID) -> None:
    directory = screenshot_root(data_root) / "instances" / str(instance_id)
    try:
        metadata = directory.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise ScreenshotArtifactError("Instance screenshot cache identity is unsafe.")
    shutil.rmtree(directory)


def read_published_png(data_root: Path, instance_id: UUID, capture_id: UUID) -> bytes:
    path = published_png_path(data_root, instance_id, capture_id)
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise ScreenshotArtifactError("Screenshot cache object is unsafe.")
        if not 1 <= metadata.st_size <= MAX_PNG_BYTES:
            raise ScreenshotArtifactError("Screenshot cache object has invalid size.")
        payload = path.read_bytes()
    except FileNotFoundError as error:
        raise ScreenshotArtifactError(
            "Screenshot cache object is unavailable."
        ) from error
    validate_png(payload)
    return payload


def xwd_to_png(payload: bytes) -> bytes:
    """Convert only the exact bounded Xvfb TrueColor XWD representation."""

    if not _XWD_HEADER_SIZE <= len(payload) <= MAX_RAW_CAPTURE_BYTES:
        raise ScreenshotArtifactError("XWD capture size is invalid.")
    try:
        words = struct.unpack(">25I", payload[:_XWD_HEADER_SIZE])
    except struct.error as error:
        raise ScreenshotArtifactError("XWD header is truncated.") from error
    (
        header_size,
        file_version,
        pixmap_format,
        pixmap_depth,
        width,
        height,
        xoffset,
        byte_order,
        bitmap_unit,
        bitmap_bit_order,
        bitmap_pad,
        bits_per_pixel,
        bytes_per_line,
        visual_class,
        red_mask,
        green_mask,
        blue_mask,
        bits_per_rgb,
        _colormap_entries,
        ncolors,
        window_width,
        window_height,
        _window_x,
        _window_y,
        _window_bdrwidth,
    ) = words
    if not _XWD_HEADER_SIZE <= header_size <= 4096 or header_size > len(payload):
        raise ScreenshotArtifactError("XWD header size is invalid.")
    expected = (
        file_version == 7
        and pixmap_format == 2
        and pixmap_depth == 24
        and width == EXPECTED_WIDTH
        and height == EXPECTED_HEIGHT
        and window_width == width
        and window_height == height
        and xoffset == 0
        and byte_order in {0, 1}
        and bitmap_unit == 32
        and bitmap_bit_order in {0, 1}
        and bitmap_pad == 32
        and bits_per_pixel == 32
        and bytes_per_line == width * 4
        and visual_class in {4, 5}
        and red_mask == 0x00FF0000
        and green_mask == 0x0000FF00
        and blue_mask == 0x000000FF
        and bits_per_rgb == 8
        and ncolors <= 256
    )
    if not expected:
        raise ScreenshotArtifactError("XWD capture geometry or format is unsupported.")
    pixel_offset = header_size + (ncolors * 12)
    pixel_size = bytes_per_line * height
    if pixel_offset + pixel_size != len(payload):
        raise ScreenshotArtifactError("XWD capture payload length is inconsistent.")

    byte_format: Literal["little", "big"] = "little" if byte_order == 0 else "big"
    raw = bytearray()
    pixels = memoryview(payload)[pixel_offset:]
    for row_number in range(height):
        raw.append(0)
        row = pixels[row_number * bytes_per_line : (row_number + 1) * bytes_per_line]
        for offset in range(0, bytes_per_line, 4):
            pixel = int.from_bytes(row[offset : offset + 4], byte_format)
            raw.extend(((pixel >> 16) & 0xFF, (pixel >> 8) & 0xFF, pixel & 0xFF))
    png = bytearray(PNG_SIGNATURE)
    png.extend(
        _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    )
    png.extend(_png_chunk(b"IDAT", zlib.compress(bytes(raw), level=6)))
    png.extend(_png_chunk(b"IEND", b""))
    result = bytes(png)
    validate_png(result)
    return result


def ppm_to_png(payload: bytes) -> bytes:
    """Convert only the fixed 1280x720 RGB result of the X11 capture primitive."""

    header = b"P6\n1280 720\n255\n"
    expected_size = len(header) + (EXPECTED_WIDTH * EXPECTED_HEIGHT * 3)
    if len(payload) != expected_size or not payload.startswith(header):
        raise ScreenshotArtifactError("Fixed RGB capture payload is invalid.")
    pixels = memoryview(payload)[len(header) :]
    raw = bytearray()
    row_bytes = EXPECTED_WIDTH * 3
    for row_number in range(EXPECTED_HEIGHT):
        raw.append(0)
        start = row_number * row_bytes
        raw.extend(pixels[start : start + row_bytes])
    png = bytearray(PNG_SIGNATURE)
    png.extend(
        _png_chunk(
            b"IHDR",
            struct.pack(">IIBBBBB", EXPECTED_WIDTH, EXPECTED_HEIGHT, 8, 2, 0, 0, 0),
        )
    )
    png.extend(_png_chunk(b"IDAT", zlib.compress(bytes(raw), level=6)))
    png.extend(_png_chunk(b"IEND", b""))
    result = bytes(png)
    validate_png(result)
    return result


def validate_png(payload: bytes) -> ValidatedPng:
    if not len(PNG_SIGNATURE) < len(payload) <= MAX_PNG_BYTES:
        raise ScreenshotArtifactError("PNG size is invalid.")
    if not payload.startswith(PNG_SIGNATURE):
        raise ScreenshotArtifactError("PNG signature is invalid.")
    offset = len(PNG_SIGNATURE)
    chunks: list[tuple[bytes, bytes]] = []
    while offset < len(payload):
        if offset + 12 > len(payload):
            raise ScreenshotArtifactError("PNG chunk is truncated.")
        length = struct.unpack(">I", payload[offset : offset + 4])[0]
        chunk_type = payload[offset + 4 : offset + 8]
        end = offset + 12 + length
        if end > len(payload):
            raise ScreenshotArtifactError("PNG chunk payload is truncated.")
        data = payload[offset + 8 : offset + 8 + length]
        expected_crc = struct.unpack(">I", payload[offset + 8 + length : end])[0]
        if binascii.crc32(chunk_type + data) & 0xFFFFFFFF != expected_crc:
            raise ScreenshotArtifactError("PNG chunk checksum is invalid.")
        chunks.append((chunk_type, data))
        offset = end
        if chunk_type == b"IEND":
            break
    if offset != len(payload):
        raise ScreenshotArtifactError("PNG has unexpected trailing bytes.")
    if [item[0] for item in chunks] != [b"IHDR", b"IDAT", b"IEND"]:
        raise ScreenshotArtifactError("PNG chunk layout is unsupported.")
    header = chunks[0][1]
    if len(header) != 13:
        raise ScreenshotArtifactError("PNG header is invalid.")
    width, height, depth, color, compression, filtering, interlace = struct.unpack(
        ">IIBBBBB", header
    )
    if not (0 < width <= MAX_WIDTH and 0 < height <= MAX_HEIGHT):
        raise ScreenshotArtifactError("PNG dimensions are invalid.")
    if (width, height) != (EXPECTED_WIDTH, EXPECTED_HEIGHT):
        raise ScreenshotArtifactError("PNG dimensions differ from runtime geometry.")
    if (depth, color, compression, filtering, interlace) != (8, 2, 0, 0, 0):
        raise ScreenshotArtifactError("PNG encoding is unsupported.")
    try:
        decoded = zlib.decompress(chunks[1][1])
    except zlib.error as error:
        raise ScreenshotArtifactError("PNG image data is invalid.") from error
    expected_length = height * (1 + (width * 3))
    if len(decoded) != expected_length:
        raise ScreenshotArtifactError("PNG decoded size is inconsistent.")
    row_size = 1 + (width * 3)
    if any(decoded[offset] != 0 for offset in range(0, len(decoded), row_size)):
        raise ScreenshotArtifactError("PNG filter is unsupported.")
    return ValidatedPng(width=width, height=height, byte_size=len(payload))


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + kind
        + data
        + struct.pack(">I", binascii.crc32(kind + data) & 0xFFFFFFFF)
    )
