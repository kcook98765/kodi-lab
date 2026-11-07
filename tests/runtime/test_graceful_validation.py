"""Image validation must detect the native Stop defect before fresh attestation."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from kodi_lab.runtime import validation
from kodi_lab.runtime.recipes import load_reviewed_recipe
from kodi_lab.runtime.temporary_networks import TemporaryNetwork


@pytest.mark.parametrize("generation", ["matrix", "nexus", "omega"])
@pytest.mark.parametrize(
    "defect",
    [
        "none",
        "not-saved",
        "exit143",
        "forced137",
        "missing-exit",
        "boolean-exit",
        "denied-setting",
    ],
)
def test_validation_checks_pending_setting_and_exact_exit(
    generation: str, defect: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    operation = uuid4()
    network = TemporaryNetwork(
        "network-id", "private-validation", operation, "validation", None
    )
    image = "sha256:" + "a" * 64
    disk = "original-name"
    memory = disk
    stopped: list[int] = []
    removed: list[bool] = []

    class Container:
        id = "validation-only"

        def __init__(self) -> None:
            self.attrs: dict[str, Any] = {"State": {"Running": False}}

        def start(self) -> None:
            nonlocal memory
            memory = disk
            self.attrs = {"State": {"Running": True}}

        def stop(self, timeout: int) -> None:
            nonlocal disk
            stopped.append(timeout)
            if defect != "not-saved":
                disk = memory
            state: dict[str, Any] = {"Running": False, "ExitCode": 0}
            if defect in {"exit143", "forced137"}:
                state["ExitCode"] = 143 if defect == "exit143" else 137
            elif defect == "missing-exit":
                state.pop("ExitCode")
            elif defect == "boolean-exit":
                state["ExitCode"] = False
            self.attrs = {"State": state}

        def reload(self) -> None:
            pass

        def remove(self, *, v: bool, force: bool) -> None:
            assert not v and not force
            removed.append(True)

    container = Container()
    inspected = {
        "Image": image,
        "HostConfig": {"NetworkMode": network.name},
        "NetworkSettings": {"Networks": {network.name: {"NetworkID": network.id}}},
        "Mounts": [],
    }
    client = SimpleNamespace(
        containers=SimpleNamespace(create=lambda *args, **kw: container),
        api=SimpleNamespace(inspect_container=lambda _: inspected),
    )
    monkeypatch.setattr(
        validation, "create_temporary_network", lambda *args, **kw: network
    )
    monkeypatch.setattr(
        validation, "remove_exact_temporary_network", lambda *args: True
    )
    monkeypatch.setattr(validation, "_readiness", lambda *args, **kw: None)
    monkeypatch.setattr(validation, "_identity", lambda *args: None)
    monkeypatch.setattr(validation, "_capture", lambda *args, **kw: {"fixture": True})
    # Display transport is independent of this settings/stop contract.
    monkeypatch.setattr(validation, "reviewed_display_capability", lambda _: None)

    def execute(_container: Any, command: tuple[str, ...], **kw: Any) -> bytes:
        return b"1000" if command[0] == "/usr/bin/id" else b"llvmpipe"

    def rpc(_container: Any, method: str, params: dict[str, Any] | None = None) -> Any:
        nonlocal memory, disk
        if method == "Input.Home":
            # A GUI save must not make an abrupt Stop appear to flush settings.
            disk = memory
        if method == "Settings.GetSettingValue":
            return {"value": memory}
        if method == "Settings.SetSettingValue":
            if defect == "denied-setting":
                return False
            assert params is not None and params["setting"] == "services.devicename"
            memory = params["value"]
            return True
        return {} if method == "VideoLibrary.GetMovies" else "OK"

    monkeypatch.setattr(validation, "_exec", execute)
    monkeypatch.setattr(validation, "_rpc", rpc)
    if defect == "none":
        result = validation.validate_reviewed_image(
            load_reviewed_recipe(generation),
            image_id=image,
            operation_id=operation,
            client=client,
        )
        assert result.report["pending_setting_persistence"] is True
        assert disk == "original-name"
        assert stopped == [10, 10]
    else:
        message = (
            "did not survive"
            if defect == "not-saved"
            else "change was rejected"
            if defect == "denied-setting"
            else "graceful stop failed"
        )
        with pytest.raises(validation.RecipeValidationError, match=message):
            validation.validate_reviewed_image(
                load_reviewed_recipe(generation),
                image_id=image,
                operation_id=operation,
                client=client,
            )
    assert removed == [True]
