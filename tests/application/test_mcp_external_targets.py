from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.mcp_administration import (
    ADDON_DEVELOPMENT_PRESET_ID,
    READ_ONLY_INSPECTION_PRESET_ID,
    McpAdministrationService,
    McpPrincipal,
)
from kodi_lab.application.mcp_target_adapters import KodiTargetAdapterService
from kodi_lab.domain import ExternalAdministrativeState, ExternalVerificationState
from kodi_lab.kodi.external import ExternalEndpointPolicy
from kodi_lab.mcp.authorization import McpAuthorizationError, McpAuthorizationService
from kodi_lab.mcp.registry import build_mcp_tool_registry
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.storage.metadata import Base
from kodi_lab.storage.models import ExternalKodiTargetRow, McpPresetRow, TargetRow
from kodi_lab.storage.secrets import ExternalCredentialStore

TARGET_ID = UUID("13000000-0000-4000-8000-000000000001")
NOW = "2026-09-21T12:00:00.000000Z"


@pytest.fixture
def external_authority(
    tmp_path: Path,
) -> Iterator[
    tuple[
        sessionmaker[Session],
        McpAdministrationService,
        KodiTargetAdapterService,
    ]
]:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'external-mcp.db'}")
    factory = create_session_factory(engine)
    Base.metadata.create_all(engine)
    administration = McpAdministrationService(factory)
    with factory.begin() as session:
        session.add(
            TargetRow(
                id=str(TARGET_ID),
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
                target_id=str(TARGET_ID),
                protocol_kind="jsonrpc_http",
                host="127.0.0.1",
                port=8080,
                authentication_mode="none",
                secret_reference=None,
                configured_capabilities_json=json.dumps(
                    ["addon_inventory", "semantic_remote", "target_inspect"]
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
    adapters = KodiTargetAdapterService(
        factory,
        managed_status=cast(Any, None),
        managed_inventory=cast(Any, None),
        managed_remote=cast(Any, None),
        managed_authority=cast(Any, None),
        endpoint_policy=ExternalEndpointPolicy(
            allowed_ports=(8080,), test_loopback_enabled=True
        ),
        credential_store=ExternalCredentialStore(
            tmp_path / "secrets", master_key=None, key_version=1
        ),
    )
    yield factory, administration, adapters
    engine.dispose()


def _principal(client_id: UUID, preset_id: UUID) -> McpPrincipal:
    return McpPrincipal(
        client_id=client_id,
        credential_id=uuid4(),
        preset_id=preset_id,
        permissions=frozenset(),
    )


def test_ui_observation_external_bridge_uses_generic_normalizer(
    external_authority: tuple[
        sessionmaker[Session], McpAdministrationService, KodiTargetAdapterService
    ],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from kodi_lab.application.external_bridges import ExternalBridgeServiceError
    from kodi_lab.kodi.ui_state_native import collect_native
    from tests.kodi.test_ui_state import movie_collection, native_fixture

    _, _, adapters = external_authority
    adapters._external_bridges = cast(
        Any,
        SimpleNamespace(
            inspect_ui_state=lambda target: movie_collection(),
            screenshot_available=lambda target: False,
            deployment_available=lambda target: False,
        ),
    )
    result = adapters.inspect_ui_state(TARGET_ID)
    assert result.selected_item.associated and result.focus.control_id == 50

    def unavailable(target: UUID) -> None:
        raise ExternalBridgeServiceError("bridge_not_paired")

    monkeypatch.setattr(adapters._external_bridges, "inspect_ui_state", unavailable)
    checked = []
    monkeypatch.setattr(
        adapters,
        "_require_identity",
        lambda snapshot, adapter: checked.append(snapshot.target_id),
    )
    monkeypatch.setattr(
        adapters,
        "_external_adapter",
        lambda snapshot: SimpleNamespace(
            inspect_ui_state=lambda: collect_native(native_fixture)
        ),
    )
    fallback = adapters.inspect_ui_state(TARGET_ID)
    assert checked == [TARGET_ID] and not fallback.selected_item.associated

    def mismatch(target: UUID) -> None:
        raise ExternalBridgeServiceError("bridge_identity_mismatch")

    monkeypatch.setattr(adapters._external_bridges, "inspect_ui_state", mismatch)
    from kodi_lab.application.mcp_target_adapters import KodiTargetAdapterError

    with pytest.raises(KodiTargetAdapterError, match="bridge_identity_mismatch"):
        adapters.inspect_ui_state(TARGET_ID)


def _authorize(
    authorization: McpAuthorizationService,
    principal: McpPrincipal,
    tool: str,
) -> None:
    if tool == "target_list":
        authorization.authorize_target_eligibility(principal, tool, TARGET_ID)
    else:
        authorization.authorize_target(principal, tool, TARGET_ID)


def test_external_summary_projects_only_verified_capabilities_without_duplicates(
    external_authority: tuple[
        sessionmaker[Session],
        McpAdministrationService,
        KodiTargetAdapterService,
    ],
) -> None:
    factory, _administration, adapters = external_authority
    limited = adapters.summaries([TARGET_ID])[0]
    assert limited.capabilities == ("target_status", "inspect_ui_state", "log_read")
    assert limited.process_state == "unknown"
    assert limited.jsonrpc_state == "not_checked"
    assert limited.usability_state == "not_checked"
    assert limited.readiness_state == "not_checked"

    with factory.begin() as session:
        row = session.get(ExternalKodiTargetRow, str(TARGET_ID))
        assert row is not None
        row.verified_capabilities_json = row.configured_capabilities_json
    complete = adapters.summaries([TARGET_ID])[0]
    assert complete.capabilities == (
        "target_status",
        "addon_list",
        "remote_action",
        "inspect_ui_state",
        "log_read",
    )
    assert len(complete.capabilities) == len(set(complete.capabilities))
    assert "screenshot_capture" not in complete.capabilities
    assert adapters.summaries([TARGET_ID])[0].capabilities == complete.capabilities


@pytest.mark.parametrize(
    ("tool", "capability"),
    [
        ("target_list", "target_inspect"),
        ("target_status", "target_inspect"),
        ("inspect_ui_state", "target_inspect"),
        ("log_read", "target_inspect"),
        ("addon_list", "addon_inventory"),
        ("remote_action", "semantic_remote"),
    ],
)
@pytest.mark.parametrize(
    ("denial", "expected"),
    [
        ("configured_not_verified", "capability_unavailable"),
        ("verified_not_configured", "capability_unavailable"),
        ("permission_missing", "permission_denied"),
        ("grant_missing", "target_not_found_or_not_authorized"),
        ("policy_denied", "permission_denied"),
        ("disabled", "target_not_found_or_not_authorized"),
        ("needs_reverification", "needs_reverification"),
        ("identity_mismatch", "identity_mismatch"),
    ],
)
def test_every_external_tool_requires_complete_authority_intersection(
    external_authority: tuple[
        sessionmaker[Session],
        McpAdministrationService,
        KodiTargetAdapterService,
    ],
    tool: str,
    capability: str,
    denial: str,
    expected: str,
) -> None:
    factory, administration, adapters = external_authority
    client = administration.create_client(
        display_name=f"Intersection {tool}", preset_id=ADDON_DEVELOPMENT_PRESET_ID
    )
    administration.assign_target_preset(TARGET_ID, ADDON_DEVELOPMENT_PRESET_ID)
    if denial != "grant_missing":
        administration.grant_target(client.id, TARGET_ID)
    empty_preset = uuid4()
    if denial in {"permission_missing", "policy_denied"}:
        with factory.begin() as session:
            session.add(
                McpPresetRow(
                    id=str(empty_preset),
                    display_name=f"Deny {tool}",
                    builtin=False,
                    assignable=True,
                )
            )
        if denial == "permission_missing":
            administration.assign_client_preset(client.id, empty_preset)
        else:
            administration.assign_target_preset(TARGET_ID, empty_preset)
    with factory.begin() as session:
        row = session.get(ExternalKodiTargetRow, str(TARGET_ID))
        assert row is not None
        all_capabilities = {"addon_inventory", "semantic_remote", "target_inspect"}
        if denial == "configured_not_verified":
            row.verified_capabilities_json = json.dumps(
                sorted(all_capabilities - {capability})
            )
        elif denial == "verified_not_configured":
            row.configured_capabilities_json = json.dumps(
                sorted(all_capabilities - {capability})
            )
        elif denial == "disabled":
            row.administrative_state = ExternalAdministrativeState.DISABLED
        elif denial == "needs_reverification":
            row.verification_state = ExternalVerificationState.NEEDS_REVERIFICATION
        elif denial == "identity_mismatch":
            row.verification_state = ExternalVerificationState.IDENTITY_MISMATCH
    authorization = McpAuthorizationService(
        factory, build_mcp_tool_registry(), targets=adapters
    )
    with pytest.raises(McpAuthorizationError, match=expected):
        _authorize(
            authorization,
            _principal(client.id, ADDON_DEVELOPMENT_PRESET_ID),
            tool,
        )


@pytest.mark.parametrize(
    "tool", ["target_list", "target_status", "addon_list", "remote_action"]
)
def test_every_external_tool_all_authority_dimensions_pass(
    external_authority: tuple[
        sessionmaker[Session],
        McpAdministrationService,
        KodiTargetAdapterService,
    ],
    tool: str,
) -> None:
    factory, administration, adapters = external_authority
    client = administration.create_client(
        display_name=f"Allow {tool}", preset_id=ADDON_DEVELOPMENT_PRESET_ID
    )
    administration.assign_target_preset(TARGET_ID, ADDON_DEVELOPMENT_PRESET_ID)
    administration.grant_target(client.id, TARGET_ID)
    with factory.begin() as session:
        row = session.get(ExternalKodiTargetRow, str(TARGET_ID))
        assert row is not None
        row.verified_capabilities_json = row.configured_capabilities_json
    authorization = McpAuthorizationService(
        factory, build_mcp_tool_registry(), targets=adapters
    )
    _authorize(
        authorization,
        _principal(client.id, ADDON_DEVELOPMENT_PRESET_ID),
        tool,
    )


def test_external_authority_is_configured_intersect_verified_grant_policy_state(
    external_authority: tuple[
        sessionmaker[Session],
        McpAdministrationService,
        KodiTargetAdapterService,
    ],
) -> None:
    factory, administration, adapters = external_authority
    client = administration.create_client(
        display_name="External client", preset_id=ADDON_DEVELOPMENT_PRESET_ID
    )
    administration.assign_target_preset(TARGET_ID, ADDON_DEVELOPMENT_PRESET_ID)
    administration.grant_target(client.id, TARGET_ID)
    authorization = McpAuthorizationService(
        factory, build_mcp_tool_registry(), targets=adapters
    )
    principal = _principal(client.id, ADDON_DEVELOPMENT_PRESET_ID)

    authorization.authorize_target(principal, "target_status", TARGET_ID)
    authorization.authorize_target_eligibility(principal, "target_list", TARGET_ID)
    for tool in ("addon_list", "remote_action"):
        with pytest.raises(McpAuthorizationError, match="capability_unavailable"):
            authorization.authorize_target(principal, tool, TARGET_ID)

    with factory.begin() as session:
        row = session.get(ExternalKodiTargetRow, str(TARGET_ID))
        assert row is not None
        row.verified_capabilities_json = json.dumps(
            ["addon_inventory", "semantic_remote", "target_inspect"]
        )
    authorization.authorize_target(principal, "addon_list", TARGET_ID)
    authorization.authorize_target(principal, "remote_action", TARGET_ID)

    with factory.begin() as session:
        row = session.get(ExternalKodiTargetRow, str(TARGET_ID))
        assert row is not None
        row.configured_capabilities_json = json.dumps(["target_inspect"])
    for tool in ("addon_list", "remote_action"):
        with pytest.raises(McpAuthorizationError, match="capability_unavailable"):
            authorization.authorize_target(principal, tool, TARGET_ID)


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("administrative_state", "disabled", "target_not_found_or_not_authorized"),
        ("verification_state", "needs_reverification", "needs_reverification"),
        ("verification_state", "identity_mismatch", "identity_mismatch"),
        ("verified_addresses_json", "[]", "needs_reverification"),
        (
            "verified_capabilities_json",
            '{"target_inspect":true}',
            "needs_reverification",
        ),
    ],
)
def test_external_state_and_identity_fail_closed(
    external_authority: tuple[
        sessionmaker[Session],
        McpAdministrationService,
        KodiTargetAdapterService,
    ],
    field: str,
    value: str,
    code: str,
) -> None:
    factory, administration, adapters = external_authority
    client = administration.create_client(
        display_name="State client", preset_id=READ_ONLY_INSPECTION_PRESET_ID
    )
    administration.assign_target_preset(TARGET_ID, READ_ONLY_INSPECTION_PRESET_ID)
    administration.grant_target(client.id, TARGET_ID)
    with factory.begin() as session:
        row = session.get(ExternalKodiTargetRow, str(TARGET_ID))
        assert row is not None
        setattr(row, field, value)
    authorization = McpAuthorizationService(
        factory, build_mcp_tool_registry(), targets=adapters
    )
    with pytest.raises(McpAuthorizationError, match=code):
        authorization.authorize_target(
            _principal(client.id, READ_ONLY_INSPECTION_PRESET_ID),
            "target_status",
            TARGET_ID,
        )


def test_external_grant_policy_permission_and_cross_client_privacy(
    external_authority: tuple[
        sessionmaker[Session],
        McpAdministrationService,
        KodiTargetAdapterService,
    ],
) -> None:
    factory, administration, adapters = external_authority
    client_a = administration.create_client(
        display_name="Client A", preset_id=ADDON_DEVELOPMENT_PRESET_ID
    )
    client_b = administration.create_client(
        display_name="Client B", preset_id=ADDON_DEVELOPMENT_PRESET_ID
    )
    administration.assign_target_preset(TARGET_ID, READ_ONLY_INSPECTION_PRESET_ID)
    administration.grant_target(client_a.id, TARGET_ID)
    authorization = McpAuthorizationService(
        factory, build_mcp_tool_registry(), targets=adapters
    )
    with pytest.raises(McpAuthorizationError, match="permission_denied"):
        authorization.authorize_target(
            _principal(client_a.id, ADDON_DEVELOPMENT_PRESET_ID),
            "remote_action",
            TARGET_ID,
        )
    for tool in ("target_status", "addon_list", "remote_action"):
        with pytest.raises(
            McpAuthorizationError, match="target_not_found_or_not_authorized"
        ):
            authorization.authorize_target(
                _principal(client_b.id, ADDON_DEVELOPMENT_PRESET_ID), tool, TARGET_ID
            )

    administration.remove_target_grant(client_a.id, TARGET_ID)
    with pytest.raises(
        McpAuthorizationError, match="target_not_found_or_not_authorized"
    ):
        authorization.authorize_target(
            _principal(client_a.id, ADDON_DEVELOPMENT_PRESET_ID),
            "target_status",
            TARGET_ID,
        )
