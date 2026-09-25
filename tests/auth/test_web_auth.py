from __future__ import annotations

import getpass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab import admin_cli
from kodi_lab.auth.passwords import PasswordService
from kodi_lab.auth.service import (
    SESSION_ABSOLUTE_LIFETIME,
    SESSION_IDLE_LIFETIME,
    LoginThrottler,
    WebAuthenticationService,
)
from kodi_lab.storage.engine import create_session_factory
from kodi_lab.storage.metadata import Base
from kodi_lab.storage.models import WebAdminCredentialRow, WebSessionRow


class MutableClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 9, 15, tzinfo=UTC)
        self.monotonic = 0.0

    def utc(self) -> datetime:
        return self.now

    def seconds(self) -> float:
        return self.monotonic


@pytest.fixture
def auth_factory(tmp_path: Path) -> sessionmaker[Session]:
    engine = create_engine(f"sqlite:///{tmp_path / 'auth.db'}")
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


def test_password_set_login_and_storage_are_one_way(
    auth_factory: sessionmaker[Session],
) -> None:
    service = WebAuthenticationService(auth_factory)
    service.set_password("correct horse battery staple")

    with auth_factory() as session:
        credential = session.get(WebAdminCredentialRow, "local-admin")
        assert credential is not None
        assert credential.password_hash.startswith("$argon2id$")
        assert "correct horse" not in credential.password_hash

    assert service.login("wrong password") is None
    created = service.login("correct horse battery staple")
    assert created is not None
    assert len(created.token) == 43
    with auth_factory() as session:
        stored = session.scalar(select(WebSessionRow))
        assert stored is not None
        assert stored.token_digest != created.token
        assert len(stored.token_digest) == 64
        assert service.token_digest(created.token) == stored.token_digest


def test_no_credentials_configured_still_performs_dummy_verification(
    auth_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    passwords = PasswordService()
    calls = 0
    original = passwords.verify

    def verify(password: str, password_hash: str | None) -> bool:
        nonlocal calls
        calls += 1
        assert password_hash is None
        return original(password, password_hash)

    monkeypatch.setattr(passwords, "verify", verify)
    service = WebAuthenticationService(auth_factory, passwords=passwords)
    assert service.login("guess") is None
    assert calls == 1


def test_idle_and_absolute_expiration_delete_sessions(
    auth_factory: sessionmaker[Session],
) -> None:
    clock = MutableClock()
    service = WebAuthenticationService(auth_factory, now=clock.utc)
    service.set_password("password")
    idle = service.login("password")
    assert idle is not None
    clock.now += SESSION_IDLE_LIFETIME
    assert service.resolve(idle.token) is None

    absolute = service.login("password")
    assert absolute is not None
    for _ in range(13):
        clock.now += timedelta(hours=11)
        assert service.resolve(absolute.token) is not None
    clock.now = absolute.authenticated.created_at + SESSION_ABSOLUTE_LIFETIME
    assert service.resolve(absolute.token) is None

    with auth_factory() as session:
        assert session.scalar(select(WebSessionRow)) is None


def test_logout_and_password_change_invalidate_sessions(
    auth_factory: sessionmaker[Session],
) -> None:
    service = WebAuthenticationService(auth_factory)
    service.set_password("first")
    first = service.login("first")
    assert first is not None
    service.logout(first.token)
    assert service.resolve(first.token) is None

    old = service.login("first")
    assert old is not None
    service.set_password("second")
    assert service.resolve(old.token) is None
    assert service.login("first") is None
    assert service.login("second") is not None


def test_malformed_cookie_tokens_fail_closed(
    auth_factory: sessionmaker[Session],
) -> None:
    service = WebAuthenticationService(auth_factory)
    service.set_password("password")
    assert service.resolve("é" * 43) is None
    assert service.resolve("." * 43) is None
    service.logout("é" * 43)


def test_csrf_is_bound_to_its_session(auth_factory: sessionmaker[Session]) -> None:
    service = WebAuthenticationService(auth_factory)
    service.set_password("password")
    first = service.login("password")
    second = service.login("password")
    assert first is not None and second is not None
    resolved = service.resolve(first.token)
    assert resolved is not None
    assert service.csrf_matches(resolved, first.authenticated.csrf_token)
    assert not service.csrf_matches(resolved, second.authenticated.csrf_token)


def test_login_throttler_refuses_hash_work_after_repeated_failures() -> None:
    clock = MutableClock()
    throttler = LoginThrottler(clock=clock.seconds)
    for _ in range(4):
        assert throttler.allowed("127.0.0.1")
        throttler.record_failure("127.0.0.1")
    throttler.record_failure("127.0.0.1")
    assert not throttler.allowed("127.0.0.1")
    clock.monotonic += 2.0
    assert throttler.allowed("127.0.0.1")
    throttler.record_success("127.0.0.1")
    assert throttler.allowed("127.0.0.1")


def test_local_bootstrap_prompts_hidden_and_invalidates_sessions(
    auth_factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_path = tmp_path / "auth.db"
    (tmp_path / "database").mkdir()
    (tmp_path / "database" / "kodi-lab.db").touch()
    answers = iter(["replacement", "replacement"])
    monkeypatch.setattr(getpass, "getpass", lambda _prompt: next(answers))
    monkeypatch.setenv("KODI_LAB_DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(
        admin_cli,
        "create_database_engine",
        lambda _url: create_engine(f"sqlite:///{database_path}"),
    )

    assert admin_cli.main(["set-password"]) == 0
    output = capsys.readouterr().out
    assert "replacement" not in output
    assert WebAuthenticationService(auth_factory).login("replacement") is not None
