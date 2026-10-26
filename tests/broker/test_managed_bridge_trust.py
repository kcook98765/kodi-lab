"""Fixed broker provisioning accepts identities, never caller secrets or code."""

from __future__ import annotations

import base64
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from kodi_lab.broker.managed_bridge_trust import (
    ESTABLISH_PROGRAM,
    ManagedBridgeProvisionError,
    SqliteManagedBridgeTrustResolver,
    approved_bridge_build,
)
from kodi_lab.broker.operations import EstablishManagedBridgeTrustParams
from kodi_lab.storage.models import ManagedBridgeTrustRow, RuntimeContainerArtifactRow
from kodi_lab.storage.secrets import ExternalBridgeSecretStore, SecretStoreError
from tests.web.conftest import INSTANCE_ID, REVISION_ID
from tests.web.conftest import database as database


@pytest.mark.parametrize("extra", ["secret", "program", "path", "otp", "bridge_id"])
def test_no_caller_secret_or_arbitrary_execution(extra: str) -> None:
    with pytest.raises(ValidationError):
        EstablishManagedBridgeTrustParams.model_validate(
            {
                "managed_instance_id": str(INSTANCE_ID),
                "expected_container_id": "a" * 64,
                "generation": 1,
                extra: "anything",
            }
        )


def test_manifest_matches_packaged_approved_bridge() -> None:
    root = Path(__file__).parents[2] / "bridge/service.kodi_lab_bridge"
    for name, digest in approved_bridge_build()["files"].items():
        assert hashlib.sha256((root / name).read_bytes()).hexdigest() == digest


def test_stable_bridge_approval_is_not_replaced_by_preview() -> None:
    legacy = approved_bridge_build()
    assert legacy["addon_version"] == "1.1.29"
    assert legacy["build_fingerprint"] == (
        "85ec077c7766758b43e09c3227811ea3cfa69505abbc63d7ff8437bb494de09a"
    )
    assert approved_bridge_build("21.3") == legacy
    assert approved_bridge_build("20.5") == legacy
    assert approved_bridge_build("22.0-rc.1")["addon_version"] == "1.1.30"


@pytest.mark.parametrize("release", ["20.5", "20.4", "19.4"])
def test_broker_reviewed_nexus_installation_requires_exact_release_and_live_lease(
    database: Any, tmp_path: Path, release: str
) -> None:
    from kodi_lab.application.lifecycle_operations import (
        ManagedInstanceLifecycleOperationCoordinator,
    )
    from kodi_lab.domain import (
        ManagedInstanceLifecycleKind,
        ManagedInstanceSemanticState,
    )
    from tests.application.test_nexus_reviewed_bridge import set_release

    set_release(database, release)
    secrets = ExternalBridgeSecretStore(
        tmp_path / "secrets", master_key=b"x" * 32, key_version=1
    )
    reference = secrets.write(INSTANCE_ID, "request_authentication", b"y" * 32)
    with database.begin() as session:
        session.add(
            RuntimeContainerArtifactRow(
                runtime_revision_id=str(REVISION_ID), image_id="sha256:" + "b" * 64
            )
        )
        session.add(
            ManagedBridgeTrustRow(
                target_id=str(INSTANCE_ID),
                runtime_revision_id=str(REVISION_ID),
                container_id="a" * 64,
                bridge_port=8765,
                secret_reference=str(reference),
                state="prepared",
                generation=1,
                prepared_at="2026-10-26T00:00:00.000000Z",
                reviewed_installation=True,
            )
        )
    resolver = SqliteManagedBridgeTrustResolver(database, secrets)
    # Even an eligible release cannot install outside its typed lifecycle owner.
    with pytest.raises(ManagedBridgeProvisionError):
        resolver.resolve(str(INSTANCE_ID), "a" * 64, 1, install_reviewed=True)
    ManagedInstanceLifecycleOperationCoordinator(database).begin(
        INSTANCE_ID,
        instance_name="Nexus test",
        kind=ManagedInstanceLifecycleKind.START,
        prior_state=ManagedInstanceSemanticState.RUNNING_USABLE,
    )
    if release != "20.5":
        with pytest.raises(ManagedBridgeProvisionError, match="identity_mismatch"):
            resolver.resolve(str(INSTANCE_ID), "a" * 64, 1, install_reviewed=True)
    else:
        spec = resolver.resolve(str(INSTANCE_ID), "a" * 64, 1, install_reviewed=True)
        assert spec.kodi_release == "20.5" and spec.install_reviewed
        assert spec.provisioning.runtime_uid == spec.provisioning.runtime_gid == 1000
        for container, generation in [("b" * 64, 1), ("a" * 64, 2)]:
            with pytest.raises(ManagedBridgeProvisionError, match="not_prepared"):
                resolver.resolve(
                    str(INSTANCE_ID), container, generation, install_reviewed=True
                )


@pytest.mark.parametrize(
    "state,generation,container",
    [
        ("prepared", 1, "a"),
        ("active", 1, "a"),
        ("prepared", 2, "a"),
        ("prepared", 1, "b"),
    ],
)
def test_resolver_requires_exact_prepared_lifecycle_record(
    database: Any, tmp_path: Path, state: str, generation: int, container: str
) -> None:
    secrets = ExternalBridgeSecretStore(
        tmp_path / "secrets", master_key=b"x" * 32, key_version=1
    )
    reference = secrets.write(INSTANCE_ID, "request_authentication", b"y" * 32)
    with database.begin() as session:
        session.add(
            RuntimeContainerArtifactRow(
                runtime_revision_id=str(REVISION_ID), image_id="sha256:" + "b" * 64
            )
        )
        session.add(
            ManagedBridgeTrustRow(
                target_id=str(INSTANCE_ID),
                runtime_revision_id=str(REVISION_ID),
                container_id="a" * 64,
                bridge_id=str(uuid4()) if state == "active" else None,
                bridge_port=8765,
                secret_reference=str(reference),
                identity_json="{}" if state == "active" else None,
                state=state,
                generation=1,
                prepared_at="2026-10-07T00:00:00.000000Z",
                established_at="2026-10-07T00:00:00.000000Z"
                if state == "active"
                else None,
            )
        )
    resolver = SqliteManagedBridgeTrustResolver(database, secrets)
    if (state, generation, container) == ("prepared", 1, "a"):
        result = resolver.resolve(str(INSTANCE_ID), container * 64, generation)
        assert result.secret == b"y" * 32 and "yyyy" not in repr(result)
    else:
        with pytest.raises(ManagedBridgeProvisionError):
            resolver.resolve(str(INSTANCE_ID), container * 64, generation)


def local_program(tmp_path: Path) -> tuple[str, Path, dict[str, Any]]:
    """Only substitute sandbox root in a test; production has no path parameter."""
    home = tmp_path / "kodi/.kodi"
    home.mkdir(parents=True)
    addon = home / "addons/service.kodi_lab_bridge"
    addon.mkdir(parents=True)
    root = Path(__file__).parents[2] / "bridge/service.kodi_lab_bridge"
    approved = approved_bridge_build()
    for relative in approved["files"]:
        path = addon / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((root / relative).read_bytes())
    program = (
        ESTABLISH_PROGRAM.replace('"/home/kodi/.kodi"', repr(str(home)))
        .replace('directory("/home")', "directory(" + repr(str(tmp_path)) + ")")
        .replace('directory("/home/kodi")', "directory(" + repr(str(home.parent)) + ")")
    )
    value = {
        "target_id": str(INSTANCE_ID),
        "runtime_revision_id": str(REVISION_ID),
        "container_id": "a" * 64,
        "generation": 1,
        "secret": base64.urlsafe_b64encode(b"x" * 32).decode(),
        "approved": approved,
    }
    return program, home, value


def invoke(program: str, value: dict[str, Any]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", program],
        input=json.dumps(value) + "\n",
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )


def test_fixed_local_program_preserves_uuid_no_otp_and_private_state(
    tmp_path: Path,
) -> None:
    program, home, value = local_program(tmp_path)
    first = invoke(program, value)
    assert first.returncode == 0, first.stderr
    bridge = json.loads(first.stdout)["bridge_id"]
    path = home / "userdata/addon_data/service.kodi_lab_bridge/state.json"
    state = json.loads(path.read_text())
    assert state["target_id"] == str(INSTANCE_ID) and str(UUID(bridge)) == bridge
    assert len(state["secret"]) == 43 and "=" not in state["secret"]
    assert path.stat().st_mode & 0o777 == 0o600
    assert value["secret"] not in first.stdout and "otp" not in state
    assert json.loads(invoke(program, value).stdout)["bridge_id"] == bridge
    value["target_id"] = str(uuid4())
    assert invoke(program, value).returncode != 0
    assert json.loads(path.read_text())["target_id"] == str(INSTANCE_ID)


@pytest.mark.parametrize("change", ["code", "symlink", "malformed"])
def test_fixed_program_rejects_unapproved_or_unsafe_state(
    tmp_path: Path, change: str
) -> None:
    program, home, value = local_program(tmp_path)
    if change == "code":
        (home / "addons/service.kodi_lab_bridge/service.py").write_text("unapproved")
    else:
        root = home / "userdata/addon_data/service.kodi_lab_bridge"
        root.mkdir(parents=True)
        if change == "symlink":
            elsewhere = tmp_path / "elsewhere"
            elsewhere.write_text("preserve")
            (root / "state.json").symlink_to(elsewhere)
        else:
            (root / "state.json").write_text("{}")
    result = invoke(program, value)
    assert (
        result.returncode != 0 and value["secret"] not in result.stdout + result.stderr
    )


def test_missing_secret_cannot_be_resolved(database: Any, tmp_path: Path) -> None:
    secrets = ExternalBridgeSecretStore(
        tmp_path / "secrets", master_key=b"x" * 32, key_version=1
    )
    reference = secrets.write(INSTANCE_ID, "request_authentication", b"x" * 32)
    with database.begin() as session:
        session.add(
            RuntimeContainerArtifactRow(
                runtime_revision_id=str(REVISION_ID), image_id="sha256:" + "b" * 64
            )
        )
        session.add(
            ManagedBridgeTrustRow(
                target_id=str(INSTANCE_ID),
                runtime_revision_id=str(REVISION_ID),
                container_id="a" * 64,
                bridge_port=8765,
                secret_reference=str(reference),
                state="prepared",
                generation=1,
                prepared_at="2026-10-07T00:00:00.000000Z",
            )
        )
    secrets.retire(reference)
    with pytest.raises(SecretStoreError):
        SqliteManagedBridgeTrustResolver(database, secrets).resolve(
            str(INSTANCE_ID), "a" * 64, 1
        )


@pytest.mark.parametrize("drift", ["container", "revision", "image", "network"])
def test_untrusted_runtime_refused_before_credential_attachment(drift: str) -> None:
    from kodi_lab.broker.managed_bridge_trust import (
        DockerManagedBridgeTrustProvider,
        ManagedBridgeProvisionSpec,
    )
    from tests.broker.test_provision_managed_container import (
        CONTAINER_ID,
        FakeProvisioningDockerClient,
        container_data,
        spec,
    )

    inspected: Any = container_data(state="running")
    if drift == "revision":
        inspected["Config"]["Labels"]["io.kodi-lab.runtime-revision-id"] = str(uuid4())

    class Docker(FakeProvisioningDockerClient):
        executed = False

        def exec_create(self, *a: Any, **kw: Any) -> Any:
            self.executed = True
            raise AssertionError("Credential must never reach untrusted runtime")

    client = Docker(
        owned=[{"Id": "2" * 64 if drift == "container" else CONTAINER_ID}],
        inspections=[inspected],
    )
    if drift == "image":
        client.image = {"Id": "sha256:" + "f" * 64, "Config": {}}
    if drift == "network":
        client.network = {}
    with pytest.raises(ManagedBridgeProvisionError):
        DockerManagedBridgeTrustProvider(lambda: client).establish(
            ManagedBridgeProvisionSpec(spec(), CONTAINER_ID, 1, b"x" * 32)
        )
    assert client.executed is False


def test_partial_publication_removes_private_staging_material(tmp_path: Path) -> None:
    program, home, value = local_program(tmp_path)
    program = program.replace(
        "\ntry:\n    run()",
        "\noriginal_replace = os.replace\ndef fail_state(source, target):\n"
        '    if target.endswith("state.json"):\n'
        '        raise OSError("publication failure")\n'
        "    return original_replace(source, target)\n"
        "os.replace = fail_state\ntry:\n    run()",
    )
    result = invoke(program, value)
    assert result.returncode != 0
    assert not list(home.rglob("*.managed-*"))
    assert value["secret"] not in result.stdout + result.stderr
    assert not (
        home / "userdata/addon_data/service.kodi_lab_bridge/state.json"
    ).exists()


@pytest.mark.parametrize(
    "prior,release,archive_sha",
    [
        (
            "1.1.23",
            "21.3",
            "a38c81d5198cbbb5dc3fbdb0453190102ddf5888d1e48666b9e9ef7bd4406533",
        ),
        (
            "1.1.25",
            "20.5",
            "7e8a798c75ba042c96eaa9e8f049da9c038e3b4f78c617d52b1b87aa2b2f6f63",
        ),
        (
            "1.1.26",
            "22.0-rc.1",
            "77f68a85de430d75df9dc9504a9abb5086878778655a3016c39a73dd35bd07d7",
        ),
    ],
)
def test_exact_previous_reviewed_code_upgrade_is_atomic_and_retains_old_tree(
    tmp_path: Path,
    prior: str,
    release: str,
    archive_sha: str,
) -> None:
    import shutil
    import zipfile
    from importlib.resources import files

    from kodi_lab.kodi.rc1_bridge import (
        packaged_rc1_bridge_sources,
        packaged_stable_bridge_sources,
    )

    program, home, value = local_program(tmp_path)
    addon = home / "addons/service.kodi_lab_bridge"
    shutil.rmtree(addon)
    archive = Path(__file__).parents[1] / f"fixtures/reviewed-bridge-{prior}.zip"
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == archive_sha
    with zipfile.ZipFile(archive) as package:
        package.extractall(home / "addons")
        old_files = {
            name.removeprefix("service.kodi_lab_bridge/"): package.read(name)
            for name in package.namelist()
            if not name.endswith("/")
        }
    sources = (
        packaged_rc1_bridge_sources()
        if release == "22.0-rc.1"
        else packaged_stable_bridge_sources()
    )
    value["approved"] = approved_bridge_build(release)
    value["reviewed_sources"] = {
        name: base64.b64encode(data).decode() for name, data in sources.items()
    }
    value["reviewed_inventory"] = {
        name: hashlib.sha256(data).hexdigest() for name, data in sources.items()
    }
    value["previous_inventories"] = json.loads(
        files("kodi_lab.kodi")
        .joinpath("managed_bridge_previous_inventories.json")
        .read_text()
    )
    first = invoke(program, value)
    assert first.returncode == 0, first.stderr
    backups = list((home / "userdata/managed-bridge-upgrades").iterdir())
    assert len(backups) == 1
    assert {
        str(path.relative_to(backups[0])): path.read_bytes()
        for path in backups[0].rglob("*")
        if path.is_file()
    } == old_files
    assert f'version="{prior}"' in (backups[0] / "addon.xml").read_text()
    assert (
        f'version="{value["approved"]["addon_version"]}"'
        in (addon / "addon.xml").read_text()
    )
    marker = json.loads(
        (
            home / "userdata/addon_data/service.kodi_lab_bridge/managed-launch.json"
        ).read_text()
    )
    assert marker["binding"]["container_id"] == value["container_id"]
    assert invoke(program, value).returncode == 0
    assert len(list((home / "userdata/managed-bridge-upgrades").iterdir())) == 1


def test_reviewed_upgrade_rejects_drift_without_overwriting_any_tree(
    tmp_path: Path,
) -> None:
    from kodi_lab.kodi.rc1_bridge import packaged_stable_bridge_sources

    program, home, value = local_program(tmp_path)
    addon = home / "addons/service.kodi_lab_bridge"
    (addon / "narrow_bridge.py").write_text("unreviewed old code")
    value["reviewed_sources"] = {
        name: base64.b64encode(data).decode()
        for name, data in packaged_stable_bridge_sources().items()
    }
    value["previous_inventories"] = {}
    assert invoke(program, value).returncode != 0
    assert (addon / "narrow_bridge.py").read_text() == "unreviewed old code"
    assert not (home / "userdata/managed-bridge-upgrades").exists()


def test_broker_marker_is_accepted_by_the_actual_resident_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.kodi.test_narrow_bridge_addon import _load

    program, home, value = local_program(tmp_path)
    assert invoke(program, value).returncode == 0
    state_path = home / "userdata/addon_data/service.kodi_lab_bridge/state.json"
    resident = _load(monkeypatch, state_path=state_path)
    binding = resident["_managed_launch_marker"](json.loads(state_path.read_text()))
    assert binding["target_id"] == value["target_id"]
    assert binding["container_id"] == value["container_id"]
    assert binding["generation"] == value["generation"]
