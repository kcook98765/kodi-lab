"""Independent continuations share real SQLite child and authority claims."""

import json
import select as io_select
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application import create_kodi_registration as registration_module
from kodi_lab.domain.create_kodi import (
    CreateCheckpoint,
    CreateStage,
    CreateTransitionError,
)
from kodi_lab.runtime.install import bootstrap_recipe_instance
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.storage.models import ManagedStartAuthorizationRow
from tests.application.test_create_kodi_installation import factory as factory
from tests.application.test_create_kodi_installation import schema as schema
from tests.application.test_create_kodi_runner import reach
from tests.create_kodi_runner_support import Environment


def _worker() -> None:
    url, parent, version, log = sys.argv[1:]
    engine = create_database_engine(url)
    env = Environment(create_session_factory(engine), operation_id=UUID(parent))
    env.broker.mutation_log = Path(log)

    def registration(*args: Any, **kwargs: Any) -> Any:
        env.broker._record("registration")
        return bootstrap_recipe_instance(*args, **kwargs)

    registration_patch = patch.object(
        registration_module, "bootstrap_recipe_instance", registration
    )
    registration_patch.start()
    print("ready", flush=True)
    try:
        assert sys.stdin.readline() == "go\n"
        try:
            result = env.runner.continue_once(env.op.id, version=int(version))
            state = str(result.checkpoint)
        except CreateTransitionError:
            state = "stale"
        print(json.dumps(state), flush=True)
    finally:
        registration_patch.stop()
        env.close()
        engine.dispose()


@pytest.mark.parametrize(
    "stage",
    [
        CreateStage.INSTALLATION,
        CreateStage.REGISTRATION,
        CreateStage.PROVISIONING,
        CreateStage.STARTUP,
    ],
)
def test_independent_runners_dispatch_original_child_once(
    factory: sessionmaker[Session], tmp_path: Path, stage: CreateStage
) -> None:
    env = Environment(factory)
    workers: list[subprocess.Popen[str]] = []
    try:
        reach(env, stage, CreateCheckpoint.PLANNED)
        planned = env.op
        with factory() as session:
            url = str(session.get_bind().engine.url)
        log = tmp_path / "dispatch.jsonl"
        for _ in range(2):
            workers.append(
                subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        "from tests.application.test_create_kodi_runner_processes "
                        "import _worker; _worker()",
                        url,
                        str(planned.id),
                        str(planned.version),
                        str(log),
                    ],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            )
        for worker in workers:
            assert worker.stdout
            ready, _, _ = io_select.select([worker.stdout], [], [], 15)
            assert ready and worker.stdout.readline() == "ready\n"
        for worker in workers:
            assert worker.stdin
            worker.stdin.write("go\n")
            worker.stdin.flush()
        for worker in workers:
            stdout, stderr = worker.communicate(timeout=20)
            assert worker.returncode == 0, stderr
            assert json.loads(stdout.strip()) in {"confirmed", "stale", "dispatched"}
        entries = [json.loads(line) for line in log.read_text().splitlines()]
        assert entries == [
            [
                {
                    CreateStage.INSTALLATION: "installation",
                    CreateStage.REGISTRATION: "registration",
                    CreateStage.PROVISIONING: "provision",
                    CreateStage.STARTUP: "start",
                }[stage],
                str(planned.child_id),
            ]
        ]
        result = env.repo.get(planned.id)
        assert result and result.checkpoint is CreateCheckpoint.CONFIRMED
        assert result.child_id == planned.child_id
        if stage is CreateStage.STARTUP:
            with factory() as session:
                proofs = session.scalars(select(ManagedStartAuthorizationRow)).all()
                assert len(proofs) == 1 and proofs[0].consumed_at
    finally:
        for worker in workers:
            if worker.poll() is None:
                worker.kill()
                worker.communicate(timeout=5)
        env.close()
