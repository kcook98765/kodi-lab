"""Independent authenticated HTTP processes share SQLite Create admission."""

import json
import select as io_select
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.auth.service import WebAuthenticationService
from kodi_lab.config import Settings
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.storage.models import CreateKodiOperationRow
from kodi_lab.web.app import create_app
from kodi_lab.web.create_kodi import BASE
from tests.web.conftest import ORIGIN, FakeBroker
from tests.web.test_create_kodi_api import approval
from tests.web.test_create_kodi_api import headers as headers
from tests.web.test_create_kodi_api import no_execution as no_execution
from tests.web.test_runtime_installations import Flow


def _worker() -> None:
    url, data_root, development_root = sys.argv[1:]
    engine = create_database_engine(url)
    sessions = create_session_factory(engine)
    broker = FakeBroker()
    flow = Flow()
    flow.attach(broker)

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("Create API must not dispatch native/installation work")

    dynamic: Any = broker
    dynamic.observe_managed_kodi_runtime = forbidden
    dynamic.launch_reviewed_runtime_installation = forbidden
    settings = Settings(
        data_root=Path(data_root),
        development_root=Path(development_root),
        external_target_master_key=SecretStr(
            "d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3c"
        ),
    )
    app = create_app(
        settings=settings,
        session_factory=sessions,
        broker=broker,
        auth=WebAuthenticationService(sessions),
    )
    try:
        with TestClient(app, base_url="https://127.0.0.1") as client:
            print("ready", flush=True)
            request = json.loads(sys.stdin.readline())
            client.cookies.update(request["cookies"])
            response = client.post(
                BASE, json=request["body"], headers=request["headers"]
            )
            assert broker.calls == [] and flow.launches == []
            print(
                json.dumps({"status": response.status_code, "body": response.json()}),
                flush=True,
            )
    finally:
        engine.dispose()


@pytest.mark.parametrize("same_request", [True, False])
def test_independent_authenticated_requests_replay_or_conflict_without_execution(
    client: TestClient,
    database: sessionmaker[Session],
    data_root: Path,
    tmp_path: Path,
    headers: dict[str, str],
    same_request: bool,
) -> None:
    body = approval()
    with database() as session:
        url = str(session.get_bind().engine.url)
    workers: list[subprocess.Popen[str]] = []
    try:
        for _ in range(2):
            workers.append(
                subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        "from tests.web.test_create_kodi_api_processes "
                        "import _worker; _worker()",
                        url,
                        str(data_root),
                        str(tmp_path / "development-root"),
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
        for index, worker in enumerate(workers):
            assert worker.stdin
            packet = {
                "body": body if same_request or index == 0 else approval(),
                "headers": {**ORIGIN, **headers},
                "cookies": dict(client.cookies),
            }
            worker.stdin.write(json.dumps(packet) + "\n")
            worker.stdin.flush()
        responses = []
        for worker in workers:
            stdout, stderr = worker.communicate(timeout=20)
            assert worker.returncode == 0, stderr
            responses.append(json.loads(stdout.strip()))
        assert sorted(response["status"] for response in responses) == (
            [200, 202] if same_request else [202, 409]
        )
        ids = {response["body"]["operation_id"] for response in responses}
        assert len(ids) == 1
        with database() as session:
            rows = session.scalars(select(CreateKodiOperationRow)).all()
            assert len(rows) == 1 and rows[0].id in ids
            assert rows[0].checkpoint == "accepted" and rows[0].version == 0
            assert rows[0].installation_operation_id is None
            assert rows[0].provision_operation_id is rows[0].start_operation_id is None
    finally:
        for worker in workers:
            if worker.poll() is None:
                worker.kill()
                worker.communicate(timeout=5)
