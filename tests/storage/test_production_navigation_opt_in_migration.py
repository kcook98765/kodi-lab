"""Forward-only opt-in default, exact history preservation and safe rollback."""

from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import inspect, text

from kodi_lab.application.mcp_remote_actions import McpRemoteActionReceiptService
from kodi_lab.domain import TargetType
from kodi_lab.kodi.remote import BrowserRemoteAction
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.storage.models import ExternalKodiTargetRow, TargetRow
from tests.application.test_production_navigation import (
    NOW,
    _authority,
    _OuterLimiter,
    _Remote,
)
from tests.migration_support import migration_setup
from tests.migration_support import migration_templates as migration_templates

OLD, NEW = "0006_external_screenshots", "0007_production_navigation"


def test_upgrade_preserves_rows_triggers_history_and_defaults_existing_targets_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, url = migration_setup(monkeypatch, tmp_path, start_revision=OLD)
    engine = create_database_engine(url)
    sessions = create_session_factory(engine)
    target_id = str(uuid4())
    with sessions.begin() as session:
        session.add(
            TargetRow(
                id=target_id,
                name="Preserved production",
                target_type=TargetType.EXTERNAL_PRODUCTION,
                kodi_version="21.3",
                availability="available",
                health="healthy",
                connection_state="disconnected",
            )
        )
        session.flush()
        session.add(
            ExternalKodiTargetRow(
                target_id=target_id,
                protocol_kind="jsonrpc_http",
                host="192.168.50.44",
                port=8080,
                authentication_mode="none",
                configured_capabilities_json='["target_inspect"]',
                verified_capabilities_json="[]",
                administrative_state="disabled",
                verification_state="unverified",
                verified_addresses_json="[]",
                production_policy_revision=1,
                created_at=NOW,
            )
        )
        session.flush()
        session.execute(
            text(
                "INSERT INTO external_production_policies "
                "(target_id,designation_state,identity_generation,policy_revision,"
                "mutations_suspended,created_at,updated_at) "
                "VALUES (:id,'pending_native_identity',1,1,1,:now,:now)"
            ),
            {"id": target_id, "now": NOW},
        )
    service = McpRemoteActionReceiptService(
        sessions, cast(Any, _Remote()), _OuterLimiter()
    )
    authority = _authority()
    old_receipt = service.invoke_production(
        client_id=uuid4(),
        credential_id=uuid4(),
        target_id=uuid4(),
        request_id=uuid4(),
        action=BrowserRemoteAction.HOME,
        authorize_new=lambda: authority,
    )
    with engine.connect() as connection:
        tables = set(inspect(connection).get_table_names()) - {"alembic_version"}
        columns = {
            table: [item["name"] for item in inspect(connection).get_columns(table)]
            for table in tables
        }
        before = {
            table: connection.execute(text(f'SELECT * FROM "{table}"')).all()
            for table in tables
        }
        triggers = dict(
            (name, sql)
            for name, sql in connection.execute(
                text("SELECT name,sql FROM sqlite_master WHERE type='trigger'")
            ).all()
        )
    with engine.connect() as connection:
        indexes = {
            name: "".join(sql.replace('"', "").split())
            for name, sql in connection.execute(
                text(
                    "SELECT name,sql FROM sqlite_master "
                    "WHERE type='index' AND sql IS NOT NULL"
                )
            )
        }
    command.upgrade(config, NEW)
    with engine.connect() as connection:
        for table in tables:
            names = ",".join('"' + name + '"' for name in columns[table])
            assert (
                before[table]
                == connection.execute(text(f'SELECT {names} FROM "{table}"')).all()
            )
        assert connection.execute(
            text(
                "SELECT remote_navigation_enabled,navigation_binding_digest "
                "FROM external_production_policies"
            )
        ).tuples().all() == [(0, None)]
        after_triggers = dict(
            (name, sql)
            for name, sql in connection.execute(
                text("SELECT name,sql FROM sqlite_master WHERE type='trigger'")
            ).all()
        )
        changed = {name for name in triggers if triggers[name] != after_triggers[name]}
        assert changed == {"trg_external_production_policy_material_revision"}
        assert set(triggers) == set(after_triggers)
        assert indexes == {
            name: "".join(sql.replace('"', "").split())
            for name, sql in connection.execute(
                text(
                    "SELECT name,sql FROM sqlite_master "
                    "WHERE type='index' AND sql IS NOT NULL"
                )
            )
        }
        assert connection.scalar(text("PRAGMA integrity_check")) == "ok"
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
    # Legacy history retains its original navigate scope and remains replayable
    # only under an exact authority snapshot supplied by this isolated fixture.
    with sessions() as session:
        from kodi_lab.storage.models import McpProductionNavigationAuditRow

        audit = session.get(
            McpProductionNavigationAuditRow, str(old_receipt.invocation_id)
        )
        assert audit is not None and audit.navigate_scope_id == str(
            authority.navigate_scope_id
        )
    command.downgrade(config, OLD)
    with engine.connect() as connection:
        assert before == {
            table: connection.execute(text(f'SELECT * FROM "{table}"')).all()
            for table in tables
        }
        assert triggers == dict(
            (name, sql)
            for name, sql in connection.execute(
                text("SELECT name,sql FROM sqlite_master WHERE type='trigger'")
            ).all()
        )
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
    command.upgrade(config, NEW)
    current = replace(
        _authority(), navigate_scope_id=None, native_http_acknowledgement_id=None
    )
    service.invoke_production(
        client_id=uuid4(),
        credential_id=uuid4(),
        target_id=uuid4(),
        request_id=uuid4(),
        action=BrowserRemoteAction.HOME,
        authorize_new=lambda: current,
    )
    with pytest.raises(RuntimeError, match="receipt history prevents downgrade"):
        command.downgrade(config, OLD)
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == NEW
        assert connection.scalar(text("PRAGMA integrity_check")) == "ok"
    engine.dispose()


def test_fresh_head_has_the_same_opt_in_and_receipt_shapes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, url = migration_setup(monkeypatch, tmp_path, start_revision=None)
    command.upgrade(config, "head")
    engine = create_database_engine(url)
    with engine.connect() as connection:
        columns = {
            item["name"]: item
            for item in inspect(connection).get_columns("external_production_policies")
        }
        assert columns["remote_navigation_enabled"]["nullable"] is False
        assert columns["remote_navigation_enabled"]["default"] == "0"
        audits = {
            item["name"]: item
            for item in inspect(connection).get_columns(
                "mcp_production_navigation_audits"
            )
        }
        assert audits["navigate_scope_id"]["nullable"] is True
        assert connection.scalar(text("PRAGMA integrity_check")) == "ok"
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
    engine.dispose()
