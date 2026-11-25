from pathlib import Path
from uuid import uuid4

import pytest

from kodi_lab.managed.controller import Controller
from kodi_lab.managed.packages import Package
from kodi_lab.managed.protocol import Document, Refusal
from kodi_lab.managed.source_provenance import read_sources
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
    slot = uuid4().hex
    controller.runtime.pointer.write_text(slot)
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
        assert read_sources(root, slot) == {artifact.id: artifact.members}
    else:
        assert result["state"] == "failed"
        assert operation["state"] == "pending"
        assert read_sources(root, slot) == {}
        assert "interaction" not in result
        if verify_failure:
            assert result["verification_error"] == "native_origin_mismatch"
    assert checks == (
        [artifact.digest, artifact.id] if verified or verify_failure else []
    )


@pytest.mark.parametrize("verified_dependency", [True, False])
def test_same_version_replacement_verifies_exact_bytes_origin_and_dependencies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verified_dependency: bool
) -> None:
    import sqlite3
    from io import BytesIO
    from zipfile import ZipFile

    from kodi_lab.managed.runtime import ESTUARY, REPO

    def build(addon: str, *, dependency: str | None = None) -> bytes:
        stream = BytesIO()
        extension = "xbmc.python.script" if dependency else "xbmc.python.module"
        requires = (
            f'<requires><import addon="{dependency}"/></requires>' if dependency else ""
        )
        with ZipFile(stream, "w") as archive:
            archive.writestr(
                f"{addon}/addon.xml",
                f'<addon id="{addon}" version="1.0.0" name="{addon}">{requires}'
                f'<extension point="{extension}" library="default.py"/></addon>',
            )
            archive.writestr(f"{addon}/default.py", "print('reviewed replacement')")
        return stream.getvalue()

    root = tmp_path / "lab"
    root.mkdir(mode=0o700)
    controller = Controller(root)
    runtime = controller.runtime
    runtime.material.mkdir(exist_ok=True)
    (runtime.material / "manifest.xml").write_text("<addons/>")
    slot = uuid4().hex
    runtime.pointer.write_text(slot)
    profile = runtime.root / "volumes" / slot
    database = profile / "userdata/Database/Addons33.db"
    database.parent.mkdir(parents=True)
    dep = controller.packages.approve(build("script.module.fixture"))
    (root / "catalog.json").write_text(f'["{dep.digest}"]')
    package = controller.packages.approve(build("script.fixture", dependency=dep.id))
    actor = str(uuid4())
    controller.upload_package(package.data, package.digest, actor=actor)
    controller.perform("managed_acquire", {}, actor=actor)
    monkeypatch.setattr(runtime, "ready", lambda: {"state": "ready"})
    monkeypatch.setattr(
        controller, "installed", lambda name: {"enabled": True, "version": "1.0.0"}
    )

    def install(intent: Document) -> Document:
        assert intent["replace"] is True
        assert intent["files"] == package.members
        assert dep.id in intent["dependencies"]
        controller.checkpoint("native_dispatch", native_pending=True)
        with sqlite3.connect(database) as connection:
            connection.execute("CREATE TABLE installed (addonID, origin, enabled)")
            for artifact in (package, dep):
                connection.execute(
                    "INSERT INTO installed VALUES (?, ?, 1)",
                    (
                        artifact.id,
                        REPO
                        if verified_dependency or artifact == package
                        else "foreign",
                    ),
                )
                with ZipFile(BytesIO(artifact.data)) as archive:
                    destination = profile / "addons" / artifact.id
                    destination.mkdir(parents=True)
                    for member in artifact.members:
                        (destination / member).write_bytes(
                            archive.read(f"{artifact.id}/{member}")
                        )
        return {"prior_skin": ESTUARY, "final_skin": ESTUARY}

    monkeypatch.setattr(controller, "install_on_estuary", install)
    args = {"addon": package.id, "version": package.version, "digest": package.digest}
    if not verified_dependency:
        with pytest.raises(Refusal, match="native_origin_mismatch"):
            controller.perform("addon_install", args, actor=actor)
        with pytest.raises(Refusal, match="managed_recovery_required"):
            controller.perform("addon_install", args, actor=actor)
        assert read_sources(root, slot) == {}
        return
    result = controller.perform("addon_install", args, actor=actor)["result"]
    assert isinstance(result, dict) and result["state"] == "verified"
    assert result["package"]["digest"] == package.digest
    assert result["dependencies"][0]["digest"] == dep.digest
    assert read_sources(root, slot) == {package.id: package.members}
    receipt = controller.last_operation()
    assert receipt is not None and receipt["state"] == "complete"
