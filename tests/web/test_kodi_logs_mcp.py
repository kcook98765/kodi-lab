from __future__ import annotations

import json
import logging
from typing import Any

import jsonschema  # type: ignore[import-untyped]
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from kodi_lab.application.mcp_target_adapters import KodiTargetAdapterService
from kodi_lab.storage.models import McpClientTargetGrantRow
from tests.application.test_mcp_kodi_logs import excerpt
from tests.web.conftest import (
    INSTANCE_ID,
    UNRELATED_INSTANCE_ID,
    guard_targeted_status_isolation,
)
from tests.web.test_mcp import _authority, _post


@pytest.mark.parametrize("revoke", [False, True])
def test_read_only_catalog_call_limits_grants_and_content_free_audit(
    client: TestClient,
    database: Any,
    broker: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    revoke: bool,
) -> None:
    # Alembic's test logging configuration may disable existing loggers. Audit
    # assertions must exercise an enabled logger rather than depend on order.
    monkeypatch.setattr(logging.getLogger("kodi_lab.mcp"), "disabled", False)
    guard_targeted_status_isolation(database, broker, client, monkeypatch)
    client_id, token = _authority(client)
    called = []

    def read(self: Any, target: Any, request: Any) -> Any:
        called.append(target)
        if revoke:
            with database.begin() as session:
                session.execute(
                    delete(McpClientTargetGrantRow).where(
                        McpClientTargetGrantRow.client_id == str(client_id)
                    )
                )
        return excerpt(request)

    monkeypatch.setattr(KodiTargetAdapterService, "read_log", read)
    catalog = _post(client, token, "tools/list").json()["result"]["tools"]
    tool = next(t for t in catalog if t["name"] == "log_read")
    assert tool["annotations"]["readOnlyHint"] is True
    assert "remote_action" not in {t["name"] for t in catalog}
    with caplog.at_level(logging.INFO, logger="kodi_lab.mcp"):
        result = _post(
            client,
            token,
            "tools/call",
            params={
                "name": "log_read",
                "arguments": {"target_id": str(INSTANCE_ID), "mode": "tail"},
            },
        ).json()["result"]
    assert called == [INSTANCE_ID]
    if revoke:
        assert result["isError"]
        assert "sensitive log marker" not in json.dumps(result)
    else:
        assert not result.get("isError", False)
        jsonschema.validate(result["structuredContent"], tool["outputSchema"])
        assert result["structuredContent"]["content"] == "sensitive log marker\n"
    assert "sensitive log marker" not in caplog.text
    assert '"tool":"log_read"' in caplog.text
    assert len(called) == 1


@pytest.mark.parametrize(
    "extra",
    [{"path": "/etc/passwd"}, {"max_bytes": 8193}, {"max_lines": True}, {"before": 1}],
)
def test_wire_rejects_arbitrary_paths_and_excess_limits_before_adapter(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    extra: dict[str, Any],
) -> None:
    _, token = _authority(client)
    monkeypatch.setattr(
        KodiTargetAdapterService,
        "read_log",
        lambda *a: pytest.fail("invalid input reached adapter"),
    )
    result = _post(
        client,
        token,
        "tools/call",
        params={
            "name": "log_read",
            "arguments": {"target_id": str(INSTANCE_ID), **extra},
        },
    ).json()
    assert result.get("error") or result["result"]["isError"]


def test_ungranted_target_never_reaches_adapter(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, token = _authority(client)
    monkeypatch.setattr(
        KodiTargetAdapterService,
        "read_log",
        lambda *a: pytest.fail("ungranted target reached adapter"),
    )
    result = _post(
        client,
        token,
        "tools/call",
        params={
            "name": "log_read",
            "arguments": {"target_id": str(UNRELATED_INSTANCE_ID)},
        },
    ).json()["result"]
    assert result["isError"]
    assert "target_not_found_or_not_authorized" in json.dumps(result)
