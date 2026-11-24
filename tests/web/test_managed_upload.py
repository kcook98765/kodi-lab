"""Bulk intake retains the existing bearer, listener and MCP JSON limits."""

from hashlib import sha256

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kodi_lab.application.mcp_administration import FULL_LAB_CONTROL_PRESET_ID
from kodi_lab.managed.broker import BrokerClient
from kodi_lab.managed.packages import MAX_ZIP
from kodi_lab.managed.protocol import OperationReply


def bearer(client: TestClient, *, managed: bool) -> tuple[str, str]:
    assert isinstance(client.app, FastAPI)
    admin = client.app.state.kodi_lab.mcp_administration
    row = admin.create_client(
        display_name=f"upload-{managed}",
        preset_id=FULL_LAB_CONTROL_PRESET_ID,
        trusted_managed_lab=managed,
    )
    return str(row.id), admin.issue_credential(row.id).token


def test_upload_needs_trusted_scope_and_preserves_mcp_size(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []
    data = b"z" * 65_537
    digest = sha256(data).hexdigest()

    def upload(
        _broker: BrokerClient, actor: str, archive: bytes, expected: str
    ) -> OperationReply:
        calls.append((actor, archive, expected))
        return {
            "result": {
                "addon": "script.fixture",
                "version": "1.0.0",
                "digest": expected,
            },
            "elapsed_seconds": 0.0,
        }

    monkeypatch.setattr(BrokerClient, "upload", upload)
    actor, token = bearer(client, managed=True)
    _, outside = bearer(client, managed=False)
    headers = {"Content-Type": "application/zip", "X-Kodi-Lab-Package-SHA256": digest}
    assert client.post("/mcp", headers=headers, content=data).status_code == 401
    assert (
        client.post(
            "/mcp",
            headers={**headers, "Authorization": f"Bearer {outside}"},
            content=data,
        ).status_code
        == 403
    )
    headers["Authorization"] = f"Bearer {token}"
    assert client.post(
        "/mcp", headers={**headers, "Host": "foreign.invalid"}, content=data
    ).status_code in {400, 421}
    assert (
        client.post(
            "/mcp",
            headers={**headers, "Content-Length": str(MAX_ZIP + 1)},
            content=data,
        ).status_code
        == 413
    )
    assert (
        client.post(
            "/mcp",
            headers={**headers, "Content-Length": str(len(data) - 1)},
            content=data,
        ).json()["code"]
        == "incomplete_package_upload"
    )
    assert (
        client.post(
            "/mcp",
            headers={**headers, "X-Kodi-Lab-Package-SHA256": "invalid"},
            content=data,
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/mcp",
            headers={**headers, "Content-Type": "application/json"},
            content=data,
        ).status_code
        == 413
    )
    assert not calls
    accepted = client.post("/mcp", headers=headers, content=data)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["result"]["digest"] == digest
    assert calls == [(actor, data, digest)]
