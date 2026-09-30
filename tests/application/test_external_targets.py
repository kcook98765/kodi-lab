from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.external_targets import (
    ExternalTargetAdministrationService,
    ExternalTargetError,
    ExternalTargetRead,
)
from kodi_lab.application.mcp_administration import (
    READ_ONLY_INSPECTION_PRESET_ID,
    McpAdministrationService,
)
from kodi_lab.domain import (
    ExternalAuthenticationMode,
    ExternalProtocolKind,
)
from kodi_lab.kodi.external import (
    ExternalEndpointPolicy,
    ExternalKodiProbeResult,
    ValidatedExternalEndpoint,
)
from kodi_lab.mcp.registry import build_mcp_tool_registry
from kodi_lab.storage.engine import create_session_factory
from kodi_lab.storage.metadata import Base
from kodi_lab.storage.models import (
    ExternalKodiTargetEventRow,
    ExternalKodiTargetObservationRow,
    ExternalKodiTargetRow,
    TargetRow,
)
from kodi_lab.storage.secrets import ExternalCredentialStore, ExternalKodiCredential


class FakeVerifier:
    def __init__(self) -> None:
        self.name = "Kodi"
        self.version = "21.3"
        self.calls = 0

    def probe(
        self,
        endpoint: ValidatedExternalEndpoint,
        credential: ExternalKodiCredential | None,
        configured: frozenset[str],
    ) -> ExternalKodiProbeResult:
        self.calls += 1
        address = endpoint.address
        return ExternalKodiProbeResult(
            addresses=(address,),
            kodi_name=self.name,
            kodi_version=self.version,
            protocol_version="13.0.0",
            verified_capabilities=(
                ("target_inspect",) if "target_inspect" in configured else ()
            ),
        )


@pytest.fixture
def external_service(
    tmp_path: Path,
) -> tuple[ExternalTargetAdministrationService, sessionmaker[Session], FakeVerifier]:
    engine = create_engine(f"sqlite:///{tmp_path / 'external.db'}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    verifier = FakeVerifier()
    service = ExternalTargetAdministrationService(
        factory,
        endpoint_policy=ExternalEndpointPolicy(allowed_ports=(8080,)),
        credential_store=ExternalCredentialStore(
            tmp_path / "secrets", master_key=b"k" * 32, key_version=1
        ),
        verifier=verifier,  # type: ignore[arg-type]
    )
    return service, factory, verifier


def _register(
    service: ExternalTargetAdministrationService, *, password: str = "secret"
) -> ExternalTargetRead:
    return service.register(
        logical_name="LAN development Kodi",
        protocol_kind=ExternalProtocolKind.JSONRPC_HTTP,
        host="192.168.50.12",
        port=8080,
        authentication_mode=ExternalAuthenticationMode.USERNAME_PASSWORD,
        configured_capabilities={
            "target_inspect",
            "addon_inventory",
            "semantic_remote",
            "screenshot",
            "workspace_deployment",
            "fixed_execution",
        },
        username="kodi",
        password=password,
    )


def test_registration_probe_confirmation_enable_and_mcp_administration_eligibility(
    external_service: tuple[
        ExternalTargetAdministrationService, sessionmaker[Session], FakeVerifier
    ],
) -> None:
    service, factory, _verifier = external_service
    created = _register(service)
    assert created.administrative_state.value == "disabled"
    assert created.verification_state.value == "unverified"
    assert created.verified_capabilities == ()
    with pytest.raises(ExternalTargetError, match="verification_required"):
        service.set_enabled(created.target_id, enabled=True)

    observed = service.probe(created.target_id)
    assert observed.verification_state.value == "unverified"
    assert observed.kodi_name == "Kodi"
    assert observed.kodi_version == "21.3"
    assert observed.verified_capabilities == ("target_inspect",)
    assert observed.pending_observation_id is not None
    with pytest.raises(ExternalTargetError, match="verification_required"):
        service.set_enabled(created.target_id, enabled=True)

    confirmed = service.confirm(created.target_id, observed.pending_observation_id)
    assert confirmed.verification_state.value == "verified"
    enabled = service.set_enabled(created.target_id, enabled=True)
    assert enabled.administrative_state.value == "enabled"

    administration = McpAdministrationService(factory)
    client = administration.create_client(
        display_name="read only", preset_id=READ_ONLY_INSPECTION_PRESET_ID
    )
    assert created.target_id in {
        item.target_id for item in administration.list_target_policies()
    }
    granted = administration.grant_target(client.id, created.target_id)
    assert granted.target_grants == (created.target_id,)
    assert tuple(build_mcp_tool_registry().names()) == (
        "addon_list",
        "development_source_list",
        "promotion_request",
        "remote_action",
        "screenshot_capture",
        "target_list",
        "target_status",
        "workspace_capture",
        "workspace_create",
        "workspace_deploy",
        "workspace_edit",
        "workspace_execute",
        "workspace_read",
        "workspace_source_scan",
        "workspace_status",
        "workspace_tree",
        "workspace_validate",
    )


def test_endpoint_and_credential_changes_require_reverification_and_hide_password(
    external_service: tuple[
        ExternalTargetAdministrationService, sessionmaker[Session], FakeVerifier
    ],
    tmp_path: Path,
) -> None:
    service, factory, _verifier = external_service
    created = _register(service, password="database-must-not-contain-me")
    observed = service.probe(created.target_id)
    assert observed.pending_observation_id is not None
    service.confirm(created.target_id, observed.pending_observation_id)
    changed = service.edit_endpoint(
        created.target_id,
        protocol_kind=ExternalProtocolKind.JSONRPC_HTTP,
        host="192.168.50.13",
        port=8080,
    )
    assert changed.verification_state.value == "needs_reverification"
    rotated = service.rotate_credentials(
        created.target_id,
        authentication_mode=ExternalAuthenticationMode.USERNAME_PASSWORD,
        username="new-user",
        password="new-password-must-not-leak",
    )
    assert rotated.verification_state.value == "needs_reverification"
    assert "password" not in rotated.model_dump()
    database_path = factory.kw["bind"].url.database
    assert database_path is not None
    database_bytes = Path(database_path).read_bytes()
    assert b"database-must-not-contain-me" not in database_bytes
    assert b"new-password-must-not-leak" not in database_bytes
    with factory() as session:
        row = session.get(ExternalKodiTargetRow, str(created.target_id))
        assert row is not None and row.secret_reference is not None
        assert row.secret_reference != "new-password-must-not-leak"


def test_identity_mismatch_blocks_enable_until_exact_admin_confirmation(
    external_service: tuple[
        ExternalTargetAdministrationService, sessionmaker[Session], FakeVerifier
    ],
) -> None:
    service, _factory, verifier = external_service
    created = _register(service)
    first = service.probe(created.target_id)
    assert first.pending_observation_id is not None
    service.confirm(created.target_id, first.pending_observation_id)
    verifier.version = "22.0"
    second = service.probe(created.target_id)
    assert second.verification_state.value == "identity_mismatch"
    with pytest.raises(ExternalTargetError, match="identity_mismatch"):
        service.set_enabled(created.target_id, enabled=True)
    assert second.pending_observation_id is not None
    accepted = service.confirm(created.target_id, second.pending_observation_id)
    assert accepted.verification_state.value == "verified"


def test_deactivation_retires_credentials_preserves_identity_and_history(
    external_service: tuple[
        ExternalTargetAdministrationService, sessionmaker[Session], FakeVerifier
    ],
) -> None:
    service, factory, _verifier = external_service
    created = _register(service)
    observed = service.probe(created.target_id)
    assert observed.pending_observation_id is not None
    service.confirm(created.target_id, observed.pending_observation_id)
    service.set_enabled(created.target_id, enabled=True)
    deactivated = service.deactivate(created.target_id)
    assert deactivated.administrative_state.value == "deactivated"
    assert deactivated.target_id == created.target_id
    with factory() as session:
        assert session.get(TargetRow, str(created.target_id)) is not None
        row = session.get(ExternalKodiTargetRow, str(created.target_id))
        assert row is not None and row.secret_reference is None
        assert (
            session.scalar(
                select(ExternalKodiTargetObservationRow).where(
                    ExternalKodiTargetObservationRow.target_id == str(created.target_id)
                )
            )
            is not None
        )
        kinds = set(
            session.scalars(
                select(ExternalKodiTargetEventRow.event_kind).where(
                    ExternalKodiTargetEventRow.target_id == str(created.target_id)
                )
            )
        )
    assert {
        "registered",
        "probe_attempted",
        "probe_succeeded",
        "verification_confirmed",
        "enabled",
        "deactivated",
    } <= kinds


def test_external_development_service_does_not_expose_production_rows(
    external_service: tuple[
        ExternalTargetAdministrationService, sessionmaker[Session], FakeVerifier
    ],
) -> None:
    service, factory, _verifier = external_service
    target_id = UUID("00000000-0000-4000-8000-000000000001")
    with factory.begin() as session:
        session.add(
            TargetRow(
                id=str(target_id),
                name="production",
                target_type="external_production",
                kodi_version=None,
                availability="unavailable",
                health="warning",
                connection_state="disconnected",
            )
        )
    with pytest.raises(ExternalTargetError, match="target_not_found"):
        service.get(target_id)


def test_existing_encrypted_credential_requires_master_key_at_startup(
    external_service: tuple[
        ExternalTargetAdministrationService, sessionmaker[Session], FakeVerifier
    ],
    tmp_path: Path,
) -> None:
    service, factory, _verifier = external_service
    _register(service)
    with pytest.raises(RuntimeError, match="master key is unavailable"):
        ExternalTargetAdministrationService(
            factory,
            endpoint_policy=ExternalEndpointPolicy(allowed_ports=(8080,)),
            credential_store=ExternalCredentialStore(
                tmp_path / "unavailable-secrets",
                master_key=None,
                key_version=1,
            ),
        )


def test_probe_result_cannot_confirm_configuration_changed_in_flight(
    external_service: tuple[
        ExternalTargetAdministrationService, sessionmaker[Session], FakeVerifier
    ],
) -> None:
    service, _factory, _verifier = external_service
    created = _register(service)

    class MutatingVerifier:
        def probe(
            self,
            endpoint: ValidatedExternalEndpoint,
            _credential: ExternalKodiCredential | None,
            _configured: frozenset[str],
        ) -> ExternalKodiProbeResult:
            service.edit_endpoint(
                created.target_id,
                protocol_kind=ExternalProtocolKind.JSONRPC_HTTP,
                host="192.168.50.99",
                port=8080,
            )
            return ExternalKodiProbeResult(
                addresses=(endpoint.address,),
                kodi_name="Kodi",
                kodi_version="21.3",
                protocol_version="13.0.0",
                verified_capabilities=("target_inspect",),
            )

    service._verifier = MutatingVerifier()  # type: ignore[assignment]
    with pytest.raises(ExternalTargetError, match="needs_reverification"):
        service.probe(created.target_id)
    current = service.get(created.target_id)
    assert current.host == "192.168.50.99"
    assert current.verification_state.value == "needs_reverification"
    assert current.pending_observation_id is None
