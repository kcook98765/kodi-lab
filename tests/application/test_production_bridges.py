from __future__ import annotations

import base64
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.external_bridges import (
    ExternalBridgeService,
    ExternalBridgeServiceError,
)
from kodi_lab.application.external_targets import ExternalTargetAdministrationService
from kodi_lab.application.mcp_administration import (
    McpAdministrationError,
    McpAdministrationService,
    McpAuthenticationService,
)
from kodi_lab.application.mcp_target_adapters import KodiTargetAdapterService
from kodi_lab.application.production_inspection import PRODUCTION_VISUAL_PRESET_ID
from kodi_lab.application.production_screenshots import (
    ProductionScreenshotError,
    ProductionScreenshotService,
)
from kodi_lab.domain import (
    ExternalAuthenticationMode,
    ExternalProtocolKind,
    TargetType,
)
from kodi_lab.kodi.external import (
    ExternalEndpointPolicy,
    ExternalKodiProbeResult,
    ValidatedExternalEndpoint,
)
from kodi_lab.mcp.authorization import McpAuthorizationError, McpAuthorizationService
from kodi_lab.mcp.registry import build_mcp_tool_registry
from kodi_lab.mcp.tools import McpTargetToolService
from kodi_lab.storage.engine import create_session_factory
from kodi_lab.storage.metadata import Base
from kodi_lab.storage.models import ProductionMutationControlRow
from kodi_lab.storage.secrets import ExternalBridgeSecretStore, ExternalCredentialStore
from tests.kodi.fake_external_bridge import FakeExternalBridge


class _Verifier:
    def probe(
        self,
        endpoint: ValidatedExternalEndpoint,
        _credential: object,
        configured: frozenset[str],
    ) -> ExternalKodiProbeResult:
        return ExternalKodiProbeResult(
            addresses=(endpoint.address,),
            kodi_name="Kodi",
            kodi_version="21.3",
            protocol_version="13.0.0",
            verified_capabilities=tuple(
                item
                for item in ("target_inspect", "addon_inventory")
                if item in configured
            ),
        )


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=")


def _production_target(
    factory: sessionmaker[Session], tmp_path: Path
) -> tuple[UUID, ExternalCredentialStore]:
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
    credentials = ExternalCredentialStore(
        tmp_path / "native-secrets", master_key=None, key_version=1
    )
    targets = ExternalTargetAdministrationService(
        factory,
        endpoint_policy=ExternalEndpointPolicy(
            allowed_ports=(8080,), test_loopback_enabled=True
        ),
        credential_store=credentials,
        verifier=cast(Any, _Verifier()),
        target_type=TargetType.EXTERNAL_PRODUCTION,
    )
    target = targets.register(
        logical_name="Mock production Kodi",
        protocol_kind=ExternalProtocolKind.JSONRPC_HTTP,
        host="127.0.0.1",
        port=8080,
        authentication_mode=ExternalAuthenticationMode.NONE,
        configured_capabilities={"target_inspect", "addon_inventory", "screenshot"},
    )
    observed = targets.probe(target.target_id)
    assert observed.pending_observation_id is not None
    targets.confirm(target.target_id, observed.pending_observation_id)
    targets.confirm_production_designation(
        target.target_id, observed.pending_observation_id
    )
    targets.set_enabled(target.target_id, enabled=True)
    return target.target_id, credentials


def test_production_pairing_visual_scope_and_replay_are_independent_and_bounded(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'production-bridge.db'}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    target_id, credentials = _production_target(factory, tmp_path)

    with FakeExternalBridge(target_id=target_id, token=b"x" * 32) as fake:
        fake.capabilities = ["screenshot"]
        bridges = ExternalBridgeService(
            factory,
            endpoint_policy=ExternalEndpointPolicy(
                allowed_ports=(fake.port,), test_loopback_enabled=True
            ),
            secret_store=ExternalBridgeSecretStore(
                tmp_path / "bridge-secrets", master_key=b"b" * 32, key_version=1
            ),
        )
        reviewed = bridges.register_reviewed_build(
            semantic_fingerprint=fake.build_fingerprint,
            zip_sha256="a" * 64,
            addon_id="service.kodi_lab_bridge",
            addon_version=fake.addon_version,
            protocol_version=fake.protocol_version,
            capabilities=tuple(fake.capabilities),
            supported_kodi_versions=(fake.kodi_version,),
            source_reference="deterministic local fixture",
        )
        assert reviewed.state == "active"
        acknowledgement = bridges.acknowledge_production_http_risk(
            target_id, bridge_port=fake.port
        )
        assert acknowledgement.bridge_trust_revision == 1
        token = bridges.generate_pairing_token(target_id, bridge_port=fake.port)
        fake.token = _decode(token.token)
        pending = bridges.attempt_pairing(target_id)
        assert pending.production_bridge_trust_revision == 1
        assert pending.observed_bridge_id == fake.bridge_id
        assert pending.observed_addon_version == fake.addon_version
        assert pending.observed_build_fingerprint == fake.build_fingerprint
        assert pending.observed_capabilities == ("screenshot",)
        paired = bridges.confirm_pairing(target_id)
        assert paired.reviewed_build_id == reviewed.build_id
        assert paired.production_identity_generation == 1
        assert paired.production_bridge_trust_revision == 1
        # Technical expansion is observed but cannot create production
        # deployment authority or disturb the reviewed screenshot subset.
        fake.capabilities.append("workspace_deployment")
        assert not bridges.deployment_available(target_id)

        administration = McpAdministrationService(factory)
        client = administration.create_client(
            display_name="Production visual reader",
            preset_id=PRODUCTION_VISUAL_PRESET_ID,
        )
        credential = administration.issue_credential(client.id)
        administration.assign_target_preset(target_id, PRODUCTION_VISUAL_PRESET_ID)
        administration.grant_target(client.id, target_id)
        principal = McpAuthenticationService(factory).authenticate(credential.token)
        assert principal is not None
        adapters = KodiTargetAdapterService(
            factory,
            managed_status=cast(Any, None),
            managed_inventory=cast(Any, None),
            managed_remote=cast(Any, None),
            managed_authority=cast(Any, None),
            endpoint_policy=ExternalEndpointPolicy(
                allowed_ports=(8080,), test_loopback_enabled=True
            ),
            credential_store=credentials,
            external_bridges=bridges,
        )
        authorization = McpAuthorizationService(
            factory, build_mcp_tool_registry(), targets=adapters
        )
        screenshots = ProductionScreenshotService(factory, bridges)
        tools = McpTargetToolService(
            factory,
            authorization,
            cast(Any, None),
            cast(Any, None),
            cast(Any, None),
            target_adapters=adapters,
            production_screenshots=screenshots,
        )
        # Pairing and the generic grant do not create MCP screenshot authority.
        with pytest.raises(McpAuthorizationError):
            tools.screenshot_capture(principal, target_id=target_id, request_id=uuid4())
        with pytest.raises(
            McpAdministrationError, match="production_scope_target_invalid"
        ):
            administration.grant_production_visual_scope(
                client.id,
                target_id,
                identity_generation=1,
                bridge_trust_revision=1,
            )
        administration.grant_production_inspection_scope(
            client.id, target_id, identity_generation=1
        )
        administration.grant_production_visual_scope(
            client.id,
            target_id,
            identity_generation=1,
            bridge_trust_revision=1,
        )
        request_id = uuid4()
        first = tools.screenshot_capture(
            principal, target_id=target_id, request_id=request_id
        )
        replay = tools.screenshot_capture(
            principal, target_id=target_id, request_id=request_id
        )
        assert first.png == replay.png
        assert first.metadata == replay.metadata
        assert fake.capture_count == 1
        # Caller replay identity is client-scoped. Reusing the same caller UUID
        # from another authorized client must create a distinct protected bridge
        # request rather than disclose the first client's cached response.
        second_client = administration.create_client(
            display_name="Second production visual reader",
            preset_id=PRODUCTION_VISUAL_PRESET_ID,
        )
        second_credential = administration.issue_credential(second_client.id)
        administration.grant_target(second_client.id, target_id)
        administration.grant_production_inspection_scope(
            second_client.id, target_id, identity_generation=1
        )
        administration.grant_production_visual_scope(
            second_client.id,
            target_id,
            identity_generation=1,
            bridge_trust_revision=1,
        )
        second_principal = McpAuthenticationService(factory).authenticate(
            second_credential.token
        )
        assert second_principal is not None
        second = tools.screenshot_capture(
            second_principal, target_id=target_id, request_id=request_id
        )
        assert second.metadata.capture_id == request_id
        assert fake.capture_count == 2
        with factory() as session:
            bridge_request_ids = session.scalars(
                text(
                    "SELECT bridge_request_id FROM mcp_production_screenshot_receipts "
                    "WHERE request_id=:request ORDER BY client_id"
                ),
                {"request": str(request_id)},
            ).all()
        assert len(bridge_request_ids) == len(set(bridge_request_ids)) == 2

        # A required metadata commit failure after capture discards volatile
        # pixels while preserving the completed bridge replay identity.
        audit_client = administration.create_client(
            display_name="Audit failure reader",
            preset_id=PRODUCTION_VISUAL_PRESET_ID,
        )
        audit_request = uuid4()

        def fail_metadata_commit(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("forced metadata failure")

        with monkeypatch.context() as patch:
            patch.setattr(screenshots, "_complete", fail_metadata_commit)
            with pytest.raises(RuntimeError, match="forced metadata failure"):
                screenshots.capture(
                    client_id=audit_client.id,
                    target_id=target_id,
                    request_id=audit_request,
                    identity_generation=1,
                    bridge_trust_revision=1,
                )
        with factory() as session:
            protected_bridge_request = session.scalar(
                text(
                    "SELECT bridge_request_id FROM mcp_production_screenshot_receipts "
                    "WHERE client_id=:client AND request_id=:request"
                ),
                {
                    "client": str(audit_client.id),
                    "request": str(audit_request),
                },
            )
        assert protected_bridge_request is not None
        with pytest.raises(
            ExternalBridgeServiceError, match="screenshot_replay_unavailable"
        ):
            bridges.capture_screenshot(
                target_id, request_id=UUID(protected_bridge_request)
            )
        with pytest.raises(
            ProductionScreenshotError, match="screenshot_replay_unavailable"
        ):
            screenshots.capture(
                client_id=client.id,
                target_id=target_id,
                request_id=request_id,
                identity_generation=1,
                bridge_trust_revision=2,
            )
        assert fake.capture_count == 3
        restarted_bridges = ExternalBridgeService(
            factory,
            endpoint_policy=ExternalEndpointPolicy(
                allowed_ports=(fake.port,), test_loopback_enabled=True
            ),
            secret_store=ExternalBridgeSecretStore(
                tmp_path / "bridge-secrets", master_key=b"b" * 32, key_version=1
            ),
        )
        restarted_screenshots = ProductionScreenshotService(factory, restarted_bridges)
        with pytest.raises(
            ProductionScreenshotError, match="screenshot_replay_unavailable"
        ):
            restarted_screenshots.capture(
                client_id=client.id,
                target_id=target_id,
                request_id=request_id,
                identity_generation=1,
                bridge_trust_revision=1,
            )
        assert fake.capture_count == 3
        with pytest.raises(McpAuthorizationError, match="rate_limited"):
            tools.screenshot_capture(principal, target_id=target_id, request_id=uuid4())

        # Pairing did not grant authority by itself, and bridge drift affects
        # only visual authority. Native inspection state remains confirmed.
        original_addon_version = fake.addon_version
        fake.addon_version = "9.9.9"
        with pytest.raises(
            ExternalBridgeServiceError, match="bridge_build_not_reviewed"
        ):
            bridges.reverify(target_id)
        suspended = bridges.status(target_id)
        assert suspended.production_bridge_trust_revision == 2
        assert suspended.pairing_state.value == "needs_reverification"
        fake.addon_version = original_addon_version
        bridges.mark_needs_reverification(target_id)
        with pytest.raises(McpAuthorizationError):
            tools.screenshot_capture(principal, target_id=target_id, request_id=uuid4())
        refreshed_acknowledgement = bridges.acknowledge_production_http_risk(
            target_id, bridge_port=fake.port
        )
        assert refreshed_acknowledgement.bridge_trust_revision == 2
        assert bridges.reverify(target_id).production_bridge_trust_revision == 2
        # Reverification never revives the old revision-1 visual scope.
        with pytest.raises(McpAuthorizationError):
            tools.screenshot_capture(principal, target_id=target_id, request_id=uuid4())
        with factory() as session:
            assert (
                session.scalar(
                    text(
                        "SELECT designation_state FROM external_production_policies "
                        "WHERE target_id=:target"
                    ),
                    {"target": str(target_id)},
                )
                == "confirmed"
            )
            # No SQLite column contains production PNG bytes.
            assert (
                session.scalar(
                    text(
                        "SELECT COUNT(*) FROM mcp_production_screenshot_receipts "
                        "WHERE response_digest IS NOT NULL"
                    )
                )
                == 2
            )
    engine.dispose()


def test_failed_production_pairing_retires_the_show_once_token(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'failed-pairing.db'}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    target_id, _credentials = _production_target(factory, tmp_path)
    secret_root = tmp_path / "failed-pairing-secrets"

    with FakeExternalBridge(target_id=target_id, token=b"x" * 32) as fake:
        fake.capabilities = ["screenshot"]
        bridges = ExternalBridgeService(
            factory,
            endpoint_policy=ExternalEndpointPolicy(
                allowed_ports=(fake.port,), test_loopback_enabled=True
            ),
            secret_store=ExternalBridgeSecretStore(
                secret_root, master_key=b"b" * 32, key_version=1
            ),
        )
        bridges.register_reviewed_build(
            semantic_fingerprint=fake.build_fingerprint,
            zip_sha256="a" * 64,
            addon_id="service.kodi_lab_bridge",
            addon_version=fake.addon_version,
            protocol_version=fake.protocol_version,
            capabilities=tuple(fake.capabilities),
            supported_kodi_versions=(fake.kodi_version,),
            source_reference="deterministic local fixture",
        )
        bridges.acknowledge_production_http_risk(target_id, bridge_port=fake.port)
        bridges.generate_pairing_token(target_id, bridge_port=fake.port)
        assert len(list(secret_root.glob("*.json"))) == 1

        with pytest.raises(ExternalBridgeServiceError):
            bridges.attempt_pairing(target_id)

        status = bridges.status(target_id)
        assert not status.pairing_token_pending
        assert list(secret_root.glob("*.json")) == []
    engine.dispose()
