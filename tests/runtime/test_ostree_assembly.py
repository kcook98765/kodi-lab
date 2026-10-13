"""Offline Docker contract/failure tests. Live acceptance is a separate gate."""

import io
import json
import tarfile
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import docker  # type: ignore[import-untyped]
import pytest

from kodi_lab.runtime import ostree_assembly as assembly
from kodi_lab.runtime.ostree import OstreeAcquisitionError
from kodi_lab.runtime.ubuntu_support import BASE_IMAGE, load_support_closure
from tests.runtime.test_ostree_context import acquirer as acquirer


class FakeDocker:
    def __init__(self, operation: UUID, labels: dict[str, str]) -> None:
        self.operation = operation
        self.labels = labels
        self.calls: list[dict[str, Any]] = []
        self.removed: list[str] = []
        self.failure: str | None = None
        self.built = False
        self.stream_closed = False
        self.image: dict[str, Any] = {
            "Id": "sha256:" + "1" * 64,
            "Os": "linux",
            "Architecture": "amd64",
            "Size": 2000000000,
            "Config": {
                "User": "1000:1000",
                "Volumes": None,
                "ExposedPorts": None,
                "Entrypoint": [
                    "/usr/bin/python3",
                    "/usr/local/lib/kodi-lab/supervisor.py",
                ],
                "Cmd": [],
                "Labels": labels,
                "Env": [
                    "HOME=/home/kodi",
                    "DISPLAY=:99",
                    "XDG_RUNTIME_DIR=/run/kodi",
                    "LIBGL_ALWAYS_SOFTWARE=1",
                    "MESA_LOADER_DRIVER_OVERRIDE=llvmpipe",
                    "GALLIUM_DRIVER=llvmpipe",
                    "PULSE_SERVER=none",
                ],
                "Healthcheck": {
                    "Test": [
                        "CMD",
                        "/usr/bin/python3",
                        "-I",
                        "/usr/local/lib/kodi-lab/healthcheck.py",
                    ]
                },
            },
        }
        self.api = SimpleNamespace(inspect_image=self.inspect, build=self.build)
        self.images = SimpleNamespace(
            pull=lambda *a, **kw: None,
            remove=lambda tag, **kw: self.removed.append(tag),
        )
        self.containers = SimpleNamespace(create=self.create)

    def inspect(self, tag: str) -> dict[str, Any]:
        if tag == BASE_IMAGE:
            return {
                "Os": "linux",
                "Architecture": "arm64" if self.failure == "base" else "amd64",
                "RepoDigests": [BASE_IMAGE],
            }
        if not self.built:
            raise docker.errors.ImageNotFound("absent")
        return self.image

    def build(self, **kwargs: Any):  # type: ignore[no-untyped-def]
        self.calls.append(kwargs)
        try:
            if self.failure == "interrupt":
                raise KeyboardInterrupt
            if self.failure == "build":
                yield {"error": "failed"}
                return
            self.built = True
            yield {"stream": "success\n"}
        finally:
            self.stream_closed = True

    def create(self, *args: Any, **kwargs: Any) -> Any:
        assert kwargs["network_mode"] == "none" and kwargs["cap_drop"] == ["ALL"]

        class Container:
            id = "owned-inspection-container"

            def get_archive(self, path: str):  # type: ignore[no-untyped-def]
                raw = "".join(
                    "\t".join(row) + "\n"
                    for row in load_support_closure().final_inventory
                ).encode()
                if self_outer.failure == "dependency":
                    raw += b"unreviewed\t1\tamd64\n"
                buf = io.BytesIO()
                with tarfile.open(fileobj=buf, mode="w") as t:
                    m = tarfile.TarInfo("installed-packages.tsv")
                    m.size = len(raw)
                    t.addfile(m, io.BytesIO(raw))
                return iter([buf.getvalue()]), {}

            def remove(self, **kw: Any) -> None:
                if self_outer.failure == "cleanup":
                    raise RuntimeError("cleanup failed")

        self_outer = self
        return Container()


def client_for(acquired: Any, operation: UUID) -> FakeDocker:
    import hashlib

    return FakeDocker(
        operation,
        {
            "io.kodi-lab.operation-id": operation.hex,
            "io.kodi-lab.experimental": "true",
            "io.kodi-lab.recipe-digest": acquired.evidence.recipe_digest,
            "io.kodi-lab.acquisition-digest": "sha256:"
            + hashlib.sha256(acquired.evidence.canonical_bytes()).hexdigest(),
            "io.kodi-lab.support-digest": load_support_closure().digest,
            "io.kodi-lab.assets-digest": "sha256:" + assembly.ASSETS_SHA256,
            "io.kodi-lab.installation-status": "inactive",
            "org.opencontainers.image.revision": acquired.evidence.kodi_source_revision,
            "org.opencontainers.image.version": "22.0-rc.1",
        },
    )


@pytest.fixture
def support_double(monkeypatch: pytest.MonkeyPatch) -> None:
    # Only assembly flow is doubled; support integrity has its own real/live gate.
    monkeypatch.setattr(assembly, "download_support", lambda *args: {})
    monkeypatch.setattr(assembly, "validate_support_entries", lambda *args: None)


def test_offline_exact_assembly_evidence(acquirer: Any, support_double: None) -> None:
    operation = uuid4()
    with acquirer.acquire(operation) as acquired:
        client = client_for(acquired, operation)
        evidence = assembly.assemble_ostree_image(
            acquired, operation_id=operation, client=client
        )
        assert evidence.canonical_bytes() == evidence.canonical_bytes()
        doc = json.loads(evidence.canonical_bytes())
        assert (
            doc["installation_status"] == "inactive"
            and doc["production_approval"] is False
        )
        assert doc["kodi_release"] == "22.0-rc.1" and len(doc["input_components"]) == 3
        call = client.calls[0]
        assert call["network_mode"] == "none" and call["platform"] == "linux/amd64"
        assert (
            call["pull"] is False
            and call["nocache"] is True
            and call["forcerm"] is True
        )
        assert call["fileobj"].closed and client.stream_closed and not client.removed


@pytest.mark.parametrize(
    "failure", ["base", "build", "interrupt", "dependency", "cleanup"]
)
def test_assembly_failure_cleanup(
    failure: str, acquirer: Any, support_double: None
) -> None:
    operation = uuid4()
    with (
        pytest.raises(
            KeyboardInterrupt if failure == "interrupt" else OstreeAcquisitionError
        ),
        acquirer.acquire(operation) as acquired,
    ):
        client = client_for(acquired, operation)
        client.failure = failure
        assembly.assemble_ostree_image(acquired, operation_id=operation, client=client)
    assert not acquired.stage.exists()
    if client.calls:
        assert client.calls[0]["fileobj"].closed and client.stream_closed
    if client.built:
        assert len(client.removed) == 1


@pytest.mark.parametrize(
    "kind",
    [
        "architecture",
        "user",
        "profile",
        "entrypoint",
        "volume",
        "ports",
        "health",
        "rc2",
    ],
)
def test_bad_final_image_rejected(
    kind: str, acquirer: Any, support_double: None
) -> None:
    operation = uuid4()
    with acquirer.acquire(operation) as acquired:
        client = client_for(acquired, operation)
        config = client.image["Config"]
        if kind == "architecture":
            client.image["Architecture"] = "arm64"
        elif kind == "user":
            config["User"] = "0:0"
        elif kind == "profile":
            config["Env"].remove("HOME=/home/kodi")
        elif kind == "entrypoint":
            config["Entrypoint"] = ["/bin/sh"]
        elif kind == "volume":
            config["Volumes"] = {"/home/kodi": {}}
        elif kind == "ports":
            config["ExposedPorts"] = {"8080/tcp": {}}
        elif kind == "health":
            config["Healthcheck"] = {"Test": ["NONE"]}
        elif kind == "rc2":
            config["Labels"]["org.opencontainers.image.version"] = "22.0-rc.2"
        with pytest.raises(
            assembly.OstreeAssemblyError, match="final_image_profile_mismatch"
        ):
            assembly.assemble_ostree_image(
                acquired, operation_id=operation, client=client
            )
        assert len(client.removed) == 1


def test_preexisting_operation_image_is_preserved(
    acquirer: Any, support_double: None
) -> None:
    operation = uuid4()
    with acquirer.acquire(operation) as acquired:
        client = client_for(acquired, operation)
        client.built = True
        with pytest.raises(assembly.OstreeAssemblyError, match="already_exists"):
            assembly.assemble_ostree_image(
                acquired, operation_id=operation, client=client
            )
        assert not client.calls and not client.removed


def test_asset_inventory_and_symlink_layout() -> None:
    assets = assembly.load_assembly_assets()
    dockerfile = assets["Dockerfile"].decode()
    assert "test ! -e /opt/fdo/lib/x86_64-linux-gnu/addons" in dockerfile
    assert "test ! -L /opt/fdo/lib/x86_64-linux-gnu/addons" in dockerfile
    assert (
        "ln -s /app/lib/kodi/addons /opt/fdo/lib/x86_64-linux-gnu/addons" in dockerfile
    )
    assert "FROM scratch" in dockerfile and "apt-get update" not in dockerfile
    assert "PYTHONHOME=/opt/fdo" in assets["launcher.sh"].decode()
    assert (
        "/opt/fdo/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2"
        in assets["launcher.sh"].decode()
    )


def test_altered_assets_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(assembly, "ASSETS_SHA256", "0" * 64)
    with pytest.raises(
        assembly.OstreeAssemblyError, match="assembly_manifest_mismatch"
    ):
        assembly.load_assembly_assets()


@pytest.mark.parametrize("kind", ["loader", "symlink-destination", "extra"])
def test_unreviewed_image_assets_fail_closed(
    kind: str, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hashlib
    from importlib import resources

    assets = assembly.load_assembly_assets()
    root = tmp_path / "assembly_assets/piers-22.0-rc1-r1"
    root.mkdir(parents=True)
    manifest = {
        name: hashlib.sha256(payload).hexdigest() for name, payload in assets.items()
    }
    if kind == "loader":
        assets["launcher.sh"] = assets["launcher.sh"].replace(
            b"/opt/fdo/", b"/untrusted/"
        )
    if kind == "symlink-destination":
        assets["Dockerfile"] = assets["Dockerfile"].replace(
            b"ln -s /app/lib/kodi/addons", b"ln -s /etc"
        )
    if kind == "extra":
        manifest["unreviewed.sh"] = "0" * 64
    raw = json.dumps(manifest, sort_keys=True).encode()
    (root / "assembly-assets.json").write_bytes(raw)
    for name, payload in assets.items():
        (root / name).write_bytes(payload)
    monkeypatch.setattr(assembly, "ASSETS_SHA256", hashlib.sha256(raw).hexdigest())
    monkeypatch.setattr(resources, "files", lambda _: tmp_path)
    with pytest.raises(assembly.OstreeAssemblyError):
        assembly.load_assembly_assets()


def test_resource_identifiers_are_reported_before_inspection_cleanup(
    acquirer: Any, support_double: None
) -> None:
    operation = uuid4()
    with acquirer.acquire(operation) as acquired:
        client = client_for(acquired, operation)
        resources = []
        result = assembly.assemble_ostree_image(
            acquired,
            operation_id=operation,
            client=client,
            on_resource=lambda kind, identity: resources.append((kind, identity)),
        )
        assert resources == [
            ("image", result.image_id),
            ("container", "owned-inspection-container"),
        ]


def test_build_timeout_closes_context_and_removes_only_owned_image(
    acquirer: Any, support_double: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    operation = uuid4()
    monkeypatch.setattr(assembly, "_MAX_BUILD_SECONDS", -1)
    with acquirer.acquire(operation) as acquired:
        client = client_for(acquired, operation)
        with pytest.raises(assembly.OstreeAssemblyError, match="assembly_timeout"):
            assembly.assemble_ostree_image(
                acquired, operation_id=operation, client=client
            )
        assert client.stream_closed and client.calls[0]["fileobj"].closed
        assert len(client.removed) == 1
