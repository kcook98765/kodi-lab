from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from argon2 import PasswordHasher as Argon2PasswordHasher
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.browser_remote import BrowserRemoteRateLimiter
from kodi_lab.auth import passwords as password_module
from kodi_lab.auth.service import WebAuthenticationService
from kodi_lab.config import Settings
from kodi_lab.storage.engine import create_session_factory
from kodi_lab.storage.models import (
    ExternalKodiTargetRow,
    ProductionMutationControlRow,
    TargetRow,
)
from kodi_lab.web.app import create_app
from tests.efficiency import CurrentSchemaTemplate

INSTANCE_ID = UUID("00000000-0000-4000-8000-000000000101")
UNRELATED_INSTANCE_ID = UUID("00000000-0000-4000-8000-000000000102")
EXTERNAL_TARGET_ID = UUID("13000000-0000-4000-8000-000000000101")
DEFINITION_ID = UUID("3a000000-0000-4000-8000-000000000001")
REVISION_ID = UUID("3a000000-0000-4000-8000-000000000006")
CONTAINER_ID = "a" * 64
IMAGE_ID = "sha256:" + ("b" * 64)
PASSWORD = "correct horse battery staple"
ORIGIN = {"Origin": "https://127.0.0.1"}


@dataclass
class FakeBroker:
    calls: list[tuple[str, dict[str, object]]] = field(default_factory=list)


@dataclass
class FakeExternalKodi:
    methods: list[str] = field(default_factory=list)
    mutations: int = 0
    on_identity: Callable[[], None] | None = None
    kodi_major: int = 21
    kodi_minor: int = 3

    def handle(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/jsonrpc"
        document = json.loads(request.content)
        method = document["method"]
        self.methods.append(method)
        if method == "JSONRPC.Ping":
            result: object = "pong"
        elif method == "JSONRPC.Version":
            result = {"version": {"major": 13, "minor": 0, "patch": 0}}
        elif method == "Application.GetProperties":
            callback, self.on_identity = self.on_identity, None
            if callback is not None:
                callback()
            result = {
                "name": "Kodi",
                "version": {
                    "major": self.kodi_major,
                    "minor": self.kodi_minor,
                    "tag": "stable",
                },
            }
        elif method == "Addons.GetAddons":
            result = {
                "addons": [
                    {
                        "addonid": "plugin.video.external",
                        "name": "External",
                        "version": "1.0.0",
                        "enabled": True,
                        "type": "xbmc.python.pluginsource",
                    }
                ],
                "limits": {"start": 0, "end": 1, "total": 1},
            }
        elif method == "Input.Home":
            self.mutations += 1
            result = "OK"
        else:  # pragma: no cover - a new native call must be explicitly accepted
            raise AssertionError(f"unexpected external method: {method}")
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": document["id"], "result": result},
        )


@pytest.fixture
def database(
    tmp_path: Path, current_schema: CurrentSchemaTemplate
) -> Iterator[sessionmaker[Session]]:
    current_schema.clone(tmp_path / "web.db")
    engine = create_engine(f"sqlite:///{tmp_path / 'web.db'}")
    factory = create_session_factory(engine)
    with factory.begin() as session:
        session.add(
            ProductionMutationControlRow(
                id=1,
                production_mutations_blocked=True,
                policy_epoch=1,
                updated_by="system",
                updated_at="2026-09-21T12:00:00.000000Z",
            )
        )
    yield factory
    engine.dispose()


def seed_external_target(factory: sessionmaker[Session]) -> None:
    with factory.begin() as session:
        session.add(
            TargetRow(
                id=str(EXTERNAL_TARGET_ID),
                name="External development",
                target_type="external_development",
                kodi_version="21.3",
                availability="available",
                health="healthy",
                connection_state="connected",
            )
        )
        session.flush()
        session.add(
            ExternalKodiTargetRow(
                target_id=str(EXTERNAL_TARGET_ID),
                protocol_kind="jsonrpc_http",
                host="192.168.50.10",
                port=8080,
                authentication_mode="none",
                secret_reference=None,
                configured_capabilities_json=json.dumps(
                    ["addon_inventory", "semantic_remote", "target_inspect"]
                ),
                verified_capabilities_json=json.dumps(
                    ["addon_inventory", "semantic_remote", "target_inspect"]
                ),
                administrative_state="enabled",
                verification_state="verified",
                observation_state="reachable",
                verified_addresses_json='["192.168.50.10"]',
                verified_kodi_name="Kodi",
                verified_kodi_version="21.3",
                observed_kodi_name="Kodi",
                observed_kodi_version="21.3",
                observed_protocol_version="13.0.0",
                pending_observation_id=None,
                created_at="2026-09-21T12:00:00.000000Z",
                verified_at="2026-09-21T12:00:00.000000Z",
                deactivated_at=None,
                last_observed_at="2026-09-21T12:00:00.000000Z",
            )
        )


@pytest.fixture
def data_root(tmp_path: Path) -> Path:
    return tmp_path / "web-data"


@pytest.fixture
def broker() -> FakeBroker:
    return FakeBroker()


@pytest.fixture
def external_kodi() -> FakeExternalKodi:
    return FakeExternalKodi()


@pytest.fixture
def client(
    database: sessionmaker[Session],
    broker: FakeBroker,
    data_root: Path,
    external_kodi: FakeExternalKodi,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    development_root = data_root.parent / "development-root"
    development_root.mkdir(parents=True)

    async def no_advertised_network(_base: str, _checksum: str) -> str:
        return "not_checked"

    monkeypatch.setattr(
        "kodi_lab.repository.address.probe_advertised_origin", no_advertised_network
    )
    # Web route tests exercise authentication semantics, not Argon2 cost tuning.
    # Dedicated auth tests continue to instantiate the production-strength KDF.
    monkeypatch.setattr(
        password_module,
        "PasswordHasher",
        lambda **kwargs: Argon2PasswordHasher(
            time_cost=1,
            memory_cost=8,
            parallelism=1,
            hash_len=16,
            salt_len=8,
            **kwargs,
        ),
    )
    auth = WebAuthenticationService(database)
    auth.set_password(PASSWORD)
    app = create_app(
        settings=Settings(
            data_root=data_root,
            development_root=development_root,
            external_target_master_key=SecretStr(
                "d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3c"
            ),
        ),
        session_factory=database,
        broker=broker,
        auth=auth,
        browser_remote_rate_limiter=BrowserRemoteRateLimiter(clock=lambda: 100.0),
        external_target_transport=httpx.MockTransport(external_kodi.handle),
    )
    with TestClient(app, base_url="https://127.0.0.1") as test_client:
        yield test_client


def login(client: TestClient) -> str:
    response = client.post(
        "/login",
        data={"password": PASSWORD},
        headers=ORIGIN,
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/kodi"
    page = client.get("/")
    assert page.status_code == 200
    match = re.search(r'name="kodi-lab-csrf" content="([^"]+)"', page.text)
    assert match is not None
    return match.group(1)
