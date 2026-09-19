from __future__ import annotations

import io
import tarfile
from collections.abc import Iterable

import pytest
from pydantic import ValidationError

from kodi_lab.broker.kodi_logs import (
    MAX_NEW_LOG_BYTES,
    MAX_RETAINED_BYTES,
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


@pytest.mark.parametrize(
    ("secret_form", "literal"),
    [
        ("Bearer bearer-token-123", "bearer-token-123"),
        ("Authorization: Bearer authorization-token-456", "authorization-token-456"),
        ("api_key=api-value-789", "api-value-789"),
        ("ApIkEy=apikey-value-012", "apikey-value-012"),
        ("api-key=api-hyphen-value-345", "api-hyphen-value-345"),
        ("ToKeN: token-value-abc", "token-value-abc"),
        ("ACCESS_TOKEN=access-value-678", "access-value-678"),
        ("auth_token: auth-value-901", "auth-value-901"),
        ("password='password-value-def'", "password-value-def"),
        ("PaSsWd=passwd-value-234", "passwd-value-234"),
        ("secret=secret-value-ghi", "secret-value-ghi"),
        (
            "https://audit-user:audit-password@example.invalid/path",
            "audit-password",
        ),
        (
            "github_pat_abcdefghijklmnopqrstuvwxyz",
            "github_pat_abcdefghijklmnopqrstuvwxyz",
        ),
        ("sk-abcdefghijklmnopqrstuvwxyz", "sk-abcdefghijklmnopqrstuvwxyz"),
    ],
)
def test_obvious_secrets_are_redacted_from_target_snippets(
    secret_form: str, literal: str
) -> None:
    result = classify_kodi_log_window(
        f"ERROR plugin.video.example ({secret_form}) dependency failed\n".encode(),
        "plugin.video.example",
    )

    assert result.classification == "target_errors"
    assert result.snippets
    rendered = "\n".join(result.snippets)
    assert "[REDACTED]" in rendered
    assert literal not in rendered
    assert "dependency failed" in rendered
    assert rendered.endswith(") dependency failed")


def test_multiple_long_secrets_are_redacted_before_snippet_truncation() -> None:
    long_secret = "s" * 2_000
    result = classify_kodi_log_window(
        (
            "ERROR plugin.video.example api_key="
            + long_secret
            + " password=second-secret token: third-secret dependency failed\n"
        ).encode(),
        "plugin.video.example",
    )

    rendered = "\n".join(result.snippets)
    assert rendered.count("[REDACTED]") == 3
    assert long_secret not in rendered
    assert "second-secret" not in rendered
    assert "third-secret" not in rendered
    assert len(rendered.encode()) <= 512


def test_secret_at_truncation_boundary_cannot_leak_a_fragment() -> None:
    secret = "boundary-secret-" + "z" * 600
    prefix = "ERROR plugin.video.example " + "x" * 460
    result = classify_kodi_log_window(
        f"{prefix} api-key={secret} dependency failed\n".encode(),
        "plugin.video.example",
    )

    rendered = "\n".join(result.snippets)
    assert secret not in rendered
    assert "boundary-secret" not in rendered
    assert "[REDACTED]" in rendered


def test_secret_crossing_retained_window_boundary_is_redacted_before_cut() -> None:
    secret = "audit-boundary-secret-" + "q" * (MAX_RETAINED_BYTES + 256)
    result = classify_kodi_log_window(
        (f"api_key={secret} ERROR plugin.video.example dependency failed\n").encode(),
        "plugin.video.example",
    )

    rendered = "\n".join(result.snippets)
    assert result.classification == "target_errors"
    assert "[REDACTED]" in rendered
    assert "audit-boundary-secret" not in rendered
    assert "q" * 32 not in rendered
    assert "dependency failed" in rendered


def test_secrets_in_different_retained_regions_leave_no_fragments() -> None:
    first = "first-region-" + "a" * 2_000
    crossing = "crossing-region-" + "b" * (MAX_RETAINED_BYTES + 128)
    final = "final-region-" + "c" * 2_000
    data = (
        f"NOTICE token={first}\n"
        f"api-key={crossing} ERROR plugin.video.example dependency failed\n"
        f"ERROR plugin.video.example passwd={final} service failed\n"
    ).encode()

    result = classify_kodi_log_window(data, "plugin.video.example")
    rendered = "\n".join(result.snippets)

    assert result.classification == "target_errors"
    assert rendered.count("[REDACTED]") >= 2
    for fragment in ("first-region", "crossing-region", "final-region"):
        assert fragment not in rendered
    for repeated in ("a" * 32, "b" * 32, "c" * 32):
        assert repeated not in rendered


def test_secret_before_retained_cut_and_redacted_output_truncation_are_safe() -> None:
    early = "early-secret-" + "d" * 2_000
    late = "late-secret-" + "e" * 2_000
    filler = ("NOTICE ordinary Kodi text\n" * 2_000).encode()
    data = (
        f"NOTICE api_key={early}\n".encode()
        + filler
        + (
            "ERROR plugin.video.example "
            + "x" * 470
            + f" auth_token={late} dependency failed\n"
        ).encode()
    )

    result = classify_kodi_log_window(data, "plugin.video.example")
    rendered = "\n".join(result.snippets)

    assert len(data) > MAX_RETAINED_BYTES
    assert all(len(snippet.encode()) <= 512 for snippet in result.snippets)
    assert "early-secret" not in rendered
    assert "late-secret" not in rendered
    assert "d" * 32 not in rendered
    assert "e" * 32 not in rendered


def test_near_scan_limit_secret_is_fully_redacted() -> None:
    secret = "near-scan-limit-" + "v" * (MAX_NEW_LOG_BYTES - 256)
    data = (
        f"api_key={secret} ERROR plugin.video.example importerror dependency failed\n"
    ).encode()
    assert len(data) <= MAX_NEW_LOG_BYTES

    result = classify_kodi_log_window(data, "plugin.video.example")
    rendered = "\n".join(result.snippets)

    assert "[REDACTED]" in rendered
    assert "near-scan-limit" not in rendered
    assert "v" * 32 not in rendered
    assert result.classification == "target_errors"


def test_malformed_utf8_inside_secret_is_redacted_before_normalization() -> None:
    result = classify_kodi_log_window(
        b"api_key=prefix\xffsuffix ERROR plugin.video.example service failed\n",
        "plugin.video.example",
    )

    rendered = "\n".join(result.snippets)
    assert "[REDACTED]" in rendered
    assert "prefix" not in rendered
    assert "suffix" not in rendered


def test_ordinary_target_error_text_is_unchanged_by_secret_sanitizer() -> None:
    line = "ERROR plugin.video.example dependency import failed normally"
    result = classify_kodi_log_window(f"{line}\n".encode(), "plugin.video.example")

    assert result.snippets == (line,)
    assert result.classification == "target_errors"


@pytest.mark.parametrize(
    "classification_text",
    [
        "Traceback: Python exception",
        "ImportError dependency failed",
        "ModuleNotFoundError dependency failed",
        "service failed",
    ],
)
def test_redaction_preserves_target_error_classification(
    classification_text: str,
) -> None:
    result = classify_kodi_log_window(
        (
            "NOTICE plugin.video.example token=classification-secret\n"
            f"{classification_text}\n"
        ).encode(),
        "plugin.video.example",
    )

    assert result.classification == "target_errors"
    assert result.target_error_count == 1
    assert "classification-secret" not in "\n".join(result.snippets)


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
