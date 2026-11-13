"""Human consent composes the real Reset and Bridge services, without MCP identity."""

from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from kodi_lab.application.managed_versions import ManagedVersionError
from kodi_lab.storage.managed_lifecycle_owner import WEB_ADMIN_OWNER
from kodi_lab.storage.models import ManagedBridgePreparationRow, ManagedCleanResetRow
from kodi_lab.web.managed_slots import ManagedSlotsWeb, WebSlotRequest
from tests.application.test_create_kodi_installation import factory as factory
from tests.application.test_create_kodi_installation import schema as schema
from tests.application.test_managed_agent_reset import Setup
from tests.application.test_managed_agent_reset import setup as setup


def adapter(setup: Setup) -> ManagedSlotsWeb:
    return ManagedSlotsWeb(setup.env.service, lambda _: False)


def request(web: ManagedSlotsWeb, *, prepare: bool = True) -> WebSlotRequest:
    with web.sessions() as db:
        snapshot = web.fingerprint(db, "omega")
    return WebSlotRequest(
        request_id=uuid4(),
        expected_snapshot=snapshot,
        confirm_reset=True,
        prepare_bridge=prepare,
    )


def admin(_: Any) -> None:
    pass


@pytest.mark.parametrize("prepare", [False, True])
def test_explicit_choice_exact_receipts_and_replay(setup: Setup, prepare: bool) -> None:
    web = adapter(setup)
    original = request(web, prepare=prepare)
    first = web.invoke("omega", "reset", original, admin)
    assert first["phase"] == "complete", first
    assert first["done"] and not first["can_resume"]
    assert first["request"]["prepare_bridge"] is prepare
    assert setup.env.broker.records[0].runtime_state == "stopped"
    assert setup.stages == int(prepare)
    counts = setup.env.broker.removals, setup.env.broker.provisions, setup.stages
    read = web.receipt(original.request_id, admin)
    replay = web.invoke("omega", "reset", original, admin)
    assert read == replay == first
    assert (
        setup.env.broker.removals,
        setup.env.broker.provisions,
        setup.stages,
    ) == counts
    with web.sessions() as db:
        assert db.scalar(select(func.count()).select_from(ManagedCleanResetRow)) == 1
        assert db.scalar(
            select(func.count()).select_from(ManagedBridgePreparationRow)
        ) == int(prepare)
    if prepare:
        bridge = first["technical"]["bridge_preparation"]
        assert bridge["checkpoint"] == "complete" and bridge["stored_trust"] == "active"
        assert bridge["live_readiness"] == "not_observed"
        assert bridge["operation_id"] != first["operation_id"]
    changed = original.model_copy(update={"prepare_bridge": not prepare})
    with pytest.raises(ManagedVersionError, match="request_conflict"):
        web.invoke("omega", "reset", changed, admin)


def test_failed_preparation_preserves_replacement_and_read_is_inert(
    setup: Setup,
) -> None:
    web = adapter(setup)
    setup.fail_stage = True
    original = request(web)
    first = web.invoke("omega", "reset", original, admin)
    assert first["phase"] == "bridge_blocked" and not first["done"], first
    assert first["technical"]["phase"] == "complete"
    assert first["technical"]["replacement_current"]
    before = setup.env.broker.provisions, setup.env.broker.removals, setup.stages
    profile = setup.env.broker.records[0].container_id
    for _ in range(2):
        assert web.receipt(original.request_id, admin) == first
        web.status(admin)
    replay = web.invoke("omega", "reset", original, admin)
    assert replay["phase"] == "bridge_blocked"
    assert (
        setup.env.broker.provisions,
        setup.env.broker.removals,
        setup.stages,
    ) == before
    assert setup.env.broker.records[0].container_id == profile
    assert next(s for s in web.status(admin)["slots"] if s["version"] == "omega")[
        "pending_request"
    ] == str(original.request_id)


def test_interruption_before_preparation_resumes_same_reset(
    setup: Setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    web = adapter(setup)
    original = request(web)
    invoke = web.prepared_reset.invoke
    with monkeypatch.context() as patch:
        patch.setattr(
            web.prepared_reset,
            "invoke",
            lambda *_: (_ for _ in ()).throw(RuntimeError("interrupted")),
        )
        with pytest.raises(RuntimeError, match="interrupted"):
            web.invoke("omega", "reset", original, admin)
    read = web.receipt(original.request_id, admin)
    assert read["phase"] == "bridge_pending" and read["can_resume"]
    assert setup.stages == 0
    assert web.prepared_reset.invoke == invoke
    result = web.invoke("omega", "reset", original, admin)
    assert (
        result["phase"] == "complete" and result["operation_id"] == read["operation_id"]
    )
    assert setup.env.broker.removals == setup.stages == 1


def test_revoked_human_authority_after_replacement_refuses_preparation(
    setup: Setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    web = adapter(setup)
    original = request(web)
    allowed = True

    def authorize(_: Any) -> None:
        if not allowed:
            raise PermissionError("revoked human session")

    invoke = web.prepared_reset.invoke

    def revoked(*args: Any, **kwargs: Any) -> Any:
        nonlocal allowed
        allowed = False
        return invoke(*args, **kwargs)

    monkeypatch.setattr(web.prepared_reset, "invoke", revoked)
    with pytest.raises(PermissionError, match="revoked"):
        web.invoke("omega", "reset", original, authorize)
    assert setup.env.broker.removals == 1 and setup.stages == 0
    assert setup.env.broker.records[0].runtime_state == "stopped"
    with pytest.raises(PermissionError):
        web.receipt(original.request_id, authorize)


def test_preparation_cannot_be_attached_to_start_or_stop(setup: Setup) -> None:
    web = adapter(setup)
    original = request(web)
    for action in ("start", "stop"):
        with pytest.raises(ManagedVersionError, match="request_conflict"):
            web.invoke("omega", action, original, admin)
    assert setup.env.broker.removals == setup.stages == 0


def test_bridge_recovery_cannot_create_a_human_reset(setup: Setup) -> None:
    web = adapter(setup)
    original = request(web).model_copy(update={"resume_bridge_version": 0})
    with pytest.raises(ManagedVersionError, match="original_request_required"):
        web.invoke("omega", "reset", original, admin)
    assert setup.env.broker.removals == setup.stages == 0


@pytest.mark.parametrize("blocker", ["input", "view-only", "interactive", "playback"])
def test_human_preparation_retains_active_use_guards(
    setup: Setup, blocker: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from kodi_lab.application.browser_remote_coordination import (
        acquire_browser_remote,
        release_browser_remote,
    )
    from kodi_lab.application.novnc_control import HumanViewerOwnership
    from kodi_lab.broker.novnc import ViewBinding
    from kodi_lab.storage.models import ManagedInstanceRow

    setup.env.invoke()
    setup.env.wait(setup.target)
    # The Web owner is distinct from the fixture agent. Release that agent's
    # running-use receipt before isolating playback/viewer/input protection.
    setup.env.invoke("stop")
    setup.env.service.invoke(
        client_id=WEB_ADMIN_OWNER,
        request_id=uuid4(),
        version="omega",
        action="start",
        authorize=lambda *_: None,
    )
    setup.env.wait(setup.target)
    web = adapter(setup)
    original = request(web)
    handle = None
    if blocker == "input":
        handle = acquire_browser_remote(web.sessions, setup.target)
    elif blocker == "playback":
        monkeypatch.setattr(
            setup.env.resources,
            "_playback_factory",
            lambda *_: SimpleNamespace(playback_active=lambda: True),
        )
    else:
        record = setup.env.broker.records[0]
        with web.sessions() as db:
            instance = db.get(ManagedInstanceRow, str(setup.target))
            assert instance is not None
            epoch = instance.runtime_state_revision
        HumanViewerOwnership(web.sessions).create(
            setup.target,
            uuid4(),
            "f" * 64,
            ViewBinding(
                record.container_id,
                record.image_id,
                "fixture",
                (str(record.runtime_revision_id), epoch),
            ),
            blocker,
        )
    try:
        if blocker == "playback":
            result = web.invoke("omega", "reset", original, admin)
            assert result["phase"] == "blocked" and result["code"] == "playback_active"
        else:
            with pytest.raises(ManagedVersionError):
                web.invoke("omega", "reset", original, admin)
    finally:
        if handle is not None:
            release_browser_remote(handle)
    assert setup.env.broker.removals == setup.stages == 0


def test_capacity_failure_preserves_native_receipt_and_explicit_recovery(
    setup: Setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kodi_lab.application.managed_resources import ManagedResourceProtected

    web = adapter(setup)
    original = request(web)
    start = setup.env.lifecycle.start_managed

    def unavailable(*_: Any, **__: Any) -> Any:
        raise ManagedResourceProtected("managed_capacity_exhausted")

    with monkeypatch.context() as patch:
        patch.setattr(setup.env.lifecycle, "start_managed", unavailable)
        first = web.invoke("omega", "reset", original, admin)
    assert (
        first["phase"] == "bridge_blocked" and first["technical"]["phase"] == "complete"
    )
    assert setup.env.broker.records[0].runtime_state == "stopped"
    assert first["resume_bridge_version"] is not None
    assert setup.env.lifecycle.start_managed == start
    recovered = web.invoke(
        "omega",
        "reset",
        original.model_copy(
            update={"resume_bridge_version": first["resume_bridge_version"]}
        ),
        admin,
    )
    assert recovered["phase"] == "complete", recovered
    assert recovered["operation_id"] == first["operation_id"]
    assert (
        recovered["technical"]["bridge_preparation"]["operation_id"]
        == first["technical"]["bridge_preparation"]["operation_id"]
    )
    assert setup.env.broker.removals == setup.stages == 1
