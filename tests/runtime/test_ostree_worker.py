"""Closed helper execution, bounds and cancellation; Docker calls are doubles."""

from __future__ import annotations

from pathlib import Path
from threading import Event
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from kodi_lab.config import Settings
from kodi_lab.runtime.ostree import OstreeAcquisitionError
from kodi_lab.runtime.ostree_worker import (
    STAGING_ROOT,
    HelperRunner,
    assemble_in_worker,
)
from kodi_lab.runtime.recipes import load_inactive_ostree_recipe


def helper(output: bytes = b"verified", code: int = 0) -> Any:
    calls: list[Any] = []

    def create(*args: Any, **kwargs: Any) -> dict[str, str]:
        calls.append((args, kwargs))
        return {"Id": "execution"}

    api = SimpleNamespace(
        exec_create=create,
        exec_start=lambda *args, **kwargs: iter([output]),
        exec_inspect=lambda *args: {"ExitCode": code},
    )
    return SimpleNamespace(
        client=SimpleNamespace(api=api),
        id="helper",
        kill=lambda: calls.append("kill"),
        calls=calls,
    )


def pull(stage: Path, index: int = 0) -> tuple[str, ...]:
    c = load_inactive_ostree_recipe().recipe.provider.components[index]
    return (
        "/usr/bin/ostree",
        f"--repo={stage / 'repo'}",
        "pull",
        "--depth=0",
        "--http-header=User-Agent=Kodi-Lab-reviewed-OSTree",
        "flathub-beta" if c.role == "app" else "flathub",
        f"{c.ref}@{c.commit}",
    )


@pytest.mark.parametrize("index", [0, 1, 2])
def test_helper_accepts_only_exact_pulls(
    index: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage = STAGING_ROOT / ("ostree-" + uuid4().hex)
    monkeypatch.setattr(
        "kodi_lab.runtime.ostree_worker._stage_quota", lambda path: None
    )
    h = helper()
    result = HelperRunner(h).run(pull(stage, index), stage=stage, timeout=1)
    assert result.returncode == 0 and result.output == b"verified"
    args, options = h.calls[0]
    assert args[0] == "helper"
    assert args[1][-7:] == list(pull(stage, index))
    assert options["workdir"] == str(stage)


@pytest.mark.parametrize("change", ["ref", "commit", "shell", "url", "repo", "path"])
def test_helper_rejects_changed_commands(change: str) -> None:
    stage = STAGING_ROOT / ("ostree-" + uuid4().hex)
    argv = list(pull(stage))
    if change == "ref":
        argv[-1] = argv[-1].replace("beta@", "stable@")
    if change == "commit":
        argv[-1] = argv[-1].split("@")[0] + "@" + "f" * 64
    if change == "shell":
        argv = ["/bin/sh", "-c", "evil"]
    if change == "url":
        argv[-2] = "https://attacker.invalid"
    if change == "repo":
        argv[1] = "--repo=/var/lib/kodi-lab"
    if change == "path":
        stage = Path("/tmp") / stage.name
    with pytest.raises(OstreeAcquisitionError):
        HelperRunner(helper()).execute(tuple(argv), stage=stage, timeout=1)


def test_helper_output_overflow_fails_and_kills(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stage = STAGING_ROOT / ("ostree-" + uuid4().hex)
    monkeypatch.setattr(
        "kodi_lab.runtime.ostree_worker._stage_quota", lambda path: None
    )
    h = helper(b"x" * (1024**2 + 1))
    with pytest.raises(OstreeAcquisitionError):
        HelperRunner(h).execute(pull(stage), stage=stage, timeout=1)
    assert "kill" in h.calls


def test_provider_cannot_run_from_arbitrary_native_worker_root(tmp_path: Path) -> None:
    with pytest.raises(OstreeAcquisitionError, match="contract"):
        assemble_in_worker(uuid4(), Settings(runtime_root=tmp_path), lambda: None)


@pytest.mark.parametrize("interrupted", [False, True])
def test_helper_timeout_or_interruption_reaps_execution(
    interrupted: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stopped = Event()
    h = helper()

    def stream(*args: Any, **kwargs: Any) -> Any:
        stopped.wait(timeout=2)
        return iter([b"stopped"])

    h.client.api.exec_start = stream
    h.kill = lambda: stopped.set()

    def quota(stage: Path) -> None:
        if interrupted:
            raise KeyboardInterrupt

    monkeypatch.setattr("kodi_lab.runtime.ostree_worker._stage_quota", quota)
    stage = STAGING_ROOT / ("ostree-" + uuid4().hex)
    with pytest.raises(KeyboardInterrupt if interrupted else OstreeAcquisitionError):
        HelperRunner(h).execute(pull(stage), stage=stage, timeout=0.01)
    assert stopped.is_set()


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("pinned_support_unavailable", 71),
        ("support_artifact_mismatch", 72),
        ("support_download_bound", 73),
        ("private unexpected text", 1),
    ],
)
def test_support_helper_transfers_only_closed_failure_categories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    code: str,
    expected: int,
) -> None:
    """Actual helper entrypoint, including its finally, never emits raw HTTP errors."""
    import os
    import stat
    import sys
    from email.message import Message
    from urllib.error import HTTPError

    from kodi_lab.runtime import ostree_worker as worker
    from kodi_lab.runtime.ubuntu_support import SupportError

    operation = uuid4()
    root = tmp_path / "staging"
    stage = root / ("ostree-" + operation.hex)
    stage.mkdir(parents=True, mode=0o700)
    monkeypatch.setattr(worker, "STAGING_ROOT", root)
    monkeypatch.setattr(sys, "argv", ["worker", str(operation)])
    monkeypatch.setattr(
        worker,
        "os",
        SimpleNamespace(
            fstat=lambda fd: SimpleNamespace(st_uid=0, st_mode=stat.S_IFDIR | 0o700),
            mkdir=os.mkdir,
            close=os.close,
        ),
    )

    def unavailable(*, revision: int = 2) -> dict[str, bytes]:
        try:
            raise HTTPError(
                "https://private.invalid/token", 404, "secret body", Message(), None
            )
        except HTTPError as error:
            raise SupportError(code) from error

    monkeypatch.setattr(worker, "fetch_support_packages", unavailable)
    assert worker.main() == expected
    assert capsys.readouterr() == ("", "")
    assert list((stage / "support").iterdir()) == []


@pytest.mark.parametrize("status", [1, 71, 72, 73, 255])
def test_support_failure_cannot_become_ready_or_inject_durable_error(
    status: int,
) -> None:
    from kodi_lab.runtime.install import _installation_failure_code
    from kodi_lab.runtime.ostree import CommandResult
    from kodi_lab.runtime.ostree_worker import (
        SUPPORT_FAILURE_EXITS,
        require_support_success,
    )

    with pytest.raises(OstreeAcquisitionError) as failure:
        require_support_success(
            CommandResult(status, b"secret arbitrary error or fake success")
        )
    assert str(failure.value) == SUPPORT_FAILURE_EXITS.get(
        status, "support_acquisition_failed"
    )
    assert _installation_failure_code(failure.value) == (
        SUPPORT_FAILURE_EXITS.get(status, "installation_failed")
    )


def test_support_classification_requires_type_and_follows_explicit_cause() -> None:
    from kodi_lab.runtime.install import _installation_failure_code

    assert (
        _installation_failure_code(ValueError("pinned_support_unavailable"))
        == "installation_failed"
    )
    cause = OstreeAcquisitionError("pinned_support_unavailable")
    wrapped = RuntimeError("private wrapper")
    wrapped.__cause__ = cause
    assert _installation_failure_code(wrapped) == "pinned_support_unavailable"


def test_display_support_helper_variant_is_exact_and_not_a_package_selector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operation = uuid4()
    stage = STAGING_ROOT / ("ostree-" + operation.hex)
    monkeypatch.setattr("kodi_lab.runtime.ostree_worker._stage_quota", lambda _: None)
    command = (
        "/usr/local/bin/python",
        "-m",
        "kodi_lab.runtime.ostree_worker",
        str(operation),
        "3",
    )
    h = helper()
    assert (
        HelperRunner(h, support_revision=3)
        .execute(command, stage=stage, timeout=1)
        .returncode
        == 0
    )
    assert h.calls[0][0][1][-5:] == list(command)
    with pytest.raises(OstreeAcquisitionError, match="unreviewed_helper_command"):
        HelperRunner(h).execute(command, stage=stage, timeout=1)
    with pytest.raises(OstreeAcquisitionError, match="unreviewed_helper_command"):
        HelperRunner(h, support_revision=3).execute(
            (*command[:-1], "arbitrary-package"), stage=stage, timeout=1
        )
    with pytest.raises(OstreeAcquisitionError, match="unsupported_support_revision"):
        HelperRunner(h, support_revision=4)
