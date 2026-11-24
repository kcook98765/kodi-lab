from __future__ import annotations

import json

import pytest

from kodi_lab.application.snapshot_recovery_evidence import RecoveryRefused, canonical
from tests.snapshot_recovery_support import Case
from tests.snapshot_recovery_support import factory as factory
from tests.snapshot_recovery_support import recovery_case as recovery_case


def test_legacy_primary_chain_records_review_not_historical_admission(
    recovery_case: Case,
) -> None:
    proof = recovery_case.review.normalized()
    assert (
        proof["historical_admission"] == proof["historical_dispatch"] == "not_recorded"
    )
    assert len(proof["primary_digests"]) == 10
    assert "admission" not in proof["receipt"]
    assert (
        proof["removal"]["broker_request_id"] != proof["refusal"]["broker_request_id"]
    )


def test_web_stop_and_independent_profile_capture_are_not_forged_as_mcp(
    recovery_case: Case,
) -> None:
    review = recovery_case.review.model_copy(deep=True)
    original = review.normalized()
    receipt, selected, native = (
        original["receipt"],
        original["selection"],
        original["native_identity"],
    )
    review.native_stop_kind = "web_lifecycle_stop_v1"
    review.captures["web"] = (
        'INFO: 127.0.0.1:1 - "POST /login HTTP/1.1" 303 See Other\n'
        + review.captures["web"]
    )
    review.captures["native_stop"] = canonical(
        {
            "managed_instance_id": receipt["managed_instance_id"],
            "runtime_revision_id": selected["runtime_revision_id"],
            "container_state": "stopped",
            "process_state": "not_running",
        }
    )
    review.captures["native_identity"] = canonical(
        {
            "container_id": native["container_id"],
            "image_id": native["image_id"],
            "stopped": True,
            "snapshot_id": receipt["snapshot_id"],
            "labels": {
                "io.kodi-lab.instance-id": receipt["managed_instance_id"],
                "io.kodi-lab.runtime-revision-id": selected["runtime_revision_id"],
            },
        }
    )
    review.captures["broker"] = "\n".join(
        receipt["requested_at"] + " " + line
        for line in review.captures["broker"].splitlines()
    )
    result = review.normalized()
    assert result["native_stop_kind"] == "web_lifecycle_stop_v1"
    assert result["native_identity"] == native
    assert "stop_receipt" not in review.captures["native_stop"]
    assert result["historical_dispatch"] == "not_recorded"


@pytest.mark.parametrize(
    "extra",
    [
        '"POST /login HTTP/1.1" 200 OK',
        '"POST /api/instances/other/start HTTP/1.1" 200 OK',
        '"POST /login/extra HTTP/1.1" 303 See Other',
    ],
)
def test_harmless_login_exception_does_not_admit_unrelated_posts(
    recovery_case: Case, extra: str
) -> None:
    review = recovery_case.review.model_copy(deep=True)
    review.captures["web"] += "\n" + extra
    with pytest.raises(RecoveryRefused):
        review.normalized()


@pytest.mark.parametrize(
    "fault",
    [
        "missing",
        "second_post",
        "wrong_accepted",
        "wrong_old_cid",
        "wrong_revision",
        "missing_removal",
        "transport",
        "null_successor_only",
        "duplicate",
        "third_mutation",
        "reverse",
        "nonterminal",
        "wrong_code",
        "wrong_target",
        "wrong_peer",
    ],
)
def test_incomplete_or_contradictory_primary_chain_refuses(
    recovery_case: Case, fault: str
) -> None:
    review = recovery_case.review.model_copy(deep=True)
    events = [json.loads(line) for line in review.captures["broker"].splitlines()]
    if fault == "missing":
        del review.captures["native_stop"]
    elif fault == "second_post":
        review.captures["web"] += '\n"POST /else HTTP/1.1" 202 Accepted'
    elif fault == "wrong_accepted":
        doc = json.loads(review.captures["accepted"])
        doc["operation"]["operation_id"] = "00000000-0000-4000-8000-000000000001"
        review.captures["accepted"] = canonical(doc)
    elif fault == "wrong_old_cid":
        doc = json.loads(review.captures["receipt"])
        doc["old_container_id"] = "0" * 64
        review.captures["receipt"] = canonical(doc)
    elif fault == "nonterminal":
        review.captures["before"] = canonical(
            {
                "nonterminal_operations": {"restore": 1},
                "active_leases_or_reservations": {},
            }
        )
    elif fault == "wrong_revision":
        events[0]["broker_runtime_revision_id"] = "other"
    elif fault in {"missing_removal", "null_successor_only"}:
        events = events[1:]
    elif fault == "transport":
        events[0]["broker_result_code"] = "response_transport_error"
    elif fault == "duplicate":
        events.append(events[1])
    elif fault == "third_mutation":
        events.append({**events[1], "broker_request_id": "third"})
    elif fault == "reverse":
        events.reverse()
    elif fault == "wrong_code":
        events[1]["broker_result_code"] = "internal_error"
    elif fault == "wrong_target":
        events[1]["broker_managed_instance_id"] = "other"
    elif fault == "wrong_peer":
        events[1]["broker_peer_uid"] = 0
    review.captures["broker"] = "\n".join(canonical(event) for event in events)
    with pytest.raises(RecoveryRefused):
        review.normalized()
    assert recovery_case.provider.calls == []
