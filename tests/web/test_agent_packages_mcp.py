"""Role-authorized original package transfer over the real authenticated MCP wire."""

from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from fastapi.testclient import TestClient

from kodi_lab.application.mcp_administration import READ_ONLY_INSPECTION_PRESET_ID
from tests.application.test_agent_packages import operation
from tests.development.test_addon_zip import make_zip
from tests.web.conftest import INSTANCE_ID
from tests.web.test_mcp import (
    _administration,
    _assert_tool_error,
    _post,
    _strict_tool_result,
)


def test_wire_original_package_intake_schema_owner_and_revocation(
    client: TestClient,
    data_root: Path,
) -> None:
    data_root.mkdir(mode=0o700)
    admin = _administration(client)
    agent = admin.create_client(
        display_name="Package agent",
        preset_id=READ_ONLY_INSPECTION_PRESET_ID,
        trusted_managed_lab=True,
    )
    token = admin.issue_credential(agent.id).token
    catalog = {
        item["name"]: item
        for item in _post(client, token, "tools/list").json()["result"]["tools"]
    }

    def call(name: str, values: dict[str, Any]) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            _post(
                client, token, "tools/call", params={"name": name, "arguments": values}
            ).json()["result"],
        )

    assert all(
        catalog[name]["_meta"]["kodiLabMcpSchemaVersion"] == 1
        for name in (
            "package_import",
            "package_status",
            "package_install",
            "package_install_status",
        )
    )
    request = operation(make_zip())
    result = call("package_import", request.model_dump(mode="json"))
    assert not result.get("isError")
    _strict_tool_result(result, catalog["package_import"]["outputSchema"])
    accepted = result["structuredContent"]
    assert accepted["state"] == "accepted"
    assert (
        call("package_import", request.model_dump(mode="json"))["structuredContent"]
        == accepted
    )
    _strict_tool_result(
        call("package_status", {"artifact_id": accepted["artifact_id"]}),
        catalog["package_status"]["outputSchema"],
    )
    _assert_tool_error(
        call(
            "package_install",
            {
                "request_id": str(uuid4()),
                "target_id": str(INSTANCE_ID),
                "artifact_id": accepted["artifact_id"],
            },
        ),
        "target_not_running",
    )
    _assert_tool_error(
        call(
            "package_install_status",
            {
                "operation_id": str(uuid4()),
                "verify": False,
            },
        ),
        "target_not_found_or_not_authorized",
    )
    unknown = call("package_status", {"artifact_id": str(uuid4())})
    _assert_tool_error(unknown, "package_not_found_or_not_authorized")
    assert admin.get_client(agent.id).target_grants == ()
    assert admin.get_client(agent.id).development_source_grants == ()
    admin.set_managed_lab_role(agent.id, False)
    _assert_tool_error(
        call("package_status", {"artifact_id": accepted["artifact_id"]}),
        "permission_denied",
    )
