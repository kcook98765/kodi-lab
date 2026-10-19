"""Execute the fixed broker program on verified text in an isolated filesystem."""

from __future__ import annotations

import base64
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from kodi_lab.broker.managed_bridge_trust import (
    ESTABLISH_PROGRAM,
    approved_bridge_build,
)
from kodi_lab.kodi.rc1_bridge import packaged_rc1_bridge_sources
from tests.web.conftest import REVISION_ID
from tests.web.conftest import database as database


def run_candidate(tmp_path: Path) -> tuple[subprocess.CompletedProcess[str], Path]:
    home = tmp_path / "kodi/.kodi"
    home.mkdir(parents=True, exist_ok=True)
    program = (
        ESTABLISH_PROGRAM.replace("/home/kodi/.kodi", str(home))
        .replace('directory("/home")', "")
        .replace('directory("/home/kodi")', "")
    )
    value = {
        "target_id": "00000000-0000-4000-8000-000000000101",
        "runtime_revision_id": str(REVISION_ID),
        "container_id": "a" * 64,
        "generation": 1,
        "previous_inventories": {},
        "secret": base64.urlsafe_b64encode(b"x" * 32).decode().rstrip("="),
        "approved": approved_bridge_build("22.0-rc.1"),
        "reviewed_inventory": {
            name: hashlib.sha256(data).hexdigest()
            for name, data in packaged_rc1_bridge_sources().items()
        },
        "reviewed_sources": {
            k: base64.b64encode(v).decode()
            for k, v in packaged_rc1_bridge_sources().items()
        },
    }
    return subprocess.run(
        [sys.executable, "-c", program],
        input=json.dumps(value) + "\n",
        capture_output=True,
        text=True,
        timeout=10,
    ), home


def test_closed_candidate_materialization_and_readback(tmp_path: Path) -> None:
    result, home = run_candidate(tmp_path)
    assert result.returncode == 0, result.stdout
    assert json.loads(result.stdout)["bridge_id"]
    for name, data in packaged_rc1_bridge_sources().items():
        assert (home / "addons/service.kodi_lab_bridge" / name).read_bytes() == data
    assert run_candidate(tmp_path)[0].returncode == 0
    assert json.loads(
        (home / "userdata/addon_data/service.kodi_lab_bridge/state.json").read_text()
    )["target_id"]


@pytest.mark.parametrize(
    "failure", ["source", "metadata", "symlink", "directory_symlink", "extra"]
)
def test_existing_drift_or_symlink_fails_closed(tmp_path: Path, failure: str) -> None:
    _, home = run_candidate(tmp_path)
    addon = home / "addons/service.kodi_lab_bridge"
    if failure == "extra":
        (addon / "json.py").write_text("unreviewed shadow library")
    elif failure in ("source", "metadata"):
        (
            addon / ("narrow_bridge.py" if failure == "source" else "addon.xml")
        ).write_text("unreviewed")
    elif failure == "symlink":
        (addon / "narrow_bridge.py").unlink()
        (addon / "narrow_bridge.py").symlink_to(tmp_path / "outside")
    else:
        (addon / "resources/settings.xml").unlink()
        import shutil

        shutil.rmtree(addon / "resources")
        (addon / "resources").symlink_to(tmp_path)
    result, _ = run_candidate(tmp_path)
    assert result.returncode == 1
    assert result.stdout.strip() == "managed_bridge_establishment_failed"


def test_packaged_candidate_cannot_adopt_changed_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("kodi_lab.kodi.rc1_bridge.RC1_ARCHIVE_SHA256", "0" * 64)
    with pytest.raises(ValueError, match="differs from review"):
        packaged_rc1_bridge_sources()


@pytest.mark.parametrize("flag", ["true", 1, None, "install"])
def test_install_switch_has_no_command_or_selector_input(flag: object) -> None:
    from pydantic import ValidationError

    from kodi_lab.broker.operations import EstablishManagedBridgeTrustParams

    with pytest.raises(ValidationError):
        EstablishManagedBridgeTrustParams.model_validate(
            {
                "managed_instance_id": "00000000-0000-4000-8000-000000000101",
                "expected_container_id": "a" * 64,
                "generation": 1,
                "install_reviewed": flag,
            }
        )


@pytest.mark.parametrize(
    "failure", [None, "missing", "expired", "wrong_kind", "finished"]
)
def test_candidate_requires_live_lifecycle_install_owner(
    database: object, failure: str | None
) -> None:
    from datetime import UTC, datetime, timedelta
    from typing import Any, cast

    from kodi_lab.application.lifecycle_operations import (
        ManagedInstanceLifecycleOperationCoordinator,
    )
    from kodi_lab.broker.managed_bridge_trust import (
        ManagedBridgeProvisionError,
        require_reviewed_installation_lease,
    )
    from kodi_lab.domain import (
        ManagedInstanceLifecycleKind,
        ManagedInstanceLifecycleOperationState,
        ManagedInstanceMutationKind,
        ManagedInstanceSemanticState,
    )
    from kodi_lab.storage.models import (
        ManagedInstanceLifecycleOperationRow,
        ManagedInstanceMutationLeaseRow,
    )
    from tests.web.conftest import INSTANCE_ID

    factory = cast(Any, database)
    if failure != "missing":
        owner = ManagedInstanceLifecycleOperationCoordinator(factory)
        handle = owner.begin(
            INSTANCE_ID,
            instance_name="owned preview test",
            kind=ManagedInstanceLifecycleKind.START,
            prior_state=ManagedInstanceSemanticState.RUNNING_USABLE,
        )
        with factory.begin() as db:
            lease = db.get(ManagedInstanceMutationLeaseRow, str(INSTANCE_ID))
            operation = db.get(
                ManagedInstanceLifecycleOperationRow, str(handle.operation_id)
            )
            if failure == "expired":
                lease.lease_expires_at = (
                    datetime.now(UTC) - timedelta(seconds=1)
                ).isoformat()
            elif failure == "wrong_kind":
                lease.operation_kind = ManagedInstanceMutationKind.RESET
            elif failure == "finished":
                operation.state = ManagedInstanceLifecycleOperationState.FAILED
                operation.completed_at = datetime.now(UTC).isoformat()
                operation.resulting_state = "stopped"
                operation.failure_reason = "test failure"
    with factory() as db:
        if failure:
            with pytest.raises(ManagedBridgeProvisionError):
                require_reviewed_installation_lease(db, str(INSTANCE_ID))
        else:
            require_reviewed_installation_lease(db, str(INSTANCE_ID))
