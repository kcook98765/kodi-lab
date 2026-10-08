"""Real authenticated transport + collector shutdown + a waiting target caller."""

from __future__ import annotations

import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import uuid4

import pytest

from kodi_lab.application.ui_state import ObservationGate
from kodi_lab.kodi.bridge import ExternalBridgeError, b64url
from kodi_lab.kodi.observation_bridge import ObservationBridgeClient
from tests.kodi.test_bridge_otp_bootstrap import _endpoint
from tests.kodi.test_narrow_bridge_addon import _load
from tests.kodi.test_ui_observation_bridge import _collector


def test_inflight_gui_shutdown_is_bounded_gate_releases_and_reconnects(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target, bridge_id, secret = uuid4(), uuid4(), b"x" * 32
    state_path = tmp_path / "state.json"
    bridge = _load(monkeypatch, state_path=state_path)
    state_path.write_text(
        json.dumps(
            {
                "format": 2,
                "bridge_id": str(bridge_id),
                "target_id": str(target),
                "secret": b64url(secret),
                "replay": [],
                "deployments": {},
                "rollback": None,
            }
        )
    )
    collector, requests = _collector(monkeypatch, tmp_path)
    # Static build identity is separate from the GUI batch being paused.
    bridge["_identity"].__globals__["_kodi_version"] = lambda: "21.3"
    module = ModuleType("ui_observation")
    module.collect = collector["collect"]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "ui_observation", module)
    entered, release = threading.Event(), threading.Event()
    original = collector["collect"].__globals__["_rpc"]

    def paused(*a: Any, **kw: Any) -> Any:
        entered.set()
        assert release.wait(2)
        return original(*a, **kw)

    collector["collect"].__globals__["_rpc"] = paused
    server = bridge["Server"](("127.0.0.1", 0), bridge["Handler"])
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    port = server.server_address[1]
    client = ObservationBridgeClient()
    gate = ObservationGate(timeout=2)

    def observe() -> object:
        with gate.enter(target):
            return client.inspect_ui_state(
                _endpoint(),
                bridge_port=port,
                target_id=target,
                bridge_id=bridge_id,
                secret=secret,
            )

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(observe)
            assert entered.wait(2)
            second = pool.submit(observe)
            before = time.monotonic()
            server.stopping.set()
            server.shutdown()
            server.server_close()
            assert time.monotonic() - before < 1
            release.set()
            for future in (first, second):
                with pytest.raises(ExternalBridgeError):
                    future.result(timeout=3)
        assert not gate._entries
        assert [request["method"] for request in requests] == ["XBMC.GetInfoLabels"]
        collector["collect"].__globals__["_rpc"] = original
        recovered = bridge["Server"](("127.0.0.1", port), bridge["Handler"])
        again = threading.Thread(target=recovered.serve_forever, daemon=True)
        again.start()
        try:
            assert observe()[1].labels_available  # type: ignore[index]
        finally:
            recovered.stopping.set()
            recovered.shutdown()
            recovered.server_close()
            again.join(timeout=2)
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
        client.close()
