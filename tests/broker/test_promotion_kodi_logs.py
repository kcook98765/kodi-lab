from __future__ import annotations

import io
import tarfile
from collections.abc import Iterable

import pytest
from pydantic import ValidationError

from kodi_lab.broker.kodi_logs import (
    MAX_NEW_LOG_BYTES,
    MAX_SNIPPETS,
    KodiLogAuthority,
    KodiLogWindowProvider,
    classify_kodi_log_window,
)
from kodi_lab.broker.operations import (
    PromotionKodiLogParams,
    ReadPromotionKodiLogWindowParams,
)

INSTANCE_ID = "91000000-0000-4000-8000-000000000001"
VERIFICATION_ID = "92000000-0000-4000-8000-000000000001"
CONTAINER_ID = "a" * 64
RUNTIME_REVISION_ID = "93000000-0000-4000-8000-000000000001"
AUTHORITY = KodiLogAuthority(
    VERIFICATION_ID,
    INSTANCE_ID,
    CONTAINER_ID,
    RUNTIME_REVISION_ID,
    "plugin.video.example",
)


def _archive(files: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for name, content in files.items():
            info = tarfile.TarInfo(f"temp/{name}")
            info.mode = 0o644
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return output.getvalue()


class FakeDocker:
    def __init__(self, observations: list[dict[str, bytes]]) -> None:
        self.observations = observations
        self.closed = False

    def containers(self, **_kwargs: object) -> list[dict[str, str]]:
        return [{"Id": CONTAINER_ID}]

    def inspect_container(self, _container_id: str) -> dict[str, object]:
        return {
            "Id": CONTAINER_ID,
            "Config": {
                "Labels": {
                    "io.kodi-lab.managed": "true",
                    "io.kodi-lab.instance-id": INSTANCE_ID,
                    "io.kodi-lab.runtime-revision-id": RUNTIME_REVISION_ID,
                }
            },
        }

    def get_archive(
        self, _container_id: str, path: str
    ) -> tuple[Iterable[bytes], dict[str, object]]:
        assert path == "/home/kodi/.kodi/temp"
        return iter([_archive(self.observations.pop(0))]), {}

    def close(self) -> None:
        self.closed = True


def _provider(*observations: dict[str, bytes]) -> KodiLogWindowProvider:
    client = FakeDocker(list(observations))
    return KodiLogWindowProvider(lambda: client)


def test_cursor_normal_append_returns_only_new_bounded_window() -> None:
    before = b"old historical error\n"
    after = before + b"NOTICE plugin.video.example started\n"
    provider = _provider({"kodi.log": before}, {"kodi.log": after})
    cursor = provider.capture(AUTHORITY)
    result = provider.read(AUTHORITY, cursor)
    assert result.complete
    assert result.classification == "clean"
    assert result.scanned_bytes == len(after) - len(before)


def test_cursor_correlates_normal_rotation_into_old_log() -> None:
    before = b"pre-stop\n"
    shutdown = b"shutdown after cursor\n"
    current = b"post-start\n"
    provider = _provider(
        {"kodi.log": before},
        {"kodi.old.log": before + shutdown, "kodi.log": current},
    )
    result = provider.read(AUTHORITY, provider.capture(AUTHORITY))
    assert result.complete
    assert result.scanned_bytes == len(shutdown) + len(current)


def test_truncation_or_recreation_is_incomplete_not_clean() -> None:
    provider = _provider(
        {"kodi.log": b"pre-stop bytes\n"},
        {"kodi.log": b"different\n"},
    )
    result = provider.read(AUTHORITY, provider.capture(AUTHORITY))
    assert not result.complete
    assert result.classification == "incomplete"


def test_target_errors_are_separate_from_unrelated_kodi_warnings() -> None:
    target = classify_kodi_log_window(
        b"ERROR plugin.video.example Python exception traceback\n",
        "plugin.video.example",
    )
    unrelated = classify_kodi_log_window(
        b"ERROR DBus unavailable\nERROR VDPAU unavailable\nERROR Zeroconf failed\n",
        "plugin.video.example",
    )
    assert target.classification == "target_errors"
    assert target.target_error_count == 1
    assert len(target.snippets) == 1
    assert unrelated.classification == "unrelated_warnings"
    assert unrelated.target_error_count == 0
    assert unrelated.unrelated_warning_count == 3


def test_multiline_traceback_is_correlated_and_snippets_are_bounded() -> None:
    data = (
        b"NOTICE plugin.video.example service starting\n"
        + b"Traceback: Python exception\n" * (MAX_SNIPPETS + 10)
    )
    result = classify_kodi_log_window(data, "plugin.video.example")
    assert result.classification == "target_errors"
    assert result.target_error_count > 0
    assert len(result.snippets) <= MAX_SNIPPETS
    assert all(len(item.encode()) <= 512 for item in result.snippets)


def test_post_cursor_scan_ceiling_is_incomplete() -> None:
    before = b"pre-stop\n"
    after = before + b"x" * (MAX_NEW_LOG_BYTES + 1)
    provider = _provider({"kodi.log": before}, {"kodi.log": after})
    result = provider.read(AUTHORITY, provider.capture(AUTHORITY))
    assert result.classification == "incomplete"
    assert not result.complete


def test_protocol_has_no_path_offset_or_caller_limit() -> None:
    with pytest.raises(ValidationError):
        PromotionKodiLogParams.model_validate(
            {
                "verification_id": VERIFICATION_ID,
                "managed_instance_id": INSTANCE_ID,
                "expected_container_id": CONTAINER_ID,
                "path": "/etc/shadow",
            }
        )
    with pytest.raises(ValidationError):
        ReadPromotionKodiLogWindowParams.model_validate(
            {
                "verification_id": VERIFICATION_ID,
                "managed_instance_id": INSTANCE_ID,
                "expected_container_id": CONTAINER_ID,
                "cursor": "opaque",
                "limit": 2**31,
            }
        )
