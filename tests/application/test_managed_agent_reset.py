"""One role composes real Reset/Bridge coordinators with isolated native doubles."""

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.managed_agent_reset import ManagedAgentResetService
from kodi_lab.application.managed_bridge_trust import ManagedBridgeTrustService
from kodi_lab.application.managed_clean_reset import (
    ManagedResetInput,
    ManagedResetResult,
)
from kodi_lab.application.managed_versions import (
    ManagedProfileIdentity,
)
from kodi_lab.application.mcp_administration import READ_ONLY_INSPECTION_PRESET_ID
from kodi_lab.broker.managed_bridge_trust import (
    approved_bridge_build,
    require_reviewed_installation_lease,
)
from kodi_lab.broker.operations import EstablishManagedBridgeTrustResult
from kodi_lab.mcp.authorization import McpAuthorizationError
from kodi_lab.mcp.tools import McpTargetToolService
from kodi_lab.storage.models import (
    ManagedBridgePreparationRow,
    ManagedBridgeTrustRow,
    ManagedCleanResetRow,
)
from kodi_lab.storage.secrets import ExternalBridgeSecretStore
from tests.application.test_create_kodi_installation import factory as factory
from tests.application.test_create_kodi_installation import schema as schema
from tests.application.test_managed_bridge_preparation import Native
from tests.application.test_managed_bridge_trust import Client
from tests.create_kodi_readiness_support import Transport
from tests.managed_versions_support import Environment


class Setup:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self.env = Environment(sessions, root)
        self.target = self.env.prepare()
        self.env.invoke()
        self.env.wait(self.target)
        self.env.invoke("stop")
        self.env.admin.assign_client_preset(
            self.env.principal.client_id, READ_ONLY_INSPECTION_PRESET_ID
        )
        self.env.admin.remove_target_grant(self.env.principal.client_id, self.target)
        self.env.admin.set_managed_lab_role(self.env.principal.client_id, True)
        record = self.env.broker.records[0]
        self.identity = ManagedProfileIdentity(
            container_id=record.container_id,
            runtime_revision_id=UUID(str(record.runtime_revision_id)),
            image_id=record.image_id,
        )
        build = approved_bridge_build("21.3")
        client = Client()
        client.value = replace(
            client.value,
            addon_version=build["addon_version"],
            build_fingerprint=build["build_fingerprint"],
        )
        secrets = ExternalBridgeSecretStore(
            root / "bridge-secrets", master_key=b"x" * 32, key_version=1
        )
        self.trust = ManagedBridgeTrustService(
            sessions, cast(Any, self.env.broker), secrets, client=client
        )
        self.native = Native(
            cast(Any, SimpleNamespace(transport=Transport())), build["addon_version"]
        )
        self.trust.transport = self.native
        self.env.lifecycle._bridge_trust = self.trust
        self.stages = 0
        self.fail_stage = False

        def establish(**kwargs: Any) -> EstablishManagedBridgeTrustResult:
            with sessions() as db:
                require_reviewed_installation_lease(db, str(self.target))
            if kwargs.get("install_reviewed"):
                self.stages += 1
                if self.fail_stage:
                    raise RuntimeError("fixture Bridge stage failure")
                self.native.installed = True
            return EstablishManagedBridgeTrustResult(
                managed_instance_id=kwargs["managed_instance_id"],
                container_id=kwargs["expected_container_id"],
                generation=kwargs["generation"],
                bridge_id=str(client.value.bridge_id),
            )

        monkeypatch.setattr(
            self.env.broker, "establish_managed_bridge_trust", establish, raising=False
        )
        self.tools = McpTargetToolService(
            sessions,
            self.env.authorization,
            self.env.status,
            cast(Any, None),
            cast(Any, None),
            managed_versions=self.env.service,
        )
        self.service = cast(ManagedAgentResetService, self.tools._managed_agent_reset)

    def authorize(self, db: Session, target: UUID) -> None:
        self.env.authorization.recheck_managed_versions(
            db, self.env.principal, "managed_reset", target
        )
        self.env.authorization.require_managed_lab_role(db, self.env.principal)

    def operation(self, *, restart: bool = False) -> ManagedResetInput:
        return ManagedResetInput(
            version="omega",
            request_id=uuid4(),
            expected_profile=self.identity,
            restart=restart,
        )

    def invoke(self, operation: ManagedResetInput) -> ManagedResetResult:
        return self.tools.managed_reset(self.env.principal, operation)


@pytest.fixture
def setup(
    factory: sessionmaker[Session], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Setup]:
    value = Setup(factory, tmp_path, monkeypatch)
    try:
        yield value
    finally:
        value.env.close()


@pytest.mark.parametrize("restart", [False, True])
def test_role_reset_replacement_reviewed_bridge_and_exact_replay(
    setup: Setup, restart: bool
) -> None:
    operation = setup.operation(restart=restart)
    result = setup.invoke(operation)
    assert result.phase == "complete" and result.replacement_current, result
    bridge = result.bridge_preparation
    assert bridge is not None and bridge.checkpoint == "complete", result
    assert bridge.stored_trust == "active" and bridge.live_readiness == "not_observed"
    assert setup.env.broker.records[0].runtime_state == (
        "running" if restart else "stopped"
    )
    assert setup.native.installed and setup.native.enabled and setup.stages == 1
    original_counts = (
        setup.env.broker.provisions,
        setup.env.broker.removals,
        setup.env.broker.starts,
    )
    replay = setup.invoke(operation)
    assert replay.replayed and replay.operation_id == result.operation_id
    assert (
        replay.bridge_preparation is not None
        and replay.bridge_preparation.operation_id == bridge.operation_id
    )
    assert (
        setup.env.broker.provisions,
        setup.env.broker.removals,
        setup.env.broker.starts,
    ) == original_counts
    with setup.env.factory() as db:
        assert db.scalar(select(func.count()).select_from(ManagedCleanResetRow)) == 1
        assert (
            db.scalar(select(func.count()).select_from(ManagedBridgePreparationRow))
            == 1
        )
        trust = db.get(ManagedBridgeTrustRow, str(setup.target))
        assert trust is not None and trust.container_id != setup.identity.container_id


def test_stale_expected_profile_refuses_before_replacement(setup: Setup) -> None:
    wrong = setup.identity.model_copy(update={"container_id": "f" * 64})
    with pytest.raises(McpAuthorizationError, match="identity_changed"):
        setup.invoke(setup.operation().model_copy(update={"expected_profile": wrong}))
    assert (
        setup.env.broker.provisions == 1
        and setup.env.broker.removals == setup.stages == 0
    )


def test_role_reset_requires_saved_profile_identity_without_second_approval(
    setup: Setup,
) -> None:
    with pytest.raises(McpAuthorizationError, match="expected_profile_required"):
        setup.invoke(ManagedResetInput(version="omega", request_id=uuid4()))
    assert setup.env.broker.provisions == 1 and setup.env.broker.removals == 0


def test_bridge_recovery_cannot_create_a_new_reset(setup: Setup) -> None:
    with pytest.raises(McpAuthorizationError, match="original_request_required"):
        setup.invoke(setup.operation().model_copy(update={"resume_bridge_version": 0}))
    assert setup.env.broker.provisions == 1 and setup.env.broker.removals == 0


def test_bridge_failure_preserves_replacement_and_original_child(setup: Setup) -> None:
    setup.fail_stage = True
    operation = setup.operation()
    result = setup.invoke(operation)
    assert result.phase == "complete" and result.replacement_current
    assert result.bridge_preparation is not None
    assert result.bridge_preparation.checkpoint == "needs_attention"
    count = setup.env.broker.provisions, setup.env.broker.removals, setup.stages
    replay = setup.invoke(operation)
    assert replay.bridge_preparation is not None
    assert (
        replay.bridge_preparation.operation_id == result.bridge_preparation.operation_id
    )
    assert (
        setup.env.broker.provisions,
        setup.env.broker.removals,
        setup.stages,
    ) == count


def test_explicit_bridge_recovery_keeps_original_reset_and_profile(
    setup: Setup,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = {"playback": False, "block_once": True}
    monkeypatch.setattr(
        setup.env.resources,
        "_playback_factory",
        lambda *_: SimpleNamespace(
            playback_active=lambda: active["playback"],
        ),
    )
    continuation = setup.service.bridge.continue_once

    def guarded(identity: UUID, **kwargs: Any) -> Any:
        row = setup.service.bridge.get(identity)
        if row is not None and row.phase == "stop" and active["block_once"]:
            active["playback"], active["block_once"] = True, False
        return continuation(identity, **kwargs)

    monkeypatch.setattr(setup.service.bridge, "continue_once", guarded)
    operation = setup.operation()
    first = setup.invoke(operation)
    bridge = first.bridge_preparation
    assert bridge is not None and bridge.checkpoint == "needs_attention"
    assert bridge.stored_trust == "active" and first.replacement_current
    profile = setup.env.broker.records[0].container_id
    active["playback"] = False
    recovered = setup.invoke(
        operation.model_copy(update={"resume_bridge_version": bridge.version})
    )
    assert recovered.operation_id == first.operation_id and recovered.replayed
    assert recovered.bridge_preparation is not None
    assert recovered.bridge_preparation.operation_id == bridge.operation_id
    assert recovered.bridge_preparation.checkpoint == "complete"
    assert setup.env.broker.records[0].container_id == profile
    assert setup.env.broker.records[0].runtime_state == "stopped"
    assert setup.env.broker.removals == setup.stages == 1
    replay = setup.invoke(
        operation.model_copy(update={"resume_bridge_version": bridge.version})
    )
    assert replay.bridge_preparation is not None
    assert replay.bridge_preparation.checkpoint == "complete"
    assert setup.env.broker.removals == setup.stages == 1


@pytest.mark.parametrize("boundary", ["accept", "dispatch"])
def test_role_revocation_prevents_native_bridge_work_after_replacement(
    setup: Setup,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    accept = setup.service.bridge.accept

    def revoke(**kwargs: Any) -> Any:
        setup.env.admin.set_managed_lab_role(setup.env.principal.client_id, False)
        return accept(**kwargs)

    if boundary == "accept":
        monkeypatch.setattr(setup.service.bridge, "accept", revoke)
    else:
        start = setup.env.lifecycle.start_managed

        def guarded_start(*args: Any, **kwargs: Any) -> Any:
            dispatch_guard = kwargs["before_dispatch"]

            def revoked_dispatch() -> None:
                setup.env.admin.set_managed_lab_role(
                    setup.env.principal.client_id, False
                )
                dispatch_guard()

            kwargs["before_dispatch"] = revoked_dispatch
            return start(*args, **kwargs)

        monkeypatch.setattr(setup.env.lifecycle, "start_managed", guarded_start)
    with pytest.raises(McpAuthorizationError):
        setup.invoke(setup.operation())
    assert setup.env.broker.removals == 1 and setup.env.broker.provisions == 2
    assert setup.stages == 0 and setup.env.broker.records[0].runtime_state == "stopped"
