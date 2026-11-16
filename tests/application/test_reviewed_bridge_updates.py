"""Isolated authenticated Bridge transitions; never production/native acceptance."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import delete, select

from kodi_lab.application.external_bridges import ExternalBridgeServiceError
from kodi_lab.application.production_inspection import evaluate_production_visual_state
from kodi_lab.application.production_navigation import navigation_binding_digest
from kodi_lab.application.production_policy import (
    advance_production_policy,
    bind_external_production_transition,
)
from kodi_lab.kodi.bridge import BRIDGE_CAPABILITIES, b64url
from kodi_lab.kodi.logs import KodiLogRequest, KodiLogScanBridgeRequest
from kodi_lab.repository import stable
from kodi_lab.storage.models import (
    ExternalBridgePairingRow,
    ExternalKodiTargetRow,
    ExternalProductionPolicyRow,
    ProductionBridgeAdoptionRow,
    ProductionBridgeHttpAcknowledgementRow,
    ReviewedBridgeBuildRow,
)
from tests.application.test_production_otp_pairing import _code
from tests.application.test_production_otp_pairing import (
    production_otp as production_otp,
)

OLD_FINGERPRINT = "e18b4176611506335316a41e4dee9ea2ed113f8b5e7a0b95f6c156597d1618be"
OLD_ZIP = "213e56cde46e10017c67a9f5fbd32a2be29023870bc99acacce26d28b45d955f"


@pytest.fixture
def updated(
    production_otp: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Any:
    factory, service, bridge, port, store, target = production_otp
    globals_ = bridge["_identity"].__globals__
    # The reused fixture has not paired yet. Replace its synthetic unreferenced
    # provenance with the two exact reviewed package identities in this case.
    with factory.begin() as session:
        session.execute(delete(ReviewedBridgeBuildRow))
    globals_["ADDON_VERSION"] = "1.1.31"
    globals_["BUILD_FINGERPRINT"] = OLD_FINGERPRINT
    globals_["CAPABILITIES"] = sorted(BRIDGE_CAPABILITIES - {"log_scan"})
    service.register_reviewed_build(
        semantic_fingerprint=OLD_FINGERPRINT,
        zip_sha256=OLD_ZIP,
        addon_id=stable.BRIDGE_ID,
        addon_version="1.1.31",
        protocol_version="1",
        capabilities=tuple(globals_["CAPABILITIES"]),
        supported_kodi_versions=("21.3",),
        source_reference="retained reviewed 1.1.31 fixture",
    )
    # Establish the retained pairing under its then-current backend release.
    with monkeypatch.context() as prior_backend:
        prior_backend.setattr(
            "kodi_lab.application.external_bridges.SUPPORTED_V1_BRIDGE_ADDON_VERSION",
            "1.1.31",
        )
        service.begin_otp_pairing(target, bridge_port=port, code=_code(bridge))
        service.commit_otp_pairing(target)
    root = tmp_path / "logs"
    root.mkdir()
    (root / "kodi.log").write_text(
        "2026-11-16 01:00:00.000 T:1 ERROR <general>: fixture\n"
    )
    (root / "kodi.old.log").write_text(
        "2026-11-15 01:00:00.000 T:1 INFO <general>: fixture\n"
    )
    vfs = sys.modules["xbmcvfs"]
    translate = vfs.translatePath
    monkeypatch.setattr(
        vfs,
        "translatePath",
        lambda p: str(root) if p == "special://logpath" else translate(p),
    )

    def install_current() -> None:
        release = stable.CURRENT_BRIDGE
        globals_["ADDON_VERSION"] = release.version
        globals_["BUILD_FINGERPRINT"] = release.fingerprint
        globals_["CAPABILITIES"] = sorted(BRIDGE_CAPABILITIES)
        service.register_reviewed_build(
            semantic_fingerprint=release.fingerprint,
            zip_sha256=release.sha256,
            addon_id=stable.BRIDGE_ID,
            addon_version=release.version,
            protocol_version="1",
            capabilities=tuple(sorted(BRIDGE_CAPABILITIES)),
            supported_kodi_versions=("21.3",),
            source_reference="exact compiled official release fixture",
        )

    install_current()
    return SimpleNamespace(
        factory=factory,
        service=service,
        bridge=bridge,
        globals=globals_,
        target=target,
        port=port,
        store=store,
        install_current=install_current,
    )


def enable(env: Any) -> None:
    env.service.set_reviewed_update_policy(
        env.target, enabled=True, expected_revision=0
    )


def scan(env: Any) -> Any:
    return env.service.read_log(
        env.target, KodiLogScanBridgeRequest(client_binding="c" * 64, max_matches=0)
    )


def test_exact_shield_transition_keeps_pairing_consent_and_navigation_anchor(
    updated: Any,
) -> None:
    e = updated
    before = e.service.status(e.target)
    with e.factory() as session:
        pair = session.get(ExternalBridgePairingRow, str(e.target))
        secret_ref = pair.bridge_secret_reference
        ack_rows = [
            dict(r._mapping)
            for r in session.execute(
                select(ProductionBridgeHttpAcknowledgementRow.__table__)
            )
        ]
        nav = navigation_binding_digest(session, e.target)
    assert not e.service.reviewed_update_policy(e.target).enabled
    enable(e)
    result = scan(e)
    assert result.complete and result.counts_exact
    assert sum(f.matched_records for f in result.files) == 1
    after = e.service.status(e.target)
    assert after.addon_version == "1.1.33"
    assert (
        after.production_bridge_trust_revision
        == before.production_bridge_trust_revision + 1
    )
    assert after.pinned_bridge_id == before.pinned_bridge_id
    assert e.service.read_log(e.target, KodiLogRequest()).mode == "stats"
    with e.factory() as session:
        assert (
            session.get(ExternalBridgePairingRow, str(e.target)).bridge_secret_reference
            == secret_ref
        )
        assert [
            dict(r._mapping)
            for r in session.execute(
                select(ProductionBridgeHttpAcknowledgementRow.__table__)
            )
        ] == ack_rows
        assert navigation_binding_digest(session, e.target) == nav
        assert (
            evaluate_production_visual_state(session, e.target).bridge_trust_revision
            == 2
        )
        receipts = list(session.scalars(select(ProductionBridgeAdoptionRow)))
        assert (
            len(receipts) == 1
            and receipts[0].http_acknowledgement_id == ack_rows[0]["id"]
        )
    # Restart/lost response/duplicate observation does not make another revision.
    assert scan(e).counts_exact
    assert e.service.status(e.target) == after
    receipt_id = e.service.reviewed_update_policy(e.target).last_adoption_id
    assert receipt_id is not None
    receipt = e.service.reviewed_adoption(e.target, receipt_id)
    assert receipt.previous_build.zip_sha256 == OLD_ZIP
    assert receipt.accepted_build.zip_sha256 == stable.CURRENT_BRIDGE.sha256
    assert receipt.from_revision == 1 and receipt.to_revision == 2
    assert receipt.actor == "automatic_reviewed_update"


def test_opt_out_preserves_manual_flow(updated: Any) -> None:
    with pytest.raises(
        ExternalBridgeServiceError, match="bridge_capability_unavailable"
    ):
        scan(updated)
    assert updated.service.status(updated.target).addon_version == "1.1.31"
    pending = updated.service.reverify(updated.target)
    assert pending.pairing_state.value == "pairing_pending"
    with pytest.raises(
        ExternalBridgeServiceError, match="transport_acknowledgement_required"
    ):
        updated.service.confirm_pairing(updated.target)


def test_successor_and_disabling_policy_preserve_completed_consent(
    updated: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    e = updated
    enable(e)
    assert scan(e).counts_exact
    monkeypatch.setattr(
        stable, "CURRENT_BRIDGE", stable.ReviewedBridge("1.1.35", "b" * 64, "c" * 64)
    )
    e.install_current()
    assert scan(e).counts_exact
    assert e.service.status(e.target).production_bridge_trust_revision == 3
    e.service.set_reviewed_update_policy(e.target, enabled=False, expected_revision=1)
    assert scan(e).counts_exact
    with e.factory() as session:
        assert len(list(session.scalars(select(ProductionBridgeAdoptionRow)))) == 2
        evaluate_production_visual_state(session, e.target)


@pytest.mark.parametrize(
    "damage",
    [
        "unknown",
        "retired",
        "wrong_zip",
        "wrong_previous",
        "protocol",
        "unsupported_kodi",
        "uuid",
        "secret",
        "native",
        "host",
        "native_port",
        "bridge_port",
        "transport",
        "old_retired",
        "downgrade",
        "unconfirmed",
    ],
)
def test_negative_admission_does_not_change_pinned_trust(
    updated: Any, damage: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    e = updated
    enable(e)
    before = e.service.status(e.target)
    if damage == "unknown":
        e.globals["BUILD_FINGERPRINT"] = "f" * 64
    elif damage == "protocol":
        e.globals["PROTOCOL"] = "2"
    elif damage == "unsupported_kodi":
        e.globals["_kodi_version"] = lambda: "18.9"
    elif damage == "uuid":
        state = e.bridge["_load_state"]()
        state["bridge_id"] = str(uuid4())
        e.bridge["_save_state"](state)
    elif damage == "secret":
        state = e.bridge["_load_state"]()
        state["secret"] = b64url(b"z" * 32)
        e.bridge["_save_state"](state)
    elif damage == "downgrade":
        monkeypatch.setattr(
            stable,
            "CURRENT_BRIDGE",
            stable.ReviewedBridge(
                "1.1.30",
                stable.CURRENT_BRIDGE.sha256,
                stable.CURRENT_BRIDGE.fingerprint,
            ),
        )
        e.globals["ADDON_VERSION"] = "1.1.30"
    else:
        with e.factory.begin() as session:
            pair = session.get(ExternalBridgePairingRow, str(e.target))
            external = session.get(ExternalKodiTargetRow, str(e.target))
            if damage in ["retired", "wrong_zip", "old_retired"]:
                build = session.scalar(
                    select(ReviewedBridgeBuildRow).where(
                        ReviewedBridgeBuildRow.addon_version
                        == ("1.1.31" if damage == "old_retired" else "1.1.33")
                    )
                )
                if damage == "wrong_zip":
                    build.zip_sha256 = "f" * 64
                else:
                    build.state = "retired"
                    build.retired_by = "fixture"
                    build.retired_at = "2026-11-16T00:00:00.000000Z"
            elif damage == "wrong_previous":
                pair.reviewed_build_id = session.scalar(
                    select(ReviewedBridgeBuildRow.id).where(
                        ReviewedBridgeBuildRow.addon_version == "1.1.33"
                    )
                )
            elif damage == "native":
                policy = session.get(ExternalProductionPolicyRow, str(e.target))
                policy.identity_generation += 1
                advance_production_policy(policy, now=datetime.now(UTC))
            elif damage == "host":
                bind_external_production_transition(
                    session,
                    external,
                    session.get(ExternalProductionPolicyRow, str(e.target)),
                    now=datetime.now(UTC),
                )
                external.host = "127.0.0.2"
            elif damage == "native_port":
                bind_external_production_transition(
                    session,
                    external,
                    session.get(ExternalProductionPolicyRow, str(e.target)),
                    now=datetime.now(UTC),
                )
                external.port = 8081
            elif damage == "bridge_port":
                pair.bridge_port += 1
            elif damage == "transport":
                bind_external_production_transition(
                    session,
                    external,
                    session.get(ExternalProductionPolicyRow, str(e.target)),
                    now=datetime.now(UTC),
                )
                external.protocol_kind = "jsonrpc_https"
            elif damage == "unconfirmed":
                pair.pairing_state = "needs_reverification"
    with pytest.raises(ExternalBridgeServiceError):
        scan(e)
    assert e.service.status(e.target).pinned_bridge_id == before.pinned_bridge_id
    assert e.service.status(e.target).addon_version == "1.1.31"
    with e.factory() as session:
        assert not list(session.scalars(select(ProductionBridgeAdoptionRow)))


def test_concurrent_policy_change_and_old_inflight_result_are_fenced(
    updated: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    e = updated
    enable(e)
    original = e.service._client.capabilities

    def changed(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        e.service.set_reviewed_update_policy(
            e.target, enabled=False, expected_revision=1
        )
        return result

    monkeypatch.setattr(e.service._client, "capabilities", changed)
    with pytest.raises(
        ExternalBridgeServiceError, match="reviewed_update_state_changed"
    ):
        scan(e)
    assert e.service.status(e.target).addon_version == "1.1.31"


def test_off_and_manual_transition_breaks_carried_consent(
    updated: Any,
) -> None:
    e = updated
    enable(e)
    assert scan(e).counts_exact
    e.service.mark_needs_reverification(e.target)
    # Reverification is an owner operation, not an automatic transition.
    e.service.set_reviewed_update_policy(e.target, enabled=False, expected_revision=1)
    assert not e.service.reviewed_update_policy(e.target).enabled
    with pytest.raises(
        ExternalBridgeServiceError, match="transport_acknowledgement_required"
    ):
        e.service.reverify(e.target)
    e.service.revoke(e.target)
    e.service.set_reviewed_update_policy(e.target, enabled=False, expected_revision=2)


@pytest.mark.parametrize(
    "change", ["retire_new", "retire_old", "disable_reenable", "revoke_pairing"]
)
def test_remote_observation_races_fail_closed(
    updated: Any, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    e = updated
    enable(e)
    original = e.service._client.capabilities

    def raced(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        if change == "disable_reenable":
            e.service.set_reviewed_update_policy(
                e.target, enabled=False, expected_revision=1
            )
            e.service.set_reviewed_update_policy(
                e.target, enabled=True, expected_revision=2
            )
        elif change == "revoke_pairing":
            e.service.revoke(e.target)
        else:
            with e.factory.begin() as session:
                r = session.scalar(
                    select(ReviewedBridgeBuildRow).where(
                        ReviewedBridgeBuildRow.addon_version
                        == ("1.1.31" if change == "retire_old" else "1.1.33")
                    )
                )
                r.state = "retired"
                r.retired_by = "fixture"
                r.retired_at = "2026-11-16T00:00:00.000000Z"
        return result

    monkeypatch.setattr(e.service._client, "capabilities", raced)
    with pytest.raises(ExternalBridgeServiceError):
        scan(e)
    with e.factory() as session:
        assert not list(session.scalars(select(ProductionBridgeAdoptionRow)))


def test_old_inflight_log_is_not_published_after_adoption(
    updated: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    e = updated
    e.globals["ADDON_VERSION"] = "1.1.31"
    e.globals["BUILD_FINGERPRINT"] = OLD_FINGERPRINT
    e.globals["CAPABILITIES"] = sorted(BRIDGE_CAPABILITIES - {"log_scan"})
    original = e.service._client.read_log

    def old_response(*args: Any, **kwargs: Any) -> Any:
        value = original(*args, **kwargs)
        e.globals["ADDON_VERSION"] = stable.CURRENT_BRIDGE.version
        e.globals["BUILD_FINGERPRINT"] = stable.CURRENT_BRIDGE.fingerprint
        e.globals["CAPABILITIES"] = sorted(BRIDGE_CAPABILITIES)
        enable(e)
        e.service.reverify(e.target)
        return value

    monkeypatch.setattr(e.service._client, "read_log", old_response)
    with pytest.raises(ExternalBridgeServiceError, match="bridge_identity_mismatch"):
        e.service.read_log(e.target, KodiLogRequest())
    assert e.service.status(e.target).addon_version == "1.1.33"


def test_security_capability_additions_require_manual_review(updated: Any) -> None:
    e = updated
    with e.factory.begin() as session:
        old = session.scalar(
            select(ReviewedBridgeBuildRow).where(
                ReviewedBridgeBuildRow.addon_version == "1.1.31"
            )
        )
        caps = '["log_read","screenshot","ui_observation"]'
        old.capabilities_json = caps
        session.get(
            ExternalBridgePairingRow, str(e.target)
        ).observed_capabilities_json = caps
    enable(e)
    with pytest.raises(
        ExternalBridgeServiceError, match="reviewed_update_capability_expansion"
    ):
        scan(e)


@pytest.mark.parametrize("revocation", ["none", "before", "during"])
def test_real_inspection_authorization_still_fences_scans(
    updated: Any, monkeypatch: pytest.MonkeyPatch, revocation: str
) -> None:
    from kodi_lab.application.mcp_administration import (
        McpAdministrationService,
        McpAuthenticationService,
    )
    from kodi_lab.application.production_inspection import (
        PRODUCTION_INSPECTION_PRESET_ID,
    )
    from kodi_lab.mcp.authorization import (
        McpAuthorizationError,
        McpAuthorizationService,
    )
    from kodi_lab.mcp.registry import build_mcp_tool_registry
    from kodi_lab.storage.models import McpClientTargetGrantRow

    e = updated
    enable(e)
    admin = McpAdministrationService(e.factory)
    client = admin.create_client(
        display_name="fixture reader", preset_id=PRODUCTION_INSPECTION_PRESET_ID
    )
    token = admin.issue_credential(client.id)
    admin.assign_target_preset(e.target, PRODUCTION_INSPECTION_PRESET_ID)
    admin.grant_target(client.id, e.target)
    admin.grant_production_inspection_scope(client.id, e.target, identity_generation=1)
    principal = McpAuthenticationService(e.factory).authenticate(token.token)
    assert principal is not None
    auth = McpAuthorizationService(e.factory, build_mcp_tool_registry())

    def revoke() -> None:
        with e.factory.begin() as session:
            session.execute(
                delete(McpClientTargetGrantRow).where(
                    McpClientTargetGrantRow.client_id == str(client.id)
                )
            )

    if revocation == "before":
        revoke()
    elif revocation == "during":
        original = e.service._client.capabilities

        def changed(*args: Any, **kwargs: Any) -> Any:
            value = original(*args, **kwargs)
            revoke()
            return value

        monkeypatch.setattr(e.service._client, "capabilities", changed)
    if revocation != "none":
        with pytest.raises(
            McpAuthorizationError, match="target_not_found_or_not_authorized"
        ):
            auth.invoke_target(principal, "log_scan", e.target, lambda: scan(e))
        assert e.service.status(e.target).addon_version == "1.1.31"
    else:
        assert auth.invoke_target(
            principal, "log_scan", e.target, lambda: scan(e)
        ).counts_exact


def test_capability_maximum_amendment_revokes_continuity(updated: Any) -> None:
    from kodi_lab.application.production_capability_amendments import (
        ProductionCapabilityAmendmentService,
    )

    e = updated
    enable(e)
    with e.factory() as session:
        policy = session.get(ExternalProductionPolicyRow, str(e.target))
        revision = policy.policy_revision
        external = session.get(ExternalKodiTargetRow, str(e.target))
        caps = frozenset(json.loads(external.configured_capabilities_json))
    amendments = ProductionCapabilityAmendmentService(e.factory, bridge_secrets=e.store)
    amendments.amend_production_capability_maximum(
        e.target,
        operation="remove",
        capability="screenshot",
        expected_configured_capabilities=caps,
        expected_policy_revision=revision,
    )
    with pytest.raises(ExternalBridgeServiceError):
        scan(e)
    with e.factory() as session:
        assert not list(session.scalars(select(ProductionBridgeAdoptionRow)))


def test_observation_has_no_writer_lock_and_capability_responses_must_agree(
    updated: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy import text

    e = updated
    enable(e)
    original = e.service._client.capabilities

    def inconsistent(*args: Any, **kwargs: Any) -> Any:
        # This independent writer reservation would fail if observation held it.
        with e.factory() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            session.rollback()
        return tuple(c for c in original(*args, **kwargs) if c != "log_scan")

    monkeypatch.setattr(e.service._client, "capabilities", inconsistent)
    with pytest.raises(ExternalBridgeServiceError, match="bridge_identity_mismatch"):
        scan(e)


def test_transport_policy_change_requires_current_consent(
    updated: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    e = updated
    enable(e)
    monkeypatch.setattr(
        "kodi_lab.application.external_bridges.PRODUCTION_TRANSPORT_POLICY_REVISION", 2
    )
    with pytest.raises(
        ExternalBridgeServiceError, match="transport_acknowledgement_required"
    ):
        scan(e)


def test_completed_history_rejects_update_delete_and_insert_or_replace(
    updated: Any,
) -> None:
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    e = updated
    enable(e)
    assert scan(e).counts_exact
    for sql in [
        "UPDATE production_bridge_adoptions SET binding_digest='" + "0" * 64 + "'",
        "DELETE FROM production_bridge_adoptions",
        (
            "INSERT OR REPLACE INTO production_bridge_adoptions "
            "SELECT * FROM production_bridge_adoptions"
        ),
        (
            "INSERT OR REPLACE INTO production_bridge_update_policies "
            "SELECT * FROM production_bridge_update_policies"
        ),
    ]:
        with (
            e.factory() as session,
            pytest.raises(
                IntegrityError,
                match=r"bridge_adoption_immutable|bridge_update_policy_history",
            ),
        ):
            session.execute(text(sql))
    assert scan(e).counts_exact


def test_simultaneous_observations_admit_at_most_one_transition(
    updated: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    e = updated
    enable(e)
    barrier = Barrier(2)
    original = e.service._client.capabilities

    def synchronized(*args: Any, **kwargs: Any) -> Any:
        value = original(*args, **kwargs)
        barrier.wait(timeout=10)
        return value

    monkeypatch.setattr(e.service._client, "capabilities", synchronized)

    def observe() -> str:
        try:
            scan(e)
            return "adopted"
        except ExternalBridgeServiceError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: observe(), range(2)))
    assert results.count("adopted") == 1
    assert set(results) <= {"adopted", "reviewed_update_state_changed"}
    monkeypatch.setattr(e.service._client, "capabilities", original)
    assert scan(e).counts_exact
    with e.factory() as session:
        assert len(list(session.scalars(select(ProductionBridgeAdoptionRow)))) == 1


def test_backend_rollback_does_not_automatically_downgrade_trust(
    updated: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    e = updated
    enable(e)
    assert scan(e).counts_exact
    monkeypatch.setattr(
        stable,
        "CURRENT_BRIDGE",
        stable.ReviewedBridge("1.1.31", OLD_ZIP, OLD_FINGERPRINT),
    )
    e.globals["ADDON_VERSION"] = "1.1.31"
    e.globals["BUILD_FINGERPRINT"] = OLD_FINGERPRINT
    e.globals["CAPABILITIES"] = sorted(BRIDGE_CAPABILITIES - {"log_scan"})
    with pytest.raises(
        ExternalBridgeServiceError, match="reviewed_update_not_successor"
    ):
        scan(e)
    assert e.service.status(e.target).addon_version == "1.1.33"


def test_existing_on_navigation_preference_survives_without_promoting_old_actions(
    updated: Any,
) -> None:
    from kodi_lab.application.production_navigation import (
        ProductionNavigationPreferenceService,
        require_navigation_preference,
    )
    from kodi_lab.storage.models import ProductionMutationControlRow

    e = updated
    # Simulate an already deliberately configured navigable fixture, never a
    # live production target. Adoption must not edit these existing choices.
    with e.factory.begin() as session:
        mutation = session.get(ProductionMutationControlRow, 1)
        mutation.production_mutations_blocked = False
        mutation.policy_epoch += 1
        policy = session.get(ExternalProductionPolicyRow, str(e.target))
        policy.mutations_suspended = False
        advance_production_policy(policy, now=datetime.now(UTC))
    preference = ProductionNavigationPreferenceService(e.factory)
    preference.update(
        e.target, enabled=True, expected_revision=preference.read(e.target).revision
    )
    before = preference.read(e.target)
    enable(e)
    assert scan(e).counts_exact
    assert preference.read(e.target) == before
    with e.factory() as session:
        require_navigation_preference(session, e.target)
    assert e.service.status(e.target).production_bridge_trust_revision == 2


def test_failed_authenticated_observation_never_commits_adoption(
    updated: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kodi_lab.kodi.bridge import ExternalBridgeError

    e = updated
    enable(e)

    def unavailable(*args: Any, **kwargs: Any) -> Any:
        raise ExternalBridgeError("bridge_unreachable")

    monkeypatch.setattr(e.service._client, "capabilities", unavailable)
    with pytest.raises(ExternalBridgeServiceError, match="bridge_unreachable"):
        scan(e)
    assert e.service.status(e.target).addon_version == "1.1.31"
    with e.factory() as session:
        assert not list(session.scalars(select(ProductionBridgeAdoptionRow)))
    # Removing the standing opt-in still works after the failed network proof.
    assert not e.service.set_reviewed_update_policy(
        e.target, enabled=False, expected_revision=1
    ).enabled


def test_lost_commit_response_and_service_restart_preserve_single_adoption(
    updated: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kodi_lab.application.external_bridges import ExternalBridgeService

    e = updated
    enable(e)
    snapshot = e.service._active_snapshot
    lost = [False]

    def observe(*args: Any, **kwargs: Any) -> Any:
        result = snapshot(*args, **kwargs)
        if result.addon_version == "1.1.33" and not lost[0]:
            lost[0] = True
            raise ExternalBridgeServiceError("bridge_unreachable")
        return result

    monkeypatch.setattr(e.service, "_active_snapshot", observe)
    with pytest.raises(ExternalBridgeServiceError, match="bridge_unreachable"):
        scan(e)
    assert e.service.status(e.target).addon_version == "1.1.33"
    restarted = ExternalBridgeService(
        e.factory, endpoint_policy=e.service._endpoint_policy, secret_store=e.store
    )
    e.service = restarted
    assert scan(e).counts_exact
    with e.factory() as session:
        assert len(list(session.scalars(select(ProductionBridgeAdoptionRow)))) == 1
