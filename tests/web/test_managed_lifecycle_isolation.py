"""Fake managed lifecycle must not depend on a routable Kodi endpoint."""

from __future__ import annotations

import time
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.kodi.readiness import (
    JsonRpcUnavailableError,
    KodiApplicationIdentity,
    _BoundedHttpTransport,
)
from kodi_lab.storage.models import ManagedBridgeTrustRow

from .conftest import INSTANCE_ID, ORIGIN, FakeManagedKodiTransport, login


@pytest.mark.parametrize("bridge_state", ["missing", "unreachable", "wrong_identity"])
def test_fake_managed_start_never_uses_network_for_optional_bridge(
    client: TestClient,
    database: sessionmaker[Session],
    managed_native: FakeManagedKodiTransport,
    monkeypatch: pytest.MonkeyPatch,
    bridge_state: str,
) -> None:
    if bridge_state == "unreachable":
        managed_native.failure = JsonRpcUnavailableError("simulated fixture outage")
    elif bridge_state == "wrong_identity":
        managed_native.identity = KodiApplicationIdentity("Kodi", 20, 5, "stable")
    network_attempts: list[tuple[str, int]] = []

    def reject_network(
        _transport: _BoundedHttpTransport,
        *,
        host: str,
        port: int,
        path: str,
        body: bytes,
        timeout: float,
    ) -> bytes:
        network_attempts.append((host, port))
        raise JsonRpcUnavailableError("fixture must not open a real connection")

    monkeypatch.setattr(_BoundedHttpTransport, "post", reject_network)
    csrf = login(client)
    headers = {**ORIGIN, "X-Kodi-Lab-CSRF": csrf}
    provisioned = client.post(
        f"/api/instances/{INSTANCE_ID}/provision", json={}, headers=headers
    )
    assert provisioned.status_code == 200, provisioned.text
    started = client.post(
        f"/api/instances/{INSTANCE_ID}/start", json={}, headers=headers
    )
    assert started.status_code == 200, started.text
    registry = cast(FastAPI, client.app).state.kodi_lab.readiness
    deadline = time.monotonic() + 5
    while registry.snapshot(INSTANCE_ID).state.value == "checking":
        assert time.monotonic() < deadline, "fake managed readiness did not finish"
        time.sleep(0.01)
    current = client.get(f"/api/instances/{INSTANCE_ID}").json()
    assert current["jsonrpc_state"] == "responsive", current
    assert current["usability_state"] == "usable", current
    assert network_attempts == []
    assert managed_native.calls == (
        ["Application.GetProperties", "Addons.GetAddonDetails"]
        if bridge_state == "missing"
        else ["Application.GetProperties"]
    )
    with database() as session:
        assert session.get(ManagedBridgeTrustRow, str(INSTANCE_ID)) is None
