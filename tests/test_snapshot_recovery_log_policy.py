from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from kodi_lab.application.snapshot_recovery_evidence import RecoveryRefused
from kodi_lab.snapshot_recovery_host import (
    BrokerLogPolicy,
    InstallationPlan,
    drain,
    inspect_broker_log_policy,
    validate_drain_log,
)
from tests.snapshot_recovery_support import Case, ConsumerEngine
from tests.snapshot_recovery_support import factory as factory
from tests.snapshot_recovery_support import recovery_case as recovery_case


def diagnostic() -> dict[str, object]:
    return {
        "completed": False,
        "schema_version": 1,
        "layer": "copy_sync",
        "operation_id": str(uuid4()),
        "total_seconds": 0.1,
        "counts": {
            "source_entries": 2,
            "source_files": 1,
            "source_scan_bytes": 500,
            "destination_before_archive_bytes": 2048,
            "review_confirm_archive_bytes": 2048,
        },
        "phases_seconds": {
            key: 0.01
            for key in (
                "stage_create",
                "source_scan_hash",
                "destination_before",
                "review_confirm",
                "stage_cleanup",
            )
        },
    }


def audit() -> dict[str, object]:
    return {
        "event": "broker request completed",
        "broker_operation": "review_source_additions",
        "broker_request_id": str(uuid4()),
        "broker_protocol_version": 1,
        "broker_peer_uid": 10001,
        "broker_peer_gid": 10001,
        "broker_peer_pid": 0,
        "broker_result_code": "success",
        "broker_elapsed_seconds": 0.2,
    }


def review_log(
    measured: dict[str, object], following: dict[str, object] | None
) -> bytes:
    return (
        "2026-11-23 20:21:48,750 INFO kodi_lab.deployment.measurement "
        "deployment measurement "
        + json.dumps(measured)
        + ("\n" + json.dumps(following) if following is not None else "")
    ).encode()


@pytest.fixture
def policy(tmp_path: Path) -> BrokerLogPolicy:
    sdk = ConsumerEngine(tmp_path)
    sdk.records["d" * 64] = sdk.record("d" * 64, "broker", running=True)
    return inspect_broker_log_policy(sdk, "d" * 64)


def test_successful_readonly_preview_preserves_raw_false_and_absent_id_join(
    policy: BrokerLogPolicy,
) -> None:
    measured, following = diagnostic(), audit()
    assert measured["operation_id"] != following["broker_request_id"]
    raw = review_log(measured, following)
    before = bytes(raw)
    result = validate_drain_log(raw, policy)
    assert (
        result["read_only_review_diagnostics"] == 1
        and result["completed_measurements"] == 0
    )
    assert result["audits"] == 1 and raw == before and measured["completed"] is False


@pytest.mark.parametrize(
    "fault",
    [
        "unmatched",
        "next_operation",
        "next_error",
        "apply_phase",
        "write_count",
        "missing_confirm",
        "unknown_peer",
        "authorized_other",
        "null_request",
        "protocol",
        "interposed",
        "no_policy",
    ],
)
def test_false_diagnostic_near_misses_are_not_ignored(
    policy: BrokerLogPolicy, fault: str
) -> None:
    measured, following = diagnostic(), audit()
    if fault == "next_operation":
        following["broker_operation"] = "copy_sync_attached_development_source"
    if fault == "next_error":
        following["broker_result_code"] = "response_transport_error"
    if fault == "unknown_peer":
        following["broker_peer_uid"] = None
    if fault == "authorized_other":
        following["broker_peer_uid"] = 0
    if fault == "null_request":
        following["broker_request_id"] = None
    if fault == "protocol":
        following["broker_protocol_version"] = True
    if fault == "apply_phase":
        measured["phases_seconds"] = {"docker_copy": 0.01}
    if fault == "write_count":
        measured["counts"] = {"transferred_payload_bytes": 500}
    if fault == "missing_confirm":
        measured["phases_seconds"] = {"source_scan_hash": 0.01}
    raw = review_log(measured, None if fault == "unmatched" else following)
    if fault == "interposed":
        raw = raw.replace(
            b"\n{",
            b"\n2026-11-23 20:21:48,750 INFO alembic.runtime.plugins "
            b"setup plugin alembic.autogenerate.schemas\n{",
        )
    with pytest.raises(RecoveryRefused):
        validate_drain_log(raw, None if fault == "no_policy" else policy)


def denial() -> dict[str, object]:
    return {
        "event": "broker request completed",
        "broker_operation": None,
        "broker_request_id": None,
        "broker_protocol_version": None,
        "broker_peer_uid": 0,
        "broker_peer_gid": 0,
        "broker_peer_pid": 759,
        "broker_elapsed_seconds": 0.0001,
        "broker_result_code": "response_transport_error",
    }


def test_actual_unauthorized_predecode_reply_loss_remains_a_transport_error(
    policy: BrokerLogPolicy,
) -> None:
    raw = json.dumps(denial()).encode()
    result = validate_drain_log(raw, policy)
    assert result["predecode_denial_reply_losses"] == 1 and result["audits"] == 1
    assert json.loads(raw)["broker_result_code"] == "response_transport_error"


@pytest.mark.parametrize(
    "fault",
    [
        "operation",
        "request",
        "protocol",
        "authorized",
        "unknown",
        "gid",
        "pid",
        "missing",
        "no_policy",
        "wrong_policy_uid",
        "wrong_policy_source",
    ],
)
def test_transport_loss_only_classifies_exact_proven_predecode_denial(
    policy: BrokerLogPolicy, fault: str
) -> None:
    entry = denial()
    if fault == "operation":
        entry["broker_operation"] = "provision_managed_container"
    if fault == "request":
        entry["broker_request_id"] = str(uuid4())
    if fault == "protocol":
        entry["broker_protocol_version"] = 1
    if fault == "authorized":
        entry["broker_peer_uid"] = 10001
    if fault == "unknown":
        entry["broker_peer_uid"] = None
    if fault == "gid":
        entry["broker_peer_gid"] = 10001
    if fault == "pid":
        entry["broker_peer_pid"] = 0
    if fault == "missing":
        del entry["broker_operation"]
    if fault == "wrong_policy_uid":
        policy = policy.model_copy(update={"dedicated_uid": 0})
    if fault == "wrong_policy_source":
        policy = policy.model_copy(update={"source_digests": {}})
    with pytest.raises(RecoveryRefused):
        validate_drain_log(
            json.dumps(entry).encode(), None if fault == "no_policy" else policy
        )


@pytest.mark.parametrize(
    "fault",
    [None, "parsed_transport", "wrong_next", "wrong_entrypoint", "wrong_passwd"],
)
def test_helper_derives_actual_policy_before_stopping_services(
    recovery_case: Case, monkeypatch: pytest.MonkeyPatch, fault: str | None
) -> None:
    case = recovery_case
    sdk = ConsumerEngine(case.root)
    sdk.records = {
        "c" * 64: sdk.record("c" * 64, "web", running=True),
        "d" * 64: sdk.record("d" * 64, "broker", running=True),
    }
    following = audit()
    if fault == "wrong_next":
        following["broker_operation"] = "provision_managed_container"
    raw = review_log(diagnostic(), following) + b"\n" + json.dumps(denial()).encode()
    if fault == "parsed_transport":
        entry = denial()
        entry["broker_operation"] = "provision_managed_container"
        raw = json.dumps(entry).encode()
    if fault == "wrong_entrypoint":
        sdk.records["d" * 64]["Config"]["Entrypoint"] = ["other-broker"]
    if fault == "wrong_passwd":
        actual = sdk.get_archive

        def changed(cid: str, path: str) -> tuple[list[bytes], dict[str, object]]:
            if path == "/etc/passwd":
                return actual(cid, "/etc/group")
            return actual(cid, path)

        monkeypatch.setattr(sdk, "get_archive", changed)
    monkeypatch.setattr(sdk, "logs", lambda *_a, **_kw: raw)
    plan = InstallationPlan(
        installation="owned-qa",
        bundle_root=str(case.root),
        compose_file=str(case.root / "compose.yml"),
        engine_id="separate-synthetic-engine",
        state_volume="qa-state",
        web_container_id="c" * 64,
        broker_container_id="d" * 64,
        candidate_package_digest=case.seal.candidate_package_digest,
        review=case.review,
    )
    if fault is None:
        seal = drain(plan, sdk)
        assert (
            seal.broker_log_policy is not None
            and seal.broker_log_policy["dedicated_uid"] == 10001
        )
        assert sdk.stops == ["c" * 64, "d" * 64]
    else:
        with pytest.raises((RecoveryRefused, ValueError)):
            drain(plan, sdk)
        assert sdk.stops == []
