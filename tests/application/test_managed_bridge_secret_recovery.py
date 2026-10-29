"""Production Bridge admission and recoverable secret-publication boundaries."""

import os
import stat
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from kodi_lab.application.managed_bridge_trust import (
    ManagedBridgeMutationUnconfirmedError,
)
from kodi_lab.broker.docker_inventory import ContainerRuntimeState
from kodi_lab.storage.development_sessions import DevelopmentSessionConflict
from kodi_lab.storage.models import (
    DevelopmentSessionPolicyRow,
    ExternalSecretRetirementRow,
    ManagedBridgeTrustRow,
    ManagedInstanceLifecycleOperationRow,
    ManagedProvisionReservationRow,
    ManagedStartReservationRow,
)
from kodi_lab.storage.secrets import retire_pending_external_secrets
from tests.application.test_mcp_workspace_development import NOW_TEXT
from tests.application.test_retained_bridge_lifecycle import files, setup


@pytest.mark.parametrize("kind", ["start", "provision"])
@pytest.mark.parametrize("phase", ["reserved", "uncertain"])
def test_legacy_background_bridge_preserves_runtime_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, phase: str
) -> None:
    s = setup(tmp_path, monkeypatch)
    tid, record = s.ids["omega"], s.records[2]
    s.records[:] = [replace(record, runtime_state=ContainerRuntimeState.RUNNING)]
    model = (
        ManagedStartReservationRow
        if kind == "start"
        else ManagedProvisionReservationRow
    )
    operation_id = str(uuid4())
    with s.env.sessions.begin() as session:
        policy = session.get(DevelopmentSessionPolicyRow, 1)
        assert policy is not None
        policy.retain_profiles = False
        policy.retained_omega_target_id = None
        values = dict(
            managed_instance_id=str(tid),
            runtime_revision_id=str(record.runtime_revision_id),
            operation_id=operation_id,
            phase=phase,
            created_at=NOW_TEXT,
        )
        if kind == "start":
            values["container_id"] = record.container_id
        session.add(model(**values))
    before = files(tmp_path / "secrets"), files(tmp_path / "profiles")
    try:
        with pytest.raises(DevelopmentSessionConflict, match=kind + "_pending"):
            s.owner.prepare_background(tid)
        assert (files(tmp_path / "secrets"), files(tmp_path / "profiles")) == before
        assert s.broker.stages == s.broker.starts == s.broker.stops == 0
        assert not s.owner.transport.calls
        with s.env.sessions() as session:
            reservation = session.get(model, str(tid))
            assert reservation is not None
            assert (reservation.operation_id, reservation.phase) == (
                operation_id,
                phase,
            )
            assert session.scalar(select(ManagedInstanceLifecycleOperationRow)) is None
            assert session.scalar(select(ManagedBridgeTrustRow)) is None
    finally:
        s.readiness.close()


@pytest.mark.parametrize("route", ["install", "resume", "background"])
@pytest.mark.parametrize("retirement_fails", [False, True])
def test_bridge_post_rename_failure_retains_exact_cleanup_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    route: str,
    retirement_fails: bool,
) -> None:
    s = setup(tmp_path, monkeypatch)
    tid = s.ids["omega"]
    s.records[2] = replace(s.records[2], runtime_state=ContainerRuntimeState.RUNNING)
    if route == "resume":
        # A real prepared intent is required; this test does not fabricate trust.
        s.owner.stage_reviewed(tid)
    stages = s.broker.stages
    old_files = files(tmp_path / "secrets")
    references: list[UUID] = []
    renamed = False
    original_replace, original_fsync, original_retire = (
        os.replace,
        os.fsync,
        s.secrets.retire,
    )

    def rename(source: Any, destination: Any, **kwargs: Any) -> None:
        nonlocal renamed
        original_replace(source, destination, **kwargs)
        references.append(UUID(str(destination).removesuffix(".json")))
        renamed = True

    def fsync(descriptor: int) -> None:
        nonlocal renamed
        if renamed and stat.S_ISDIR(os.fstat(descriptor).st_mode):
            renamed = False
            raise OSError("injected post-rename directory sync failure")
        original_fsync(descriptor)

    def retire(reference: UUID) -> None:
        if retirement_fails and reference in references:
            raise OSError("injected retirement failure")
        original_retire(reference)

    monkeypatch.setattr(os, "replace", rename)
    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(s.secrets, "retire", retire)
    try:
        with pytest.raises(ManagedBridgeMutationUnconfirmedError):
            if route == "install":
                s.lifecycle.install_reviewed_bridge(tid)
            elif route == "resume":
                s.lifecycle.resume_reviewed_bridge(tid, expected_generation=1)
            else:
                s.owner.prepare_background(tid)
        assert len(references) == 1
        reference = references[0]
        assert s.broker.stages == stages
        assert s.broker.starts == s.broker.stops == 0
        with s.env.sessions() as session:
            op = session.scalar(select(ManagedInstanceLifecycleOperationRow))
            assert op is not None and op.state == "reconciliation_required"
            assert op.failure_reason == "broker_runtime_dispatch_unconfirmed"
            trust = session.get(ManagedBridgeTrustRow, str(tid))
            assert trust is None or trust.secret_reference != str(reference)
            pending = session.get(
                ExternalSecretRetirementRow, ("bridge", str(reference))
            )
            assert (pending is not None) == retirement_fails
            if pending is not None:
                assert pending.target_id == str(tid)
        if retirement_fails:
            assert (tmp_path / "secrets" / f"{reference}.json").is_file()
            # The existing durable recovery facility needs no guessed file ID.
            retire_pending_external_secrets(
                s.env.sessions,
                store_kind="bridge",
                target_id=tid,
                retire=original_retire,
            )
        assert files(tmp_path / "secrets") == old_files
        with s.env.sessions() as session:
            assert (
                session.get(ExternalSecretRetirementRow, ("bridge", str(reference)))
                is None
            )
            assert (
                session.scalar(select(ManagedInstanceLifecycleOperationRow)).state
                == "reconciliation_required"
            )
    finally:
        s.readiness.close()
