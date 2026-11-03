"""Independent processes use real parent/child admission and fake effects only."""

import json
import select as io_select
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

import pytest
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application import create_kodi_registration as registration_module
from kodi_lab.application.create_kodi_provisioning import CreateKodiProvisioningService
from kodi_lab.application.create_kodi_registration import CreateKodiRegistrationService
from kodi_lab.application.readiness_tasks import ReadinessTaskRegistry
from kodi_lab.application.runtime_installations import RuntimeInstallationService
from kodi_lab.application.runtime_lifecycle import ManagedRuntimeLifecycleService
from kodi_lab.application.runtime_status import ManagedRuntimeStatusService
from kodi_lab.domain.create_kodi import CreateCheckpoint, CreateTransitionError
from kodi_lab.runtime.install import bootstrap_recipe_instance
from kodi_lab.runtime.recipes import ReviewedRecipe
from kodi_lab.storage.create_kodi import CreateKodiRepository
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from tests.application.test_create_kodi_installation import factory as factory
from tests.application.test_create_kodi_installation import schema as schema
from tests.create_kodi_provisioning_support import Broker, Environment


def _worker() -> None:
    url, action, operation, version, log = sys.argv[1:]
    sessions = create_session_factory(create_database_engine(url))
    repo = CreateKodiRepository(sessions)
    broker = Broker(sessions, repo)
    broker.parent, broker.dispatch_log = UUID(operation), Path(log)
    installer = RuntimeInstallationService(sessions, broker)
    readiness = ReadinessTaskRegistry()
    lifecycle = ManagedRuntimeLifecycleService(
        sessions,
        broker,
        readiness,
        ManagedRuntimeStatusService(sessions, broker, readiness),
    )

    def bootstrap(
        factory: sessionmaker[Session], reviewed: ReviewedRecipe, revision: UUID
    ) -> dict[str, str]:
        op = repo.get(UUID(operation))
        assert op and op.checkpoint is CreateCheckpoint.DISPATCHED
        with Path(log).open("a") as file:
            file.write(str(op.child_id) + "\n")
        return bootstrap_recipe_instance(factory, reviewed, revision)

    print("ready", flush=True)
    try:
        assert sys.stdin.readline() == "go\n"
        with patch.object(registration_module, "bootstrap_recipe_instance", bootstrap):
            if action == "register":
                op = CreateKodiRegistrationService(sessions, repo, installer).register(
                    UUID(operation), version=int(version)
                )
            else:
                op = CreateKodiProvisioningService(
                    repo, installer, lifecycle, broker.managed_container_inventory
                ).provision(UUID(operation), version=int(version))
            outcome = str(op.checkpoint)
    except CreateTransitionError:
        outcome = "stale"
    finally:
        readiness.close()
    print(json.dumps(outcome), flush=True)


@pytest.mark.parametrize("action", ["register", "provision"])
def test_processes_never_establish_competing_mutations(
    factory: sessionmaker[Session],
    tmp_path: Path,
    action: str,
) -> None:
    env = Environment(factory)
    if action == "provision":
        env.registered()
        env.op = env.repo.plan(env.op.id, version=env.op.version)
    with factory() as session:
        url = str(session.get_bind().engine.url)
    log = tmp_path / "dispatch.log"
    workers: list[subprocess.Popen[str]] = []
    try:
        for _ in range(2):
            workers.append(
                subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        "from tests.application.test_create_kodi_processes "
                        "import _worker; _worker()",
                        url,
                        action,
                        str(env.op.id),
                        str(env.op.version),
                        str(log),
                    ],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            )
        for worker in workers:
            assert worker.stdout and worker.stdin
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
        assert log.exists() and len(log.read_text().splitlines()) == 1
        final = env.repo.get(env.op.id)
        assert final and final.checkpoint is CreateCheckpoint.CONFIRMED
        if action == "provision":
            assert final.provision_operation_id == env.op.provision_operation_id
    finally:
        for worker in workers:
            if worker.poll() is None:
                worker.kill()
            worker.communicate(timeout=5)
        env.close()
