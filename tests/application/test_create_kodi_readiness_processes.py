"""Independent explicit native continuations share the durable parent version."""

import json
import select as io_select
import subprocess
import sys
from pathlib import Path
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.broker.docker_inventory import (
    ContainerHealthState,
    ContainerRuntimeState,
    ManagedContainerRecord,
)
from kodi_lab.domain.create_kodi import CreateCheckpoint, CreateTransitionError
from kodi_lab.storage.create_kodi import CreateKodiRepository
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from tests.application.test_create_kodi_installation import factory as factory
from tests.application.test_create_kodi_installation import schema as schema
from tests.application.test_create_kodi_readiness import Environment
from tests.create_kodi_readiness_support import Broker, Transport, service


def _worker() -> None:
    url, root, version, log = sys.argv[1:]
    engine = create_database_engine(url)
    sessions = create_session_factory(engine)
    repo = CreateKodiRepository(sessions)
    op = repo.get(UUID(root))
    assert op
    broker = Broker(
        sessions,
        ManagedContainerRecord(
            str(op.target_id),
            str(op.container_id),
            str(op.image_id),
            ContainerRuntimeState.RUNNING,
            ContainerHealthState.HEALTHY,
            str(op.runtime_revision_id),
        ),
    )
    transport = Transport()

    def record() -> None:
        with Path(log).open("a") as file:
            file.write(transport.methods[-1] + "\n")

    transport.on_request = record
    print("ready", flush=True)
    try:
        assert sys.stdin.readline() == "go\n"
        result = service(sessions, repo, broker, transport).inspect(
            UUID(root), version=int(version)
        )
        state = str(result.operation.checkpoint)
    except CreateTransitionError:
        state = "stale"
    finally:
        engine.dispose()
    print(json.dumps(state), flush=True)


def test_independent_continuations_commit_one_native_completion(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    env = Environment(factory)
    env.advance()
    original = env.op
    with factory() as session:
        url = str(session.get_bind().engine.url)
    log = tmp_path / "native.log"
    workers: list[subprocess.Popen[str]] = []
    try:
        for _ in range(2):
            workers.append(
                subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        "from tests.application.test_create_kodi_readiness_processes "
                        "import _worker; _worker()",
                        url,
                        str(original.id),
                        str(original.version),
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
            ready, _, _ = io_select.select([worker.stdout], [], [], 10)
            assert ready and worker.stdout.readline() == "ready\n"
        for worker in workers:
            assert worker.stdin
            worker.stdin.write("go\n")
            worker.stdin.flush()
        states = []
        for worker in workers:
            stdout, stderr = worker.communicate(timeout=15)
            assert worker.returncode == 0, stderr
            states.append(json.loads(stdout.strip()))
        assert sorted(states) == ["complete", "stale"]
        assert log.read_text().splitlines() == [
            "JSONRPC.Ping",
            "Application.GetProperties",
        ]
        completed = env.repo.get(original.id)
        assert completed and completed.checkpoint is CreateCheckpoint.COMPLETE
        assert (completed.provision_operation_id, completed.start_operation_id) == (
            original.provision_operation_id,
            original.start_operation_id,
        )
        assert env.started.broker.starts == 1 and env.started.broker.stops == 0
    finally:
        for worker in workers:
            if worker.poll() is None:
                worker.kill()
                worker.communicate(timeout=5)
