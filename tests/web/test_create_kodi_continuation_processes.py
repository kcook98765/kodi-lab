"""Independent authenticated HTTP workers share the original SQLite claims."""

import json
import select as io_select
import subprocess
import sys
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.config import Settings
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from tests.application.test_create_kodi_installation import factory as factory
from tests.application.test_create_kodi_installation import schema as schema
from tests.create_kodi_api_support import WebEnvironment
from tests.web.test_create_kodi_continuation import Web, accept, reach
from tests.web.test_create_kodi_continuation import web as web


def _worker() -> None:
    url, data_root, parent, log = sys.argv[1:]
    engine = create_database_engine(url)
    env = WebEnvironment(
        create_session_factory(engine), Path(data_root), operation_id=UUID(parent)
    )
    env.broker.mutation_log = Path(log)
    try:
        with env.client() as client:
            print("ready", flush=True)
            request = json.loads(sys.stdin.readline())
            client.cookies.update(request["cookies"])
            response = client.post(
                request["path"], json=request["body"], headers=request["headers"]
            )
            print(
                json.dumps({"status": response.status_code, "body": response.json()}),
                flush=True,
            )
    finally:
        engine.dispose()


@pytest.mark.parametrize("stage", ["installation", "provisioning", "startup"])
def test_independent_authenticated_continuations_dispatch_original_child_once(
    web: Web, factory: sessionmaker[Session], tmp_path: Path, stage: str
) -> None:
    env, client, headers = web
    accept(web)
    status = reach(web, stage, "planned")
    planned = env.repo.get(UUID(status["operation_id"]))
    assert planned and planned.child_id
    with factory() as session:
        url = str(session.get_bind().engine.url)
    log = tmp_path / "dispatch.jsonl"
    workers: list[subprocess.Popen[str]] = []
    try:
        for _ in range(2):
            workers.append(
                subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        "from tests.web.test_create_kodi_continuation_processes "
                        "import _worker; _worker()",
                        url,
                        str(Settings().data_root),
                        str(planned.id),
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
            ready, _, _ = io_select.select([worker.stdout], [], [], 20)
            assert ready and worker.stdout.readline() == "ready\n"
        for worker in workers:
            assert worker.stdin
            worker.stdin.write(
                json.dumps(
                    {
                        "path": status["status_url"] + "/continue",
                        "body": {"version": status["version"]},
                        "headers": headers,
                        "cookies": dict(client.cookies),
                    }
                )
                + "\n"
            )
            worker.stdin.flush()
        responses = []
        for worker in workers:
            stdout, stderr = worker.communicate(timeout=25)
            assert worker.returncode == 0, stderr
            response = json.loads(stdout.strip())
            assert response["status"] in {200, 409}, response
            assert response["body"]["operation_id"] == str(planned.id)
            responses.append(response)
        assert any(r["status"] == 200 for r in responses)
        assert [json.loads(line) for line in log.read_text().splitlines()] == [
            [
                {
                    "installation": "installation",
                    "provisioning": "provision",
                    "startup": "start",
                }[stage],
                str(planned.child_id),
            ]
        ]
        result = env.repo.get(planned.id)
        assert result and result.checkpoint == "confirmed"
        assert result.child_id == planned.child_id
    finally:
        for worker in workers:
            if worker.poll() is None:
                worker.kill()
                worker.communicate(timeout=5)
