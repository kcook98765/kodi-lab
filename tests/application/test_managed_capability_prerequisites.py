"""Saved trust is a prerequisite, never a current capability/readiness grant."""

from dataclasses import replace
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import event

from kodi_lab.application.managed_bridge_trust import identity_json
from kodi_lab.application.managed_capability_prerequisites import (
    CapabilityPrerequisitesUnavailable,
    ManagedCapabilityPrerequisiteService,
)
from kodi_lab.broker.managed_bridge_trust import approved_bridge_build
from kodi_lab.storage.models import ManagedBridgeTrustRow
from tests.application.test_managed_bridge_trust import row
from tests.application.test_managed_bridge_trust import setup as setup
from tests.web.conftest import INSTANCE_ID
from tests.web.conftest import database as database


def assessment(owner: Any) -> Any:
    result = ManagedCapabilityPrerequisiteService(owner.sessions).inspect(INSTANCE_ID)
    assert len(result.checks) == 6
    assert result.native_readiness == result.prompt_state == "unknown"
    assert result.current_container_binding == "not_observed"
    assert all(item.live_readiness == "unknown" for item in result.checks)
    assert all(item.bridge_prerequisite == "not_required" for item in result.checks[3:])
    return result


def reasons(result: Any) -> dict[str, str]:
    return {item.capability: item.reason for item in result.checks}


def test_missing_trust_permits_only_conditional_native_fallback(setup: Any) -> None:
    owner, broker, client, _ = setup
    result = assessment(owner)
    assert result.trust_state == "no_record" and result.trust_generation is None
    assert result.last_authenticated_addon_version is None
    assert result.candidate_addon_version == approved_bridge_build()["addon_version"]
    assert reasons(result)["ui_inspection"] == "native_fallback_requires_observation"
    assert reasons(result)["log_read"] == "bridge_not_paired"
    assert broker.establishments == client.calls == 0 and not owner.transport.calls


@pytest.mark.parametrize("state", ["prepared", "invalidated", "revoked"])
def test_nonactive_trust_retains_ui_denial_and_separate_native_capabilities(
    setup: Any, state: str
) -> None:
    owner, _, _, _ = setup
    owner.prepare(INSTANCE_ID)
    with owner.sessions.begin() as session:
        trust = session.get(ManagedBridgeTrustRow, str(INSTANCE_ID))
        assert trust is not None
        trust.state = state
        trust.reviewed_installation = True
        if state != "prepared":
            trust.secret_reference = None
    result = assessment(owner)
    assert all(item.bridge_prerequisite == "blocked" for item in result.checks[:3])
    assert reasons(result)["ui_inspection"] == "bridge_recovery_required"
    assert result.last_authenticated_addon_version is None
    assert result.next_action == (
        "resume_reviewed_bridge" if state == "prepared" else "review_bridge"
    )
    assert result.mutation_admission_required


@pytest.mark.parametrize("log_capability", [True, False])
def test_active_trust_does_not_claim_liveness_or_read_secret(
    setup: Any, monkeypatch: pytest.MonkeyPatch, log_capability: bool
) -> None:
    owner, broker, client, secrets = setup
    client.value = replace(
        client.value,
        capabilities=("ui_observation", "log_read")
        if log_capability
        else ("ui_observation",),
    )
    owner.prepare(INSTANCE_ID)
    before = row(owner)

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("status tried to read authority or probe")

    for obj, name in (
        (owner, "prepare"),
        (broker, "managed_container_inventory"),
        (secrets, "read"),
        (client, "identity"),
        (owner.transport, "post"),
    ):
        monkeypatch.setattr(obj, name, forbidden)
    engine = owner.sessions.kw["bind"]
    statements: list[str] = []

    def record(_connection: Any, _cursor: Any, statement: str, *_args: Any) -> None:
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        result = assessment(owner)
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert statements and all(
        s.lstrip().upper().startswith("SELECT") for s in statements
    )
    assert result.last_authenticated_addon_version == client.value.addon_version
    assert result.last_authenticated_container_id == before.container_id
    assert result.last_authenticated_at is not None
    assert reasons(result)["log_read"] == (
        "stored_identity_requires_observation"
        if log_capability
        else "log_capability_missing"
    )
    assert reasons(result)["manifest_launch"] == "stored_identity_requires_observation"
    assert row(owner).generation == before.generation
    assert before.secret_reference is not None and before.bridge_id is not None
    payload = result.model_dump_json()
    assert before.secret_reference not in payload and before.bridge_id not in payload
    assert "build_fingerprint" not in payload


@pytest.mark.parametrize("build", ["previous", "historical", "retained", "legacy"])
def test_approved_older_observation_identity_does_not_imply_launch(
    setup: Any, build: str
) -> None:
    owner, _, client, _ = setup
    owner.prepare(INSTANCE_ID)
    approved = approved_bridge_build(
        previous=build == "previous",
        historical=build == "historical",
        retained=build == "retained",
        legacy=build == "legacy",
    )
    identity = replace(
        client.value,
        addon_version=approved["addon_version"],
        build_fingerprint=approved["build_fingerprint"],
        capabilities=("ui_observation", "log_read"),
    )
    with owner.sessions.begin() as session:
        trust = session.get(ManagedBridgeTrustRow, str(INSTANCE_ID))
        assert trust is not None
        trust.identity_json = identity_json(identity)
    result = assessment(owner)
    assert (
        result.checks[0].bridge_prerequisite
        == result.checks[1].bridge_prerequisite
        == "present"
    )
    assert reasons(result)["manifest_launch"] == "launch_upgrade_required"


@pytest.mark.parametrize(
    "defect",
    ["invalid_json", "wrong_build", "wrong_bridge", "wrong_release", "timestamp"],
)
def test_bad_stored_identity_never_claims_a_present_prerequisite(
    setup: Any, defect: str
) -> None:
    owner, _, client, _ = setup
    owner.prepare(INSTANCE_ID)
    with owner.sessions.begin() as session:
        trust = session.get(ManagedBridgeTrustRow, str(INSTANCE_ID))
        assert trust is not None
        if defect == "invalid_json":
            trust.identity_json = "{}"
        elif defect == "timestamp":
            trust.established_at = "not-a-time"
        else:
            if defect == "wrong_build":
                identity = replace(client.value, build_fingerprint="0" * 64)
            elif defect == "wrong_bridge":
                identity = replace(client.value, bridge_id=uuid4())
            else:
                identity = replace(client.value, kodi_version="20.5")
            trust.identity_json = identity_json(identity)
    result = assessment(owner)
    assert all(item.bridge_prerequisite == "blocked" for item in result.checks[:3])
    assert result.last_authenticated_addon_version is None
    assert result.next_action == "review_history"


def test_unknown_target_is_not_projected(setup: Any) -> None:
    owner, _, _, _ = setup
    with pytest.raises(
        CapabilityPrerequisitesUnavailable, match="managed_target_unavailable"
    ):
        ManagedCapabilityPrerequisiteService(owner.sessions).inspect(uuid4())


@pytest.mark.parametrize("state", ["active", "prepared"])
def test_runtime_drift_never_offers_prepared_continuation(
    setup: Any, state: str
) -> None:
    from sqlalchemy import inspect

    from kodi_lab.storage.models import RuntimeRevisionRow
    from tests.web.conftest import REVISION_ID

    owner, _, _, _ = setup
    owner.prepare(INSTANCE_ID)
    with owner.sessions.begin() as session:
        previous = session.get(RuntimeRevisionRow, str(REVISION_ID))
        assert previous is not None
        values = {
            column.key: getattr(previous, column.key)
            for column in inspect(RuntimeRevisionRow).columns
        }
        values.update(id=str(uuid4()), revision_number=previous.revision_number + 1)
        session.add(RuntimeRevisionRow(**values))
        session.flush()
        trust = session.get(ManagedBridgeTrustRow, str(INSTANCE_ID))
        assert trust is not None
        trust.runtime_revision_id = values["id"]
        trust.state = state
        trust.reviewed_installation = True
    result = assessment(owner)
    assert reasons(result)["ui_inspection"] == "bridge_runtime_mismatch"
    assert result.next_action != "resume_reviewed_bridge"


@pytest.mark.parametrize("profile_state", ["missing", "unsupported_release"])
def test_missing_or_unsupported_profile_cannot_advertise_install_candidate(
    setup: Any, profile_state: str
) -> None:
    from kodi_lab.storage.models import RuntimeExecutionProfileRow
    from tests.web.conftest import REVISION_ID

    owner, _, _, _ = setup
    with owner.sessions.begin() as session:
        profile = session.get(RuntimeExecutionProfileRow, str(REVISION_ID))
        assert profile is not None
        if profile_state == "missing":
            session.delete(profile)
        else:
            profile.expected_kodi_version = "20.5"
    result = assessment(owner)
    assert result.candidate_addon_version is None
    assert result.kodi_release == (None if profile_state == "missing" else "20.5")


def test_external_target_is_outside_managed_prerequisites(setup: Any) -> None:
    from tests.web.conftest import EXTERNAL_TARGET_ID, seed_external_target

    owner, _, _, _ = setup
    seed_external_target(owner.sessions)
    with pytest.raises(CapabilityPrerequisitesUnavailable):
        ManagedCapabilityPrerequisiteService(owner.sessions).inspect(EXTERNAL_TARGET_ID)


def test_preview_candidate_and_authenticated_release_remain_exact(setup: Any) -> None:
    from kodi_lab.storage.models import RuntimeExecutionProfileRow
    from tests.web.conftest import REVISION_ID

    owner, _, client, _ = setup
    owner.prepare(INSTANCE_ID)
    approved = approved_bridge_build("22.0-rc.1")
    identity = replace(
        client.value,
        kodi_version="22.0",
        kodi_release="22.0-rc.1",
        addon_version=approved["addon_version"],
        build_fingerprint=approved["build_fingerprint"],
    )
    with owner.sessions.begin() as session:
        profile = session.get(RuntimeExecutionProfileRow, str(REVISION_ID))
        trust = session.get(ManagedBridgeTrustRow, str(INSTANCE_ID))
        assert profile is not None and trust is not None
        profile.expected_kodi_version = "22.0"
        profile.expected_version_tag = "rc"
        profile.expected_prerelease_ordinal = 1
        trust.identity_json = identity_json(identity)
    result = assessment(owner)
    assert result.kodi_release == "22.0-rc.1"
    assert (
        result.candidate_addon_version
        == result.last_authenticated_addon_version
        == "1.1.26"
    )
    assert reasons(result)["manifest_launch"] == "stored_identity_requires_observation"
