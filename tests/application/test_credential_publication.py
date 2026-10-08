"""Real SQLite/AEAD fault boundaries for external credential ownership."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import Engine, event, select
from sqlalchemy.exc import SQLAlchemyError

from kodi_lab.application.external_bridges import ExternalBridgeService
from kodi_lab.application.household_onboarding import (
    HouseholdKodiOnboardingService,
    HouseholdResumeRequest,
)
from kodi_lab.domain import ExternalAdministrativeState, ExternalAuthenticationMode
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.storage.models import (
    ExternalBridgePairingRow,
    ExternalKodiTargetRow,
    ExternalSecretRetirementRow,
    HouseholdOnboardingRow,
    McpClientTargetGrantRow,
    McpProductionTargetScopeRow,
    TargetRow,
)
from kodi_lab.storage.secrets import SecretStoreError
from tests.application.test_household_onboarding import Environment
from tests.application.test_household_onboarding import env as _env
from tests.application.test_household_onboarding import live_application as _live

env = _env
live_application = _live


@contextmanager
def sql_failure(env: Environment, prefix: str) -> Iterator[None]:
    engine = cast(Engine, env.sessions.kw["bind"])

    def fail(*args: Any) -> None:
        if args[2].startswith(prefix):
            raise SQLAlchemyError("injected persistence boundary")

    event.listen(engine, "before_cursor_execute", fail)
    try:
        yield
    finally:
        event.remove(engine, "before_cursor_execute", fail)


def track_writes(env: Environment, monkeypatch: pytest.MonkeyPatch) -> list[UUID]:
    store = env.bridges._secrets
    write = store.write
    references: list[UUID] = []

    def tracked(*args: Any, **kwargs: Any) -> UUID:
        reference = kwargs["reference"]
        # Another SQLite connection sees the obligation BEFORE the file exists.
        with env.sessions() as session:
            assert (
                session.get(ExternalSecretRetirementRow, ("bridge", str(reference)))
                is not None
            )
        references.append(reference)
        return write(*args, **kwargs)

    monkeypatch.setattr(store, "write", tracked)
    return references


def fail_unlink(monkeypatch: pytest.MonkeyPatch, references: list[UUID]) -> None:
    unlink = os.unlink

    def failed(path: Any, *args: Any, **kwargs: Any) -> None:
        if str(path) in {f"{r}.json" for r in references}:
            raise OSError("injected secret unlink failure")
        unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", failed)


def restart(env: Environment) -> None:
    engine = cast(Engine, env.sessions.kw["bind"])
    url = engine.url
    engine.dispose()
    sessions = create_session_factory(create_database_engine(str(url)))
    env.sessions = sessions
    env.targets._session_factory = sessions
    env.admin._session_factory = sessions
    env.service._validation._sessions = sessions
    env.bridges = ExternalBridgeService(
        sessions,
        endpoint_policy=env.bridges._endpoint_policy,
        secret_store=env.bridges._secrets,
        client=env.bridges._client,
    )
    env.service = HouseholdKodiOnboardingService(
        sessions,
        targets=env.targets,
        bridges=env.bridges,
        validation=env.service._validation,
        administration=env.admin,
        lock_root=env.service._lock_root,
    )


def assert_no_authority(env: Environment, target_id: UUID) -> None:
    with env.sessions() as session:
        native = session.get(ExternalKodiTargetRow, str(target_id))
        assert native is not None
        assert native.administrative_state is ExternalAdministrativeState.DEACTIVATED
        assert native.secret_reference is None
        pairing = session.get(ExternalBridgePairingRow, str(target_id))
        assert pairing is None or pairing.bridge_secret_reference is None
        assert session.scalars(select(McpClientTargetGrantRow)).all() == []
        assert session.scalars(select(McpProductionTargetScopeRow)).all() == []


def test_original_publication_and_deletion_failure_survives_restart(
    env: Environment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = env.start()
    assert first.target_id is not None
    references = track_writes(env, monkeypatch)
    calls = 0
    bootstrap = env.bridges._client.bootstrap_otp

    def counted(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        return bootstrap(*args, **kwargs)

    monkeypatch.setattr(env.bridges._client, "bootstrap_otp", counted)
    unlink = os.unlink
    fail_unlink(monkeypatch, references)
    with sql_failure(env, "INSERT INTO external_bridge_pairings"):
        assert env.connect(first).code == "persistence_failed"
    assert len(references) == calls == 1
    reference = references[0]
    store = env.bridges._secrets
    assert len(store.read(first.target_id, reference, "request_authentication")) == 32
    for _ in range(2):
        restart(env)
        if _ == 0:
            assert (
                env.service.resume(
                    env.request.operation_id, HouseholdResumeRequest()
                ).code
                == "pairing_recovery_required"
            )
        assert (
            env.service.abandon(env.request.operation_id).code == "persistence_failed"
        )
        with env.sessions() as session:
            assert session.get(ExternalBridgePairingRow, str(first.target_id)) is None
            pending = session.get(
                ExternalSecretRetirementRow, ("bridge", str(reference))
            )
            assert pending is not None and pending.target_id == str(first.target_id)
            operation = session.get(
                HouseholdOnboardingRow, str(env.request.operation_id)
            )
            assert operation is not None and operation.stage == "bootstrap_started"
        assert_no_authority(env, first.target_id)
        assert (
            env.service.start(
                env.request.model_copy(update={"operation_id": uuid4()})
            ).code
            == "endpoint_conflict"
        )
        assert (
            len(store.read(first.target_id, reference, "request_authentication")) == 32
        )
    assert calls == 1
    monkeypatch.setattr(os, "unlink", unlink)
    restart(env)
    result = env.service.abandon(env.request.operation_id)
    assert result.code == "abandoned"
    assert env.service.abandon(env.request.operation_id) == result
    assert_no_authority(env, first.target_id)
    with env.sessions() as session:
        assert session.scalars(select(ExternalSecretRetirementRow)).all() == []
    with pytest.raises(SecretStoreError):
        store.read(first.target_id, reference, "request_authentication")
    assert (
        env.service.start(env.request.model_copy(update={"operation_id": uuid4()})).code
        == "transport_consent_required"
    )
    assert calls == 1


@pytest.mark.parametrize("boundary", ["pairing_row", "ownership_transfer"])
@pytest.mark.parametrize("delete_fails", [False, True])
def test_publication_rollback_never_loses_new_reference(
    env: Environment,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
    delete_fails: bool,
) -> None:
    first = env.start()
    assert first.target_id is not None
    references = track_writes(env, monkeypatch)
    if delete_fails:
        fail_unlink(monkeypatch, references)
    prefix = (
        "INSERT INTO external_bridge_pairings"
        if boundary == "pairing_row"
        else "DELETE FROM external_secret_retirements"
    )
    with sql_failure(env, prefix):
        assert env.connect(first).code == "persistence_failed"
    (reference,) = references
    with env.sessions() as session:
        assert session.get(ExternalBridgePairingRow, str(first.target_id)) is None
        pending = session.get(ExternalSecretRetirementRow, ("bridge", str(reference)))
        # Ownership-transfer failure also faults cleanup acknowledgement.
        assert (pending is not None) == (
            delete_fails or boundary == "ownership_transfer"
        )
    if delete_fails:
        assert (
            len(
                env.bridges._secrets.read(
                    first.target_id, reference, "request_authentication"
                )
            )
            == 32
        )
        assert (
            env.service.abandon(env.request.operation_id).code == "persistence_failed"
        )
    else:
        with pytest.raises(SecretStoreError):
            env.bridges._secrets.read(
                first.target_id, reference, "request_authentication"
            )
        assert env.service.abandon(env.request.operation_id).code == "abandoned"


def test_journal_registration_failure_creates_no_file(
    env: Environment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = env.start()
    references = track_writes(env, monkeypatch)
    with sql_failure(env, "INSERT INTO external_secret_retirements"):
        assert env.connect(first).code == "persistence_failed"
    assert references == []
    assert not list(env.bridges._secrets._root.glob("*.json"))
    assert (
        env.service.resume(env.request.operation_id, HouseholdResumeRequest()).code
        == "pairing_recovery_required"
    )
    assert env.service.abandon(env.request.operation_id).code == "abandoned"


def test_native_registration_failure_cleanup_without_target_row(
    env: Environment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env.request = env.request.model_copy(
        update={
            "username": "alice",
            "password": SecretStr("example-password"),
        }
    )
    store = env.targets._credential_store
    write = store.write
    references: list[UUID] = []

    def tracked(*args: Any, **kwargs: Any) -> UUID:
        references.append(kwargs["reference"])
        return write(*args, **kwargs)

    monkeypatch.setattr(store, "write", tracked)
    unlink = os.unlink
    fail_unlink(monkeypatch, references)
    with sql_failure(env, "INSERT INTO external_kodi_targets"):
        assert env.start().code == "persistence_failed"
    (reference,) = references
    with env.sessions() as session:
        operation = session.get(HouseholdOnboardingRow, str(env.request.operation_id))
        assert operation is not None and operation.stage == "reserved"
        target_id = UUID(operation.target_id)
        assert session.get(TargetRow, operation.target_id) is None
        assert (
            session.get(ExternalSecretRetirementRow, ("kodi", str(reference)))
            is not None
        )
    assert store.read(target_id, reference).username == "alice"
    restart(env)
    assert env.service.abandon(env.request.operation_id).code == "persistence_failed"
    monkeypatch.setattr(os, "unlink", unlink)
    assert env.service.abandon(env.request.operation_id).code == "abandoned"
    with pytest.raises(SecretStoreError):
        store.read(target_id, reference)


@pytest.mark.parametrize("rollback", [False, True])
def test_native_replacement_accounts_for_both_credentials(
    env: Environment,
    monkeypatch: pytest.MonkeyPatch,
    rollback: bool,
) -> None:
    env.request = env.request.model_copy(
        update={
            "username": "alice",
            "password": SecretStr("original-password"),
        }
    )
    first = env.start()
    assert first.target_id is not None
    env.targets.set_enabled(first.target_id, enabled=False)
    with env.sessions() as session:
        native = session.get(ExternalKodiTargetRow, str(first.target_id))
        assert native is not None and native.secret_reference is not None
        old = UUID(native.secret_reference)
    store = env.targets._credential_store
    write = store.write
    new: list[UUID] = []

    def tracked(*args: Any, **kwargs: Any) -> UUID:
        new.append(kwargs["reference"])
        return write(*args, **kwargs)

    monkeypatch.setattr(store, "write", tracked)
    fail_unlink(monkeypatch, new if rollback else [old])
    if rollback:
        with sql_failure(env, "UPDATE external_kodi_targets"), pytest.raises(OSError):
            env.targets.rotate_credentials(
                first.target_id,
                authentication_mode=ExternalAuthenticationMode.USERNAME_PASSWORD,
                username="alice",
                password="replacement-password",
            )
    else:
        with pytest.raises(OSError):
            env.targets.rotate_credentials(
                first.target_id,
                authentication_mode=ExternalAuthenticationMode.USERNAME_PASSWORD,
                username="alice",
                password="replacement-password",
            )
    (reference,) = new
    with env.sessions() as session:
        native = session.get(ExternalKodiTargetRow, str(first.target_id))
        assert native is not None
        assert native.secret_reference == str(old if rollback else reference)
        pending = session.scalars(select(ExternalSecretRetirementRow)).all()
        assert [(row.store_kind, row.reference) for row in pending] == [
            ("kodi", str(reference if rollback else old))
        ]
    assert store.read(first.target_id, old).password == "original-password"
    assert store.read(first.target_id, reference).password == "replacement-password"


@pytest.mark.parametrize("request_credential", [False, True])
def test_bridge_replacement_keeps_displaced_reference_until_deletion(
    env: Environment,
    monkeypatch: pytest.MonkeyPatch,
    request_credential: bool,
) -> None:
    first = env.start()
    assert first.target_id is not None
    if request_credential:

        def interrupted(*args: Any, **kwargs: Any) -> None:
            raise SQLAlchemyError("interrupted connected publication")

        monkeypatch.setattr(env.service, "_complete", interrupted)
        assert env.connect(first).code == "persistence_failed"
        env.bridges.acknowledge_production_http_risk(
            first.target_id,
            bridge_port=env.request.bridge_port,
        )
    else:
        env.bridges.acknowledge_production_http_risk(
            first.target_id,
            bridge_port=env.request.bridge_port,
        )
        env.bridges.generate_pairing_token(
            first.target_id,
            bridge_port=env.request.bridge_port,
        )
    with env.sessions() as session:
        pairing = session.get(ExternalBridgePairingRow, str(first.target_id))
        assert pairing is not None
        value = (
            pairing.bridge_secret_reference
            if request_credential
            else pairing.pairing_token_reference
        )
        assert value is not None
        old = UUID(value)
    new = track_writes(env, monkeypatch)
    unlink = os.unlink
    fail_unlink(monkeypatch, [old])
    with pytest.raises(OSError):
        env.bridges.generate_pairing_token(
            first.target_id, bridge_port=env.request.bridge_port
        )
    (reference,) = new
    with env.sessions() as session:
        pairing = session.get(ExternalBridgePairingRow, str(first.target_id))
        assert pairing is not None and pairing.pairing_token_reference == str(reference)
        assert pairing.bridge_secret_reference is None
        assert (
            session.get(ExternalSecretRetirementRow, ("bridge", str(old))) is not None
        )
        assert (
            session.get(ExternalSecretRetirementRow, ("bridge", str(reference))) is None
        )
    restart(env)
    assert env.service.abandon(env.request.operation_id).code == "persistence_failed"
    monkeypatch.setattr(os, "unlink", unlink)
    assert env.service.abandon(env.request.operation_id).code == "abandoned"
    for retired, purpose in (
        (old, "request_authentication" if request_credential else "pairing_token"),
        (reference, "pairing_token"),
    ):
        with pytest.raises(SecretStoreError):
            env.bridges._secrets.read(first.target_id, retired, purpose)
    assert_no_authority(env, first.target_id)


def test_cleanup_retries_never_restore_revoked_scope_or_client_grant(
    env: Environment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kodi_lab.application.production_inspection import PRODUCTION_VISUAL_PRESET_ID

    def interrupted(*args: Any, **kwargs: Any) -> None:
        raise SQLAlchemyError("interrupted connected publication")

    monkeypatch.setattr(env.service, "_complete", interrupted)
    first = env.start()
    assert first.target_id is not None
    assert env.connect(first).code == "persistence_failed"
    client = env.admin.create_client(
        display_name="Historical scope fixture", preset_id=PRODUCTION_VISUAL_PRESET_ID
    )
    env.admin.grant_target(client.id, first.target_id)
    scope_id = uuid4()
    stamp = "2026-10-08T00:00:00.000000Z"
    with env.sessions.begin() as session:
        session.add(
            McpProductionTargetScopeRow(
                id=str(scope_id),
                client_id=str(client.id),
                target_id=str(first.target_id),
                identity_generation=1,
                bridge_trust_revision=None,
                scope="inspect",
                state="revoked",
                created_at=stamp,
                created_by="test_admin",
                revoked_at=stamp,
                revoked_by="test_admin",
            )
        )
        pairing = session.get(ExternalBridgePairingRow, str(first.target_id))
        assert pairing is not None and pairing.bridge_secret_reference is not None
        reference = UUID(pairing.bridge_secret_reference)
    unlink = os.unlink
    fail_unlink(monkeypatch, [reference])
    # Drain Bridge cleanup directly once to put the target's authority in the
    # revoked state. Abandonment then completes native retirement independently.
    with pytest.raises(OSError):
        env.bridges.revoke(first.target_id)
    env.targets.deactivate(first.target_id)
    for _ in range(2):
        restart(env)
        assert (
            env.service.abandon(env.request.operation_id).code == "persistence_failed"
        )
        with env.sessions() as session:
            assert session.scalars(select(McpClientTargetGrantRow)).all() == []
            scope = session.get(McpProductionTargetScopeRow, str(scope_id))
            assert scope is not None and scope.state == "revoked"
            assert scope.revoked_at == stamp and scope.revoked_by == "test_admin"
            pairing = session.get(ExternalBridgePairingRow, str(first.target_id))
            assert pairing is not None and pairing.pairing_state.value == "revoked"
            assert pairing.bridge_secret_reference is None
    monkeypatch.setattr(os, "unlink", unlink)
    assert env.service.abandon(env.request.operation_id).code == "abandoned"
    with env.sessions() as session:
        assert session.scalars(select(McpClientTargetGrantRow)).all() == []
        scope = session.get(McpProductionTargetScopeRow, str(scope_id))
        assert scope is not None and scope.state == "revoked"


def test_generation_failure_creates_no_stored_credential_or_bootstrap(
    env: Environment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = env.start()
    urandom = os.urandom
    references = track_writes(env, monkeypatch)
    bootstrap = env.bridges._client.bootstrap_otp
    calls = 0

    def counted(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        return bootstrap(*args, **kwargs)

    monkeypatch.setattr(env.bridges._client, "bootstrap_otp", counted)

    def fail_random(size: int) -> bytes:
        if size == 32:
            raise OSError("injected credential generation failure")
        return urandom(size)

    monkeypatch.setattr(os, "urandom", fail_random)
    assert env.connect(first).code == "persistence_failed"
    assert references == [] and calls == 0
    with env.sessions() as session:
        assert session.scalars(select(ExternalSecretRetirementRow)).all() == []
    assert env.otp_file.exists()
    assert env.service.abandon(env.request.operation_id).code == "abandoned"


def test_deactivation_before_materialization_cannot_create_new_credential(
    env: Environment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = env.start()
    assert first.target_id is not None
    references = track_writes(env, monkeypatch)
    engine = cast(Engine, env.sessions.kw["bind"])
    journaled = False

    def registered(*args: Any) -> None:
        nonlocal journaled
        if args[2].startswith("INSERT INTO external_secret_retirements"):
            journaled = True

    def deactivate_after_commit(session: Any) -> None:
        nonlocal journaled
        if journaled:
            journaled = False
            env.targets.deactivate(cast(UUID, first.target_id))

    event.listen(engine, "after_cursor_execute", registered)
    event.listen(env.sessions, "after_commit", deactivate_after_commit)
    try:
        result = env.connect(first)
        assert result.state != "connected"
    finally:
        event.remove(engine, "after_cursor_execute", registered)
        event.remove(env.sessions, "after_commit", deactivate_after_commit)
    assert references == []
    assert not env.bridges._secrets._root.exists()
    with env.sessions() as session:
        assert session.scalars(select(ExternalSecretRetirementRow)).all() == []
        assert session.get(ExternalBridgePairingRow, str(first.target_id)) is None
    assert env.service.abandon(env.request.operation_id).code == "abandoned"
    assert_no_authority(env, first.target_id)


@pytest.mark.parametrize("expired", [False, True])
def test_historical_token_failure_retains_cleanup_obligation(
    env: Environment,
    monkeypatch: pytest.MonkeyPatch,
    expired: bool,
) -> None:
    first = env.start()
    assert first.target_id is not None
    env.bridges.acknowledge_production_http_risk(
        first.target_id,
        bridge_port=env.request.bridge_port,
    )
    env.bridges.generate_pairing_token(
        first.target_id, bridge_port=env.request.bridge_port
    )
    with env.sessions.begin() as session:
        pairing = session.get(ExternalBridgePairingRow, str(first.target_id))
        assert pairing is not None and pairing.pairing_token_reference is not None
        reference = UUID(pairing.pairing_token_reference)
        if expired:
            pairing.pairing_token_expires_at = "2020-01-01T00:00:00.000000Z"
    unlink = os.unlink
    fail_unlink(monkeypatch, [reference])
    with pytest.raises(OSError):
        if expired:
            env.bridges.attempt_pairing(first.target_id)
        else:
            env.bridges._pairing_failed(first.target_id, "bridge_authentication_failed")
    with env.sessions() as session:
        pairing = session.get(ExternalBridgePairingRow, str(first.target_id))
        assert pairing is not None and pairing.pairing_token_reference is None
        assert (
            session.get(ExternalSecretRetirementRow, ("bridge", str(reference)))
            is not None
        )
    restart(env)
    assert env.service.abandon(env.request.operation_id).code == "persistence_failed"
    monkeypatch.setattr(os, "unlink", unlink)
    assert env.service.abandon(env.request.operation_id).code == "abandoned"
    with pytest.raises(SecretStoreError):
        env.bridges._secrets.read(first.target_id, reference, "pairing_token")
