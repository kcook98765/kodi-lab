"""Opt-in Chromium proof over TestClient; every Kodi transport is a fake.

Run with --browser-module pointing to a locally installed playwright-core package.
No listener, Docker, Kodi, installation download or production state is used.
"""

import argparse
import base64
import json
import os
import subprocess
import tempfile
from dataclasses import replace
from pathlib import Path
from threading import Timer
from typing import Any
from unittest.mock import patch
from uuid import UUID

from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from sqlalchemy import select

from kodi_lab.application.managed_bridge_trust import ManagedBridgeTrustService
from kodi_lab.auth import passwords as password_module
from kodi_lab.auth.service import WebAuthenticationService
from kodi_lab.broker.docker_inventory import ContainerRuntimeState
from kodi_lab.broker.novnc import ViewBinding
from kodi_lab.config import Settings
from kodi_lab.domain import InstanceRuntimeState
from kodi_lab.host_bootstrap import generate_master_key
from kodi_lab.runtime.recipes import load_installation_recipe
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.storage.models import (
    AgentWorkspaceRow,
    CreateKodiOperationRow,
    DevelopmentSessionRow,
    ManagedInstanceRow,
)
from tests.application.test_create_kodi_installation import finish
from tests.create_kodi_api_support import WebEnvironment
from tests.create_kodi_readiness_support import Transport
from tests.efficiency import CurrentSchemaTemplate
from tests.runtime.test_recipe_installation import _ready
from tests.web.conftest import PASSWORD
from tests.web.view_attempt_fixture import view_attempt


class NativeTransport(Transport):
    version = (21, 3)

    def post(self, **kwargs: Any) -> bytes:
        raw = super().post(**kwargs)
        if self.mode == "invalid":
            return raw
        value = json.loads(raw)
        if json.loads(kwargs["body"])["method"] == "Application.GetProperties":
            value["result"]["version"].update(
                major=self.version[0], minor=self.version[1]
            )
        return json.dumps(value).encode()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--browser-module", required=True)
    parser.add_argument("--browser-executable", required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--empty-navigation", action="store_true")
    args = parser.parse_args()
    args.evidence.mkdir(parents=True, exist_ok=True)

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("Create must not prepare Bridge")

    with (
        tempfile.TemporaryDirectory(prefix="kodi-create-browser-") as directory,
        patch.dict(os.environ),
        patch.object(
            password_module,
            "PasswordHasher",
            lambda **kwargs: PasswordHasher(
                time_cost=1,
                memory_cost=8,
                parallelism=1,
                hash_len=16,
                salt_len=8,
                **kwargs,
            ),
        ),
        patch.object(ManagedBridgeTrustService, "prepare", forbidden),
        patch.object(ManagedBridgeTrustService, "prepare_background", forbidden),
    ):
        scratch = Path(directory)
        env: WebEnvironment | None = None
        client: TestClient | None = None
        engine: Any = None
        case = 0
        evidence: list[dict[str, Any]] = []

        def binding(_path: Path, target: UUID) -> ViewBinding:
            assert env
            record = next(
                r for r in env.broker.records if r.managed_instance_id == str(target)
            )
            assert record.runtime_state == "running"
            return ViewBinding(
                record.container_id, record.image_id, "fake-start-incarnation", ()
            )

        def reset(generation: str) -> None:
            nonlocal env, client, engine, case
            if client:
                client.__exit__(None, None, None)
                engine.dispose()
            case += 1
            folder = scratch / str(case)
            folder.mkdir()
            state = folder / "state"
            state.mkdir(mode=0o700)
            generate_master_key(state)
            os.environ["KODI_LAB_DATA_ROOT"] = str(state)
            os.environ["KODI_LAB_REPOSITORY_DATA_ROOT"] = str(folder / "repository")
            CurrentSchemaTemplate(folder).clone(folder / "case.db")
            engine = create_database_engine(f"sqlite:///{folder / 'case.db'}")
            sessions = create_session_factory(engine)
            WebAuthenticationService(sessions).set_password(PASSWORD)
            env = WebEnvironment(sessions, state)
            env.transport = NativeTransport()
            version = load_installation_recipe(generation).recipe.kodi_version.split(
                "."
            )
            env.transport.version = (int(version[0]), int(version[1]))
            if generation != "omega":
                _ready(sessions, generation=generation)
            client = TestClient(env.app, base_url="https://127.0.0.1")
            client.__enter__()

        process = subprocess.Popen(
            [
                "node",
                str(Path(__file__).with_name("create_kodi_browser.cjs")),
                args.browser_module,
                args.browser_executable,
                str(args.evidence),
                "empty-navigation" if args.empty_navigation else "full",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        watchdog = Timer(180, process.kill)
        watchdog.daemon = True
        watchdog.start()
        try:
            with patch("kodi_lab.web.novnc_optin.resolve_binding", binding):
                assert process.stdout and process.stdin
                for line in process.stdout:
                    request = json.loads(line)
                    result: Any
                    if request.get("action") == "reset":
                        reset(request["generation"])
                        result = {"password": PASSWORD}
                    elif request.get("action") == "summary":
                        assert env
                        with env.repo._sessions() as db:
                            rows = db.scalars(select(CreateKodiOperationRow)).all()
                            assert len(rows) <= 1
                            assert db.scalars(select(AgentWorkspaceRow)).all() == []
                            assert db.scalars(select(DevelopmentSessionRow)).all() == []
                            result = {
                                "parents": len(rows),
                                "launches": len(env.broker.launches),
                                "provisions": env.broker.provisions,
                                "starts": env.broker.starts,
                                "stops": env.broker.stops,
                                "native": len(env.transport.methods),
                                "operation_id": rows[0].id if rows else None,
                                "checkpoint": rows[0].checkpoint if rows else None,
                            }
                    elif request.get("action"):
                        assert env
                        action = request["action"]
                        if action == "pending_install":
                            env.broker.mode = "pending"
                        elif action == "failed_install":
                            env.broker.mode = "failed"
                        elif action == "finish_install":
                            op = (
                                env.repo.get(env.broker.parent)
                                if env.broker.parent
                                else None
                            )
                            assert op and op.installation_operation_id
                            finish(env.repo._sessions, op.installation_operation_id)
                            env.broker.workers[str(op.installation_operation_id)] = (
                                "exited"
                            )
                            env.broker.operation_id = env.broker.generation = None
                        elif action == "native_pending":
                            env.transport.mode = "invalid"
                        elif action == "native_ready":
                            env.transport.mode = "ready"
                        elif action == "restart_app":
                            assert client and env.broker.parent
                            previous = env
                            client.__exit__(None, None, None)
                            env = WebEnvironment(
                                previous.repo._sessions,
                                Settings().data_root,
                                operation_id=previous.broker.parent,
                            )
                            for name in (
                                "records",
                                "workers",
                                "operation_id",
                                "generation",
                                "launches",
                                "provisions",
                                "starts",
                                "stops",
                            ):
                                setattr(
                                    env.broker, name, getattr(previous.broker, name)
                                )
                            native = NativeTransport()
                            assert isinstance(previous.transport, NativeTransport)
                            native.version = previous.transport.version
                            native.methods = list(previous.transport.methods)
                            native.mode = previous.transport.mode
                            env.transport = native
                            client = TestClient(env.app, base_url="https://127.0.0.1")
                            client.__enter__()
                        elif action == "uncertain_start":
                            env.broker.start_fault = "lost_response"
                        elif action == "manual_stop":
                            env.broker.records[0] = replace(
                                env.broker.records[0],
                                runtime_state=ContainerRuntimeState.STOPPED,
                            )
                            with env.repo._sessions.begin() as db:
                                row = db.get(
                                    ManagedInstanceRow,
                                    env.broker.records[0].managed_instance_id,
                                )
                                assert row
                                row.runtime_state = InstanceRuntimeState.STOPPED
                                row.runtime_state_revision += 2
                        elif action == "view_attempt":
                            pass
                        elif action == "done":
                            evidence.append(request["result"])
                        else:
                            raise AssertionError(action)
                        result = str(view_attempt()) if action == "view_attempt" else {}
                    else:
                        assert client and env
                        assert request["url"].startswith("https://127.0.0.1/")
                        # Browser cookie header is the only session authority.
                        client.cookies.clear()
                        response = client.request(
                            request["method"],
                            request["url"],
                            headers=request["headers"],
                            content=base64.b64decode(request["body"]),
                            follow_redirects=False,
                        )
                        if (
                            request["method"] == "POST"
                            and request["url"].endswith("/api/create-kodi/operations")
                            and response.status_code in {200, 202}
                        ):
                            env.broker.parent = UUID(response.json()["operation_id"])
                        result = {
                            "status": response.status_code,
                            "headers": dict(response.headers),
                            "body": base64.b64encode(response.content).decode(),
                        }
                    process.stdin.write(
                        json.dumps({"id": request["id"], "result": result}) + "\n"
                    )
                    process.stdin.flush()
            _, stderr = process.communicate(timeout=10)
            assert process.returncode == 0, stderr
            (args.evidence / "browser-results.json").write_text(
                json.dumps(evidence, indent=2) + "\n"
            )
            print(json.dumps(evidence, indent=2))
        finally:
            watchdog.cancel()
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)
            if client:
                client.__exit__(None, None, None)
                engine.dispose()


if __name__ == "__main__":
    main()
