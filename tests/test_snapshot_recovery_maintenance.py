from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from kodi_lab.application.snapshot_recovery_evidence import RecoveryRefused, digest
from kodi_lab.application.snapshot_recovery_maintenance import (
    bind_owner,
    leave_epoch,
    startup_epoch,
)
from kodi_lab.snapshot_recovery_host import (
    InstallationPlan,
    _application_owner,
    _cgroup,
    drain,
    validate_drain_log,
)
from kodi_lab.storage.models import SnapshotRecoveryEpochRow
from tests.snapshot_recovery_support import Case
from tests.snapshot_recovery_support import factory as factory
from tests.snapshot_recovery_support import recovery_case as recovery_case


@pytest.mark.parametrize(
    "fault", [None, "unknown", "error", "incomplete", "uncertain", "diagnostics_only"]
)
def test_mixed_broker_diagnostics_are_not_fabricated_audit_completions(
    fault: str | None,
) -> None:
    stamp = "2026-11-23 22:42:56,518 INFO "
    measurement = {
        "completed": fault != "incomplete",
        "counts": {"source_files": 2},
        "layer": "copy_sync",
        "operation_id": "11111111-1111-4111-8111-111111111111",
        "phases_seconds": {"source_scan_hash": 0.001},
        "schema_version": 1,
        "total_seconds": 0.01,
    }
    lines = [
        stamp + "alembic.runtime.plugins setup plugin alembic.autogenerate.schemas",
        stamp
        + "alembic.runtime.plugins setup plugin alembic.ext.checkconstraint_byname",
        stamp
        + "kodi_lab.deployment.measurement deployment measurement "
        + json.dumps(measurement),
    ]
    if fault != "diagnostics_only":
        lines.append(
            json.dumps(
                {
                    "event": "broker request completed",
                    "broker_result_code": "response_transport_error"
                    if fault == "uncertain"
                    else "success",
                }
            )
        )
    if fault == "unknown":
        lines.append(stamp + "unknown.logger unknown dispatch")
    if fault == "error":
        lines.append(
            stamp.replace(" INFO ", " ERROR ")
            + "alembic.runtime.plugins setup plugin alembic.autogenerate.schemas"
        )
    raw = "\n".join(lines).encode()
    if fault is None:
        assert validate_drain_log(raw) == {
            "audits": 1,
            "alembic_setup": 2,
            "completed_measurements": 1,
        }
    else:
        with pytest.raises(RecoveryRefused):
            validate_drain_log(raw)


def test_active_epoch_requires_restricted_startup_and_restart_fences_claim(
    recovery_case: Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = recovery_case
    monkeypatch.delenv("KODI_LAB_SNAPSHOT_RECOVERY_EPOCH", raising=False)
    with pytest.raises(RecoveryRefused, match="restricted_startup"):
        startup_epoch(case.sessions)
    monkeypatch.setenv("KODI_LAB_SNAPSHOT_RECOVERY_EPOCH", case.epoch_id)
    assert startup_epoch(case.sessions) == case.epoch_id
    claimed = case.claim()
    bind_owner(case.sessions, case.epoch_id, "broker", "new-process")
    case.service.execute(claimed["recovery_id"])
    assert case.provider.calls == []
    with case.sessions() as session:
        boundary = session.get(SnapshotRecoveryEpochRow, case.epoch_id)
        assert boundary is not None and boundary.state == "blocked"
    with pytest.raises(RecoveryRefused):
        leave_epoch(case.sessions, case.epoch_id, case.seal)


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "live_worker",
        "broker_pid",
        "restart",
        "exit",
        "wrong_service",
        "wrong_engine",
        "wrong_context",
        "wrong_volume",
        "uncertain_audit",
    ],
)
def test_host_uses_same_verified_sdk_engine_and_full_cids_only(
    recovery_case: Case,
    monkeypatch: pytest.MonkeyPatch,
    fault: str | None,
) -> None:
    case = recovery_case
    plan = InstallationPlan(
        installation="owned-qa",
        bundle_root=str(case.root),
        compose_file=str(case.root / "compose.yml"),
        engine_id="owned-engine",
        state_volume="qa-state",
        web_container_id="c" * 64,
        broker_container_id="d" * 64,
        candidate_package_digest=case.seal.candidate_package_digest,
        review=case.review,
    )
    stopped: set[str] = set()
    calls: list[tuple[str, int]] = []

    class Host:
        def info(self) -> dict[str, str]:
            return {"ID": "wrong" if fault == "wrong_engine" else "owned-engine"}

        def inspect_volume(self, name: str) -> dict[str, str]:
            return {
                "Mountpoint": "/synthetic-state",
                "Name": name,
                "CreatedAt": "2026-11-23T00:00:00Z",
                "Driver": "local",
                "Scope": "local",
            }

        def inspect_container(self, cid: str) -> dict[str, Any]:
            role = "web" if cid == plan.web_container_id else "broker"
            exited = cid in stopped
            return {
                "Id": cid,
                "Name": "/synthetic-" + role,
                "Created": "2026-01-01T00:00:00Z",
                "Image": "sha256:" + "8" * 64,
                "RestartCount": 1 if fault == "restart" and exited else 0,
                "Config": {
                    "Hostname": cid[:12],
                    "Labels": {
                        "com.docker.compose.project": "owned-qa",
                        "com.docker.compose.service": "unrelated"
                        if fault == "wrong_service"
                        else role,
                        "com.docker.compose.project.working_dir": str(case.root),
                    },
                },
                "HostConfig": {"PidMode": "", "CgroupParent": ""},
                "Mounts": [
                    {
                        "Type": "volume",
                        "Name": "other" if fault == "wrong_volume" else "qa-state",
                        "Destination": "/var/lib/kodi-lab",
                        "RW": True,
                    }
                ],
                "State": {
                    "Running": not exited,
                    "Pid": 100
                    if not exited or (fault == "broker_pid" and role == "broker")
                    else 0,
                    "Status": "exited" if exited else "running",
                    "ExitCode": 137 if fault == "exit" else 0,
                    "FinishedAt": "2026-11-23T00:00:00.000000Z",
                },
            }

        def containers(self, *, all: bool) -> list[dict[str, Any]]:
            assert all
            return [
                {"Id": cid, "Mounts": self.inspect_container(cid)["Mounts"]}
                for cid in (plan.web_container_id, plan.broker_container_id)
            ]

        def stop(self, cid: str, *, timeout: int) -> None:
            calls.append((cid, timeout))
            stopped.add(cid)

        def logs(self, *_a: Any, **_k: Any) -> bytes:
            code = (
                "response_transport_error" if fault == "uncertain_audit" else "success"
            )
            return json.dumps(
                {"event": "broker request completed", "broker_result_code": code}
            ).encode()

    def context(_pid: int, cid: str) -> tuple[str, str]:
        if fault == "wrong_context":
            raise RecoveryRefused("recovery_exact_service_cgroup_unavailable")
        return "/docker/" + cid, "10"

    monkeypatch.setattr("kodi_lab.snapshot_recovery_host._cgroup", context)
    monkeypatch.setattr(
        "kodi_lab.snapshot_recovery_host._empty_cgroup",
        lambda _path: fault != "live_worker",
    )
    if fault is None:
        seal = drain(plan, Host())
        assert seal.engine_id == "owned-engine" and seal.state_volume == "qa-state"
        assert seal.web.cgroup.endswith(plan.web_container_id)
    else:
        with pytest.raises((RecoveryRefused, ValueError)):
            drain(plan, Host())
    assert all(
        cid in {plan.web_container_id, plan.broker_container_id} and timeout == -1
        for cid, timeout in calls
    )
    if fault == "uncertain_audit":
        assert calls == []
    assert case.provider.calls == []


@pytest.mark.parametrize("fault", [None, "cid", "pid", "start"])
def test_daemon_pid_must_belong_to_exact_full_container_cgroup(
    monkeypatch: pytest.MonkeyPatch,
    fault: str | None,
) -> None:
    cid = "a" * 64
    calls = 0

    def read(path: Path, *_a: Any, **_k: Any) -> str:
        nonlocal calls
        name = str(path)
        if name == "/proc/123/stat":
            calls += 1
            return "123 (tini) " + " ".join(
                ["S"] + ["0"] * 18 + ["11" if fault == "start" and calls > 1 else "10"]
            )
        if name == "/proc/123/cgroup":
            return "0::/docker/" + ("b" * 64 if fault == "cid" else cid)
        if name.endswith("cgroup.events"):
            return "populated 1\nfrozen 0\n"
        if name.endswith("cgroup.procs"):
            return "999\n" if fault == "pid" else "123\n"
        raise AssertionError(name)

    monkeypatch.setattr(Path, "read_text", read)
    if fault is None:
        assert _cgroup(123, cid) == ("/docker/" + cid, "10")
    else:
        with pytest.raises(RecoveryRefused):
            _cgroup(123, cid)


@pytest.mark.parametrize(
    "fault", [None, "init", "application", "namespace_pid", "cid", "volume"]
)
def test_leave_binds_tini_init_separately_from_application_owner(
    recovery_case: Case, fault: str | None
) -> None:
    case = recovery_case
    with case.sessions() as session:
        boundary = session.get(SnapshotRecoveryEpochRow, case.epoch_id)
        assert boundary is not None
        web, broker = (
            json.loads(boundary.web_owner or "null"),
            json.loads(boundary.broker_owner or "null"),
        )

    def retirement(role: str, owner: dict[str, Any]) -> dict[str, Any]:
        before = getattr(case.seal, role).model_dump()
        return {
            **before,
            "container_id": owner["hostname"] + owner["hostname"][0] * 52,
            "initial_start": owner["init_start"],
            "application_start": owner["start"],
            "application_namespace_pid": owner["pid"],
            "application_owner_digest": digest(owner),
        }

    data = {
        **case.seal.model_dump(),
        "web": retirement("web", web),
        "broker": retirement("broker", broker),
    }
    data["state_consumers"]["retired_services"] = {
        data["web"]["container_id"]: "0" * 64,
        data["broker"]["container_id"]: "1" * 64,
    }
    if fault == "init":
        data["web"]["initial_start"] = "99"
    if fault == "application":
        data["web"]["application_start"] = web["init_start"]
    if fault == "namespace_pid":
        data["web"]["application_namespace_pid"] = 999
    if fault == "cid":
        data["web"]["container_id"] = "0" * 64
    if fault == "volume":
        data["state_volume_digest"] = "0" * 64
    observed = type(case.seal).model_validate(data)
    if fault is None:
        leave_epoch(case.sessions, case.epoch_id, observed)
        with case.sessions() as session:
            boundary = session.get(SnapshotRecoveryEpochRow, case.epoch_id)
            assert (
                boundary is not None
                and boundary.active is None
                and boundary.state == "closed"
            )
    else:
        with pytest.raises(RecoveryRefused):
            leave_epoch(case.sessions, case.epoch_id, observed)


def test_wrong_init_cannot_be_asserted_as_application_owner(
    recovery_case: Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "kodi_lab.snapshot_recovery_host._cgroup",
        lambda _pid, cid: ("/docker/" + cid, "10"),
    )
    owner = {
        "init_start": "99",
        "hostname": "c" * 12,
        "boot": recovery_case.seal.boot_id,
    }
    with pytest.raises(RecoveryRefused, match="init_mismatch"):
        _application_owner(123, "c" * 64, json.dumps(owner))
