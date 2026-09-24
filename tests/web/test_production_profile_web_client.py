from __future__ import annotations

import hashlib
import inspect
import json
from typing import Any, cast
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab import production_profile_web_client as web_client_module
from kodi_lab.production_profile_web_client import (
    AuthenticationRejected,
    ProductionProfileWebClient,
)
from kodi_lab.storage.models import WebSessionRow

from .conftest import PASSWORD, FakeBroker

BASE_URL = "http://127.0.0.1"
SAFE_MISSING_INSTANCE = UUID("00000000-0000-4000-8000-000000000001")


def _harness(client: TestClient) -> ProductionProfileWebClient:
    return ProductionProfileWebClient(client=cast(Any, client), base_url=BASE_URL)


def test_canonical_login_cookie_authenticated_pages_and_logout(
    client: TestClient, database: sessionmaker[Session]
) -> None:
    harness = _harness(client)
    preflight = harness.security_preflight()
    assert preflight["canonical_host_accepted"] is True
    assert preflight["unexpected_host_rejected"] is True
    assert preflight["missing_session_rejected"] is True
    assert preflight["malformed_session_rejected"] is True
    assert preflight["invalid_session_rejected"] is True

    harness.login(PASSWORD)
    raw_cookie = client.cookies.get("kodi_lab_session")
    assert raw_cookie is not None
    with database() as session:
        rows = list(session.scalars(select(WebSessionRow)))
        assert len(rows) == 1
        assert rows[0].token_digest != raw_cookie

    for path in ("/", "/production-targets", "/mcp-clients"):
        assert harness.get_authenticated(path).status_code == 200
    csrf = harness.csrf_token()
    assert csrf

    serialized = json.dumps(dict(harness.evidence), sort_keys=True)
    assert PASSWORD not in serialized
    assert raw_cookie not in serialized
    assert hashlib.sha256(raw_cookie.encode("ascii")).hexdigest() not in serialized
    assert csrf not in serialized

    harness.logout()
    assert client.get("/api/instances").status_code == 401
    with database() as session:
        assert session.scalar(select(WebSessionRow)) is None
    assert harness.evidence["logged_out_session_rejected"] is True
    harness.close()


def test_wrong_password_is_generic_and_creates_no_session(
    client: TestClient, database: sessionmaker[Session]
) -> None:
    harness = _harness(client)
    with pytest.raises(AuthenticationRejected) as rejected:
        harness.login("wrong fixture password")
    assert rejected.value.code == "login_rejected"
    assert PASSWORD not in str(rejected.value)
    assert harness.evidence["login_succeeded"] is False
    with database() as session:
        assert session.scalar(select(WebSessionRow)) is None
    harness.close()


def test_csrf_origin_and_safe_route_validation_are_ordered_before_mutation(
    client: TestClient,
    database: sessionmaker[Session],
    broker: FakeBroker,
) -> None:
    harness = _harness(client)
    harness.login(PASSWORD)
    harness.get_authenticated("/")
    path = f"/api/instances/{SAFE_MISSING_INSTANCE}/provision"

    assert harness.post_authenticated(path, json={}, csrf_token=None).status_code == 403
    assert (
        harness.post_authenticated(path, json={}, csrf_token="A" * 43).status_code
        == 403
    )
    assert harness.post_authenticated(path, json={}, origin=None).status_code == 403
    assert (
        harness.post_authenticated(
            path, json={}, origin="http://attacker.invalid"
        ).status_code
        == 403
    )
    valid = harness.post_authenticated(path, json={})
    assert valid.status_code == 404
    assert valid.json()["error"]["code"] == "managed_instance_not_found"
    assert broker.calls == []
    with database() as session:
        assert len(list(session.scalars(select(WebSessionRow)))) == 1
    harness.logout()


def test_client_is_test_only_relative_url_and_public_web_surface() -> None:
    source = inspect.getsource(web_client_module)
    assert "kodi_lab.auth" not in source
    assert "kodi_lab.storage" not in source
    assert "sqlalchemy" not in source
    assert "sqlite" not in source.lower()
    assert "add_api_route" not in source
    assert "@app." not in source
    assert "Authorization" not in source

    client = ProductionProfileWebClient
    assert client.__module__ == "kodi_lab.production_profile_web_client"
    with pytest.raises(ValueError, match="base URL is fixed"):
        ProductionProfileWebClient(base_url="http://127.0.0.1:8180")
