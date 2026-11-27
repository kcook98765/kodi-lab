from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

import pytest

from kodi_lab.managed.broker import Broker
from kodi_lab.managed.controller import Controller
from kodi_lab.managed.protocol import Refusal
from kodi_lab.managed.runtime import Runtime
from kodi_lab.mcp.transport import MANAGED_TOOLS

SCOPE = str(uuid4())
OTHER = str(uuid4())
IMAGE = "sha256:" + "a" * 64


@pytest.fixture
def broker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Broker:
    root = tmp_path / "managed"
    root.mkdir(mode=0o700)
    controller = Controller(root, daemon_root=root, handoff_root=tmp_path)
    for runtime in controller.runtimes.values():
        runtime.pointer.write_text("a" * 32)
        runtime.image_pin.write_text(IMAGE)
        userdata = runtime.root / "volumes" / ("a" * 32) / "userdata"
        userdata.mkdir(parents=True)
        (userdata / "guisettings.xml").write_text("<settings/>")
    monkeypatch.setattr(Runtime, "inspect", lambda _: None)
    monkeypatch.setattr("kodi_lab.managed.runtime.docker", lambda *_: IMAGE)
    monkeypatch.setattr(
        "kodi_lab.managed.runtime.shutil.disk_usage",
        lambda _: SimpleNamespace(free=16 * 1024**3),
    )
    return Broker(tmp_path / "broker.sock", controller, os.getuid())


def call(broker: Broker, operation: str, actor: str = "admin", **args: str) -> object:
    return broker.dispatch({"actor": actor, "name": operation, "args": args})["result"]


def capacity(broker: Broker, actor: str = "admin") -> object:
    result = call(broker, "snapshot_list", actor, include_capacity="true")
    assert isinstance(result, dict)
    return result["snapshot_capacity"]


def test_limits_share_versions_and_keep_actor_scopes_independent(
    broker: Broker,
) -> None:
    with broker:
        assert call(broker, "storage_snapshot_quotas") == {
            "administrator": {"used": 0, "capacity": 32, "remaining": 32},
            "agent_default": 32,
        }
        assert capacity(broker, SCOPE) == {"used": 0, "capacity": 32, "remaining": 32}
        assert not {"storage_snapshot_quotas", "storage_set_snapshot_quotas"} & (
            MANAGED_TOOLS.keys() | MANAGED_TOOLS.values()
        )
        with pytest.raises(Refusal, match="storage_maintenance_admin_required"):
            call(
                broker,
                "storage_set_snapshot_quotas",
                SCOPE,
                administrator="1",
                agent_default="1",
            )
        call(
            broker, "storage_set_snapshot_quotas", administrator="2", agent_default="1"
        )
        call(broker, "managed_acquire", SCOPE)
        call(broker, "snapshot_save", SCOPE, name="Shared name", generation="matrix")
        with pytest.raises(Refusal, match="snapshot_quota_exhausted"):
            call(broker, "snapshot_save", SCOPE, name="Other name", generation="nexus")
        with pytest.raises(Refusal, match="snapshot_name_exists"):
            call(broker, "snapshot_save", SCOPE, name="Shared name", generation="nexus")
        assert call(broker, "snapshot_list", OTHER) == []
        assert capacity(broker, OTHER) == {"used": 0, "capacity": 1, "remaining": 1}
        call(broker, "managed_release", SCOPE)
        call(broker, "snapshot_save", name="Shared name", generation="omega")
        assert capacity(broker) == {"used": 1, "capacity": 2, "remaining": 1}
        assert capacity(broker, SCOPE) == {"used": 1, "capacity": 1, "remaining": 0}

        original = broker.controller
        restarted = Controller(original.omega.root)
        with Broker(
            original.omega.root / "new.sock", restarted, os.getuid()
        ) as next_broker:
            assert capacity(next_broker) == capacity(broker)
            assert capacity(next_broker, SCOPE) == capacity(broker, SCOPE)
            assert call(next_broker, "snapshot_list") == call(broker, "snapshot_list")


def test_save_and_policy_update_use_same_lock(broker: Broker) -> None:
    entered, finish = Event(), Event()

    def interrupt(event: str) -> None:
        if event == "snapshot_before_publish":
            entered.set()
            assert finish.wait(5)

    broker.controller.interrupt = interrupt
    with broker, ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(call, broker, "snapshot_save", name="in flight")
        try:
            assert entered.wait(5)
            with pytest.raises(Refusal, match="managed_busy"):
                call(
                    broker,
                    "storage_set_snapshot_quotas",
                    administrator="1",
                    agent_default="1",
                )
            assert not broker.controller.snapshot_quotas_file.exists()
        finally:
            finish.set()
        pending.result(timeout=5)
        call(
            broker, "storage_set_snapshot_quotas", administrator="1", agent_default="64"
        )
        assert capacity(broker) == {"used": 1, "capacity": 1, "remaining": 0}
        with pytest.raises(Refusal, match="snapshot_quota_exhausted"):
            call(broker, "snapshot_save", name="after change")


def test_disk_headroom_refuses_save_with_slots_available(
    broker: Broker, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "kodi_lab.managed.runtime.shutil.disk_usage",
        lambda _: SimpleNamespace(free=8 * 1024**3),
    )
    with broker:
        with pytest.raises(Refusal, match="disk_headroom"):
            call(broker, "snapshot_save", name="no room")
        assert capacity(broker) == {"used": 0, "capacity": 32, "remaining": 32}
        assert not list(broker.controller.snapshots.root.iterdir())


def test_failed_policy_write_keeps_previous_limits(
    broker: Broker, monkeypatch: pytest.MonkeyPatch
) -> None:
    with broker:
        call(
            broker, "storage_set_snapshot_quotas", administrator="12", agent_default="9"
        )

        def unavailable(_path: Path, _data: bytes) -> None:
            raise OSError("configuration write failed")

        monkeypatch.setattr("kodi_lab.managed.storage_maintenance.atomic", unavailable)
        with pytest.raises(Refusal, match="operation_outcome_unconfirmed"):
            call(
                broker,
                "storage_set_snapshot_quotas",
                administrator="64",
                agent_default="1",
            )
        assert capacity(broker) == {"used": 0, "capacity": 12, "remaining": 12}
        assert capacity(broker, SCOPE) == {"used": 0, "capacity": 9, "remaining": 9}
