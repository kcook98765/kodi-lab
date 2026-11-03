"""Independent Start runners share SQLite admission, never proof or in-memory locks."""

import json
import select as io_select
import subprocess
import sys
from pathlib import Path
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.create_kodi_start import CreateKodiStartService
from kodi_lab.application.readiness_tasks import ReadinessTaskRegistry
from kodi_lab.application.runtime_installations import RuntimeInstallationService
from kodi_lab.application.runtime_lifecycle import ManagedRuntimeLifecycleService
from kodi_lab.application.runtime_status import ManagedRuntimeStatusService
from kodi_lab.broker.docker_inventory import (
    ContainerHealthState,
    ContainerRuntimeState,
    ManagedContainerRecord,
)
from kodi_lab.domain.create_kodi import CreateCheckpoint, CreateTransitionError
from kodi_lab.storage.create_kodi import CreateKodiRepository
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.storage.models import ManagedStartAuthorizationRow
from tests.application.test_create_kodi_installation import factory as factory
from tests.application.test_create_kodi_installation import schema as schema
from tests.application.test_managed_start import NativeReady
from tests.create_kodi_start_support import Broker, Environment


def _worker() -> None:
    url, root, version, log = sys.argv[1:]
    sessions = create_session_factory(create_database_engine(url))
    repo = CreateKodiRepository(sessions)
    op = repo.get(UUID(root))
    assert op
    broker = Broker(
        sessions,
        ManagedContainerRecord(
            str(op.target_id),
            str(op.container_id),
            str(op.image_id),
            ContainerRuntimeState.STOPPED,
            ContainerHealthState.HEALTHY,
            str(op.runtime_revision_id),
        ),
    )
    broker.dispatch_log = Path(log)
    readiness = ReadinessTaskRegistry()
    lifecycle = ManagedRuntimeLifecycleService(
        sessions,
        broker,
        readiness,
        ManagedRuntimeStatusService(sessions, broker, readiness),
        coordinator_factory=lambda *_: NativeReady(),
    )
    service = CreateKodiStartService(
        repo,
        RuntimeInstallationService(sessions, broker),
        lifecycle,
        broker.managed_container_inventory,
    )
    print("ready", flush=True)
    try:
        assert sys.stdin.readline() == "go\n"
        result = str(service.start(UUID(root), version=int(version)).checkpoint)
    except CreateTransitionError:
        result = "stale"
    finally:
        readiness.close()
    print(json.dumps(result), flush=True)


def test_independent_start_runners_commit_one_authorization_and_dispatch(
    factory: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    env = Environment(factory)
    env.advance()
    op = env.repo.plan(env.op.id, version=env.op.version)
    with factory() as session:
        url = str(session.get_bind().engine.url)
    log = tmp_path / "start.log"
    workers: list[subprocess.Popen[str]] = []
    try:
        for _ in range(2):
            workers.append(
                subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        "from tests.application.test_create_kodi_start_processes "
                        "import _worker; _worker()",
                        url,
                        str(op.id),
                        str(op.version),
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
        results = []
        for worker in workers:
            stdout, stderr = worker.communicate(timeout=10)
            assert worker.returncode == 0, stderr
            results.append(json.loads(stdout))
        assert "confirmed" in results
        assert len(log.read_text().splitlines()) == 1
        result = env.repo.get(op.id)
        assert result and result.checkpoint is CreateCheckpoint.CONFIRMED
        assert result.start_operation_id == op.start_operation_id
        with factory() as session:
            auth = session.get(ManagedStartAuthorizationRow, str(op.start_operation_id))
            assert auth and auth.consumed_at
        assert env.start.start(result.id, version=result.version) == result
        assert env.broker.calls == 0
    finally:
        for worker in workers:
            if worker.poll() is None:
                worker.kill()
            worker.communicate(timeout=5)
        env.close()
