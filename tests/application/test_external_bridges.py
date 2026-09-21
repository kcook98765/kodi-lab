from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

import kodi_lab.application.external_bridges as external_bridge_module
from kodi_lab.application.external_bridges import (
    ExternalBridgeService,
    ExternalBridgeServiceError,
)
from kodi_lab.application.mcp_administration import (
    READ_ONLY_INSPECTION_PRESET_ID,
    McpAdministrationService,
    McpPrincipal,
)
from kodi_lab.application.mcp_target_adapters import KodiTargetAdapterService
from kodi_lab.development.deployment import capture_development_tree
from kodi_lab.domain import ExternalBridgePairingState, ExternalProtocolKind
from kodi_lab.kodi.bridge import ExternalBridgeClient
from kodi_lab.kodi.external import (
    ExternalEndpointError,
    ExternalEndpointPolicy,
    ValidatedExternalEndpoint,
)
from kodi_lab.mcp.authorization import McpAuthorizationError, McpAuthorizationService
from kodi_lab.mcp.registry import build_mcp_tool_registry
from kodi_lab.mcp.tools import McpTargetToolService
from kodi_lab.storage.engine import create_session_factory
from kodi_lab.storage.metadata import Base
from kodi_lab.storage.models import (
    ExternalBridgeEventRow,
    ExternalBridgePairingRow,
    ExternalBridgeRequestReceiptRow,
    ExternalKodiTargetRow,
    TargetRow,
)
from kodi_lab.storage.secrets import ExternalBridgeSecretStore, ExternalCredentialStore
from tests.kodi.fake_external_bridge import FakeExternalBridge

NOW = "2026-09-21T12:00:00.000000Z"


def _decode_token(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=")


def test_bridge_kodi_compatibility_is_exact_except_for_the_kodi_22_line() -> None:
    compatible = ExternalBridgeService._kodi_versions_compatible
    assert compatible("19.5", "19.5")
    assert compatible("20.5", "20.5")
    assert compatible("21.3", "21.3")
    assert compatible("22.0", "22.0-beta.2")
    assert compatible("22.0", "22.0")
    assert not compatible("21.3", "21.4")
    assert not compatible("22.0", "23.0")


def _seed(factory: sessionmaker[Session], target_id: UUID) -> None:
    with factory.begin() as session:
        session.add(
            TargetRow(
                id=str(target_id),
                name="Disposable external bridge",
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
                target_id=str(target_id),
                protocol_kind="jsonrpc_http",
                host="127.0.0.1",
                port=8080,
                authentication_mode="none",
                secret_reference=None,
                configured_capabilities_json=json.dumps(
                    ["screenshot", "target_inspect", "workspace_deployment"]
                ),
                verified_capabilities_json=json.dumps(["target_inspect"]),
                administrative_state="enabled",
                verification_state="verified",
                observation_state="reachable",
                verified_addresses_json='["127.0.0.1"]',
                verified_kodi_name="Kodi",
                verified_kodi_version="21.3",
                observed_kodi_name="Kodi",
                observed_kodi_version="21.3",
                observed_protocol_version="13.0.0",
                pending_observation_id=None,
                created_at=NOW,
                verified_at=NOW,
                deactivated_at=None,
                last_observed_at=NOW,
            )
        )


def _service(
    factory: sessionmaker[Session], root: Path, bridge_port: int
) -> ExternalBridgeService:
    return ExternalBridgeService(
        factory,
        endpoint_policy=ExternalEndpointPolicy(
            allowed_ports=(bridge_port,), test_loopback_enabled=True
        ),
        secret_store=ExternalBridgeSecretStore(
            root, master_key=b"b" * 32, key_version=1
        ),
    )


def test_web_pair_confirm_capture_replay_receipt_and_revoke(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'bridge.db'}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    target_id = uuid4()
    _seed(factory, target_id)
    with FakeExternalBridge(target_id=target_id, token=b"x" * 32) as fake:
        secrets = tmp_path / "bridge-secrets"
        service = _service(factory, secrets, fake.port)
        generated = service.generate_pairing_token(target_id, bridge_port=fake.port)
        assert service.status(target_id).pairing_token_pending is True
        assert generated.token not in repr(service.status(target_id))
        assert generated.token.encode() not in next(secrets.iterdir()).read_bytes()

        fake.token = _decode_token(generated.token)
        observed = service.attempt_pairing(target_id)
        assert observed.pairing_state is ExternalBridgePairingState.PAIRING_PENDING
        assert observed.observed_bridge_id == fake.bridge_id
        assert observed.verified_capabilities == ()
        with pytest.raises(ExternalBridgeServiceError, match="bridge_not_paired"):
            service.attempt_pairing(target_id)

        paired = service.confirm_pairing(target_id)
        assert paired.pairing_state is ExternalBridgePairingState.PAIRED
        assert paired.pinned_bridge_id == fake.bridge_id
        assert paired.verified_capabilities == (
            "screenshot",
            "workspace_deployment",
        )
        fake.tamper = "drop_screenshot_once"
        capture = service.capture_screenshot(target_id)
        assert capture.bridge_id == fake.bridge_id
        assert capture.byte_size == len(capture.png)
        assert fake.capture_count == 1

        with factory() as session:
            receipt = session.scalar(select(ExternalBridgeRequestReceiptRow))
            events = session.scalars(select(ExternalBridgeEventRow)).all()
        assert receipt is not None and receipt.state == "completed"
        assert receipt.response_digest == capture.digest
        assert {event.event_kind for event in events} >= {
            "pairing_token_generated",
            "pairing_attempted",
            "bridge_paired",
            "screenshot_requested",
            "screenshot_completed",
        }
        assert generated.token not in repr(events)
        assert capture.png not in repr(events).encode()

        expected_build = fake.build_fingerprint
        fake.build_fingerprint = "f" * 64
        with pytest.raises(
            ExternalBridgeServiceError, match="bridge_identity_mismatch"
        ):
            service.capture_screenshot(target_id)
        assert (
            service.status(target_id).pairing_state
            is ExternalBridgePairingState.NEEDS_REVERIFICATION
        )
        assert service.screenshot_available(target_id) is False
        fake.build_fingerprint = expected_build
        assert (
            service.reverify(target_id).pairing_state
            is ExternalBridgePairingState.PAIRED
        )

        rotated = service.generate_pairing_token(target_id, bridge_port=fake.port)
        assert service.screenshot_available(target_id) is False
        assert len(list(secrets.iterdir())) == 1
        fake.token = _decode_token(rotated.token)
        service.attempt_pairing(target_id)
        assert (
            service.confirm_pairing(target_id).pairing_state
            is ExternalBridgePairingState.PAIRED
        )

        revoked = service.revoke(target_id)
        assert revoked.pairing_state is ExternalBridgePairingState.REVOKED
        assert service.screenshot_available(target_id) is False
        with pytest.raises(ExternalBridgeServiceError, match="bridge_not_paired"):
            service.capture_screenshot(target_id)
        assert list(secrets.iterdir()) == []
    engine.dispose()


def test_bridge_deploys_exact_manifest_once_and_recovers_terminal_receipt(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'deployment.db'}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    target_id = uuid4()
    _seed(factory, target_id)
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "addon.xml").write_text(
        '<addon id="plugin.video.fixture" name="Fixture" version="2.0.0">'
        '<extension point="xbmc.python.pluginsource" library="addon.py">'
        "<provides>video</provides></extension></addon>"
    )
    (stage / "addon.py").write_text("print('fixture')\n")
    manifest = capture_development_tree(tmp_path, "stage")
    with FakeExternalBridge(target_id=target_id, token=b"x" * 32) as fake:
        service = _service(factory, tmp_path / "bridge-secrets", fake.port)
        generated = service.generate_pairing_token(target_id, bridge_port=fake.port)
        fake.token = _decode_token(generated.token)
        service.attempt_pairing(target_id)
        service.confirm_pairing(target_id)
        operation_id = uuid4()
        request_id = uuid4()
        fake.tamper = "drop_deployment_begin_once"
        first = service.deploy_workspace(
            target_id,
            operation_id=operation_id,
            request_id=request_id,
            addon_id=fake.addon_id,
            addon_version="2.0.0",
            generation=4,
            manifest=manifest,
            stage_root=stage,
        )
        replay = service.deploy_workspace(
            target_id,
            operation_id=operation_id,
            request_id=request_id,
            addon_id=fake.addon_id,
            addon_version="2.0.0",
            generation=4,
            manifest=manifest,
            stage_root=stage,
        )
        recovered = service.deployment_receipt(target_id, operation_id)
        observed = service.inspect_addon(target_id, fake.addon_id)
        assert first.state == replay.state == recovered.state == "succeeded"
        assert first.receipt_id == replay.receipt_id == recovered.receipt_id
        assert observed.manifest_digest == manifest.digest
        assert fake.replacement_count == 1
        fake.tamper = "drop_deployment_commit_once"
        recovered_after_loss = service.deploy_workspace(
            target_id,
            operation_id=uuid4(),
            request_id=uuid4(),
            addon_id=fake.addon_id,
            addon_version="2.0.0",
            generation=5,
            manifest=manifest,
            stage_root=stage,
        )
        assert recovered_after_loss.state == "succeeded"
        assert fake.replacement_count == 2
    engine.dispose()


def test_bridge_transfer_duration_exhaustion_never_replaces(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'duration.db'}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    target_id = uuid4()
    _seed(factory, target_id)
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "addon.xml").write_text(
        '<addon id="plugin.video.fixture" name="Fixture" version="2.0.0">'
        '<extension point="xbmc.python.pluginsource" library="addon.py">'
        "<provides>video</provides></extension></addon>"
    )
    (stage / "addon.py").write_text("print('fixture')\n")
    manifest = capture_development_tree(tmp_path, "stage")
    with FakeExternalBridge(target_id=target_id, token=b"x" * 32) as fake:
        service = _service(factory, tmp_path / "bridge-secrets", fake.port)
        generated = service.generate_pairing_token(target_id, bridge_port=fake.port)
        fake.token = _decode_token(generated.token)
        service.attempt_pairing(target_id)
        service.confirm_pairing(target_id)
        monkeypatch.setattr(
            external_bridge_module, "MAX_DEPLOYMENT_DURATION_SECONDS", -1
        )
        with pytest.raises(ExternalBridgeServiceError, match="transfer_invalid"):
            service.deploy_workspace(
                target_id,
                operation_id=uuid4(),
                request_id=uuid4(),
                addon_id=fake.addon_id,
                addon_version="2.0.0",
                generation=4,
                manifest=manifest,
                stage_root=stage,
            )
        assert fake.replacement_count == 0
    engine.dispose()


def test_pairing_token_is_target_bound_expiring_single_use_and_rotated(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'tokens.db'}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    target_id = uuid4()
    wrong_target_id = uuid4()
    _seed(factory, target_id)
    _seed(factory, wrong_target_id)
    with FakeExternalBridge(target_id=target_id, token=b"x" * 32) as fake:
        secrets = tmp_path / "bridge-secrets"
        service = _service(factory, secrets, fake.port)
        first = service.generate_pairing_token(target_id, bridge_port=fake.port)
        first_file = next(secrets.iterdir())
        second = service.generate_pairing_token(target_id, bridge_port=fake.port)
        assert first.token != second.token
        assert not first_file.exists()

        fake.token = _decode_token(first.token)
        with pytest.raises(
            ExternalBridgeServiceError, match="bridge_authentication_failed"
        ):
            service.attempt_pairing(target_id)

        with factory.begin() as session:
            row = session.get(ExternalBridgePairingRow, str(target_id))
            assert row is not None
            row.pairing_token_expires_at = "2020-01-01T00:00:00.000000Z"
        with pytest.raises(
            ExternalBridgeServiceError, match="bridge_authentication_failed"
        ):
            service.attempt_pairing(target_id)

        wrong = service.generate_pairing_token(wrong_target_id, bridge_port=fake.port)
        fake.token = _decode_token(wrong.token)
        with pytest.raises(
            ExternalBridgeServiceError, match="bridge_authentication_failed"
        ):
            service.attempt_pairing(wrong_target_id)
    engine.dispose()


def test_bridge_endpoint_reuses_external_ssrf_policy(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'ssrf.db'}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    target_id = uuid4()
    _seed(factory, target_id)
    service = ExternalBridgeService(
        factory,
        endpoint_policy=ExternalEndpointPolicy(allowed_ports=(8765,)),
        secret_store=ExternalBridgeSecretStore(
            tmp_path / "secrets", master_key=b"b" * 32, key_version=1
        ),
        client=ExternalBridgeClient(),
    )
    with pytest.raises(ExternalBridgeServiceError, match="bridge_unreachable"):
        service.generate_pairing_token(target_id, bridge_port=8765)

    policy = ExternalEndpointPolicy(allowed_ports=(8765,))
    for address in (
        "8.8.8.8",
        "127.0.0.1",
        "169.254.169.254",
        "10.250.0.2",
        "::ffff:192.168.1.2",
    ):
        with pytest.raises(ExternalEndpointError):
            policy.validate(ExternalProtocolKind.JSONRPC_HTTP, address, 8765)
    assert ValidatedExternalEndpoint(
        ExternalProtocolKind.JSONRPC_HTTP, "192.168.50.10", 8765
    ) == policy.validate(ExternalProtocolKind.JSONRPC_HTTP, "192.168.50.10", 8765)
    engine.dispose()


def test_mcp_external_screenshot_requires_pairing_grant_permission_and_policy(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'mcp-bridge.db'}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    target_id = uuid4()
    _seed(factory, target_id)
    with FakeExternalBridge(target_id=target_id, token=b"x" * 32) as fake:
        policy = ExternalEndpointPolicy(
            allowed_ports=(8080, fake.port), test_loopback_enabled=True
        )
        bridge = ExternalBridgeService(
            factory,
            endpoint_policy=policy,
            secret_store=ExternalBridgeSecretStore(
                tmp_path / "bridge-secrets", master_key=b"b" * 32, key_version=1
            ),
        )
        adapters = KodiTargetAdapterService(
            factory,
            managed_status=cast(Any, None),
            managed_inventory=cast(Any, None),
            managed_remote=cast(Any, None),
            managed_authority=cast(Any, None),
            endpoint_policy=policy,
            credential_store=ExternalCredentialStore(
                tmp_path / "native-secrets", master_key=None, key_version=1
            ),
            external_bridges=bridge,
        )
        administration = McpAdministrationService(factory)
        client_a = administration.create_client(
            display_name="Paired screenshot client",
            preset_id=READ_ONLY_INSPECTION_PRESET_ID,
        )
        client_b = administration.create_client(
            display_name="Ungranted screenshot client",
            preset_id=READ_ONLY_INSPECTION_PRESET_ID,
        )
        administration.assign_target_preset(target_id, READ_ONLY_INSPECTION_PRESET_ID)
        administration.grant_target(client_a.id, target_id)
        authorization = McpAuthorizationService(
            factory, build_mcp_tool_registry(), targets=adapters
        )
        principal_a = McpPrincipal(
            client_id=client_a.id,
            credential_id=uuid4(),
            preset_id=READ_ONLY_INSPECTION_PRESET_ID,
            permissions=frozenset(),
        )
        principal_b = McpPrincipal(
            client_id=client_b.id,
            credential_id=uuid4(),
            preset_id=READ_ONLY_INSPECTION_PRESET_ID,
            permissions=frozenset(),
        )
        with pytest.raises(McpAuthorizationError, match="capability_unavailable"):
            authorization.authorize_target(principal_a, "screenshot_capture", target_id)

        generated = bridge.generate_pairing_token(target_id, bridge_port=fake.port)
        fake.token = _decode_token(generated.token)
        bridge.attempt_pairing(target_id)
        bridge.confirm_pairing(target_id)
        authorization.authorize_target(principal_a, "screenshot_capture", target_id)
        tools = McpTargetToolService(
            factory,
            authorization,
            cast(Any, None),
            cast(Any, None),
            cast(Any, None),
            target_adapters=adapters,
        )
        capture = tools.screenshot_capture(principal_a, target_id=target_id)
        assert capture.metadata.mime_type == "image/png"
        assert capture.metadata.byte_size == len(capture.png)

        with pytest.raises(
            McpAuthorizationError, match="target_not_found_or_not_authorized"
        ):
            authorization.authorize_target(principal_b, "screenshot_capture", target_id)
        for excluded in (
            "workspace_deploy",
            "workspace_execute",
            "promotion_request",
        ):
            with pytest.raises(McpAuthorizationError, match="permission_denied"):
                authorization.authorize_target(principal_a, excluded, target_id)

        bridge.revoke(target_id)
        with pytest.raises(McpAuthorizationError, match="capability_unavailable"):
            authorization.authorize_target(principal_a, "screenshot_capture", target_id)
    engine.dispose()
