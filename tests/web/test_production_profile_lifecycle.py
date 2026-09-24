from __future__ import annotations

import inspect
import json
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab import production_profile_lifecycle as lifecycle_module
from kodi_lab.application.external_targets import ExternalTargetAdministrationService
from kodi_lab.kodi.external import NativeExternalKodiVerifier
from kodi_lab.production_profile_lifecycle import (
    PRODUCTION_PROFILE_CAPABILITIES,
    ProductionLifecycleTransition,
    ProductionProfileLifecycle,
    ProductionProfileLifecycleError,
)
from kodi_lab.production_profile_web_client import (
    AuthenticationRejected,
    ProductionProfileWebClient,
)
from kodi_lab.storage.models import ExternalKodiTargetRow

from .conftest import PASSWORD, FakeExternalKodi

BASE_URL = "http://127.0.0.1"
KODI_USERNAME = "fixture-kodi-user"
KODI_PASSWORD = "fixture-kodi-password"


def _web(client: TestClient) -> ProductionProfileWebClient:
    return ProductionProfileWebClient(client=cast(Any, client), base_url=BASE_URL)


def _configure_external(client: TestClient, external_kodi: FakeExternalKodi) -> None:
    external_kodi.kodi_major = 19
    external_kodi.kodi_minor = 5
    app = cast(FastAPI, client.app)
    service = cast(
        ExternalTargetAdministrationService, app.state.kodi_lab.production_targets
    )
    service._verifier = NativeExternalKodiVerifier(
        transport=httpx.MockTransport(external_kodi.handle)
    )


def _lifecycle(
    client: TestClient, external_kodi: FakeExternalKodi
) -> tuple[ProductionProfileWebClient, ProductionProfileLifecycle]:
    _configure_external(client, external_kodi)
    web = _web(client)
    web.login(PASSWORD)
    return web, ProductionProfileLifecycle(web)


def _register(
    lifecycle: ProductionProfileLifecycle,
) -> ProductionLifecycleTransition:
    return lifecycle.register_external_production(
        username=KODI_USERNAME,
        password=KODI_PASSWORD,
    )


def _revision_state(
    database: sessionmaker[Session], target_id: UUID
) -> tuple[int, int, int]:
    with database() as session:
        row = session.execute(
            text(
                "SELECT p.policy_revision,e.production_policy_revision,"
                "g.maximum_policy_revision FROM external_production_policies p "
                "JOIN external_kodi_targets e ON e.target_id=p.target_id "
                "JOIN external_production_policy_revision_guards g "
                "ON g.target_id=p.target_id WHERE p.target_id=:target"
            ),
            {"target": str(target_id)},
        ).one()
    return cast(tuple[int, int, int], tuple(row))


@pytest.mark.parametrize(
    ("host", "port", "protocol"),
    [
        ("192.168.50.135", 8080, "jsonrpc_http"),
        ("192.168.2.20", 8080, "jsonrpc_http"),
        ("10.250.0.19", 8080, "jsonrpc_http"),
        ("127.0.0.1", 8080, "jsonrpc_http"),
        ("10.251.0.10", 8080, "jsonrpc_http"),
        ("10.251.0.20", 8080, "jsonrpc_http"),
        ("10.251.0.19", 8765, "jsonrpc_http"),
        ("10.251.0.19", 8080, "jsonrpc_https"),
    ],
)
def test_registration_refuses_every_endpoint_except_fixed_fixture(
    client: TestClient, host: str, port: int, protocol: str
) -> None:
    lifecycle = ProductionProfileLifecycle(_web(client))
    with pytest.raises(
        ProductionProfileLifecycleError, match="fixture_endpoint_refused"
    ):
        lifecycle.register_external_production(
            username=KODI_USERNAME,
            password=KODI_PASSWORD,
            host=host,
            port=port,
            protocol_kind=protocol,
        )
    assert client.get("/api/production-targets").status_code == 401


def test_canonical_web_lifecycle_revisions_encrypted_secret_and_zero_authority(
    client: TestClient,
    database: sessionmaker[Session],
    data_root: Any,
    external_kodi: FakeExternalKodi,
) -> None:
    web, lifecycle = _lifecycle(client, external_kodi)

    registered = _register(lifecycle)
    assert registered.classification == "initial"
    assert registered.lifecycle_state == "registered_disabled_unverified"
    assert registered.configured_capabilities == PRODUCTION_PROFILE_CAPABILITIES
    assert _revision_state(database, registered.target_id) == (1, 1, 1)

    with database() as session:
        stored = session.scalar(
            select(ExternalKodiTargetRow).where(
                ExternalKodiTargetRow.target_id == str(registered.target_id)
            )
        )
        assert stored is not None
        assert stored.secret_reference is not None
        secret_reference = stored.secret_reference
    secret_path = (
        data_root / "secrets" / "external-targets" / f"{secret_reference}.json"
    )
    encrypted = secret_path.read_bytes()
    assert secret_path.stat().st_mode & 0o777 == 0o600
    assert KODI_USERNAME.encode() not in encrypted
    assert KODI_PASSWORD.encode() not in encrypted
    assert set(json.loads(encrypted)) == {
        "format",
        "key_version",
        "nonce",
        "ciphertext",
    }

    observed = lifecycle.observe_native(registered.target_id)
    assert observed.classification == "native_material"
    assert observed.lifecycle_state == "observed_pending_confirmation"
    assert observed.kodi_name == "Kodi"
    assert observed.kodi_version == "19.5"
    assert observed.observation_id is not None
    assert _revision_state(database, registered.target_id) == (2, 2, 2)

    native = lifecycle.confirm_native_identity(
        registered.target_id, observed.observation_id
    )
    assert native.designation_state == "pending_confirmation"
    assert native.verification_state == "verified"
    assert _revision_state(database, registered.target_id) == (3, 3, 3)

    designated = lifecycle.designate_production(
        registered.target_id, observed.observation_id
    )
    assert designated.classification == "policy_only"
    assert designated.lifecycle_state == "production_designation_confirmed"
    assert designated.designation_state == "confirmed"
    assert _revision_state(database, registered.target_id) == (4, 3, 4)

    enabled = lifecycle.enable_production_read(registered.target_id)
    assert enabled.classification == "native_material"
    assert enabled.lifecycle_state == "enabled_read_only"
    assert enabled.administrative_state == "enabled"
    assert enabled.verified_capabilities == PRODUCTION_PROFILE_CAPABILITIES
    assert _revision_state(database, registered.target_id) == (5, 5, 5)
    assert external_kodi.methods == [
        "JSONRPC.Ping",
        "JSONRPC.Version",
        "Application.GetProperties",
        "Addons.GetAddons",
    ]
    assert external_kodi.mutations == 0

    with database() as session:
        assert (
            session.execute(
                text(
                    "SELECT COUNT(*) FROM targets "
                    "WHERE target_type='external_production'"
                )
            ).scalar_one()
            == 1
        )
        policy = session.execute(
            text(
                "SELECT designation_state,identity_generation,policy_revision,"
                "mutations_suspended FROM external_production_policies "
                "WHERE target_id=:target"
            ),
            {"target": str(registered.target_id)},
        ).one()
        assert tuple(policy) == ("confirmed", 1, 5, 1)
        assert (
            session.execute(
                text(
                    "SELECT production_mutations_blocked FROM "
                    "production_mutation_control WHERE id=1"
                )
            ).scalar_one()
            == 1
        )
        for table in (
            "mcp_clients",
            "mcp_credentials",
            "mcp_client_target_grants",
            "mcp_target_preset_assignments",
            "mcp_production_target_scopes",
            "external_bridge_pairings",
            "reviewed_bridge_builds",
            "production_bridge_http_acknowledgements",
            "production_native_control_http_acknowledgements",
            "production_operation_approvals",
        ):
            assert (
                session.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one() == 0
            )

    serialized = json.dumps(
        [dict(item) for item in lifecycle.evidence], default=str, sort_keys=True
    )
    assert KODI_USERNAME not in serialized
    assert KODI_PASSWORD not in serialized
    assert PASSWORD not in serialized
    assert "192.168.50.135" not in serialized
    web.logout()


def test_duplicate_and_replay_behavior_is_canonical_and_bounded(
    client: TestClient,
    database: sessionmaker[Session],
    external_kodi: FakeExternalKodi,
) -> None:
    _web_client, lifecycle = _lifecycle(client, external_kodi)
    first = _register(lifecycle)
    duplicate_registration = _register(lifecycle)
    assert duplicate_registration.target_id != first.target_id
    assert _revision_state(database, duplicate_registration.target_id) == (1, 1, 1)

    first_observation = lifecycle.observe_native(first.target_id)
    assert first_observation.observation_id is not None
    second_observation = lifecycle.observe_native(first.target_id)
    assert second_observation.observation_id is not None
    assert second_observation.observation_id != first_observation.observation_id
    assert _revision_state(database, first.target_id) == (3, 3, 3)

    confirmed = lifecycle.confirm_native_identity(
        first.target_id, second_observation.observation_id
    )
    assert confirmed.policy_revision == 4
    with pytest.raises(ProductionProfileLifecycleError) as reconfirmed:
        lifecycle.confirm_native_identity(
            first.target_id, second_observation.observation_id
        )
    assert reconfirmed.value.code == "verification_required"
    assert reconfirmed.value.status_code == 409
    assert _revision_state(database, first.target_id) == (4, 4, 4)

    designated = lifecycle.designate_production(
        first.target_id, second_observation.observation_id
    )
    assert designated.policy_revision == 5
    with pytest.raises(ProductionProfileLifecycleError) as duplicate_designation:
        lifecycle.designate_production(
            first.target_id, second_observation.observation_id
        )
    assert duplicate_designation.value.code == "production_designation_required"
    assert duplicate_designation.value.status_code == 409
    assert _revision_state(database, first.target_id) == (5, 4, 5)

    enabled = lifecycle.enable_production_read(first.target_id)
    assert enabled.policy_revision == 6
    already_enabled = lifecycle.enable_production_read(first.target_id)
    assert already_enabled.classification == "no_material_change"
    assert already_enabled.policy_revision == 6
    assert _revision_state(database, first.target_id) == (6, 6, 6)

    with pytest.raises(
        ProductionProfileLifecycleError,
        match="target_not_registered_by_lifecycle_harness",
    ):
        lifecycle.observe_native(uuid4())


def test_wrong_order_and_unsupported_lifecycle_payloads_are_rejected(
    client: TestClient,
    database: sessionmaker[Session],
    external_kodi: FakeExternalKodi,
) -> None:
    web, lifecycle = _lifecycle(client, external_kodi)
    registered = _register(lifecycle)
    target = registered.target_id
    unknown_observation = uuid4()

    confirm = web.post_authenticated(
        f"/api/production-targets/{target}/confirm-native",
        json={"confirmed": True, "observation_id": str(unknown_observation)},
    )
    assert confirm.status_code == 409
    assert confirm.json()["error"]["code"] == "verification_required"
    designate = web.post_authenticated(
        f"/api/production-targets/{target}/confirm-designation",
        json={
            "confirmed": True,
            "observation_id": str(unknown_observation),
            "acknowledgement": (
                "I confirm this is a production Kodi target and mutations may "
                "affect real user state."
            ),
        },
    )
    assert designate.status_code == 409
    assert designate.json()["error"]["code"] == "production_designation_required"
    enable = web.post_authenticated(
        f"/api/production-targets/{target}/enabled", json={"enabled": True}
    )
    assert enable.status_code == 409
    assert enable.json()["error"]["code"] == "verification_required"
    assert _revision_state(database, target) == (1, 1, 1)

    observed = lifecycle.observe_native(target)
    assert observed.observation_id is not None
    lifecycle.confirm_native_identity(target, observed.observation_id)
    before_designation = web.post_authenticated(
        f"/api/production-targets/{target}/enabled", json={"enabled": True}
    )
    assert before_designation.status_code == 409
    assert (
        before_designation.json()["error"]["code"] == "production_designation_required"
    )

    capability_change = web.post_authenticated(
        f"/api/production-targets/{target}/enabled",
        json={
            "enabled": False,
            "configured_capabilities": ["target_inspect", "screenshot"],
        },
    )
    assert capability_change.status_code == 422
    unsupported_cas = web.post_authenticated(
        f"/api/production-targets/{target}/confirm-designation",
        json={
            "confirmed": True,
            "observation_id": str(observed.observation_id),
            "expected_policy_revision": 1,
            "acknowledgement": (
                "I confirm this is a production Kodi target and mutations may "
                "affect real user state."
            ),
        },
    )
    assert unsupported_cas.status_code == 422
    assert _revision_state(database, target) == (3, 3, 3)


def test_lifecycle_helpers_depend_on_authenticated_origin_and_csrf_path(
    client: TestClient,
    database: sessionmaker[Session],
    external_kodi: FakeExternalKodi,
) -> None:
    _configure_external(client, external_kodi)
    logged_out_web = _web(client)
    logged_out = ProductionProfileLifecycle(logged_out_web)
    with pytest.raises(AuthenticationRejected, match="session_absent"):
        _register(logged_out)

    logged_out_web.login(PASSWORD)
    lifecycle = ProductionProfileLifecycle(logged_out_web)
    registered = _register(lifecycle)
    path = f"/api/production-targets/{registered.target_id}/probe"
    assert (
        logged_out_web.post_authenticated(path, json={}, csrf_token=None).status_code
        == 403
    )
    assert (
        logged_out_web.post_authenticated(
            path, json={}, csrf_token="A" * 43
        ).status_code
        == 403
    )
    assert (
        logged_out_web.post_authenticated(path, json={}, origin=None).status_code == 403
    )
    assert (
        logged_out_web.post_authenticated(
            path, json={}, origin="http://attacker.invalid"
        ).status_code
        == 403
    )
    assert _revision_state(database, registered.target_id) == (1, 1, 1)
    assert lifecycle.observe_native(registered.target_id).policy_revision == 2

    logged_out_web.logout()
    with pytest.raises(AuthenticationRejected, match="session_absent"):
        lifecycle.enable_production_read(registered.target_id)


def test_lifecycle_helper_has_no_service_database_or_generic_mutation_surface() -> None:
    source = inspect.getsource(lifecycle_module)
    assert "kodi_lab.application" not in source
    assert "kodi_lab.storage" not in source
    assert "sqlalchemy" not in source
    assert "sqlite" not in source.lower()
    assert "capability-amendments" not in source
    assert "bridge" not in source.lower()
    assert "/mcp" not in source
    assert "192.168.50.135" not in source
