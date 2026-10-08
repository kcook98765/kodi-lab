"""Initial native credential failures retain the existing registration request."""

from __future__ import annotations

import json
import os
import stat
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import Engine, event, select
from sqlalchemy.exc import SQLAlchemyError

from kodi_lab.application.household_onboarding import HouseholdResumeRequest
from kodi_lab.storage.models import (
    ExternalKodiTargetRow,
    ExternalSecretRetirementRow,
    HouseholdOnboardingRow,
    TargetRow,
)
from tests.application.test_credential_publication import restart
from tests.application.test_household_onboarding import Environment
from tests.application.test_household_onboarding import env as _env
from tests.application.test_household_onboarding import live_application as _live

env = _env
live_application = _live


@pytest.mark.parametrize("boundary", ["missing_key", "unsafe_directory", "file_fsync"])
def test_native_storage_failure_recovers_original_registration(
    env: Environment, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    env.request = env.request.model_copy(
        update={"username": "fixture", "password": SecretStr("fixture-password")}
    )
    store = env.targets._credential_store
    dispatches: list[None] = []
    bootstrap = env.bridges._client.bootstrap_otp

    def counted(*args: Any, **kwargs: Any) -> Any:
        dispatches.append(None)
        return bootstrap(*args, **kwargs)

    monkeypatch.setattr(env.bridges._client, "bootstrap_otp", counted)
    reserved_target = None
    with monkeypatch.context() as failure:
        if boundary == "missing_key":
            failure.setattr(store, "_master_key", None)
        elif boundary == "unsafe_directory":
            store._root.mkdir(mode=0o700)
            store._root.chmod(0o750)
        else:
            fsync = os.fsync

            def fail_file_fsync(descriptor: int) -> None:
                if stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise OSError("injected native credential file fsync failure")
                fsync(descriptor)

            failure.setattr(os, "fsync", fail_file_fsync)

        def nonexistent_lookup(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError(
                "failed registration must not look up an absent target"
            )

        failure.setattr(env.targets, "get", nonexistent_lookup)
        for _ in range(2):
            result = env.start()
            assert result.state == "incomplete"
            assert result.code == (
                "persistence_failed"
                if boundary == "file_fsync"
                else "secret_unavailable"
            )
            assert result.operation_id == env.request.operation_id
            assert result.target_id is None
            assert "identity_mismatch" not in result.model_dump_json()
            assert env.request.password is not None
            assert (
                env.request.password.get_secret_value() not in result.model_dump_json()
            )
            with env.sessions() as session:
                operation = session.get(
                    HouseholdOnboardingRow, str(env.request.operation_id)
                )
                assert operation is not None and operation.stage == "reserved"
                assert operation.error_code is None
                assert operation.native_binding_digest is None
                assert operation.host == env.request.address
                assert operation.kodi_port == env.request.kodi_port
                reserved_target = reserved_target or operation.target_id
                assert operation.target_id == reserved_target
                assert session.scalars(select(TargetRow)).all() == []
                assert session.scalars(select(ExternalKodiTargetRow)).all() == []
                assert all(
                    row.target_id == reserved_target
                    for row in session.scalars(select(ExternalSecretRetirementRow))
                )
            assert dispatches == []
            assert json.loads(env.otp_file.read_text())["code"] == (
                env.request.code.get_secret_value()
            )
            assert (
                env.service.start(
                    env.request.model_copy(update={"operation_id": uuid4()})
                ).code
                == "endpoint_conflict"
            )
            resume = env.service.resume(
                env.request.operation_id, HouseholdResumeRequest()
            )
            assert resume.code == "registration_retry_required"
            assert resume.operation_id == env.request.operation_id
            assert resume.target_id is None

    if boundary == "unsafe_directory":
        store._root.chmod(0o700)
    assert reserved_target is not None
    restart(env)
    first = env.start()
    assert first.code == "transport_consent_required"
    assert first.operation_id == env.request.operation_id
    assert first.target_id == UUID(reserved_target)
    assert dispatches == []
    assert env.start().target_id == first.target_id
    result = env.connect(first)
    assert result.state == "connected", result
    assert result.operation_id == env.request.operation_id
    assert result.target_id == first.target_id
    assert len(dispatches) == 1
    with env.sessions() as session:
        assert len(session.scalars(select(HouseholdOnboardingRow)).all()) == 1
        assert len(session.scalars(select(TargetRow)).all()) == 1
        native = session.get(ExternalKodiTargetRow, reserved_target)
        assert native is not None and native.secret_reference is not None
        credential = store.read(UUID(reserved_target), UUID(native.secret_reference))
        assert credential.username == "fixture"
        assert credential.password == "fixture-password"


def test_native_registration_sql_failure_remains_reserved(
    env: Environment,
) -> None:
    env.request = env.request.model_copy(
        update={"username": "fixture", "password": SecretStr("fixture-password")}
    )
    engine = cast(Engine, env.sessions.kw["bind"])

    def fail_insert(*args: Any) -> None:
        if args[2].startswith("INSERT INTO external_kodi_targets"):
            raise SQLAlchemyError("injected native target publication failure")

    event.listen(engine, "before_cursor_execute", fail_insert)
    try:
        result = env.start()
        assert result.code == "persistence_failed" and result.state == "incomplete"
        with env.sessions() as session:
            operation = session.get(
                HouseholdOnboardingRow, str(env.request.operation_id)
            )
            assert operation is not None and operation.stage == "reserved"
            assert session.scalars(select(TargetRow)).all() == []
        assert json.loads(env.otp_file.read_text())["code"] == (
            env.request.code.get_secret_value()
        )
    finally:
        event.remove(engine, "before_cursor_execute", fail_insert)
    assert env.connect().state == "connected"


def test_blocking_error_tolerates_unregistered_target(
    env: Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.request = env.request.model_copy(
        update={"username": "fixture", "password": SecretStr("fixture-password")}
    )
    monkeypatch.setattr(env.targets._credential_store, "_master_key", None)
    assert env.start().state == "incomplete"
    with env.sessions() as session:
        operation = session.get(HouseholdOnboardingRow, str(env.request.operation_id))
        assert operation is not None

    def nonexistent_lookup(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("blocking an absent target must not retrieve it")

    monkeypatch.setattr(env.targets, "get", nonexistent_lookup)
    result = env.service._block(
        env.request.operation_id, operation, "onboarding_binding_changed"
    )
    assert result.code == "onboarding_binding_changed"
    assert result.state == "requires_action" and result.target_id is None
    with env.sessions() as session:
        persisted = session.get(HouseholdOnboardingRow, str(env.request.operation_id))
        assert persisted is not None and persisted.stage == "blocked"
