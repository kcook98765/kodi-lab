"""One role needs human installation guidance rather than another grant."""

from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.managed_versions import (
    ManagedStatusInput,
    ManagedVersionInput,
)
from kodi_lab.application.mcp_administration import READ_ONLY_INSPECTION_PRESET_ID
from kodi_lab.mcp.authorization import McpAuthorizationError
from kodi_lab.mcp.tools import McpTargetToolService
from kodi_lab.storage.models import ManagedInstanceRow, ManagedVersionOperationRow
from tests.application.test_create_kodi_installation import factory as factory
from tests.application.test_create_kodi_installation import schema as schema
from tests.managed_versions_support import Environment


@pytest.mark.parametrize("cached", [False, True])
def test_role_missing_version_is_installation_required_without_native_or_registration(
    factory: sessionmaker[Session],
    tmp_path: Path,
    cached: bool,
) -> None:
    env = Environment(factory, tmp_path)
    try:
        if cached:
            env.prepare("nexus")
        env.admin.assign_client_preset(
            env.principal.client_id, READ_ONLY_INSPECTION_PRESET_ID
        )
        env.admin.set_managed_lab_role(env.principal.client_id, True)
        tools = McpTargetToolService(
            factory,
            env.authorization,
            env.status,
            cast(Any, None),
            cast(Any, None),
            managed_versions=env.service,
        )
        status = tools.managed_status(env.principal, ManagedStatusInput())
        assert all(
            item.authorized
            and item.code == "installation_required"
            and item.next_action == "ask_administrator"
            for item in status.versions
        )
        with pytest.raises(McpAuthorizationError, match="installation_required"):
            tools.managed_version_action(
                env.principal,
                ManagedVersionInput(
                    version="nexus",
                    request_id=uuid4(),
                ),
                action="start",
            )
        assert env.broker.provisions == env.broker.starts == 0
        with factory() as db:
            assert db.scalar(select(func.count()).select_from(ManagedInstanceRow)) == 0
            assert (
                db.scalar(select(func.count()).select_from(ManagedVersionOperationRow))
                == 0
            )
    finally:
        env.close()
