"""Only reported approved names receive bounded HEAD metadata, never bodies."""

from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from kodi_lab.broker.stopped_diagnostics import (
    StoppedDiagnosticsError,
    StoppedDiagnosticsInput,
)
from kodi_lab.broker.stopped_logs import (
    LOG_PATHS,
    ReferencedCrashReportMetadata,
    StoppedLogSnapshot,
    StoppedManagedLogProvider,
)
from tests.application.test_retained_diagnostics import snapshot
from tests.broker.test_stopped_diagnostics import CONTAINER_ID, operation
from tests.broker.test_stopped_logs import Client as LogClient
from tests.broker.test_stopped_logs import Response, capture, setup

NAME = "kodi_crashlog-20261025_013012.log"
PATH = "/home/kodi/" + NAME
LINE = "Crash report available at " + PATH + "\n"


class Client(LogClient):
    def __init__(self) -> None:
        super().__init__()
        self.files[LOG_PATHS["stdout"]] = LINE.encode()
        self.report_stats: dict[str, dict[str, Any]] = {
            NAME: dict(
                name=NAME,
                mode=0o644,
                size=2 * 1024 * 1024,
                mtime="2026-10-25T08:30:12Z",
                linkTarget="",
            )
        }
        self.report_status: dict[str, int] = {}
        self.report_heads: list[str] = []
        self.changed = False
        self.disappeared = False

    def head(
        self, url: str, *, params: dict[str, str], timeout: float, allow_redirects: bool
    ) -> Response:
        path = params["path"]
        if not path.startswith("/home/kodi/kodi_crashlog-"):
            return super().head(
                url, params=params, timeout=timeout, allow_redirects=allow_redirects
            )
        assert (
            url == f"/containers/{CONTAINER_ID}/archive"
            and 0 < timeout <= 1
            and not allow_redirects
        )
        assert path.count("/") == 3
        self.report_heads.append(path)
        name = path.rsplit("/", 1)[-1]
        stat = deepcopy(self.report_stats.get(name))
        count = self.report_heads.count(path)
        if stat is not None and count > 1 and self.changed:
            stat["mtime"] = "2026-10-25T08:30:13Z"
        status = self.report_status.get(name, 200 if stat is not None else 404)
        if self.disappeared and count > 1:
            status = 404
        response = Response(status=status, stat=stat)
        self.responses.append(response)
        return response


def test_reported_metadata_uses_only_fixed_head_paths_and_retains_no_report_body(
    tmp_path: Path,
) -> None:
    client = Client()
    result = capture(tmp_path, client)
    assert result.schema_revision == 3 and result.crash_reports == "not_collected"
    assert result.crash_report_discovery == "bounded_retained_log_references"
    assert result.unreferenced_crash_reports == "not_observed"
    report = result.crash_report_metadata[0]
    assert report.basename == NAME and report.state == "available"
    assert report.referenced_by == ["stdout"] and report.body == "not_collected"
    assert (
        report.source_size_bytes == 2 * 1024 * 1024
    )  # Metadata can describe a large unread file.
    assert report.modified_at is not None and report.modified_at.utcoffset() is not None
    assert report.filename_timestamp_zone == "not_established"
    assert client.report_heads == [PATH, PATH]
    assert set(client.reads) == set(LOG_PATHS.values()) and PATH not in client.reads
    assert all(response.closed for response in client.responses)
    assert result.exit_metadata.cause == "unknown"


@pytest.mark.parametrize(
    "line",
    [
        "Crash report available at /etc/kodi_crashlog-20261025_013012.log\n",
        "Crash report available at /home/kodi/../kodi_crashlog-20261025_013012.log\n",
        "Crash report available at /home/kodi/kodi_crashlog-20261325_013012.log\n",
        "Crash report available at /home/kodi/kodi_crashlog-20261025_256199.log\n",
        "Crash report available at /home/kodi/"
        "kodi_crashlog-20261025_013012.log/secret\n",
        "untrusted prefix " + LINE,
        LINE.rstrip() + " extra fields\n",
    ],
)
def test_other_paths_malformed_names_and_ambiguous_lines_do_not_trigger_discovery(
    tmp_path: Path, line: str
) -> None:
    client = Client()
    client.files[LOG_PATHS["stdout"]] = line.encode()
    result = capture(tmp_path, client)
    assert result.crash_report_metadata == [] and not client.report_heads
    assert result.unreferenced_crash_reports == "not_observed"


def test_docker_timestamped_reference_is_deduplicated_and_bound_to_all_retained_sources(
    tmp_path: Path,
) -> None:
    client = Client()
    data = ("2026-10-25T08:30:12.123456789Z " + LINE).encode()
    client.docker_body = bytes([2, 0, 0, 0]) + len(data).to_bytes(4, "big") + data
    result = capture(tmp_path, client)
    assert len(result.crash_report_metadata) == 1
    assert result.crash_report_metadata[0].referenced_by == ["stdout", "docker_output"]
    assert len(client.report_heads) == 2


@pytest.mark.parametrize(
    "mode,expected",
    [
        ("missing", "missing"),
        ("unavailable", "unavailable"),
        ("symlink", "unsafe"),
        ("writable", "unsafe"),
        ("changed", "changed"),
        ("disappeared", "changed"),
    ],
)
def test_missing_unsafe_and_changed_metadata_stays_explicit_without_body_reads(
    tmp_path: Path, mode: str, expected: str
) -> None:
    client = Client()
    if mode == "missing":
        client.report_stats.clear()
    elif mode == "unavailable":
        client.report_status[NAME] = 500
    elif mode == "symlink":
        client.report_stats[NAME]["mode"] = (1 << 27) | 0o777
        client.report_stats[NAME]["linkTarget"] = "/private/secret"
    elif mode == "writable":
        client.report_stats[NAME]["mode"] = 0o666
    elif mode == "changed":
        client.changed = True
    else:
        client.disappeared = True
    result = capture(tmp_path, client)
    report = result.crash_report_metadata[0]
    assert (
        report.state == expected
        and report.source_size_bytes is None
        and report.modified_at is None
    )
    assert PATH not in client.reads


def test_only_two_explicit_references_are_checked_and_extra_references_remain_visible(
    tmp_path: Path,
) -> None:
    client = Client()
    names = [f"kodi_crashlog-20261025_0130{second:02d}.log" for second in range(3)]
    client.files[LOG_PATHS["stdout"]] = "".join(
        "Crash report available at /home/kodi/" + name + "\n" for name in names
    ).encode()
    for name in names:
        client.report_stats[name] = {**client.report_stats[NAME], "name": name}
    result = capture(tmp_path, client)
    assert [report.basename for report in result.crash_report_metadata] == list(
        reversed(names)
    )[:2]
    assert result.crash_report_reference_limit_exceeded
    assert set(client.report_heads) == {"/home/kodi/" + name for name in names[1:]}


def test_reference_outside_retained_tail_is_not_discovered_by_reading_more_files(
    tmp_path: Path,
) -> None:
    client = Client()
    client.files[LOG_PATHS["stdout"]] = LINE.encode() + b"ordinary lines\n" * 1000
    result = capture(tmp_path, client)
    assert result.artifacts[2].tail_truncated
    assert not result.crash_report_metadata and not client.report_heads


def test_snapshot_schema_binds_reports_to_exact_retained_claims_and_keeps_older_reads(
    tmp_path: Path,
) -> None:
    result = capture(tmp_path, Client())
    serialized = result.model_dump(mode="json")
    change: dict[str, Any]
    for change in (
        {"crash_report_metadata": []},
        {"crash_report_reference_limit_exceeded": True},
        {"crash_report_discovery": "not_collected"},
    ):
        with pytest.raises(ValidationError):
            StoppedLogSnapshot.model_validate({**serialized, **change})
    for legacy in (False, True):
        prior = snapshot(legacy=legacy)
        payload = prior.model_dump(
            mode="json",
            exclude={
                "crash_report_metadata",
                "crash_report_discovery",
                "crash_report_reference_limit_exceeded",
                "unreferenced_crash_reports",
            },
        )
        restored = StoppedLogSnapshot.model_validate(payload)
        assert restored.schema_revision == (1 if legacy else 2)
        assert (
            restored.crash_report_discovery == "not_collected"
            and not restored.crash_report_metadata
        )


def test_metadata_rejects_bodies_unsafe_names_naive_time_and_wrong_sources() -> None:
    body = dict(
        basename=NAME, state="available", referenced_by=["stdout"], source_size_bytes=1
    )
    for change in (
        {"basename": "../secret"},
        {"body": "PRIVATE"},
        {"content": "PRIVATE"},
        {"modified_at": datetime(2026, 10, 25)},
        {"referenced_by": ["arbitrary_path"]},
    ):
        with pytest.raises(ValidationError):
            ReferencedCrashReportMetadata.model_validate({**body, **change})
    with pytest.raises(ValidationError):
        StoppedDiagnosticsInput.model_validate(
            {**operation().model_dump(), "path": PATH}
        )


def test_unstable_container_after_metadata_observation_refuses_the_entire_bundle(
    tmp_path: Path,
) -> None:
    client = Client()
    original = client.head

    def changed(*args: Any, **kwargs: Any) -> Response:
        result = original(*args, **kwargs)
        if len(client.report_heads) == 2:
            client.value["State"]["FinishedAt"] = "2026-10-25T08:35:00Z"
        return result

    client.head = changed  # type: ignore[method-assign]
    resolver, _ = setup(tmp_path)
    with pytest.raises(StoppedDiagnosticsError, match="identity_changed"):
        StoppedManagedLogProvider(lambda: client).capture(resolver.resolve(operation()))
