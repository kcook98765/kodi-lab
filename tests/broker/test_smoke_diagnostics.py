from __future__ import annotations

import io
import tarfile
from collections.abc import Iterable

import pytest
from pydantic import ValidationError

from kodi_lab.broker.kodi_logs import (
    KodiLogAuthority,
    KodiLogWindowError,
    KodiLogWindowProvider,
)
from kodi_lab.broker.operations import ReadSmokeLogParams, SmokeLogParams
from kodi_lab.broker.smoke_diagnostics import (
    MAX_EXCEPTION_BLOCK_BYTES,
    MAX_SMOKE_WINDOW_BYTES,
    parse_smoke_exception_window,
)
from kodi_lab.domain.smoke_diagnostics import (
    AddonExceptionDiagnostic,
    SmokeDiagnosticStatus,
)

ADDON = "plugin.video.fixture"
ROOT = "/home/kodi/.kodi/addons/plugin.video.fixture"


def window(
    exception: str = "AttributeError",
    *,
    path: str = f"{ROOT}/default.py",
    line: int = 23,
    root: str = ADDON,
    end: bool = True,
    extra: str = "",
    detail: str = "unsafe token=https://example.test/?secret=yes",
) -> bytes:
    lines = [
        "2026-10-03 T:1 error <general>: EXCEPTION Thrown (PythonToCppException)",
        "2026-10-03 T:1 error <general>: Traceback (most recent call last):",
        f'2026-10-03 T:1 error <general>:   File "{path}", line {line}, in <module>',
        "2026-10-03 T:1 error <general>:     xbmc.Addon()",
        extra,
        f"2026-10-03 T:1 error <general>: {exception}: {detail}",
    ]
    if end:
        lines.append(
            "2026-10-03 T:1 error <general>: -->End of Python script error report<--"
        )
    lines.append(
        f"2026-10-03 T:1 error <general>: GetDirectory - Error getting plugin://{root}/"
    )
    return "\n".join(lines).encode()


@pytest.mark.parametrize(
    "exception",
    (
        "AttributeError",
        "ImportError",
        "ModuleNotFoundError",
        "TypeError",
        "KeyError",
        "SyntaxError",
        "RuntimeError",
    ),
)
def test_common_exceptions_only_expose_class_relative_frame(exception: str) -> None:
    result = parse_smoke_exception_window(window(exception), ADDON)
    assert result.status is SmokeDiagnosticStatus.MATCHED
    assert (result.exception_class, result.addon_file, result.line_number) == (
        exception,
        "default.py",
        23,
    )
    assert result.detail is None
    text = result.observation_text()
    assert text is not None and "matching addon exception" in text
    assert "secret" not in text and "https" not in text and ROOT not in text


def test_nested_traceback_chooses_last_verified_addon_frame() -> None:
    extra = (
        f'  File "{ROOT}/lib/runner.py", line 44, in run\n'
        '  File "/usr/lib/python3.11/os.py", line 2, in f'
    )
    result = parse_smoke_exception_window(window(extra=extra), ADDON)
    assert (result.addon_file, result.line_number) == ("lib/runner.py", 44)


@pytest.mark.parametrize(
    "data,status",
    (
        (window(path="/usr/lib/python3.11/os.py"), SmokeDiagnosticStatus.NO_MATCH),
        (
            window(path="/home/kodi/.kodi/addons/plugin.video.other/default.py"),
            SmokeDiagnosticStatus.NO_MATCH,
        ),
        (window(path="/tmp/default.py"), SmokeDiagnosticStatus.NO_MATCH),
        (window(path=f"{ROOT}/../other.py"), SmokeDiagnosticStatus.REJECTED),
        (window(path=f"{ROOT}/lib//other.py"), SmokeDiagnosticStatus.REJECTED),
        (window(path=f"{ROOT}/file..py"), SmokeDiagnosticStatus.REJECTED),
        (window(end=False), SmokeDiagnosticStatus.REJECTED),
        (window(root="plugin.video.other"), SmokeDiagnosticStatus.REJECTED),
        (
            window().split(b"GetDirectory - Error getting")[0],
            SmokeDiagnosticStatus.NO_MATCH,
        ),
        (window() + b"\n" + window(), SmokeDiagnosticStatus.REJECTED),
        (b"", SmokeDiagnosticStatus.NO_MATCH),
        (
            window() + b"\nGetDirectory - Error getting plugin://plugin.video.other/",
            SmokeDiagnosticStatus.REJECTED,
        ),
        (b"x" * (MAX_SMOKE_WINDOW_BYTES + 1), SmokeDiagnosticStatus.REJECTED),
        (
            window(extra="x" * (MAX_EXCEPTION_BLOCK_BYTES + 1)),
            SmokeDiagnosticStatus.REJECTED,
        ),
        (
            window().replace(b"AttributeError:", b"broken traceback"),
            SmokeDiagnosticStatus.REJECTED,
        ),
        (
            window().replace(b"default.py", b"default.py\x00"),
            SmokeDiagnosticStatus.REJECTED,
        ),
    ),
)
def test_rejected_and_unmatched_evidence_exposes_no_details(
    data: bytes, status: SmokeDiagnosticStatus
) -> None:
    result = parse_smoke_exception_window(data, ADDON)
    assert result.status is status
    assert result.observation_text() is None
    assert (
        result.exception_class,
        result.addon_file,
        result.line_number,
        result.detail,
    ) == (
        None,
        None,
        None,
        None,
    )


def test_pre_cursor_or_late_exception_is_not_in_post_cursor_window() -> None:
    before = window()
    after = b"GetDirectory - Error getting plugin://plugin.video.fixture/"
    assert before.endswith(after)
    assert (
        parse_smoke_exception_window(after, ADDON).status
        is SmokeDiagnosticStatus.NO_MATCH
    )
    assert (
        parse_smoke_exception_window(b"", ADDON).status
        is SmokeDiagnosticStatus.NO_MATCH
    )


def test_no_caller_selected_log_path_container_or_addon() -> None:
    for selector in ("path", "container_id", "addon_id", "method", "url"):
        with pytest.raises(ValidationError):
            SmokeLogParams.model_validate(
                {
                    "execution_id": "91000000-0000-4000-8000-000000000001",
                    selector: "anything",
                }
            )
    with pytest.raises(ValidationError):
        ReadSmokeLogParams.model_validate({"execution_id": "not-a-uuid", "cursor": "x"})
    with pytest.raises(ValidationError):
        AddonExceptionDiagnostic(
            status=SmokeDiagnosticStatus.MATCHED,
            exception_class="AttributeError",
            addon_file="https:secret.py",
            line_number=1,
        )


def test_signed_cursor_limits_observation_to_new_bytes_and_same_identity() -> None:
    historical = window()
    after = window("TypeError")
    authority = KodiLogAuthority(
        "91000000-0000-4000-8000-000000000001",
        "92000000-0000-4000-8000-000000000001",
        "a" * 64,
        "93000000-0000-4000-8000-000000000001",
        ADDON,
    )

    class Docker:
        def __init__(self) -> None:
            self.data = historical

        def containers(self, **_kwargs: object) -> list[dict[str, str]]:
            return [{"Id": authority.container_id}]

        def inspect_container(self, _id: str) -> dict[str, object]:
            return {
                "Id": authority.container_id,
                "Config": {
                    "Labels": {
                        "io.kodi-lab.managed": "true",
                        "io.kodi-lab.instance-id": authority.instance_id,
                        "io.kodi-lab.runtime-revision-id": (
                            authority.runtime_revision_id
                        ),
                    }
                },
            }

        def get_archive(
            self, _id: str, _path: str
        ) -> tuple[Iterable[bytes], dict[str, object]]:
            stream = io.BytesIO()
            with tarfile.open(fileobj=stream, mode="w") as archive:
                info = tarfile.TarInfo("temp/kodi.log")
                info.size = len(self.data)
                archive.addfile(info, io.BytesIO(self.data))
            return iter((stream.getvalue(),)), {}

        def close(self) -> None:
            pass

    docker = Docker()
    provider = KodiLogWindowProvider(lambda: docker)
    cursor = provider.capture(authority)
    docker.data = historical + after
    observed = provider.read_bytes(authority, cursor, max_age=20)
    assert observed == after
    assert parse_smoke_exception_window(observed, ADDON).exception_class == "TypeError"
    with pytest.raises(KodiLogWindowError):
        provider.read_bytes(
            KodiLogAuthority(
                authority.verification_id,
                authority.instance_id,
                "b" * 64,
                authority.runtime_revision_id,
                ADDON,
            ),
            cursor,
            max_age=20,
        )
    docker.data = b"different replaced log"
    assert provider.read_bytes(authority, cursor, max_age=20) is None
