"""Interactive retries preserve credentials until a valid hidden pair is accepted."""

from __future__ import annotations

import errno
import getpass
import os
import pty
import select
import subprocess
import sys
import termios
import time
import warnings
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import select as sql_select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab import admin_cli
from kodi_lab.auth.service import WebAuthenticationService
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.storage.metadata import Base
from kodi_lab.storage.models import WebAdminCredentialRow, WebSessionRow


@pytest.fixture
def password_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[sessionmaker[Session]]:
    (tmp_path / "database").mkdir()
    engine = create_database_engine(
        f"sqlite:///{tmp_path / 'database' / 'kodi-lab.db'}"
    )
    Base.metadata.create_all(engine)
    monkeypatch.setenv("KODI_LAB_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("KODI_LAB_DATABASE_URL", str(engine.url))
    yield create_session_factory(engine)
    engine.dispose()


@pytest.mark.parametrize(
    "answers,feedback",
    [
        (["accepted", "accepted"], ""),
        (["typo", "different", "accepted", "accepted"], "do not match"),
        (["", "", "accepted", "accepted"], "must not be empty"),
        (["x" * 1025, "x" * 1025, "accepted", "accepted"], "too long"),
        (["é secret", "é secret"], ""),
    ],
)
def test_retry_accepts_one_update(
    password_state: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    answers: list[str],
    feedback: str,
) -> None:
    service = WebAuthenticationService(password_state)
    service.set_password("original")
    old = service.login("original")
    assert old is not None
    supplied = iter(answers)
    monkeypatch.setattr(getpass, "getpass", lambda _: next(supplied))
    updates = []
    original = WebAuthenticationService.set_password

    def update(self: WebAuthenticationService, password: str) -> None:
        original(self, password)
        updates.append(True)

    monkeypatch.setattr(WebAuthenticationService, "set_password", update)
    assert admin_cli.main(["set-password"]) == 0
    assert updates == [True]
    output = capsys.readouterr()
    assert feedback in output.err
    for secret in set(answers) - {""}:
        assert secret not in output.out + output.err
    assert service.resolve(old.token) is None
    assert service.login(answers[-1]) is not None


@pytest.mark.parametrize("ending", ["exhausted", "empty", "eof", "cancel", "no-hidden"])
@pytest.mark.parametrize("prompt_index", [0, 1])
def test_unsuccessful_input_preserves_credentials_and_sessions(
    password_state: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    ending: str,
    prompt_index: int,
) -> None:
    service = WebAuthenticationService(password_state)
    service.set_password("original")
    old = service.login("original")
    assert old is not None
    with password_state() as session:
        before = session.get(WebAdminCredentialRow, "local-admin")
        assert before is not None
        digest = before.password_hash
    calls = []

    def prompt(_: str) -> str:
        calls.append(True)
        if len(calls) > prompt_index and ending in {"eof", "cancel", "no-hidden"}:
            if ending == "no-hidden":
                warnings.warn(
                    "hidden input unavailable", getpass.GetPassWarning, stacklevel=2
                )
                pytest.fail("must stop before getpass's echoed fallback")
            raise EOFError if ending == "eof" else KeyboardInterrupt
        return "" if ending == "empty" else str(len(calls))

    monkeypatch.setattr(getpass, "getpass", prompt)
    with pytest.raises(SystemExit) as result:
        admin_cli.main(["set-password"])
    assert result.value.code == 2
    assert len(calls) == (6 if ending in {"empty", "exhausted"} else prompt_index + 1)
    with password_state() as session:
        after = session.get(WebAdminCredentialRow, "local-admin")
        assert after is not None and after.password_hash == digest
        assert session.scalar(sql_select(WebSessionRow)) is not None
    assert service.resolve(old.token) is not None
    assert "Traceback" not in capsys.readouterr().err


def test_unrelated_database_failure_is_not_retried(
    password_state: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    prompts = []

    def prompt(_: str) -> str:
        prompts.append(True)
        return "accepted"

    def fail(self: WebAuthenticationService, password: str) -> None:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(getpass, "getpass", prompt)
    monkeypatch.setattr(WebAuthenticationService, "set_password", fail)
    with pytest.raises(RuntimeError, match="database unavailable"):
        admin_cli.main(["set-password"])
    assert len(prompts) == 2


def test_real_terminal_hides_both_attempts(
    password_state: sessionmaker[Session],
) -> None:
    master, slave = pty.openpty()
    transcript = bytearray()
    process = subprocess.Popen(
        [sys.executable, "-m", "kodi_lab.admin_cli", "set-password"],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        start_new_session=True,
        env=os.environ.copy(),
    )
    try:
        for prompt, answer in [
            (b"New Kodi Lab administrator password: ", b"first-pty-secret"),
            (b"Confirm password: ", b"typo-pty-secret"),
            (b"New Kodi Lab administrator password: ", b"final-pty-secret"),
            (b"Confirm password: ", b"final-pty-secret"),
        ]:
            window = bytearray()
            deadline = time.monotonic() + 15
            while prompt not in window:
                assert time.monotonic() < deadline, "password prompt timed out"
                if select.select([master], [], [], 0.1)[0]:
                    chunk = os.read(master, 4096)
                    window.extend(chunk)
                    transcript.extend(chunk)
            assert not termios.tcgetattr(slave)[3] & termios.ECHO
            os.write(master, answer + b"\n")
        assert process.wait(timeout=15) == 0
        os.close(slave)
        slave = -1
        while select.select([master], [], [], 0.1)[0]:
            try:
                chunk = os.read(master, 4096)
            except OSError as error:
                assert error.errno == errno.EIO
                break
            if not chunk:
                break
            transcript.extend(chunk)
        assert b"do not match" in transcript
        assert b"password updated" in transcript
        assert b"pty-secret" not in transcript
        assert WebAuthenticationService(password_state).login("final-pty-secret")
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        os.close(master)
        if slave != -1:
            os.close(slave)
