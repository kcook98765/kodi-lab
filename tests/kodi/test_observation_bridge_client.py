from pathlib import Path
from typing import Any

import httpx
import pytest

from kodi_lab.kodi.observation_bridge import ObservationBridgeClient
from tests.kodi.test_ui_observation_bridge import _collector


def test_client_reuses_setup_closes_each_request_and_reopens_after_close() -> None:
    seen: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    transport = httpx.MockTransport(respond)
    client = ObservationBridgeClient(transport=transport)
    with client._client() as first:
        first.get("http://127.0.0.1")
    with client._client() as second:
        assert second is first
        second.get("http://127.0.0.1")
    assert all(r.headers["Connection"] == "close" for r in seen)
    client.close()
    assert first.is_closed
    with client._client() as reopened:
        assert reopened is not first
    client.close()


def test_shutdown_aborts_collector_before_sensitive_dispatch_and_releases_gate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module, requests = _collector(monkeypatch, tmp_path)
    collect: Any = module["collect"]
    with pytest.raises(RuntimeError, match="stopping"):
        collect(abort=lambda: True)
    assert not requests
    assert collect()["labels_available"]


def test_shutdown_between_gui_batches_does_not_dispatch_more_labels(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module, requests = _collector(monkeypatch, tmp_path)
    collect: Any = module["collect"]
    with pytest.raises(RuntimeError, match="stopping"):
        collect(abort=lambda: bool(requests))
    assert [r["method"] for r in requests] == ["XBMC.GetInfoLabels"]
    assert collect()["labels_available"]
