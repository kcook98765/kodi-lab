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
