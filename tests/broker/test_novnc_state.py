"""Closed state-owner subprocess protocol and unconfirmed transaction behavior."""

import io
import json
from pathlib import Path
from uuid import uuid4

import pytest

from kodi_lab.application.novnc_control import HumanViewerOwnership
from kodi_lab.broker.novnc import ViewBinding
from kodi_lab.broker.novnc_state import StateOwnerOwnership, main
from kodi_lab.config import Settings
from kodi_lab.storage import metadata
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from tests.web.conftest import INSTANCE_ID, REVISION_ID, seed_instance


def test_state_owner_transactions_claim_and_release_exact_pending_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "state"
    (root / "database").mkdir(parents=True)
    root.chmod(0o700)
    monkeypatch.setenv("KODI_LAB_DATA_ROOT", str(root))
    settings = Settings()
    engine = create_database_engine(settings.database_url)
    metadata.create_all(engine)
    sessions = create_session_factory(engine)
    seed_instance(sessions)
    authority = HumanViewerOwnership(sessions)
    attempt = uuid4()
    broker = str(uuid4())
    binding = ViewBinding(
        "a" * 64, "sha256:" + "b" * 64, "current-start", (str(REVISION_ID), 0, None)
    )
    authority.create(INSTANCE_ID, attempt, "c" * 64, binding, "interactive")
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(
            json.dumps(
                {
                    "method": "claim",
                    "args": [
                        str(INSTANCE_ID),
                        str(attempt),
                        "c" * 64,
                        binding.__dict__,
                        "interactive",
                        broker,
                    ],
                }
            )
        ),
    )
    main()
    assert capsys.readouterr().out == '{"ok":true}\n'
    assert authority.valid(INSTANCE_ID, attempt, broker)
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(
            json.dumps(
                {
                    "method": "finish",
                    "args": [str(INSTANCE_ID), str(attempt), broker, True],
                }
            )
        ),
    )
    main()
    assert capsys.readouterr().out == '{"ok":true}\n'
    assert authority.status(INSTANCE_ID) is None
    monkeypatch.setattr("sys.stdin", io.StringIO('{"method":"execute","args":[]}'))
    with pytest.raises(ValueError, match="denied"):
        main()
    engine.dispose()


def test_unconfirmed_state_owner_child_keeps_existing_input_exclusion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    tmp_path.chmod(0o700)
    engine = create_database_engine("sqlite://")
    sessions = create_session_factory(engine)
    observed = []

    def failed(*args: object, **kwargs: object) -> object:
        observed.append(kwargs)
        return SimpleNamespace(returncode=-9, stdout="")

    monkeypatch.setattr("subprocess.run", failed)
    authority = StateOwnerOwnership(Settings(data_root=tmp_path), sessions)
    with pytest.raises(ValueError, match="unconfirmed"):
        authority.finish(INSTANCE_ID, uuid4(), str(uuid4()), False)
    assert observed[0]["user"] == tmp_path.stat().st_uid
    assert observed[0]["group"] == tmp_path.stat().st_gid
    assert observed[0]["extra_groups"] == [] and observed[0]["timeout"] == 2
    engine.dispose()


def test_state_writer_starts_without_loading_orm_docker_or_web() -> None:
    import subprocess
    import sys

    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-c",
            "import sys;import kodi_lab.novnc_writer;"
            "assert not {'sqlalchemy','docker','pydantic'} & sys.modules.keys()",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout == result.stderr == ""


def test_closed_sqlite_writer_preserves_scope_and_claimed_recovery_fence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy.exc import IntegrityError

    from kodi_lab.storage.models import ManagedRemoteGuardRow, NovncSessionRow

    root = tmp_path / "state"
    (root / "database").mkdir(parents=True)
    root.chmod(0o700)
    monkeypatch.setenv("KODI_LAB_DATA_ROOT", str(root))
    engine = create_database_engine(Settings().database_url)
    metadata.create_all(engine)
    sessions = create_session_factory(engine)
    seed_instance(sessions)
    authority = HumanViewerOwnership(sessions)
    attempt = uuid4()
    broker = str(uuid4())
    binding = ViewBinding(
        "a" * 64, "sha256:" + "b" * 64, "current-start", (str(REVISION_ID), 0, None)
    )
    authority.create(INSTANCE_ID, attempt, "c" * 64, binding, "interactive")

    def write(method: str, args: list[object]) -> None:
        monkeypatch.setattr(
            "sys.stdin", io.StringIO(json.dumps({"method": method, "args": args}))
        )
        main()

    with pytest.raises(ValueError, match="ownership"):
        write(
            "claim",
            [
                str(INSTANCE_ID),
                str(attempt),
                "d" * 64,
                binding.__dict__,
                "interactive",
                broker,
            ],
        )
    assert authority.status(INSTANCE_ID).state == "pending"  # type: ignore[union-attr]
    write(
        "claim",
        [
            str(INSTANCE_ID),
            str(attempt),
            "c" * 64,
            binding.__dict__,
            "interactive",
            broker,
        ],
    )
    assert authority.valid(INSTANCE_ID, attempt, broker)
    with pytest.raises(IntegrityError), sessions.begin() as db:
        db.add(
            ManagedRemoteGuardRow(
                instance_id=str(INSTANCE_ID),
                invocation_id=str(uuid4()),
                process_id=str(uuid4()),
            )
        )
    with pytest.raises(ValueError, match="revoked"):
        write("started", [str(INSTANCE_ID), str(attempt), str(uuid4()), "exec-wrong"])
    write("started", [str(INSTANCE_ID), str(attempt), broker, "a" * 64])
    write("revoke", [str(INSTANCE_ID), str(attempt), "d" * 64])
    assert authority.valid(INSTANCE_ID, attempt, broker)
    write("revoke", [str(INSTANCE_ID), str(attempt), "c" * 64])
    assert authority.status(INSTANCE_ID).state == "revoking"  # type: ignore[union-attr]
    write("expire_pending", [])
    assert authority.status(INSTANCE_ID) is not None
    write("finish", [str(INSTANCE_ID), str(attempt), broker, False])
    assert authority.status(INSTANCE_ID).state == "blocked"  # type: ignore[union-attr]
    write("finish", [str(INSTANCE_ID), str(attempt), str(uuid4()), True])
    assert authority.status(INSTANCE_ID) is not None
    write("finish", [str(INSTANCE_ID), str(attempt), broker, True])
    assert authority.status(INSTANCE_ID) is None
    with sessions() as db:
        assert db.get(NovncSessionRow, str(INSTANCE_ID)) is None
    engine.dispose()
