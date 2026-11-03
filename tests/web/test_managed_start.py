"""Ordinary Web Start uses real auth, canonical approval and broker admission."""

import base64
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest
from argon2 import PasswordHasher
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select

from kodi_lab.auth.service import WebAuthenticationService
from kodi_lab.config import Settings
from kodi_lab.storage.models import (
    AgentWorkspaceRow,
    DevelopmentSessionRow,
    ManagedStartAuthorizationRow,
)
from kodi_lab.web.app import create_app
from tests.application.test_managed_start import Environment, NativeReady
from tests.application.test_managed_start import env as env
from tests.runtime.test_recipe_installation import factory as factory
from tests.web.conftest import ORIGIN, PASSWORD, login


@pytest.fixture
def ordinary_client(
    env: Environment, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    import os

    monkeypatch.setattr(
        "kodi_lab.auth.passwords.PasswordHasher",
        lambda **kw: PasswordHasher(
            time_cost=1, memory_cost=8, parallelism=1, hash_len=16, salt_len=8, **kw
        ),
    )
    auth = WebAuthenticationService(env.sessions)
    auth.set_password(PASSWORD)
    development = tmp_path / "development"
    development.mkdir()
    app = create_app(
        settings=Settings(
            data_root=Path(os.environ["KODI_LAB_DATA_ROOT"]),
            development_root=development,
            external_target_master_key=SecretStr(
                base64.urlsafe_b64encode(b"w" * 32).decode()
            ),
        ),
        session_factory=env.sessions,
        broker=env.broker,
        readiness=env.readiness,
        auth=auth,
        coordinator_factory=lambda *_: NativeReady(),
    )
    with TestClient(app, base_url="https://127.0.0.1") as client:
        yield client


def test_auth_csrf_origin_and_passive_reads(
    ordinary_client: TestClient, env: Environment
) -> None:
    url = f"/api/instances/{env.target}/managed-start"
    assert ordinary_client.post(url, json={}).status_code == 401
    token = login(ordinary_client)
    assert ordinary_client.post(url, json={}, headers=ORIGIN).status_code == 403
    assert (
        ordinary_client.post(
            url, json={}, headers={**ORIGIN, "X-Kodi-Lab-CSRF": "bad"}
        ).status_code
        == 403
    )
    assert (
        ordinary_client.post(
            url,
            json={},
            headers={"Origin": "https://evil.invalid", "X-Kodi-Lab-CSRF": token},
        ).status_code
        == 403
    )
    for _ in range(3):
        assert ordinary_client.get(f"/api/instances/{env.target}").status_code == 200
        assert ordinary_client.get("/api/create-kodi/prerequisites").status_code == 200
    assert env.broker.starts == 0
    with env.sessions() as session:
        assert session.scalars(select(ManagedStartAuthorizationRow)).all() == []


def test_authenticated_start_is_native_only_and_proof_never_reaches_web_response(
    ordinary_client: TestClient,
    env: Environment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app_context = cast(FastAPI, ordinary_client.app).state.kodi_lab

    class ForbiddenBridge:
        def prepare_background(self, *_args: object) -> None:
            pytest.fail("ordinary readiness must not prepare Bridge")

    monkeypatch.setattr(app_context.lifecycle, "_bridge_trust", ForbiddenBridge())
    csrf = login(ordinary_client)
    response = ordinary_client.post(
        f"/api/instances/{env.target}/managed-start",
        json={},
        headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
    )
    assert response.status_code == 200, response.text
    assert env.broker.starts == 1
    assert env.broker.proof is not None
    assert env.broker.proof.get_secret_value() not in response.text
    with env.readiness._lock:
        future = env.readiness._entries[env.target].future
    assert future is not None
    future.result(timeout=5)
    with env.sessions() as session:
        assert session.scalars(select(AgentWorkspaceRow)).all() == []
        assert session.scalars(select(DevelopmentSessionRow)).all() == []
        assert session.scalars(select(ManagedStartAuthorizationRow)).one().consumed_at
    # HTTP callers cannot choose a proof or disable policy in the empty action body.
    assert (
        ordinary_client.post(
            f"/api/instances/{env.target}/managed-start",
            json={"ordinary_start_proof": "x" * 43},
            headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
        ).status_code
        == 422
    )


def test_missing_container_is_an_actionable_refusal_without_provisioning(
    ordinary_client: TestClient,
    env: Environment,
) -> None:
    token = login(ordinary_client)
    env.broker.records.clear()
    response = ordinary_client.post(
        f"/api/instances/{env.target}/managed-start",
        json={},
        headers={**ORIGIN, "X-Kodi-Lab-CSRF": token},
    )
    assert response.status_code == 409
    assert "did not provision or replace" in response.json()["error"]["message"]
    assert env.broker.starts == 0
    with env.sessions() as session:
        assert session.scalars(select(ManagedStartAuthorizationRow)).all() == []
