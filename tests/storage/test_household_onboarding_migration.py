import hashlib
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import event, inspect, select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from kodi_lab.application.external_bridges import ExternalBridgeService
from kodi_lab.application.household_onboarding import (
    HouseholdKodiOnboardingService,
    HouseholdResumeRequest,
)
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.storage.models import (
    ExternalBridgePairingRow,
    ExternalSecretRetirementRow,
    HouseholdOnboardingRow,
    ReviewedBridgeBuildRow,
)
from kodi_lab.storage.secrets import SecretStoreError
from tests.application.test_household_onboarding import (
    Environment,
)
from tests.application.test_household_onboarding import (
    env as _environment,
)
from tests.application.test_household_onboarding import (
    live_application as _base_live_application,
)
from tests.storage.test_migrations import (  # noqa: F401
    migration_setup,
    migration_templates,
)

legacy_application = _base_live_application


@pytest.fixture
def live_application(
    legacy_application: tuple[Any, ...], monkeypatch: pytest.MonkeyPatch
) -> tuple[Any, ...]:
    # Revision 0071 predates log_read. Model a reviewed legacy-capability Bridge
    # here; the real current Bridge is exercised against revision 0072 elsewhere.
    bridge = legacy_application[3]
    legacy = {
        "CAPABILITIES": [c for c in bridge["CAPABILITIES"] if c != "log_read"],
        "OPERATIONS": bridge["OPERATIONS"] - {"log_read"},
        "BUILD_FINGERPRINT": hashlib.sha256(
            b"household-migration-legacy-capability-test-double"
        ).hexdigest(),
    }
    for name, value in legacy.items():
        monkeypatch.setitem(bridge, name, value)
        monkeypatch.setitem(bridge["_identity"].__globals__, name, value)
    return legacy_application


env = _environment
REVISION = "0071_household_onboarding"
PREDECESSOR = "0070_external_screenshot_preference"


def test_upgrade_empty_downgrade_and_schema_match(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config, url = migration_setup(monkeypatch, tmp_path, start_revision=PREDECESSOR)
    engine = create_database_engine(url)
    command.upgrade(config, REVISION)
    assert set(inspect(engine).get_table_names()) >= {"household_onboarding_operations"}
    columns = {
        column["name"]
        for column in inspect(engine).get_columns("household_onboarding_operations")
    }
    assert columns == set(HouseholdOnboardingRow.__table__.columns.keys())
    command.downgrade(config, PREDECESSOR)
    assert "household_onboarding_operations" not in inspect(engine).get_table_names()
    command.upgrade(config, REVISION)
    engine.dispose()


def test_real_coordinator_on_migrated_schema_and_populated_downgrade_refusal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    env: Environment,
) -> None:
    config, url = migration_setup(monkeypatch, tmp_path, start_revision=PREDECESSOR)
    command.upgrade(config, REVISION)
    engine = create_database_engine(url)
    sessions = create_session_factory(engine)
    with env.sessions() as session:
        build = session.scalar(select(ReviewedBridgeBuildRow))
        assert build is not None
        values = {
            column.name: getattr(build, column.name)
            for column in ReviewedBridgeBuildRow.__table__.columns
        }
    with sessions.begin() as session:
        session.add(ReviewedBridgeBuildRow(**values))
    env.sessions = sessions
    env.service._sessions = sessions
    env.targets._session_factory = sessions
    env.bridges._sessions = sessions
    env.admin._session_factory = sessions
    env.service._validation._sessions = sessions
    first = env.start()
    with sessions() as session:
        operation = session.get(HouseholdOnboardingRow, str(env.request.operation_id))
        assert operation is not None and operation.stage == "native_verified"
        binding = operation.native_binding_digest
        assert binding is not None and len(binding) == 64
    env.service = HouseholdKodiOnboardingService(
        sessions,
        targets=env.targets,
        bridges=env.bridges,
        validation=env.service._validation,
        administration=env.admin,
        lock_root=env.service._lock_root,
    )
    result = env.connect(first)
    assert result.state == "connected", result
    with sessions() as session:
        operation = session.get(HouseholdOnboardingRow, str(env.request.operation_id))
        assert operation is not None and operation.native_binding_digest == binding
    assert (
        env.service.resume(env.request.operation_id, HouseholdResumeRequest()).state
        == "connected"
    )
    with pytest.raises(RuntimeError, match="cannot be safely downgraded"):
        command.downgrade(config, PREDECESSOR)
    with engine.connect() as connection:
        assert (
            connection.scalar(text("SELECT version_num FROM alembic_version"))
            == REVISION
        )
        assert (
            connection.scalar(text("SELECT stage FROM household_onboarding_operations"))
            == "connected"
        )
    engine.dispose()


def test_migrated_endpoint_reservation_and_independent_registration_guard(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config, url = migration_setup(monkeypatch, tmp_path, start_revision=PREDECESSOR)
    command.upgrade(config, REVISION)
    engine = create_database_engine(url)
    sessions = create_session_factory(engine)
    values: dict[str, Any] = dict(
        id=str(uuid4()),
        target_id=str(uuid4()),
        name="Kodi",
        host="192.168.50.40",
        kodi_port=8080,
        bridge_port=8765,
        request_digest="a" * 64,
        stage="reserved",
        error_code=None,
        disclosure_token=None,
        created_by="web_admin",
        created_at="2026-10-07T00:00:00.000000Z",
        updated_at="2026-10-07T00:00:00.000000Z",
    )
    with sessions.begin() as session:
        session.add(HouseholdOnboardingRow(**values))
    with pytest.raises(IntegrityError), sessions.begin() as session:
        session.add(
            HouseholdOnboardingRow(
                **{**values, "id": str(uuid4()), "target_id": str(uuid4())}
            )
        )
    with engine.connect() as connection:
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM sqlite_master WHERE type='trigger' "
                    "AND name LIKE 'trg_household_endpoint_%'"
                )
            )
            == 2
        )
    from tests.storage.test_screenshot_preference_migration import _register_household

    # The existing target-administration service reaches the durable SQL guard.
    with sessions.begin() as session:
        row = session.get(HouseholdOnboardingRow, values["id"])
        assert row is not None
        row.host = "127.0.0.1"
    with pytest.raises(IntegrityError, match="household_endpoint_reserved"):
        _register_household(sessions, tmp_path)
    engine.dispose()


def test_pending_retirement_constraints_and_downgrade_refusal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config, url = migration_setup(monkeypatch, tmp_path, start_revision=PREDECESSOR)
    command.upgrade(config, REVISION)
    engine = create_database_engine(url)
    sessions = create_session_factory(engine)
    reference, target = str(uuid4()), str(uuid4())
    with sessions.begin() as session:
        session.add(
            ExternalSecretRetirementRow(
                store_kind="bridge",
                reference=reference,
                target_id=target,
                created_at="2026-10-07T00:00:00.000000Z",
            )
        )
    assert {
        c["name"] for c in inspect(engine).get_columns("external_secret_retirements")
    } == set(ExternalSecretRetirementRow.__table__.columns.keys())
    operation_id = str(uuid4())
    with sessions.begin() as session:
        session.add(
            HouseholdOnboardingRow(
                id=operation_id,
                target_id=target,
                name="Kodi",
                host="192.168.50.40",
                kodi_port=8080,
                bridge_port=8765,
                request_digest="a" * 64,
                stage="reserved",
                created_by="web_admin",
                created_at="2026-10-07T00:00:00.000000Z",
                updated_at="2026-10-07T00:00:00.000000Z",
            )
        )
    with (
        pytest.raises(IntegrityError, match="household_secret_retirement_pending"),
        sessions.begin() as session,
    ):
        operation = session.get(HouseholdOnboardingRow, operation_id)
        assert operation is not None
        operation.stage = "abandoned"
    with (
        pytest.raises(IntegrityError, match="external_secret_retirement_immutable"),
        sessions.begin() as session,
    ):
        row = session.get(ExternalSecretRetirementRow, ("bridge", reference))
        assert row is not None
        row.target_id = str(uuid4())
    with (
        pytest.raises(IntegrityError, match="store_closed"),
        sessions.begin() as session,
    ):
        session.add(
            ExternalSecretRetirementRow(
                store_kind="unknown",
                reference=str(uuid4()),
                target_id=target,
                created_at="2026-10-07T00:00:00.000000Z",
            )
        )
    with pytest.raises(RuntimeError, match="Pending external secret retirement"):
        command.downgrade(config, PREDECESSOR)
    with engine.connect() as connection:
        assert (
            connection.scalar(text("SELECT version_num FROM alembic_version"))
            == REVISION
        )
        assert (
            connection.scalar(text("SELECT reference FROM external_secret_retirements"))
            == reference
        )
    engine.dispose()


@pytest.mark.parametrize("publication_failed", [False, True])
def test_durable_retirement_on_migrated_schema_after_restart(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    env: Environment,
    publication_failed: bool,
) -> None:
    from uuid import UUID

    config, url = migration_setup(monkeypatch, tmp_path, start_revision=PREDECESSOR)
    command.upgrade(config, REVISION)
    engine = create_database_engine(url)
    sessions = create_session_factory(engine)
    with env.sessions() as session:
        build = session.scalar(select(ReviewedBridgeBuildRow))
        assert build is not None
        values = {
            c.name: getattr(build, c.name)
            for c in ReviewedBridgeBuildRow.__table__.columns
        }
    with sessions.begin() as session:
        session.add(ReviewedBridgeBuildRow(**values))
    env.sessions = sessions
    env.service._sessions = sessions
    env.targets._session_factory = sessions
    env.bridges._sessions = sessions
    env.admin._session_factory = sessions
    env.service._validation._sessions = sessions

    def interrupted(*args: Any, **kwargs: Any) -> None:
        raise SQLAlchemyError("publication interrupted")

    def failed(value: UUID) -> None:
        raise OSError("credential deletion failed")

    retire = env.bridges._secrets.retire
    if publication_failed:

        def fail_publication(*args: Any) -> None:
            if args[2].startswith("INSERT INTO external_bridge_pairings"):
                raise SQLAlchemyError("pairing publication failed")

        first = env.start()
        monkeypatch.setattr(env.bridges._secrets, "retire", failed)
        event.listen(engine, "before_cursor_execute", fail_publication)
        try:
            assert first.target_id is not None
            assert env.connect(first).code == "persistence_failed"
        finally:
            event.remove(engine, "before_cursor_execute", fail_publication)
        with sessions() as session:
            assert session.get(ExternalBridgePairingRow, str(first.target_id)) is None
            pending = session.scalar(select(ExternalSecretRetirementRow))
            assert pending is not None and pending.store_kind == "bridge"
            reference = UUID(pending.reference)
    else:
        monkeypatch.setattr(env.service, "_complete", interrupted)
        first = env.start()
        assert first.target_id is not None and env.connect(first).state == "incomplete"
        with sessions() as session:
            pairing = session.get(ExternalBridgePairingRow, str(first.target_id))
            assert pairing is not None and pairing.bridge_secret_reference is not None
            reference = UUID(pairing.bridge_secret_reference)
        monkeypatch.setattr(env.bridges._secrets, "retire", failed)
    assert env.service.abandon(env.request.operation_id).code == "persistence_failed"
    engine.dispose()
    restarted_engine = create_database_engine(url)
    restarted = create_session_factory(restarted_engine)
    try:
        with restarted() as session:
            assert (
                session.get(ExternalSecretRetirementRow, ("bridge", str(reference)))
                is not None
            )
        env.targets._session_factory = restarted
        env.admin._session_factory = restarted
        bridge = ExternalBridgeService(
            restarted,
            endpoint_policy=env.bridges._endpoint_policy,
            secret_store=env.bridges._secrets,
            client=env.bridges._client,
        )
        coordinator = HouseholdKodiOnboardingService(
            restarted,
            targets=env.targets,
            bridges=bridge,
            validation=env.service._validation,
            administration=env.admin,
            lock_root=env.service._lock_root,
        )
        monkeypatch.setattr(bridge._secrets, "retire", retire)
        assert coordinator.abandon(env.request.operation_id).code == "abandoned"
        with restarted() as session:
            assert session.scalars(select(ExternalSecretRetirementRow)).all() == []
        with pytest.raises(SecretStoreError):
            bridge._secrets.read(first.target_id, reference, "request_authentication")
    finally:
        restarted_engine.dispose()
