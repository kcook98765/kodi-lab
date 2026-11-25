from pathlib import Path
from uuid import uuid4

import pytest

from kodi_lab.managed.controller import Controller
from kodi_lab.managed.packages import Package
from kodi_lab.managed.protocol import Document, Refusal
from tests.experiments.test_managed_v2 import package


@pytest.mark.parametrize(
    ("window", "previous", "verify_failure", "verified"),
    [
        (12002, False, False, True),
        (12002, False, True, False),
        (12002, True, False, False),
        (10100, False, False, False),
    ],
)
def test_cold_ok_dialog_reports_only_verified_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    window: int,
    previous: bool,
    verify_failure: bool,
    verified: bool,
) -> None:
    root = tmp_path / "lab"
    root.mkdir(mode=0o700)
    controller = Controller(root)
    actor = str(uuid4())
    controller.perform("managed_acquire", {}, actor=actor)
    artifact = controller.packages.approve(package())
    controller.upload_package(artifact.data, artifact.digest, actor=actor)
    monkeypatch.setattr(controller.runtime, "ready", lambda: {"state": "ready"})
    monkeypatch.setattr(
        controller,
        "installed",
        lambda _addon: {"version": "1.0.0"} if previous else None,
    )
    install_calls = []
    checks = []

    def install(intent: Document) -> Document:
        install_calls.append(intent)
        controller.checkpoint("native_reply", native_pending=True)
        return {
            "prior_skin": "skin.estuary",
            "final_skin": "skin.estuary",
            "error": f"unexpected_window_during_install:{window}",
        }

    def verify(pkg: Package) -> Document:
        checks.append(pkg.digest)
        return {"digest": pkg.digest, "origin": "repository.kodi-lab-v2"}

    def dependencies(pkg: Package, catalog: dict[str, Package]) -> list[Document]:
        checks.append(pkg.id)
        if verify_failure:
            raise Refusal("native_origin_mismatch")
        return [{"addon": "script.module.ready"}]

    monkeypatch.setattr(controller, "install_on_estuary", install)
    monkeypatch.setattr(controller, "verify", verify)
    monkeypatch.setattr(controller, "dependencies", dependencies)
    result = controller.perform(
        "addon_install",
        {"addon": artifact.id, "version": artifact.version, "digest": artifact.digest},
        actor=actor,
    )["result"]
    assert isinstance(result, dict)
    assert len(install_calls) == 1
    operation = controller.last_operation()
    assert operation is not None
    if verified:
        assert result["state"] == "verified"
        assert result["interaction"] == {
            "state": "dialog_observed",
            "window_id": 12002,
            "next_action": "inspect_native_gui",
        }
        assert result["package"]["digest"] == artifact.digest
        assert result["dependencies"] == [{"addon": "script.module.ready"}]
        assert operation["state"] == "complete"
    else:
        assert result["state"] == "failed"
        assert operation["state"] == "pending"
        assert "interaction" not in result
        if verify_failure:
            assert result["verification_error"] == "native_origin_mismatch"
    assert checks == (
        [artifact.digest, artifact.id] if verified or verify_failure else []
    )
