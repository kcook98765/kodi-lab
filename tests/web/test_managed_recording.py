"""Private recording transfer uses the same MCP wire authentication."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kodi_lab.application.mcp_administration import FULL_LAB_CONTROL_PRESET_ID
from kodi_lab.managed.broker import BrokerClient


def test_recording_transfer_keeps_credential_private_and_checks_current_authority(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert isinstance(client.app, FastAPI)
    administration = client.app.state.kodi_lab.mcp_administration
    owner = administration.create_client(
        display_name="Recording owner",
        preset_id=FULL_LAB_CONTROL_PRESET_ID,
        trusted_managed_lab=True,
    )
    observer = administration.create_client(
        display_name="Recording observer", preset_id=FULL_LAB_CONTROL_PRESET_ID
    )
    credential = administration.issue_credential(owner.id)
    other = administration.issue_credential(observer.id)
    recording = uuid4().hex
    calls: list[tuple[str, str, str]] = []
    expected = b"sealed MP4 fixture"

    @asynccontextmanager
    async def download(
        _client: BrokerClient, actor: str, key: str, requested: str
    ) -> AsyncIterator[tuple[asyncio.StreamReader, int]]:
        calls.append((actor, key, requested))
        reader = asyncio.StreamReader()
        reader.feed_data(expected)
        reader.feed_eof()
        yield reader, len(expected)

    monkeypatch.setattr(BrokerClient, "download", download)
    headers = {
        "Content-Type": "application/vnd.kodi-lab.recording",
        "X-Kodi-Lab-Recording": recording,
        "Content-Length": "0",
    }
    assert client.post("/mcp", headers=headers).status_code == 401
    assert (
        client.post(
            "/mcp", headers={**headers, "Authorization": f"Bearer {other.token}"}
        ).status_code
        == 403
    )
    assert not calls
    response = client.post(
        "/mcp", headers={**headers, "Authorization": f"Bearer {credential.token}"}
    )
    assert response.status_code == 200
    assert response.content == expected
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-type"] == "video/mp4"
    assert calls == [(str(owner.id), str(credential.credential.id), recording)]
    administration.revoke_credential(owner.id, credential.credential.id)
    assert (
        client.post(
            "/mcp", headers={**headers, "Authorization": f"Bearer {credential.token}"}
        ).status_code
        == 401
    )
    assert len(calls) == 1
