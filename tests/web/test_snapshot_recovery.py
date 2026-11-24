from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from kodi_lab.application.snapshot_recovery_evidence import canonical
from kodi_lab.config import Settings
from kodi_lab.storage.models import SnapshotRecoveryEpochRow
from kodi_lab.web.app import create_app
from tests.snapshot_recovery_support import Case
from tests.snapshot_recovery_support import factory as factory
from tests.snapshot_recovery_support import recovery_case as recovery_case


def test_lan_http_server_uuid_and_status_only_reply_loss() -> None:
    result = subprocess.run(
        ["node", str(Path(__file__).with_name("snapshot_recovery_browser.cjs"))],
        cwd=Path(__file__).parents[2],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_restricted_existing_web_keeps_auth_csrf_origin_and_excludes_normal_routes(
    recovery_case: Case,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = recovery_case
    monkeypatch.delenv("KODI_LAB_SNAPSHOT_RECOVERY_EPOCH", raising=False)
    monkeypatch.setenv("KODI_LAB_SNAPSHOT_RECOVERY_EPOCH", case.epoch_id)
    with case.sessions() as session:
        boundary = session.get(SnapshotRecoveryEpochRow, case.epoch_id)
        assert boundary is not None and boundary.web_owner is not None
        owner = json.loads(boundary.web_owner)
    owner["nonce"] = str(uuid4())
    monkeypatch.setattr(
        "kodi_lab.application.snapshot_recovery_maintenance.process_owner",
        lambda: canonical(owner),
    )

    def no_normal_sweep(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("ordinary router lifespan must not start")

    monkeypatch.setattr(
        "kodi_lab.application.native_zip_installations.NativeZipInstallationService.sweep",
        no_normal_sweep,
    )
    app = create_app(
        settings=Settings(data_root=case.root, development_root=case.root),
        session_factory=case.sessions,
        broker=case,
        auth=case.auth,
    )
    with TestClient(app, base_url="https://127.0.0.1") as client:
        assert client.post("/api/snapshot-recovery/reviews", json={}).status_code == 401
        # Use configured cookie name rather than adding any recovery credential.
        settings = Settings(data_root=case.root, development_root=case.root)
        client.cookies.set(settings.web_session_cookie_name, case.token)
        page = client.get("/snapshot-recovery")
        assert page.status_code == 200 and "Original snapshot recovery" in page.text
        csrf = case.auth.resolve(case.token)
        assert csrf is not None
        for headers in (
            {},
            {"Origin": "https://evil.invalid", "X-Kodi-Lab-CSRF": csrf.csrf_token},
            {"Origin": "https://127.0.0.1", "X-Kodi-Lab-CSRF": "wrong"},
        ):
            assert (
                client.post(
                    "/api/snapshot-recovery/reviews", json={}, headers=headers
                ).status_code
                == 403
            )
        headers = {"Origin": "https://127.0.0.1", "X-Kodi-Lab-CSRF": csrf.csrf_token}
        assert (
            client.post(
                "/api/snapshot-recovery/reviews",
                json={"path": "/arbitrary"},
                headers=headers,
            ).status_code
            == 422
        )
        for path in ("/mcp", "/api/instances", "/api/managed-kodi/slots", "/kodi"):
            assert client.get(path).status_code == 404
        # Test-only storage UID injection; production default is still root-owned.
        app.state.snapshot_recovery.owner_uid = os.getuid()
        reviewed = client.post(
            "/api/snapshot-recovery/reviews", json={}, headers=headers
        )
        assert reviewed.status_code == 200
        body = reviewed.json()
        assert (
            client.post(
                f"/api/snapshot-recovery/{body['recovery_id']}/confirm",
                json={
                    "fingerprint": body["fingerprint"],
                    "request_id": "00000000-0000-4000-8000-000000000001",
                    "confirmed": False,
                },
                headers=headers,
            ).status_code
            == 409
        )
        accepted = client.post(
            f"/api/snapshot-recovery/{body['recovery_id']}/confirm",
            json={
                "fingerprint": body["fingerprint"],
                "request_id": "00000000-0000-4000-8000-000000000001",
                "confirmed": True,
            },
            headers=headers,
        )
        assert accepted.status_code == 202
        completed = client.get(f"/api/snapshot-recovery/{body['recovery_id']}").json()
        assert (
            completed["state"] == "succeeded" and completed["final_state"] == "stopped"
        )
    assert case.provider.calls == ["provision", "apply", "verify"]
