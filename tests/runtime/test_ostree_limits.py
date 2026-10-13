"""Actual subprocess limits and filesystem rejection, without runtime execution."""

import os
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

from kodi_lab.runtime import ostree
from kodi_lab.runtime.ostree import (
    BoundedCommandRunner,
    OstreeAcquisitionError,
    _cleanup_owned_gpg_agents,
    _read_file,
    trusted_key_bytes,
)


@pytest.fixture
def stage(tmp_path: Path) -> Path:
    for name in ("home", "gnupg", "tmp"):
        (tmp_path / name).mkdir(mode=0o700)
    return tmp_path


def test_unreviewed_executable_is_never_started(stage: Path) -> None:
    with pytest.raises(OstreeAcquisitionError, match="unreviewed_tool"):
        BoundedCommandRunner().run(
            ("/bin/sh", "-c", "touch forbidden"), stage=stage, timeout=5
        )
    assert not (stage / "forbidden").exists()


def test_real_subprocess_timeout_cancels_and_reaps(stage: Path) -> None:
    with pytest.raises(OstreeAcquisitionError, match="acquisition_timeout"):
        BoundedCommandRunner().run(
            ("/usr/bin/gpg", "--batch", "--no-options", "--gen-random", "0"),
            stage=stage,
            timeout=0.000001,
        )


def test_real_subprocess_output_is_bounded(
    stage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ostree, "MAX_OUTPUT_BYTES", 4096)
    with pytest.raises(OstreeAcquisitionError, match="command_output_exceeded"):
        BoundedCommandRunner().run(
            ("/usr/bin/gpg", "--batch", "--no-options", "--gen-random", "0", "65536"),
            stage=stage,
            timeout=5,
        )


def test_staged_file_with_wrong_owner_is_rejected(
    stage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    file = stage / "sample"
    file.write_bytes(b"data")
    actual_uid = os.geteuid()
    monkeypatch.setattr(os, "geteuid", lambda: actual_uid + 1)
    with pytest.raises(OstreeAcquisitionError, match="unsafe_staged_file"):
        _read_file(file)


def test_trust_resource_tamper_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    from importlib import resources

    class Changed:
        def joinpath(self, *_parts: str) -> "Changed":
            return self

        def read_bytes(self) -> bytes:
            return b'{"key_base64":"attacker"}'

    monkeypatch.setattr(resources, "files", lambda _name: Changed())
    with pytest.raises(OstreeAcquisitionError, match="trust_resource_mismatch"):
        trusted_key_bytes()


def test_real_detached_agent_cleanup_preserves_other_namespace(tmp_path: Path) -> None:
    owned = tmp_path / f"ostree-{uuid4().hex}"
    protected = tmp_path / f"ostree-{uuid4().hex}"
    try:
        for directory in (owned, protected):
            directory.mkdir(mode=0o700)
            home = directory / "gnupg"
            home.mkdir(mode=0o700)
            result = subprocess.run(
                (
                    "/usr/bin/gpg-agent",
                    "--homedir",
                    str(home),
                    "--daemon",
                    "--disable-scdaemon",
                ),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )
            assert result.returncode == 0
        assert len(_cleanup_owned_gpg_agents(owned)) == 1
        assert _cleanup_owned_gpg_agents(owned) == ()
        # The other agent is still running and is stopped by its own owner.
        assert len(_cleanup_owned_gpg_agents(protected)) == 1
    finally:
        _cleanup_owned_gpg_agents(owned)
        _cleanup_owned_gpg_agents(protected)


@pytest.mark.parametrize(
    "scope", [Path("/"), Path("/home/kodi/.kodi"), Path("relative")]
)
def test_agent_cleanup_rejects_unowned_scope(scope: Path) -> None:
    with pytest.raises(OstreeAcquisitionError, match="unsafe_agent_cleanup_scope"):
        _cleanup_owned_gpg_agents(scope)
