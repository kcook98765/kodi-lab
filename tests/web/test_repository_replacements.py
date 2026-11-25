"""The agent replacement entry point uses a closed, owner-bound package request."""

from collections.abc import Mapping
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from kodi_lab.managed.broker import BrokerClient
from kodi_lab.managed.controller import Controller
from kodi_lab.managed.protocol import OperationReply
from tests.web.test_external_target_web import _mcp_call
from tests.web.test_managed_upload import bearer


def test_managed_replacement_requires_explicit_owner_and_exact_package_identity(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "lab"
    root.mkdir(mode=0o700)
    controller = Controller(root)
    actor, token = bearer(client, managed=True)
    _, outside = bearer(client, managed=False)
    calls: list[tuple[str, str, dict[str, str]]] = []

    def dispatch(
        _broker: BrokerClient, principal: str, operation: str, args: Mapping[str, str]
    ) -> OperationReply:
        calls.append((principal, operation, dict(args)))
        return controller.perform(operation, dict(args), actor=principal)

    monkeypatch.setattr(BrokerClient, "request", dispatch)
    tool = "managed_addon_install"
    args: dict[str, object] = {
        "addon": "script.fixture",
        "version": "1.0.0",
        "digest": "a" * 64,
    }
    outside_result = _mcp_call(client, outside, tool, args)
    assert "error" in outside_result or outside_result["result"]["isError"]
    assert not calls
    for forbidden in ({**args, "destination": "/tmp"}, {**args, "confirmed": True}):
        result = _mcp_call(client, token, tool, forbidden)["result"]
        assert result["isError"]
    assert not calls
    denied = _mcp_call(client, token, tool, args)["result"]
    assert (
        denied["isError"] and denied["content"][0]["text"] == "managed_acquire_required"
    )
    assert controller.last_operation() is None
    controller.perform("managed_acquire", {}, actor=actor)
    denied = _mcp_call(client, token, tool, args)["result"]
    assert denied["isError"] and denied["content"][0]["text"] == "package_not_approved"
    receipt = controller.last_operation()
    assert receipt is not None and receipt["state"] == "complete"
    assert receipt["event"] == "refused" and receipt["error"] == "package_not_approved"
    assert calls == [(actor, "addon_install", args)] * 2
    assert not list(controller.runtime.feed.iterdir())
