"""Suppress diagnostics when durable authority changes during collection."""

from types import SimpleNamespace
from typing import Any

import pytest

from kodi_lab.application.mcp_administration import (
    READ_ONLY_INSPECTION_PRESET_ID,
    McpAdministrationService,
    McpPrincipal,
)
from kodi_lab.mcp.authorization import (
    McpAuthorizationError,
    McpAuthorizationService,
    TargetTechnicalAuthority,
)
from kodi_lab.mcp.registry import TargetClass, build_mcp_tool_registry
from tests.application.test_mcp_external_targets import TARGET_ID as EXTERNAL_ID
from tests.application.test_mcp_external_targets import (
    external_authority as external_authority,
)
from tests.application.test_mcp_foundation import TARGET_ID as MANAGED_ID
from tests.application.test_mcp_foundation import mcp_database as mcp_database


@pytest.mark.parametrize("managed", [False, True])
@pytest.mark.parametrize("revocation", ["grant", "client", "credential"])
@pytest.mark.parametrize("failed_read", [False, True])
def test_system_info_rechecks_authority_before_publication(
    mcp_database: Any,
    external_authority: Any,
    managed: bool,
    revocation: str,
    failed_read: bool,
) -> None:
    if managed:
        factory = mcp_database
        administration = McpAdministrationService(factory)
        targets = SimpleNamespace(
            resolve=lambda target: TargetTechnicalAuthority(
                target_class=TargetClass.INTERNAL_LAB,
                capabilities=frozenset({"target_inspect"}),
                current_states=frozenset({"reconciled"}),
            )
        )
        target_id = MANAGED_ID
    else:
        factory, administration, targets = external_authority
        target_id = EXTERNAL_ID
    client = administration.create_client(
        display_name="Diagnostics reader", preset_id=READ_ONLY_INSPECTION_PRESET_ID
    )
    issued = administration.issue_credential(client.id)
    administration.assign_target_preset(target_id, READ_ONLY_INSPECTION_PRESET_ID)
    administration.grant_target(client.id, target_id)
    principal = McpPrincipal(
        client_id=client.id,
        credential_id=issued.credential.id,
        preset_id=READ_ONLY_INSPECTION_PRESET_ID,
        permissions=frozenset({"target.inspect"}),
    )
    authorization = McpAuthorizationService(
        factory, build_mcp_tool_registry(), targets=targets
    )

    def collect() -> str:
        if revocation == "grant":
            administration.remove_target_grant(client.id, target_id)
        elif revocation == "client":
            administration.set_client_enabled(client.id, False)
        else:
            administration.revoke_credential(client.id, issued.credential.id)
        if failed_read:
            raise McpAuthorizationError("target_unreachable")
        return "private diagnostics"

    expected = (
        "target_not_found_or_not_authorized"
        if revocation == "grant"
        else "permission_denied"
    )
    with pytest.raises(McpAuthorizationError, match=expected):
        authorization.invoke_target(principal, "system_info", target_id, collect)
