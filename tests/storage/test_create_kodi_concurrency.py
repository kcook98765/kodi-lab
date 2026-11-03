"""Spawned interpreters prove database admission, not Python-lock deduplication."""

import json
import select as io_select
import subprocess
import sys
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from kodi_lab.domain.create_kodi import (
    CreateCheckpoint,
    CreateConflict,
    CreateTransitionError,
)
from kodi_lab.storage.models import CreateKodiOperationRow
from tests.create_kodi_support import accept, database, service


def _worker() -> None:
    url, request, generation, operation_id, version = sys.argv[1:]
    app = service(url)
    print("ready", flush=True)
    try:
        assert sys.stdin.readline() == "go\n", "test start signal missing"
        if operation_id:
            op = app.repository.claim_dispatch(
                UUID(operation_id), version=int(version)
            ).operation
        else:
            op = accept(app, UUID(request), generation)
        result = ("accepted", str(op.id))
    except CreateConflict as exc:
        result = (str(exc), str(exc.operation_id))
    except CreateTransitionError as exc:
        result = (str(exc), operation_id)
    print(json.dumps(result), flush=True)


def _race(
    url: str,
    inputs: list[tuple[str, str]],
    operation_id: str | None = None,
    version: int = 0,
) -> list[tuple[str, str]]:
    processes: list[subprocess.Popen[str]] = []
    try:
        for request, generation in inputs:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    "from tests.storage.test_create_kodi_concurrency import _worker; "
                    "_worker()",
                    url,
                    request,
                    generation,
                    operation_id or "",
                    str(version),
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            processes.append(process)
        for process in processes:
            assert process.stdout and process.stdin
            readable, _, _ = io_select.select([process.stdout], [], [], 10)
            assert readable, "child did not initialize"
            assert process.stdout.readline() == "ready\n"
        # Release both independently initialized interpreters before waiting.
        for process in processes:
            assert process.stdin
            process.stdin.write("go\n")
            process.stdin.flush()
        results: list[tuple[str, str]] = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=10)
            assert process.returncode == 0, stderr
            status, identity = json.loads(stdout)
            results.append((status, identity))
        return results
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)


@pytest.mark.parametrize("mode", ["double_click", "two_tabs", "conflicting_input"])
def test_independent_process_acceptance_is_atomic(tmp_path: Path, mode: str) -> None:
    url = database(tmp_path / "db")
    request = str(uuid4())
    inputs = [
        (request, "omega"),
        (
            str(uuid4()) if mode == "two_tabs" else request,
            "nexus" if mode == "conflicting_input" else "omega",
        ),
    ]
    results = _race(url, inputs)
    assert len({result[1] for result in results}) == 1
    statuses = sorted(result[0] for result in results)
    expected = {
        "double_click": ["accepted", "accepted"],
        "two_tabs": ["accepted", "canonical_create_exists"],
        "conflicting_input": ["accepted", "request_inputs_conflict"],
    }
    assert statuses == expected[mode]
    app = service(url)
    with app.repository._sessions() as session:
        assert (
            session.scalar(select(func.count()).select_from(CreateKodiOperationRow))
            == 1
        )
    op = app.get(UUID(results[0][1]))
    assert op and op.checkpoint is CreateCheckpoint.ACCEPTED
    assert op.installation_operation_id is None


def test_independent_process_dispatch_claim_has_one_winner(tmp_path: Path) -> None:
    url = database(tmp_path / "db")
    app = service(url)
    op = accept(app)
    op = app.repository.plan(op.id, version=op.version)
    planned_child = op.child_id
    results = _race(url, [(str(uuid4()), "omega")] * 2, str(op.id), op.version)
    assert sorted(result[0] for result in results) == ["accepted", "stale_version"]
    dispatched = service(url).get(op.id)
    assert dispatched and dispatched.checkpoint is CreateCheckpoint.DISPATCHED
    assert dispatched.child_id == planned_child
    with pytest.raises(CreateTransitionError, match="invalid_checkpoint"):
        service(url).repository.claim_dispatch(
            dispatched.id, version=dispatched.version
        )
