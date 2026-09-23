from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.mcp_administration import McpAdministrationService
from kodi_lab.application.production_capability_amendments import (
    ProductionCapabilityAmendmentError,
    ProductionCapabilityAmendmentService,
)
from kodi_lab.application.production_inspection import (
    PRODUCTION_INSPECTION_PRESET_ID,
    ProductionInspectionError,
    evaluate_production_inspection_state,
    evaluate_production_visual_state,
    production_observation_digest,
)
from kodi_lab.domain import (
    ConnectionState,
    ExternalAdministrativeState,
    ExternalAuthenticationMode,
    ExternalObservationState,
    ExternalProtocolKind,
    ExternalVerificationState,
    HealthStatus,
    ProductionDesignationState,
    TargetAvailability,
    TargetType,
)
from kodi_lab.storage.engine import create_session_factory
from kodi_lab.storage.metadata import Base
from kodi_lab.storage.models import (
    ExternalBridgePairingRow,
    ExternalKodiTargetObservationRow,
    ExternalKodiTargetRow,
    ExternalProductionPolicyRevisionGuardRow,
    ExternalProductionPolicyRow,
    McpClientTargetGrantRow,
    McpProductionScreenshotReceiptRow,
    McpProductionTargetScopeRow,
    McpTargetPresetAssignmentRow,
    ProductionApprovalTransitionEventRow,
    ProductionBridgeHttpAcknowledgementRow,
    ProductionCapabilityAmendmentRow,
    ProductionMutationControlRow,
    ProductionOperationApprovalRow,
    ReviewedBridgeBuildRow,
    TargetRow,
)
from kodi_lab.storage.secrets import (
    ExternalBridgeSecretStore,
    SecretStoreError,
)

CURRENT_TARGET_ID = UUID("de8cefbd-1179-4978-a687-108e82f5b893")


def _current_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[
    sessionmaker[Session],
    ProductionCapabilityAmendmentService,
    McpAdministrationService,
]:
    engine = create_engine(f"sqlite:///{tmp_path / 'capability-amendment.db'}")
    Base.metadata.create_all(engine)
    sessions = create_session_factory(engine)
    with sessions.begin() as session:
        session.add(
            ProductionMutationControlRow(
                id=1,
                production_mutations_blocked=True,
                policy_epoch=1,
                updated_by="system",
                updated_at="2026-09-22T12:00:00.000000Z",
            )
        )

    observation_id = uuid4()
    now = "2026-09-22T12:00:00.000000Z"
    external = ExternalKodiTargetRow(
        target_id=str(CURRENT_TARGET_ID),
        protocol_kind=ExternalProtocolKind.JSONRPC_HTTP,
        host="127.0.0.1",
        port=8080,
        authentication_mode=ExternalAuthenticationMode.NONE,
        secret_reference=None,
        configured_capabilities_json='["addon_inventory","target_inspect"]',
        verified_capabilities_json='["addon_inventory","target_inspect"]',
        administrative_state=ExternalAdministrativeState.ENABLED,
        verification_state=ExternalVerificationState.VERIFIED,
        observation_state=ExternalObservationState.REACHABLE,
        verified_addresses_json='["127.0.0.1"]',
        verified_kodi_name="Kodi",
        verified_kodi_version="21.3",
        observed_kodi_name="Kodi",
        observed_kodi_version="21.3",
        observed_protocol_version="13.0.0",
        pending_observation_id=str(observation_id),
        production_policy_revision=7,
        created_at=now,
        verified_at=now,
        deactivated_at=None,
        last_observed_at=now,
    )
    observation = ExternalKodiTargetObservationRow(
        id=str(observation_id),
        target_id=str(CURRENT_TARGET_ID),
        outcome="reachable",
        addresses_json='["127.0.0.1"]',
        kodi_name="Kodi",
        kodi_version="21.3",
        protocol_version="13.0.0",
        verified_capabilities_json='["addon_inventory","target_inspect"]',
        error_code=None,
        attempted_at=now,
        completed_at=now,
    )
    digest = production_observation_digest(external, observation)
    with sessions.begin() as session:
        session.add(
            TargetRow(
                id=str(CURRENT_TARGET_ID),
                name="Current production Kodi",
                target_type=TargetType.EXTERNAL_PRODUCTION,
                kodi_version="21.3",
                availability=TargetAvailability.AVAILABLE,
                health=HealthStatus.HEALTHY,
                connection_state=ConnectionState.CONNECTED,
            )
        )
        session.flush()
        session.add(external)
        session.flush()
        session.add(observation)
        session.add(
            McpTargetPresetAssignmentRow(
                target_id=str(CURRENT_TARGET_ID),
                preset_id=str(PRODUCTION_INSPECTION_PRESET_ID),
                assigned_at=now,
            )
        )
        session.flush()
        session.add(
            ExternalProductionPolicyRow(
                target_id=str(CURRENT_TARGET_ID),
                designation_state=ProductionDesignationState.CONFIRMED,
                confirmed_observation_id=str(observation_id),
                confirmed_observation_digest=digest,
                identity_generation=2,
                policy_revision=8,
                mutations_suspended=True,
                designation_confirmed_by="stage_a_operator",
                designation_confirmed_at=now,
                designation_invalidated_at=None,
                created_at=now,
                updated_at=now,
            )
        )

    administration = McpAdministrationService(sessions)
    reader = administration.create_client(
        display_name="Accepted Stage A reader",
        preset_id=PRODUCTION_INSPECTION_PRESET_ID,
    )
    administration.grant_target(reader.id, CURRENT_TARGET_ID)
    administration.grant_production_inspection_scope(
        reader.id, CURRENT_TARGET_ID, identity_generation=2
    )

    bridge_secrets = ExternalBridgeSecretStore(
        tmp_path / "bridge-secrets", master_key=b"b" * 32, key_version=1
    )
    amendments = ProductionCapabilityAmendmentService(
        sessions, bridge_secrets=bridge_secrets
    )
    return sessions, amendments, administration


def test_exact_current_state_add_is_8_7_8_to_9_9_9_and_preserves_stage_a(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sessions, amendments, administration = _current_state(monkeypatch, tmp_path)
    before_scopes = administration.list_production_inspection_scopes()
    assert len(before_scopes) == 1
    assert before_scopes[0].effective_state == "active"

    with sessions() as session:
        external = session.get(ExternalKodiTargetRow, str(CURRENT_TARGET_ID))
        policy = session.get(ExternalProductionPolicyRow, str(CURRENT_TARGET_ID))
        guard = session.get(
            ExternalProductionPolicyRevisionGuardRow, str(CURRENT_TARGET_ID)
        )
        assert external is not None and policy is not None and guard is not None
        assert policy.policy_revision == 8
        assert external.production_policy_revision == 7
        assert guard.maximum_policy_revision == 8

    result = amendments.amend_production_capability_maximum(
        CURRENT_TARGET_ID,
        operation="add",
        capability="screenshot",
        expected_configured_capabilities=frozenset(
            {"target_inspect", "addon_inventory"}
        ),
        expected_policy_revision=8,
        administrator_reference="stage_a_operator",
    )

    assert result.policy_revision == 9
    assert result.production_policy_revision_binding == 9
    assert result.identity_generation == 2
    assert result.native_verified_capabilities == (
        "addon_inventory",
        "target_inspect",
    )
    assert result.event_kind == "production_capability_screenshot_added"
    with sessions() as session:
        external = session.get(ExternalKodiTargetRow, str(CURRENT_TARGET_ID))
        policy = session.get(ExternalProductionPolicyRow, str(CURRENT_TARGET_ID))
        guard = session.get(
            ExternalProductionPolicyRevisionGuardRow, str(CURRENT_TARGET_ID)
        )
        assert external is not None and policy is not None and guard is not None
        assert external.configured_capabilities_json == (
            '["addon_inventory","screenshot","target_inspect"]'
        )
        assert external.verified_capabilities_json == (
            '["addon_inventory","target_inspect"]'
        )
        assert (
            policy.policy_revision,
            external.production_policy_revision,
            guard.maximum_policy_revision,
        ) == (9, 9, 9)
        assert policy.identity_generation == 2
        assert session.get(ExternalBridgePairingRow, str(CURRENT_TARGET_ID)) is None
        assert external.administrative_state.value == "enabled"
        assert external.verification_state.value == "verified"
        assert policy.designation_state.value == "confirmed"
        assert (
            session.scalar(
                text(
                    "SELECT COUNT(*) FROM mcp_production_target_scopes WHERE "
                    "target_id=:target AND scope<>'inspect'"
                ),
                {"target": str(CURRENT_TARGET_ID)},
            )
            == 0
        )
        assert (
            session.scalar(
                text(
                    "SELECT COUNT(*) FROM production_bridge_http_acknowledgements "
                    "WHERE target_id=:target"
                ),
                {"target": str(CURRENT_TARGET_ID)},
            )
            == 0
        )
        assert (
            session.scalar(
                text(
                    "SELECT COUNT(*) FROM mcp_production_screenshot_receipts WHERE "
                    "target_id=:target"
                ),
                {"target": str(CURRENT_TARGET_ID)},
            )
            == 0
        )
        audit = session.scalar(select(ProductionCapabilityAmendmentRow))
        assert audit is not None
        assert audit.before_configured_json == ('["addon_inventory","target_inspect"]')
        assert audit.after_configured_json == (
            '["addon_inventory","screenshot","target_inspect"]'
        )
        inspect = evaluate_production_inspection_state(
            session,
            CURRENT_TARGET_ID,
            required_capabilities=frozenset({"target_inspect"}),
        )
        assert inspect.identity_generation == 2
        with pytest.raises(ProductionInspectionError, match="bridge_not_paired"):
            evaluate_production_visual_state(session, CURRENT_TARGET_ID)

    after_scopes = administration.list_production_inspection_scopes()
    assert after_scopes[0].scope_id == before_scopes[0].scope_id
    assert after_scopes[0].effective_state == "active"
    with sessions() as session:
        control = session.get(ProductionMutationControlRow, 1)
        policy = session.get(ExternalProductionPolicyRow, str(CURRENT_TARGET_ID))
        assert control is not None and policy is not None
        assert control.production_mutations_blocked is True
        assert policy.mutations_suspended is True
    for statement in (
        "UPDATE production_capability_amendments SET actor='changed'",
        "DELETE FROM production_capability_amendments",
    ):
        with (
            sessions.begin() as session,
            pytest.raises(
                IntegrityError, match="production_capability_amendment_immutable"
            ),
        ):
            session.execute(text(statement))


def test_add_cas_and_audit_failure_roll_back_without_partial_update(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sessions, amendments, _administration = _current_state(monkeypatch, tmp_path)
    with pytest.raises(
        ProductionCapabilityAmendmentError, match="policy_revision_stale"
    ):
        amendments.amend_production_capability_maximum(
            CURRENT_TARGET_ID,
            operation="add",
            capability="screenshot",
            expected_configured_capabilities=frozenset(
                {"target_inspect", "addon_inventory"}
            ),
            expected_policy_revision=7,
        )
    with pytest.raises(
        ProductionCapabilityAmendmentError, match="configured_capabilities_stale"
    ):
        amendments.amend_production_capability_maximum(
            CURRENT_TARGET_ID,
            operation="add",
            capability="screenshot",
            expected_configured_capabilities=frozenset({"target_inspect"}),
            expected_policy_revision=8,
        )

    def fail_audit(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("audit failed")

    monkeypatch.setattr(amendments, "_write_audit", fail_audit)
    with pytest.raises(RuntimeError, match="audit failed"):
        amendments.amend_production_capability_maximum(
            CURRENT_TARGET_ID,
            operation="add",
            capability="screenshot",
            expected_configured_capabilities=frozenset(
                {"target_inspect", "addon_inventory"}
            ),
            expected_policy_revision=8,
        )
    with sessions() as session:
        external = session.get(ExternalKodiTargetRow, str(CURRENT_TARGET_ID))
        policy = session.get(ExternalProductionPolicyRow, str(CURRENT_TARGET_ID))
        assert external is not None and policy is not None
        assert external.configured_capabilities_json == (
            '["addon_inventory","target_inspect"]'
        )
        assert external.production_policy_revision == 7
        assert policy.policy_revision == 8
        assert session.scalar(select(ProductionCapabilityAmendmentRow)) is None


def test_remove_before_pairing_tombstones_and_readd_does_not_revive_history(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sessions, amendments, administration = _current_state(monkeypatch, tmp_path)
    add = amendments.amend_production_capability_maximum(
        CURRENT_TARGET_ID,
        operation="add",
        capability="screenshot",
        expected_configured_capabilities=frozenset(
            {"target_inspect", "addon_inventory"}
        ),
        expected_policy_revision=8,
    )
    removed = amendments.amend_production_capability_maximum(
        CURRENT_TARGET_ID,
        operation="remove",
        capability="screenshot",
        expected_configured_capabilities=frozenset(
            {"target_inspect", "addon_inventory", "screenshot"}
        ),
        expected_policy_revision=add.policy_revision,
    )
    assert removed.policy_revision == 10
    with sessions() as session:
        pairing = session.get(ExternalBridgePairingRow, str(CURRENT_TARGET_ID))
        assert pairing is not None
        assert pairing.pairing_state.value == "revoked"
        assert pairing.production_bridge_trust_revision == 1
        assert pairing.verified_capabilities_json == "[]"
        inspect = session.scalar(
            select(McpProductionTargetScopeRow).where(
                McpProductionTargetScopeRow.scope == "inspect"
            )
        )
        assert inspect is not None and inspect.state == "active"

    readded = amendments.amend_production_capability_maximum(
        CURRENT_TARGET_ID,
        operation="add",
        capability="screenshot",
        expected_configured_capabilities=frozenset(
            {"target_inspect", "addon_inventory"}
        ),
        expected_policy_revision=removed.policy_revision,
    )
    assert readded.policy_revision == 11
    with sessions() as session:
        pairing = session.get(ExternalBridgePairingRow, str(CURRENT_TARGET_ID))
        assert pairing is not None
        assert pairing.pairing_state.value == "revoked"
        assert pairing.production_bridge_trust_revision == 1
        assert (
            session.scalar(
                text(
                    "SELECT COUNT(*) FROM production_capability_amendments "
                    "WHERE target_id=:target"
                ),
                {"target": str(CURRENT_TARGET_ID)},
            )
            == 3
        )
    assert administration.list_production_inspection_scopes()[0].effective_state == (
        "active"
    )


def _seed_paired_bridge_and_downstream_authority(
    sessions: sessionmaker[Session],
    amendments: ProductionCapabilityAmendmentService,
    administration: McpAdministrationService,
) -> tuple[UUID, tuple[UUID, UUID, UUID], UUID]:
    inspect = administration.list_production_inspection_scopes()[0]
    client_id = inspect.client_id
    secret_reference = amendments._bridge_secrets.write(
        CURRENT_TARGET_ID, "request_authentication", b"s" * 32
    )
    build_id = uuid4()
    bridge_id = uuid4()
    now = "2026-09-22T12:00:00.000000Z"
    with sessions.begin() as session:
        session.add(
            ReviewedBridgeBuildRow(
                id=str(build_id),
                semantic_fingerprint="a" * 64,
                zip_sha256="b" * 64,
                addon_id="service.kodi_lab_bridge",
                addon_version="1.1.8",
                protocol_version="1",
                capabilities_json='["screenshot","workspace_deployment"]',
                supported_kodi_versions_json='["21.3"]',
                source_reference="deterministic fixture",
                reviewed_by="test",
                reviewed_at=now,
                state="active",
                retired_by=None,
                retired_at=None,
            )
        )
        session.add(
            ExternalBridgePairingRow(
                target_id=str(CURRENT_TARGET_ID),
                bridge_port=8765,
                pairing_state="paired",
                reachability_state="reachable",
                pairing_token_reference=None,
                bridge_secret_reference=str(secret_reference),
                pairing_token_expires_at=None,
                observed_bridge_id=str(bridge_id),
                observed_protocol_version="1",
                observed_addon_id="service.kodi_lab_bridge",
                observed_addon_version="1.1.8",
                observed_kodi_version="21.3",
                observed_build_fingerprint="a" * 64,
                observed_capabilities_json=('["screenshot","workspace_deployment"]'),
                pinned_bridge_id=str(bridge_id),
                pinned_protocol_version="1",
                pinned_addon_id="service.kodi_lab_bridge",
                pinned_addon_version="1.1.8",
                pinned_kodi_version="21.3",
                pinned_build_fingerprint="a" * 64,
                verified_capabilities_json='["screenshot"]',
                endpoint_revision_digest="c" * 64,
                token_generated_at=None,
                pairing_attempted_at=now,
                paired_at=now,
                verified_at=now,
                revoked_at=None,
                production_identity_generation=2,
                production_bridge_trust_revision=1,
                reviewed_build_id=str(build_id),
                production_confirmed_by="test",
                production_confirmed_at=now,
            )
        )
    scope_ids = (uuid4(), uuid4(), uuid4())
    with sessions.begin() as session:
        for scope_id, scope in zip(
            scope_ids,
            ("visual", "navigate", "request_transient_control"),
            strict=True,
        ):
            session.add(
                McpProductionTargetScopeRow(
                    id=str(scope_id),
                    client_id=str(client_id),
                    target_id=str(CURRENT_TARGET_ID),
                    identity_generation=2,
                    bridge_trust_revision=1,
                    scope=scope,
                    state="active",
                    created_at=now,
                    created_by="test",
                    revoked_at=None,
                    revoked_by=None,
                )
            )
    return secret_reference, scope_ids, client_id


def _approval(
    *,
    approval_id: UUID,
    client_id: UUID,
    transient_scope_id: UUID,
    state: str,
) -> ProductionOperationApprovalRow:
    now = "2026-09-22T12:00:00.000000Z"
    approved = state == "approved"
    return ProductionOperationApprovalRow(
        id=str(approval_id),
        client_id=str(client_id),
        credential_id=str(uuid4()),
        target_id=str(CURRENT_TARGET_ID),
        tool="remote_action",
        action="play_pause",
        payload_sha256="d" * 64,
        canonicalization_revision=1,
        request_id=str(uuid4()),
        native_identity_generation=2,
        bridge_trust_revision=1,
        target_policy_revision=9,
        mutation_control_epoch=1,
        client_authority_revision=1,
        generic_grant_id=str(uuid4()),
        client_preset_id=str(uuid4()),
        target_preset_id=str(uuid4()),
        inspect_scope_id=str(uuid4()),
        transient_scope_id=str(transient_scope_id),
        native_http_acknowledgement_id=None,
        reviewed_build_id=str(uuid4()),
        state=state,
        terminal_reason=None,
        created_at=now,
        pending_expires_at="2026-09-22T12:15:00.000000Z",
        approved_at=now if approved else None,
        approved_dispatch_expires_at=(
            "2026-09-22T12:05:00.000000Z" if approved else None
        ),
        approving_administrator="test" if approved else None,
        consumed_at=None,
        linked_remote_receipt_id=None,
    )


def test_remove_revokes_bridge_scopes_approvals_and_retains_history_and_receipts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sessions, amendments, administration = _current_state(monkeypatch, tmp_path)
    amendments.amend_production_capability_maximum(
        CURRENT_TARGET_ID,
        operation="add",
        capability="screenshot",
        expected_configured_capabilities=frozenset(
            {"target_inspect", "addon_inventory"}
        ),
        expected_policy_revision=8,
    )
    secret_reference, scope_ids, client_id = (
        _seed_paired_bridge_and_downstream_authority(
            sessions, amendments, administration
        )
    )
    acknowledgement_id = uuid4()
    pending_id = uuid4()
    approved_id = uuid4()
    receipt_request_id = uuid4()
    now = "2026-09-22T12:00:00.000000Z"
    with sessions.begin() as session:
        session.add(
            ProductionBridgeHttpAcknowledgementRow(
                id=str(acknowledgement_id),
                target_id=str(CURRENT_TARGET_ID),
                identity_generation=2,
                bridge_trust_revision=1,
                protocol="jsonrpc_http",
                ip_literal="127.0.0.1",
                native_port=8080,
                bridge_port=8765,
                endpoint_digest="c" * 64,
                transport_policy_revision=1,
                acknowledged_by="test",
                acknowledged_at=now,
            )
        )
        session.add(
            _approval(
                approval_id=pending_id,
                client_id=client_id,
                transient_scope_id=scope_ids[2],
                state="pending",
            )
        )
        session.add(
            _approval(
                approval_id=approved_id,
                client_id=client_id,
                transient_scope_id=scope_ids[2],
                state="approved",
            )
        )
        session.add(
            McpProductionScreenshotReceiptRow(
                client_id=str(client_id),
                request_id=str(receipt_request_id),
                target_id=str(CURRENT_TARGET_ID),
                parameter_digest="e" * 64,
                identity_generation=2,
                bridge_trust_revision=1,
                bridge_request_id=str(uuid4()),
                state="completed",
                response_digest="f" * 64,
                byte_size=128,
                width=16,
                height=16,
                created_at=now,
                completed_at=now,
                expires_at="2026-09-23T12:00:00.000000Z",
            )
        )

    removed = amendments.amend_production_capability_maximum(
        CURRENT_TARGET_ID,
        operation="remove",
        capability="screenshot",
        expected_configured_capabilities=frozenset(
            {"target_inspect", "addon_inventory", "screenshot"}
        ),
        expected_policy_revision=9,
    )
    assert removed.policy_revision == 10
    with pytest.raises(SecretStoreError):
        amendments._bridge_secrets.read(
            CURRENT_TARGET_ID, secret_reference, "request_authentication"
        )
    with sessions() as session:
        pairing = session.get(ExternalBridgePairingRow, str(CURRENT_TARGET_ID))
        assert pairing is not None
        assert pairing.pairing_state.value == "revoked"
        assert pairing.production_bridge_trust_revision == 2
        assert pairing.bridge_secret_reference is None
        assert pairing.verified_capabilities_json == "[]"
        assert pairing.production_identity_generation is None
        assert pairing.reviewed_build_id is None
        scopes = session.scalars(
            select(McpProductionTargetScopeRow).where(
                McpProductionTargetScopeRow.id.in_(tuple(map(str, scope_ids)))
            )
        ).all()
        assert {scope.scope for scope in scopes} == {
            "visual",
            "navigate",
            "request_transient_control",
        }
        assert all(scope.state == "revoked" for scope in scopes)
        assert all(scope.revoked_by == "local_administrator" for scope in scopes)
        for approval_id in (pending_id, approved_id):
            approval = session.get(ProductionOperationApprovalRow, str(approval_id))
            assert approval is not None
            assert approval.state == "cancelled"
            assert approval.terminal_reason == "capability_lost"
        events = session.scalars(
            select(ProductionApprovalTransitionEventRow).where(
                ProductionApprovalTransitionEventRow.approval_id.in_(
                    (str(pending_id), str(approved_id))
                )
            )
        ).all()
        assert len(events) == 2
        assert all(event.terminal_reason == "capability_lost" for event in events)
        assert (
            session.get(ProductionBridgeHttpAcknowledgementRow, str(acknowledgement_id))
            is not None
        )
        assert (
            session.get(McpTargetPresetAssignmentRow, str(CURRENT_TARGET_ID))
            is not None
        )
        assert (
            session.scalar(
                select(McpClientTargetGrantRow).where(
                    McpClientTargetGrantRow.client_id == str(client_id),
                    McpClientTargetGrantRow.target_id == str(CURRENT_TARGET_ID),
                )
            )
            is not None
        )
        assert session.scalar(select(ReviewedBridgeBuildRow)) is not None
        receipt = session.get(
            McpProductionScreenshotReceiptRow,
            {"client_id": str(client_id), "request_id": str(receipt_request_id)},
        )
        assert receipt is not None and receipt.state == "completed"
        with pytest.raises(ProductionInspectionError):
            evaluate_production_visual_state(session, CURRENT_TARGET_ID)

    amendments.amend_production_capability_maximum(
        CURRENT_TARGET_ID,
        operation="add",
        capability="screenshot",
        expected_configured_capabilities=frozenset(
            {"target_inspect", "addon_inventory"}
        ),
        expected_policy_revision=10,
    )
    with sessions() as session:
        assert all(
            scope.state == "revoked"
            for scope in session.scalars(
                select(McpProductionTargetScopeRow).where(
                    McpProductionTargetScopeRow.id.in_(tuple(map(str, scope_ids)))
                )
            )
        )
        pairing = session.get(ExternalBridgePairingRow, str(CURRENT_TARGET_ID))
        assert pairing is not None and pairing.pairing_state.value == "revoked"
        for approval_id in (pending_id, approved_id):
            approval = session.get(ProductionOperationApprovalRow, str(approval_id))
            assert approval is not None and approval.state == "cancelled"


def test_pending_pairing_token_is_retired_and_direct_unsafe_removal_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sessions, amendments, _administration = _current_state(monkeypatch, tmp_path)
    amendments.amend_production_capability_maximum(
        CURRENT_TARGET_ID,
        operation="add",
        capability="screenshot",
        expected_configured_capabilities=frozenset(
            {"target_inspect", "addon_inventory"}
        ),
        expected_policy_revision=8,
    )
    reference = amendments._bridge_secrets.write(
        CURRENT_TARGET_ID, "pairing_token", b"t" * 32
    )
    with sessions.begin() as session:
        session.add(
            ExternalBridgePairingRow(
                target_id=str(CURRENT_TARGET_ID),
                bridge_port=8765,
                pairing_state="unpaired",
                reachability_state="unknown",
                pairing_token_reference=str(reference),
                pairing_token_expires_at="2026-09-22T12:10:00.000000Z",
                bridge_secret_reference=None,
                observed_capabilities_json="[]",
                verified_capabilities_json="[]",
                production_bridge_trust_revision=1,
            )
        )
    with (
        sessions.begin() as session,
        pytest.raises(
            Exception,
            match=r"production_(policy_revision_required|screenshot_removal_unsafe)",
        ),
    ):
        session.execute(
            text(
                "UPDATE external_kodi_targets SET configured_capabilities_json="
                '\'["addon_inventory","target_inspect"]\' WHERE target_id=:target'
            ),
            {"target": str(CURRENT_TARGET_ID)},
        )

    amendments.amend_production_capability_maximum(
        CURRENT_TARGET_ID,
        operation="remove",
        capability="screenshot",
        expected_configured_capabilities=frozenset(
            {"target_inspect", "addon_inventory", "screenshot"}
        ),
        expected_policy_revision=9,
    )
    with pytest.raises(SecretStoreError):
        amendments._bridge_secrets.read(CURRENT_TARGET_ID, reference, "pairing_token")


def test_remove_with_http_acknowledgement_only_retains_stale_history(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sessions, amendments, _administration = _current_state(monkeypatch, tmp_path)
    amendments.amend_production_capability_maximum(
        CURRENT_TARGET_ID,
        operation="add",
        capability="screenshot",
        expected_configured_capabilities=frozenset(
            {"target_inspect", "addon_inventory"}
        ),
        expected_policy_revision=8,
    )
    acknowledgement_id = uuid4()
    with sessions.begin() as session:
        session.add(
            ProductionBridgeHttpAcknowledgementRow(
                id=str(acknowledgement_id),
                target_id=str(CURRENT_TARGET_ID),
                identity_generation=2,
                bridge_trust_revision=1,
                protocol="jsonrpc_http",
                ip_literal="127.0.0.1",
                native_port=8080,
                bridge_port=8765,
                endpoint_digest="a" * 64,
                transport_policy_revision=1,
                acknowledged_by="test",
                acknowledged_at="2026-09-22T12:00:00.000000Z",
            )
        )
    amendments.amend_production_capability_maximum(
        CURRENT_TARGET_ID,
        operation="remove",
        capability="screenshot",
        expected_configured_capabilities=frozenset(
            {"target_inspect", "addon_inventory", "screenshot"}
        ),
        expected_policy_revision=9,
    )
    with sessions() as session:
        assert (
            session.get(ProductionBridgeHttpAcknowledgementRow, str(acknowledgement_id))
            is not None
        )
        pairing = session.get(ExternalBridgePairingRow, str(CURRENT_TARGET_ID))
        assert pairing is not None
        assert pairing.pairing_state.value == "revoked"
        assert pairing.production_bridge_trust_revision == 1
        with pytest.raises(ProductionInspectionError):
            evaluate_production_visual_state(session, CURRENT_TARGET_ID)


def test_post_commit_secret_retirement_failure_leaves_only_inert_orphan(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sessions, amendments, _administration = _current_state(monkeypatch, tmp_path)
    amendments.amend_production_capability_maximum(
        CURRENT_TARGET_ID,
        operation="add",
        capability="screenshot",
        expected_configured_capabilities=frozenset(
            {"target_inspect", "addon_inventory"}
        ),
        expected_policy_revision=8,
    )
    reference = amendments._bridge_secrets.write(
        CURRENT_TARGET_ID, "pairing_token", b"o" * 32
    )
    with sessions.begin() as session:
        session.add(
            ExternalBridgePairingRow(
                target_id=str(CURRENT_TARGET_ID),
                bridge_port=8765,
                pairing_state="unpaired",
                reachability_state="unknown",
                pairing_token_reference=str(reference),
                pairing_token_expires_at="2026-09-22T12:10:00.000000Z",
                bridge_secret_reference=None,
                observed_capabilities_json="[]",
                verified_capabilities_json="[]",
                production_bridge_trust_revision=1,
            )
        )

    def fail_retirement(_reference: UUID) -> None:
        raise SecretStoreError("injected orphan")

    monkeypatch.setattr(amendments._bridge_secrets, "retire", fail_retirement)
    with pytest.raises(
        ProductionCapabilityAmendmentError,
        match="bridge_secret_retirement_failed",
    ):
        amendments.amend_production_capability_maximum(
            CURRENT_TARGET_ID,
            operation="remove",
            capability="screenshot",
            expected_configured_capabilities=frozenset(
                {"target_inspect", "addon_inventory", "screenshot"}
            ),
            expected_policy_revision=9,
        )
    assert (
        amendments._bridge_secrets.read(CURRENT_TARGET_ID, reference, "pairing_token")
        == b"o" * 32
    )
    with sessions() as session:
        external = session.get(ExternalKodiTargetRow, str(CURRENT_TARGET_ID))
        pairing = session.get(ExternalBridgePairingRow, str(CURRENT_TARGET_ID))
        assert external is not None and pairing is not None
        assert "screenshot" not in external.configured_capabilities_json
        assert pairing.pairing_state.value == "revoked"
        assert pairing.pairing_token_reference is None
        assert pairing.bridge_secret_reference is None


def test_concurrent_expectation_has_one_winner_and_tombstone_failure_rolls_back(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sessions, amendments, _administration = _current_state(monkeypatch, tmp_path)
    first = amendments.amend_production_capability_maximum(
        CURRENT_TARGET_ID,
        operation="add",
        capability="screenshot",
        expected_configured_capabilities=frozenset(
            {"target_inspect", "addon_inventory"}
        ),
        expected_policy_revision=8,
    )
    assert first.policy_revision == 9
    with pytest.raises(ProductionCapabilityAmendmentError):
        amendments.amend_production_capability_maximum(
            CURRENT_TARGET_ID,
            operation="add",
            capability="screenshot",
            expected_configured_capabilities=frozenset(
                {"target_inspect", "addon_inventory"}
            ),
            expected_policy_revision=8,
        )

    def fail_tombstone(*_args: object, **_kwargs: object) -> tuple[UUID, ...]:
        raise RuntimeError("tombstone failed")

    monkeypatch.setattr(amendments, "_tombstone_removed_screenshot", fail_tombstone)
    with pytest.raises(RuntimeError, match="tombstone failed"):
        amendments.amend_production_capability_maximum(
            CURRENT_TARGET_ID,
            operation="remove",
            capability="screenshot",
            expected_configured_capabilities=frozenset(
                {"target_inspect", "addon_inventory", "screenshot"}
            ),
            expected_policy_revision=9,
        )
    with sessions() as session:
        external = session.get(ExternalKodiTargetRow, str(CURRENT_TARGET_ID))
        policy = session.get(ExternalProductionPolicyRow, str(CURRENT_TARGET_ID))
        assert external is not None and policy is not None
        assert "screenshot" in external.configured_capabilities_json
        assert policy.policy_revision == 9
        assert external.production_policy_revision == 9


def test_two_simultaneous_add_amendments_have_exactly_one_winner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _sessions, amendments, _administration = _current_state(monkeypatch, tmp_path)
    barrier = Barrier(2)

    def contend() -> str:
        barrier.wait()
        try:
            amendments.amend_production_capability_maximum(
                CURRENT_TARGET_ID,
                operation="add",
                capability="screenshot",
                expected_configured_capabilities=frozenset(
                    {"target_inspect", "addon_inventory"}
                ),
                expected_policy_revision=8,
            )
        except ProductionCapabilityAmendmentError as error:
            return error.code
        return "winner"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _index: contend(), range(2)))
    assert outcomes.count("winner") == 1
    assert len(outcomes) == 2
    assert set(outcomes) <= {
        "winner",
        "policy_revision_stale",
        "configured_capabilities_stale",
        "capability_already_configured",
    }
