"""Web resource administration is scoped, CSRF protected and does not wake Kodi."""

import re
import subprocess
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.storage.models import TargetRow
from tests.web.conftest import INSTANCE_ID, ORIGIN, FakeBroker, login


def test_resource_policy_enrollment_and_hold_require_web_csrf(
    client: TestClient,
    broker: FakeBroker,
) -> None:
    assert client.get("/managed-resources", follow_redirects=False).status_code == 303
    csrf = login(client)
    headers = {**ORIGIN, "X-Kodi-Lab-CSRF": csrf}
    before = client.get("/api/managed-resources").json()
    assert before["policy"] == {"max_running": 4, "idle_minutes": 60}
    assert before["instances"][0]["status"]["enrolled"] is False
    policy = {"max_running": 2, "idle_minutes": 10}
    assert client.post("/api/managed-resources/policy", json=policy).status_code == 403
    assert (
        client.post(
            "/api/managed-resources/policy",
            json=policy,
            headers={**headers, "Origin": "http://other.invalid"},
        ).status_code
        == 403
    )
    assert client.post(
        "/api/managed-resources/policy", json=policy, headers=headers
    ).json()["saved"]
    enrolled = client.post(
        f"/api/managed-resources/{INSTANCE_ID}/enrollment",
        json={"enrolled": True},
        headers=headers,
    )
    assert enrolled.status_code == 200 and enrolled.json()["enrolled"] is True
    held = client.post(
        f"/api/managed-resources/{INSTANCE_ID}/keep-awake",
        json={"minutes": 15},
        headers=headers,
    )
    assert held.status_code == 200 and held.json()["keep_awake_until"] is not None
    assert (
        client.post(
            "/api/managed-resources/policy",
            json={"max_running": 2, "idle_minutes": None},
            headers=headers,
        ).status_code
        == 200
    )
    assert client.get("/api/managed-resources").json()["policy"]["idle_minutes"] is None
    assert not any(name in {"start", "stop"} for name, _ in broker.calls)
    assert "Maximum running managed Kodi" in client.get("/managed-resources").text


def test_status_polling_does_not_enroll_change_activity_or_wake(
    client: TestClient,
    broker: FakeBroker,
    database: sessionmaker[Session],
) -> None:
    login(client)
    statements: list[str] = []

    def record(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: object,
    ) -> None:
        statements.append(statement)

    engine = database.kw["bind"]
    event.listen(engine, "before_cursor_execute", record)
    try:
        original = client.get("/api/managed-resources").json()["instances"][0]["status"]
        for _ in range(3):
            assert (
                client.get("/api/managed-resources").json()["instances"][0]["status"]
                == original
            )
        assert original["last_activity_at"] is None and not original["enrolled"]
        assert not any(name in {"start", "stop"} for name, _ in broker.calls)
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert not [
        statement
        for statement in statements
        if re.match(
            r"\s*(INSERT|UPDATE|DELETE|REPLACE|CREATE|DROP|ALTER)\b", statement, re.I
        )
    ]


def test_resource_controls_reject_external_registration_before_dispatch(
    client: TestClient,
    broker: FakeBroker,
    database: sessionmaker[Session],
) -> None:
    target_id = uuid4()
    with database.begin() as session:
        session.add(
            TargetRow(
                id=str(target_id),
                name="External resource exclusion fixture",
                target_type="external_development",
                kodi_version="21.3",
                availability="unavailable",
                health="warning",
                connection_state="disconnected",
            )
        )
    csrf = login(client)
    headers = {**ORIGIN, "X-Kodi-Lab-CSRF": csrf}
    for operation, body in (
        ("enrollment", {"enrolled": True}),
        ("keep-awake", {"minutes": 60}),
    ):
        response = client.post(
            f"/api/managed-resources/{target_id}/{operation}",
            json=body,
            headers=headers,
        )
        assert (
            response.status_code == 404
            and response.json()["error"]["code"] == "managed_target_required"
        )
    assert not any(name in {"start", "stop"} for name, _ in broker.calls)


def test_resource_limits_reject_coerced_or_out_of_bound_values(
    client: TestClient,
) -> None:
    csrf = login(client)
    headers = {**ORIGIN, "X-Kodi-Lab-CSRF": csrf}
    for body in (
        {"max_running": 0, "idle_minutes": 60},
        {"max_running": 4, "idle_minutes": 0},
        {"max_running": "4", "idle_minutes": 60},
    ):
        assert (
            client.post(
                "/api/managed-resources/policy", json=body, headers=headers
            ).status_code
            == 422
        )
    assert (
        client.post(
            f"/api/managed-resources/{INSTANCE_ID}/keep-awake",
            json={"minutes": 241},
            headers=headers,
        ).status_code
        == 422
    )


def test_resource_browser_polls_passively_and_saves_once() -> None:
    root = Path(__file__).resolve().parents[2]
    subprocess.run(
        [
            "node",
            str(Path(__file__).with_name("managed_resources_browser_harness.js")),
            str(root / "src/kodi_lab/web/static/managed-resources.js"),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
