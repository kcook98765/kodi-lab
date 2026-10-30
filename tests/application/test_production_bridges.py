from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from mcp_types import CallToolRequestParams, CallToolResult, ImageContent, TextContent
from sqlalchemy import create_engine, select, text
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
    McpPrincipal,
)
from kodi_lab.application.mcp_target_adapters import KodiTargetAdapterService
from kodi_lab.application.production_inspection import (
    PRODUCTION_VISUAL_PRESET_ID,
    ProductionInspectionError,
    evaluate_production_visual_state,
)
from kodi_lab.application.production_screenshots import (
    ProductionScreenshotError,
    ProductionScreenshotService,
)
from kodi_lab.domain import (
    ExternalAuthenticationMode,
    ExternalProtocolKind,
    TargetType,
)
from kodi_lab.kodi.bridge import ExternalBridgeError
from kodi_lab.kodi.external import (
    ExternalEndpointPolicy,
    ExternalKodiProbeResult,
    ValidatedExternalEndpoint,
)
from kodi_lab.mcp import transport as transport_module
from kodi_lab.mcp.authorization import McpAuthorizationError, McpAuthorizationService
from kodi_lab.mcp.registry import build_mcp_tool_registry
from kodi_lab.mcp.tools import (
    McpTargetToolService,
    ScreenshotCaptureMetadata,
    ScreenshotToolResult,
)
from kodi_lab.screenshot_diagnostics import (
    ScreenshotDiagnosticTrace,
    screenshot_diagnostic_trace,
)
from kodi_lab.storage.engine import create_session_factory
from kodi_lab.storage.metadata import Base
from kodi_lab.storage.models import (
    ExternalBridgeEventRow,
    ExternalBridgeRequestReceiptRow,
    McpProductionScreenshotReceiptRow,
    ProductionMutationControlRow,
)
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


def test_reviewed_build_service_represents_exact_bridge_1_1_8(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'reviewed-1.1.8.db'}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    bridges = ExternalBridgeService(
        factory,
        endpoint_policy=ExternalEndpointPolicy(
            allowed_ports=(8765,), test_loopback_enabled=True
        ),
        secret_store=ExternalBridgeSecretStore(
            tmp_path / "reviewed-secrets", master_key=b"b" * 32, key_version=1
        ),
    )

    reviewed = bridges.register_reviewed_build(
        semantic_fingerprint=(
            "4a055b9479aeb72d2f8fe16d317ed525f69991908b28580d8a071be33aa26e5a"
        ),
        zip_sha256=("20a14873a46969cd3f1f962875a3905caa56ce6f088e95f6d3b2b8de6e666762"),
        addon_id="service.kodi_lab_bridge",
        addon_version="1.1.8",
        protocol_version="1",
        capabilities=("screenshot", "workspace_deployment"),
        supported_kodi_versions=("19.5", "20.5", "21.3", "22.0"),
        source_reference="local production-validation candidate",
        administrator_reference="deterministic_test",
    )

    assert reviewed.addon_version == "1.1.8"
    assert reviewed.capabilities == ("screenshot", "workspace_deployment")
    assert reviewed.supported_kodi_versions == ("19.5", "20.5", "21.3", "22.0")
    assert reviewed.state == "active"
    assert bridges.list_reviewed_builds() == [reviewed]
    engine.dispose()


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


def _pair_production_bridge(
    factory: sessionmaker[Session],
    tmp_path: Path,
    target_id: UUID,
    fake: FakeExternalBridge,
) -> ExternalBridgeService:
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
    bridges.acknowledge_production_http_risk(target_id, bridge_port=fake.port)
    token = bridges.generate_pairing_token(target_id, bridge_port=fake.port)
    fake.token = _decode(token.token)
    bridges.attempt_pairing(target_id)
    paired = bridges.confirm_pairing(target_id)
    assert paired.reviewed_build_id == reviewed.build_id
    return bridges


class _ProductionScreenshotTransportTools:
    def __init__(self, screenshots: ProductionScreenshotService) -> None:
        self._screenshots = screenshots

    def screenshot_capture(
        self,
        principal: McpPrincipal,
        *,
        target_id: UUID,
        request_id: UUID,
    ) -> ScreenshotToolResult:
        capture = self._screenshots.capture(
            client_id=principal.client_id,
            target_id=target_id,
            request_id=request_id,
            identity_generation=1,
            bridge_trust_revision=1,
        )
        return ScreenshotToolResult(
            metadata=ScreenshotCaptureMetadata(
                target_id=target_id,
                capture_id=capture.capture_id,
                captured_at=capture.captured_at,
                width=capture.width,
                height=capture.height,
                mime_type="image/png",
                byte_size=capture.byte_size,
            ),
            png=capture.png,
        )


@pytest.mark.parametrize("failure", ["correlation", "trace_binding", "evidence_hash"])
def test_transport_precomputation_failure_preserves_real_receipts_and_reachability(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: str
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / f'transport-{failure}.db'}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    target_id, _credentials = _production_target(factory, tmp_path)

    with FakeExternalBridge(target_id=target_id, token=b"x" * 32) as fake:
        bridges = _pair_production_bridge(factory, tmp_path, target_id, fake)
        screenshots = ProductionScreenshotService(factory, bridges)
        tools = _ProductionScreenshotTransportTools(screenshots)
        transport = transport_module.build_mcp_http_transport(
            authentication=cast(Any, None),
            authorization=cast(Any, None),
            registry=cast(Any, None),
            tools=cast(Any, tools),
        )
        entry = transport.server.get_request_handler("tools/call")
        assert entry is not None
        principal = McpPrincipal(
            client_id=uuid4(),
            credential_id=uuid4(),
            preset_id=uuid4(),
            permissions=frozenset(),
        )
        request_id = uuid4()
        if failure == "correlation":

            def fail_correlation(*_args: object, **_kwargs: object) -> object:
                raise RuntimeError("forced diagnostic correlation failure")

            monkeypatch.setattr(
                transport_module, "mcp_audit_request_correlation", fail_correlation
            )
        elif failure == "trace_binding":

            class _BrokenTraceContext:
                def __enter__(self) -> None:
                    raise RuntimeError("forced diagnostic trace binding failure")

                def __exit__(self, *_args: object) -> None:
                    raise RuntimeError("forced diagnostic trace cleanup failure")

            monkeypatch.setattr(
                transport_module,
                "screenshot_diagnostic_trace",
                lambda **_kwargs: _BrokenTraceContext(),
            )
        else:
            original_sha256 = hashlib.sha256

            def fail_evidence_hash(value: bytes = b"") -> Any:
                if value.startswith(b"\x89PNG\r\n\x1a\n"):
                    raise RuntimeError("forced diagnostic evidence hash failure")
                return original_sha256(value)

            monkeypatch.setattr(
                transport_module,
                "hashlib",
                SimpleNamespace(sha256=fail_evidence_hash),
            )

        token = transport_module._principal_context.set(principal)
        try:
            response = asyncio.run(
                cast(
                    Any,
                    entry.handler(
                        cast(Any, SimpleNamespace(request_id="diagnostic-request")),
                        CallToolRequestParams(
                            name="screenshot_capture",
                            arguments={
                                "target_id": str(target_id),
                                "request_id": str(request_id),
                            },
                        ),
                    ),
                )
            )
        finally:
            transport_module._principal_context.reset(token)

        result = cast(CallToolResult, response)
        assert result.is_error is False
        assert len(result.content) == 2
        image = ImageContent.model_validate(result.content[0])
        assert image.mime_type == "image/png"
        metadata = json.loads(TextContent.model_validate(result.content[1]).text)
        assert metadata == result.structured_content
        assert metadata["target_id"] == str(target_id)
        assert (metadata["width"], metadata["height"]) == (1280, 720)
        assert metadata["byte_size"] == len(base64.b64decode(image.data))
        assert fake.capture_count == 1
        assert bridges.status(target_id).reachability_state.value == "reachable"
        with factory() as session:
            mcp_receipt = session.get(
                McpProductionScreenshotReceiptRow,
                {"client_id": str(principal.client_id), "request_id": str(request_id)},
            )
            assert mcp_receipt is not None and mcp_receipt.state == "completed"
            assert (
                mcp_receipt.response_digest
                == hashlib.sha256(base64.b64decode(image.data)).hexdigest()
            )
            assert mcp_receipt.byte_size == metadata["byte_size"]
            assert (mcp_receipt.width, mcp_receipt.height) == (
                metadata["width"],
                metadata["height"],
            )
            bridge_receipt = session.get(
                ExternalBridgeRequestReceiptRow,
                {
                    "target_id": str(target_id),
                    "request_id": mcp_receipt.bridge_request_id,
                },
            )
            assert bridge_receipt is not None and bridge_receipt.state == "completed"
    engine.dispose()


@pytest.mark.parametrize(
    ("tamper", "code"),
    [
        ("capture_error_invalid", "screenshot_invalid"),
        ("invalid_png", "screenshot_format_invalid"),
        ("capture_error", "screenshot_capture_failed"),
    ],
)
def test_screenshot_failure_rejects_receipts_without_losing_reachability(
    tmp_path: Path, tamper: str, code: str
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'screenshot-failure.db'}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    target_id, _credentials = _production_target(factory, tmp_path)

    with FakeExternalBridge(target_id=target_id, token=b"x" * 32) as fake:
        bridges = _pair_production_bridge(factory, tmp_path, target_id, fake)
        screenshots = ProductionScreenshotService(factory, bridges)
        before = bridges.status(target_id)
        with factory() as session:
            visual_before = evaluate_production_visual_state(session, target_id)

        fake.tamper = tamper
        client_id = uuid4()
        request_id = uuid4()
        with pytest.raises(ProductionScreenshotError, match=code):
            screenshots.capture(
                client_id=client_id,
                target_id=target_id,
                request_id=request_id,
                identity_generation=visual_before.identity_generation,
                bridge_trust_revision=visual_before.bridge_trust_revision,
            )

        with factory() as session:
            receipt = session.get(
                McpProductionScreenshotReceiptRow,
                {"client_id": str(client_id), "request_id": str(request_id)},
            )
            assert receipt is not None
            bridge_receipt = session.get(
                ExternalBridgeRequestReceiptRow,
                {
                    "target_id": str(target_id),
                    "request_id": receipt.bridge_request_id,
                },
            )
            assert receipt.state == "rejected"
            assert receipt.response_digest is None
            assert bridge_receipt is not None
            assert bridge_receipt.state == "rejected"
            assert bridge_receipt.response_digest is None
            assert (
                session.scalar(
                    select(McpProductionScreenshotReceiptRow).where(
                        McpProductionScreenshotReceiptRow.state == "completed"
                    )
                )
                is None
            )
            assert evaluate_production_visual_state(session, target_id) == visual_before

        after = bridges.status(target_id)
        assert after.reachability_state.value == "reachable"
        assert after.pairing_state == before.pairing_state
        assert after.pinned_bridge_id == before.pinned_bridge_id
        assert after.reviewed_build_id == before.reviewed_build_id
        assert (
            after.production_bridge_trust_revision
            == before.production_bridge_trust_revision
        )
        assert after.production_confirmed_at == before.production_confirmed_at
        assert cast(Any, bridges)._production_pixel_cache == {}
        assert fake.capture_count == 1

        with pytest.raises(
            ProductionScreenshotError, match="screenshot_replay_unavailable"
        ):
            screenshots.capture(
                client_id=client_id,
                target_id=target_id,
                request_id=request_id,
                identity_generation=visual_before.identity_generation,
                bridge_trust_revision=visual_before.bridge_trust_revision,
            )
        assert fake.capture_count == 1
    engine.dispose()


def test_diagnostic_failure_cannot_change_success_receipts_or_reachability(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'diagnostic-success.db'}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    target_id, _credentials = _production_target(factory, tmp_path)

    with FakeExternalBridge(target_id=target_id, token=b"x" * 32) as fake:
        bridges = _pair_production_bridge(factory, tmp_path, target_id, fake)
        screenshots = ProductionScreenshotService(factory, bridges)
        client_id = uuid4()
        request_id = uuid4()

        def fail_diagnostic_read(*_args: object, **_kwargs: object) -> str:
            raise RuntimeError("forced diagnostic reachability read failure")

        monkeypatch.setattr(
            bridges, "_screenshot_reachability_value", fail_diagnostic_read
        )
        with screenshot_diagnostic_trace(
            protocol_request={
                "type": "string",
                "byte_length": 36,
                "sha256": "a" * 64,
            },
            client_id=client_id,
            target_id=target_id,
            request_id=request_id,
        ):
            result = screenshots.capture(
                client_id=client_id,
                target_id=target_id,
                request_id=request_id,
                identity_generation=1,
                bridge_trust_revision=1,
            )

        assert result.png.startswith(b"\x89PNG\r\n\x1a\n")
        assert result.capture_id == request_id
        assert fake.capture_count == 1
        assert bridges.status(target_id).reachability_state.value == "reachable"
        with factory() as session:
            mcp_receipt = session.get(
                McpProductionScreenshotReceiptRow,
                {"client_id": str(client_id), "request_id": str(request_id)},
            )
            assert mcp_receipt is not None and mcp_receipt.state == "completed"
            bridge_receipt = session.get(
                ExternalBridgeRequestReceiptRow,
                {
                    "target_id": str(target_id),
                    "request_id": mcp_receipt.bridge_request_id,
                },
            )
            assert bridge_receipt is not None and bridge_receipt.state == "completed"
    engine.dispose()


@pytest.mark.parametrize(
    ("failure", "code", "receipt_state", "reachability"),
    [
        ("semantic", "screenshot_format_invalid", "rejected", "reachable"),
        ("transport", "bridge_unreachable", "ambiguous", "unreachable"),
    ],
)
def test_diagnostic_failure_preserves_canonical_failure_and_reachability(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure: str,
    code: str,
    receipt_state: str,
    reachability: str,
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / f'diagnostic-{failure}.db'}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    target_id, _credentials = _production_target(factory, tmp_path)

    with FakeExternalBridge(target_id=target_id, token=b"x" * 32) as fake:
        bridges = _pair_production_bridge(factory, tmp_path, target_id, fake)
        screenshots = ProductionScreenshotService(factory, bridges)
        client_id = uuid4()
        request_id = uuid4()

        def fail_diagnostic_read(*_args: object, **_kwargs: object) -> str:
            raise RuntimeError("forced diagnostic reachability read failure")

        monkeypatch.setattr(
            bridges, "_screenshot_reachability_value", fail_diagnostic_read
        )
        capture_attempts = 0
        if failure == "semantic":
            fake.tamper = "invalid_png"
        else:

            def fail_transport(*_args: object, **_kwargs: object) -> None:
                nonlocal capture_attempts
                capture_attempts += 1
                raise ExternalBridgeError("bridge_unreachable")

            monkeypatch.setattr(
                cast(Any, bridges)._client, "capture_screenshot", fail_transport
            )

        with (
            screenshot_diagnostic_trace(
                protocol_request={"type": "missing"},
                client_id=client_id,
                target_id=target_id,
                request_id=request_id,
            ),
            pytest.raises(ProductionScreenshotError) as raised,
        ):
            screenshots.capture(
                client_id=client_id,
                target_id=target_id,
                request_id=request_id,
                identity_generation=1,
                bridge_trust_revision=1,
            )

        assert raised.value.code == code
        if failure == "semantic":
            assert fake.capture_count == 1
        else:
            assert capture_attempts == 1
            assert fake.capture_count == 0
        assert bridges.status(target_id).reachability_state.value == reachability
        with factory() as session:
            mcp_receipt = session.get(
                McpProductionScreenshotReceiptRow,
                {"client_id": str(client_id), "request_id": str(request_id)},
            )
            assert mcp_receipt is not None and mcp_receipt.state == receipt_state
            bridge_receipt = session.get(
                ExternalBridgeRequestReceiptRow,
                {
                    "target_id": str(target_id),
                    "request_id": mcp_receipt.bridge_request_id,
                },
            )
            assert bridge_receipt is not None
            assert bridge_receipt.state == receipt_state
            assert bridge_receipt.response_digest is None
    engine.dispose()


def test_diagnostic_failure_cannot_mask_mcp_receipt_completion_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'diagnostic-mcp-receipt.db'}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    target_id, _credentials = _production_target(factory, tmp_path)

    with FakeExternalBridge(target_id=target_id, token=b"x" * 32) as fake:
        bridges = _pair_production_bridge(factory, tmp_path, target_id, fake)
        screenshots = ProductionScreenshotService(factory, bridges)
        client_id = uuid4()
        request_id = uuid4()

        def fail_diagnostic_read(*_args: object, **_kwargs: object) -> str:
            raise RuntimeError("forced diagnostic reachability read failure")

        def fail_completion(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("forced MCP receipt failure")

        monkeypatch.setattr(
            bridges, "_screenshot_reachability_value", fail_diagnostic_read
        )
        monkeypatch.setattr(screenshots, "_complete", fail_completion)
        with (
            screenshot_diagnostic_trace(
                protocol_request={"type": "missing"},
                client_id=client_id,
                target_id=target_id,
                request_id=request_id,
            ),
            pytest.raises(ProductionScreenshotError) as raised,
        ):
            screenshots.capture(
                client_id=client_id,
                target_id=target_id,
                request_id=request_id,
                identity_generation=1,
                bridge_trust_revision=1,
            )

        assert raised.value.code == "screenshot_receipt_persistence_failed"
        assert fake.capture_count == 1
        assert bridges.status(target_id).reachability_state.value == "reachable"
        with factory() as session:
            mcp_receipt = session.get(
                McpProductionScreenshotReceiptRow,
                {"client_id": str(client_id), "request_id": str(request_id)},
            )
            assert mcp_receipt is not None and mcp_receipt.state == "rejected"
            bridge_receipt = session.get(
                ExternalBridgeRequestReceiptRow,
                {
                    "target_id": str(target_id),
                    "request_id": mcp_receipt.bridge_request_id,
                },
            )
            assert bridge_receipt is not None and bridge_receipt.state == "completed"
            assert (
                session.scalar(
                    select(McpProductionScreenshotReceiptRow).where(
                        McpProductionScreenshotReceiptRow.state == "pending"
                    )
                )
                is None
            )
            assert (
                session.scalar(
                    select(ExternalBridgeRequestReceiptRow).where(
                        ExternalBridgeRequestReceiptRow.state == "pending"
                    )
                )
                is None
            )
    engine.dispose()


def test_bridge_receipt_completion_failure_is_bounded_at_mcp_and_observer_boundary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'diagnostic-bridge-receipt.db'}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    target_id, _credentials = _production_target(factory, tmp_path)

    with FakeExternalBridge(target_id=target_id, token=b"x" * 32) as fake:
        bridges = _pair_production_bridge(factory, tmp_path, target_id, fake)
        screenshots = ProductionScreenshotService(factory, bridges)
        principal = McpPrincipal(
            client_id=uuid4(),
            credential_id=uuid4(),
            preset_id=uuid4(),
            permissions=frozenset(),
        )
        tools = McpTargetToolService(
            factory,
            cast(Any, None),
            cast(Any, None),
            cast(Any, None),
            cast(Any, None),
            production_screenshots=screenshots,
        )
        request_id = uuid4()

        def fail_diagnostic(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("forced diagnostic failure")

        def fail_bridge_completion(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("forced bridge receipt failure")

        monkeypatch.setattr(ScreenshotDiagnosticTrace, "_emit", fail_diagnostic)
        monkeypatch.setattr(bridges, "_complete_receipt", fail_bridge_completion)
        with (
            screenshot_diagnostic_trace(
                protocol_request={"type": "missing"},
                client_id=principal.client_id,
                target_id=target_id,
                request_id=request_id,
            ),
            pytest.raises(McpAuthorizationError) as raised,
        ):
            tools._screenshot_capture_authorized(
                principal, target_id, request_id=request_id
            )

        assert raised.value.code == "screenshot_receipt_persistence_failed"
        assert fake.capture_count == 1
        assert bridges.status(target_id).reachability_state.value == "reachable"
        with factory() as session:
            mcp_receipt = session.get(
                McpProductionScreenshotReceiptRow,
                {
                    "client_id": str(principal.client_id),
                    "request_id": str(request_id),
                },
            )
            assert mcp_receipt is not None and mcp_receipt.state == "rejected"
            bridge_receipt = session.get(
                ExternalBridgeRequestReceiptRow,
                {
                    "target_id": str(target_id),
                    "request_id": mcp_receipt.bridge_request_id,
                },
            )
            assert bridge_receipt is not None and bridge_receipt.state == "rejected"
            assert bridge_receipt.response_digest is None
            assert (
                session.scalar(
                    select(ExternalBridgeEventRow).where(
                        ExternalBridgeEventRow.target_id == str(target_id),
                        ExternalBridgeEventRow.event_kind == "screenshot_completed",
                    )
                )
                is None
            )
    engine.dispose()


def test_screenshot_network_failure_still_marks_bridge_unreachable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'screenshot-network.db'}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    target_id, _credentials = _production_target(factory, tmp_path)

    with FakeExternalBridge(target_id=target_id, token=b"x" * 32) as fake:
        bridges = _pair_production_bridge(factory, tmp_path, target_id, fake)
        screenshots = ProductionScreenshotService(factory, bridges)
        bridge_client = cast(Any, bridges)._client
        attempts = 0

        def refuse_connection(*_args: object, **_kwargs: object) -> None:
            nonlocal attempts
            attempts += 1
            raise ExternalBridgeError("bridge_unreachable")

        monkeypatch.setattr(bridge_client, "capture_screenshot", refuse_connection)
        client_id = uuid4()
        request_id = uuid4()
        with pytest.raises(ProductionScreenshotError, match="bridge_unreachable"):
            screenshots.capture(
                client_id=client_id,
                target_id=target_id,
                request_id=request_id,
                identity_generation=1,
                bridge_trust_revision=1,
            )

        assert attempts == 1
        assert bridges.status(target_id).reachability_state.value == "unreachable"
        with factory() as session:
            receipt = session.get(
                McpProductionScreenshotReceiptRow,
                {"client_id": str(client_id), "request_id": str(request_id)},
            )
            assert receipt is not None and receipt.state == "ambiguous"
            bridge_receipt = session.get(
                ExternalBridgeRequestReceiptRow,
                {
                    "target_id": str(target_id),
                    "request_id": receipt.bridge_request_id,
                },
            )
            assert bridge_receipt is not None and bridge_receipt.state == "ambiguous"
            with pytest.raises(ProductionInspectionError, match="bridge_not_paired"):
                evaluate_production_visual_state(session, target_id)
    engine.dispose()


@pytest.mark.parametrize(
    ("code", "reachability"),
    [
        ("bridge_authentication_failed", "authentication_failed"),
        ("bridge_identity_mismatch", "identity_mismatch"),
        ("bridge_protocol_unsupported", "protocol_unsupported"),
    ],
)
def test_screenshot_trust_failures_keep_existing_fail_closed_classification(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    code: str,
    reachability: str,
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'screenshot-trust.db'}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    target_id, _credentials = _production_target(factory, tmp_path)

    with FakeExternalBridge(target_id=target_id, token=b"x" * 32) as fake:
        bridges = _pair_production_bridge(factory, tmp_path, target_id, fake)
        screenshots = ProductionScreenshotService(factory, bridges)
        before = bridges.status(target_id)

        def fail_closed(*_args: object, **_kwargs: object) -> None:
            raise ExternalBridgeError(code)

        monkeypatch.setattr(
            cast(Any, bridges)._client, "capture_screenshot", fail_closed
        )
        with pytest.raises(ProductionScreenshotError, match=code):
            screenshots.capture(
                client_id=uuid4(),
                target_id=target_id,
                request_id=uuid4(),
                identity_generation=1,
                bridge_trust_revision=1,
            )

        after = bridges.status(target_id)
        assert after.reachability_state.value == reachability
        assert after.pairing_state == before.pairing_state
        assert (
            after.production_bridge_trust_revision
            == before.production_bridge_trust_revision
        )
        with (
            factory() as session,
            pytest.raises(ProductionInspectionError, match="bridge_not_paired"),
        ):
            evaluate_production_visual_state(session, target_id)
    engine.dispose()


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
        bridge_client = cast(Any, bridges)._client
        identity = bridge_client.identity
        attempts = 0

        def fail_once(*args: object, **kwargs: object) -> object:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise ExternalBridgeError("bridge_unreachable")
            return identity(*args, **kwargs)

        monkeypatch.setattr(bridge_client, "identity", fail_once)
        with pytest.raises(ExternalBridgeServiceError, match="bridge_unreachable"):
            bridges.authenticate_production_identity(target_id)
        assert bridges.status(target_id).reachability_state.value == "unreachable"
        bridges.authenticate_production_identity(target_id)
        assert bridges.status(target_id).reachability_state.value == "reachable"
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
        assert fake.capture_count == 0
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
            with pytest.raises(
                ProductionScreenshotError,
                match="screenshot_receipt_persistence_failed",
            ):
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
        with factory() as session:
            failed_receipt = session.get(
                McpProductionScreenshotReceiptRow,
                {
                    "client_id": str(audit_client.id),
                    "request_id": str(audit_request),
                },
            )
            assert failed_receipt is not None
            assert failed_receipt.state == "rejected"
            assert failed_receipt.response_digest is None
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
        assert fake.capture_count == 3
        with pytest.raises(
            ExternalBridgeServiceError, match="transport_acknowledgement_required"
        ):
            bridges.reverify(target_id)
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
