"""Fixed local credential establishment for an exact owned managed container.

The broker accepts only target/container/generation identities. It resolves the
prepared credential reference itself; no secret, path or program is a parameter.
"""

from __future__ import annotations

import base64
import json
import os
import stat
import struct
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib.resources import files
from typing import Any, cast
from uuid import UUID

import docker  # type: ignore[import-untyped]
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.broker.docker_inventory import DockerManagedContainerAdapter
from kodi_lab.broker.provisioning import (
    ManagedContainerProvisioningSpec,
    SqliteProvisioningResolver,
)
from kodi_lab.config import Settings
from kodi_lab.domain import (
    ManagedInstanceLifecycleKind,
    ManagedInstanceLifecycleOperationState,
    ManagedInstanceMutationKind,
    TargetType,
)
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.storage.models import (
    ManagedBridgeTrustRow,
    ManagedInstanceLifecycleOperationRow,
    ManagedInstanceMutationLeaseRow,
    TargetRow,
)
from kodi_lab.storage.secrets import ExternalBridgeSecretStore, decode_master_key


class ManagedBridgeProvisionError(RuntimeError):
    """Only a bounded content-free failure crosses the broker boundary."""


def approved_bridge_build(
    kodi_release: str | None = None, *, previous: bool = False, retained: bool = False
) -> dict[str, Any]:
    if previous and retained:
        raise ValueError("ambiguous Bridge build selection")
    resource = (
        "managed_bridge_preview_build.json"
        if kodi_release == "22.0-rc.1"
        else "managed_bridge_build.json"
    )
    if previous:
        resource = (
            "managed_bridge_previous_preview_build.json"
            if kodi_release == "22.0-rc.1"
            else "managed_bridge_previous_build.json"
        )
    if retained:
        resource = (
            "managed_bridge_retained_preview_build.json"
            if kodi_release == "22.0-rc.1"
            else "managed_bridge_retained_build.json"
        )
    return cast(
        dict[str, Any],
        json.loads(files("kodi_lab.kodi").joinpath(resource).read_text()),
    )


@dataclass(frozen=True)
class ManagedBridgeProvisionSpec:
    provisioning: ManagedContainerProvisioningSpec
    container_id: str
    generation: int
    secret: bytes = field(repr=False)
    kodi_release: str | None = None
    install_reviewed: bool = False


def require_reviewed_installation_lease(session: Session, target_id: str) -> None:
    """Candidate materialization belongs to one live, typed lifecycle owner."""
    lease = session.get(ManagedInstanceMutationLeaseRow, target_id)
    operation = (
        session.get(ManagedInstanceLifecycleOperationRow, lease.operation_id)
        if lease
        else None
    )
    if (
        lease is None
        or lease.operation_kind is not ManagedInstanceMutationKind.LIFECYCLE
        or not lease.lease_token_digest
        or not lease.lease_expires_at
        or datetime.fromisoformat(lease.lease_expires_at) <= datetime.now(UTC)
        or operation is None
        or operation.managed_instance_id != target_id
        or operation.kind is not ManagedInstanceLifecycleKind.START
        or operation.state is not ManagedInstanceLifecycleOperationState.RUNNING
    ):
        raise ManagedBridgeProvisionError("managed_bridge_installation_not_owned")


class SqliteManagedBridgeTrustResolver:
    def __init__(
        self, sessions: sessionmaker[Session], secrets: ExternalBridgeSecretStore
    ) -> None:
        self.sessions, self.secrets = sessions, secrets

    @classmethod
    def production(cls) -> SqliteManagedBridgeTrustResolver:
        settings = Settings()
        key_text = settings.external_target_master_key
        if key_text is None:
            descriptor = os.open(
                settings.data_root / "master-key", os.O_RDONLY | os.O_NOFOLLOW
            )
            try:
                metadata = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or stat.S_IMODE(metadata.st_mode) != 0o600
                    or metadata.st_size > 128
                ):
                    raise ManagedBridgeProvisionError(
                        "managed_bridge_establishment_failed"
                    )
                key = decode_master_key(
                    os.read(descriptor, 129).decode("ascii").strip()
                )
            finally:
                os.close(descriptor)
        else:
            key = decode_master_key(key_text.get_secret_value())
        return cls(
            create_session_factory(create_database_engine(settings.database_url)),
            ExternalBridgeSecretStore(
                settings.data_root / "secrets/external-bridges",
                master_key=key,
                key_version=settings.external_target_master_key_version,
            ),
        )

    def resolve(
        self,
        target_id: str,
        container_id: str,
        generation: int,
        *,
        install_reviewed: bool = False,
    ) -> ManagedBridgeProvisionSpec:
        provisioning = SqliteProvisioningResolver(self.sessions).resolve(target_id)
        with self.sessions() as session:
            row = session.get(ManagedBridgeTrustRow, target_id)
            target = session.get(TargetRow, target_id)
            if (
                target is None
                or target.target_type is not TargetType.MANAGED
                or row is None
                or row.state != "prepared"
                or row.generation != generation
                or row.container_id != container_id
                or row.runtime_revision_id != provisioning.runtime_revision_id
                or row.secret_reference is None
            ):
                raise ManagedBridgeProvisionError("managed_bridge_trust_not_prepared")
            secret = self.secrets.read(
                UUID(target_id), UUID(row.secret_reference), "request_authentication"
            )
            if install_reviewed:
                if target.kodi_version != "22.0-rc.1":
                    raise ManagedBridgeProvisionError(
                        "managed_bridge_identity_mismatch"
                    )
                require_reviewed_installation_lease(session, target_id)
        return ManagedBridgeProvisionSpec(
            provisioning,
            container_id,
            generation,
            secret,
            target.kodi_version,
            install_reviewed,
        )


# Repository-owned program, not caller code. Secret input travels over the
# Docker attachment's stdin, never in exec arguments, output or audit fields.
ESTABLISH_PROGRAM = r"""
import base64, fcntl, hashlib, json, os, stat, sys, uuid, xml.etree.ElementTree as ET


def run():
    value = json.loads(sys.stdin.buffer.readline(1048577))
    target = str(uuid.UUID(value["target_id"]))
    assert (
        target == value["target_id"]
        and len(base64.urlsafe_b64decode(
            value["secret"] + "=" * (-len(value["secret"]) % 4)
        )) == 32
    )
    home = "/home/kodi/.kodi"

    def directory(path):
        if not os.path.exists(path):
            os.mkdir(path, 0o700)
        m = os.lstat(path)
        assert stat.S_ISDIR(m.st_mode) and not stat.S_ISLNK(m.st_mode)

    directory("/home")
    directory("/home/kodi")
    directory(home)
    addon = home + "/addons/service.kodi_lab_bridge"
    sources = value.get("reviewed_sources")
    if sources is not None:
        # Only broker-owned reviewed bytes arrive here. No destination parameter.
        assert value["approved"]["addon_version"] == "1.1.24"
        directory(home + "/addons")
        lock = os.open(
            home + "/addons/.reviewed-bridge.lock",
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
        )
        try:
            assert stat.S_ISREG(os.fstat(lock).st_mode)
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if os.path.lexists(addon):
                directory(addon)
                # Existing code cannot be overwritten or adopted after drift.
                for relative, encoded in sources.items():
                    path = addon
                    for part in relative.split("/")[:-1]:
                        path += "/" + part
                        directory(path)
                    fd = os.open(addon + "/" + relative, os.O_RDONLY | os.O_NOFOLLOW)
                    try:
                        assert stat.S_ISREG(os.fstat(fd).st_mode)
                        assert os.read(fd, 1048577) == base64.b64decode(
                            encoded, validate=True
                        )
                    finally:
                        os.close(fd)
            else:
                temporary = home + "/addons/.reviewed-bridge-" + str(uuid.uuid4())
                os.mkdir(temporary, 0o700)
                try:
                    for relative, encoded in sources.items():
                        assert not relative.startswith("/") and all(
                            p not in ("", ".", "..")
                            for p in relative.split("/")
                        )
                        current = temporary
                        for part in relative.split("/")[:-1]:
                            current += "/" + part
                            directory(current)
                        data = base64.b64decode(encoded, validate=True)
                        assert len(data) <= 1048576
                        fd = os.open(
                            temporary + "/" + relative,
                            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                            0o644
                        )
                        try:
                            with os.fdopen(fd, "wb", closefd=False) as out:
                                out.write(data)
                                out.flush()
                                os.fsync(fd)
                        finally:
                            os.close(fd)
                    assert not os.path.lexists(addon)
                    os.rename(temporary, addon)
                finally:
                    if os.path.exists(temporary):
                        import shutil
                        shutil.rmtree(temporary)
        finally:
            os.close(lock)
    inventory = value.get("reviewed_inventory")
    if inventory is not None:
        directory(addon)
        seen = set()
        for root, directories, filenames in os.walk(addon, followlinks=False):
            for name in directories:
                directory(root + "/" + name)
            for name in filenames:
                path = root + "/" + name
                relative = os.path.relpath(path, addon)
                assert relative in inventory
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                try:
                    assert stat.S_ISREG(os.fstat(fd).st_mode)
                    data = os.read(fd, 1048577)
                    assert hashlib.sha256(data).hexdigest() == inventory[relative]
                finally:
                    os.close(fd)
                seen.add(relative)
        assert seen == set(inventory)
    for relative, digest in value["approved"]["files"].items():
        path = addon + "/" + relative
        current = addon
        directory(current)
        for part in relative.split("/")[:-1]:
            current += "/" + part
            directory(current)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            assert stat.S_ISREG(os.fstat(fd).st_mode)
            data = b""
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                data += chunk
                assert len(data) <= 1048576
            assert hashlib.sha256(data).hexdigest() == digest
        finally:
            os.close(fd)
    root = home + "/userdata"
    directory(root)
    directory(root + "/addon_data")
    directory(root + "/addon_data/service.kodi_lab_bridge")
    root += "/addon_data/service.kodi_lab_bridge"

    def read(path):
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return None
        try:
            assert stat.S_ISREG(os.fstat(fd).st_mode)
            data = os.read(fd, 65537)
            assert len(data) <= 65536
            return data
        finally:
            os.close(fd)

    fd = os.open(
        root + "/state.json.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
    )
    try:
        assert stat.S_ISREG(os.fstat(fd).st_mode)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        encoded = read(root + "/state.json")
        state = (
            json.loads(encoded)
            if encoded
            else dict(
                format=2,
                bridge_id=str(uuid.uuid4()),
                target_id=None,
                secret=None,
                replay=[],
                deployments={},
                rollback=None,
            )
        )
        assert state.get("format") == 2 and set(state) == {
            "format",
            "bridge_id",
            "target_id",
            "secret",
            "replay",
            "deployments",
            "rollback",
        }
        bridge_id = str(uuid.UUID(state["bridge_id"]))
        assert bridge_id == state["bridge_id"]
        assert state["target_id"] in (None, target)
        assert isinstance(state["replay"], list) and isinstance(
            state["deployments"], dict
        )
        state.update(target_id=target, secret=value["secret"].rstrip("="), replay=[])

        def publish(path, data):
            # Reject existing special files before replacing, and use exclusive
            # same-directory staging with a private, unpredictable name.
            read(path)
            temporary = path + ".managed-" + str(uuid.uuid4())
            d = os.open(
                temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
            try:
                try:
                    with os.fdopen(d, "wb", closefd=False) as out:
                        out.write(data)
                        out.flush()
                        os.fsync(d)
                finally:
                    os.close(d)
                os.replace(temporary, path)
            finally:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass

        old = read(root + "/settings.xml")
        settings = ET.fromstring(old) if old else ET.Element("settings", version="2")
        for name, text in [("enabled", "true"), ("bridge_port", "8765")]:
            element = settings.find("setting[@id='%s']" % name)
            if element is None:
                element = ET.SubElement(settings, "setting", id=name)
            element.text = text
            element.attrib.pop("default", None)
        publish(root + "/settings.xml", ET.tostring(settings, encoding="utf-8"))
        publish(
            root + "/state.json",
            json.dumps(state, sort_keys=True, separators=(",", ":")).encode(),
        )
        d = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(d)
        finally:
            os.close(d)
    finally:
        os.close(fd)
    print(json.dumps({"bridge_id": bridge_id}), flush=True)


try:
    run()
except Exception:
    print("managed_bridge_establishment_failed", flush=True)
    sys.exit(1)
"""


class DockerManagedBridgeTrustProvider:
    def __init__(self, client_factory: Any = None) -> None:
        self._factory = client_factory or (
            lambda: docker.APIClient(version="auto", timeout=5)
        )

    def establish(self, spec: ManagedBridgeProvisionSpec) -> str:
        inventory = None
        sources = None
        if spec.kodi_release == "22.0-rc.1":
            import hashlib

            from kodi_lab.kodi.rc1_bridge import packaged_rc1_bridge_sources

            reviewed = packaged_rc1_bridge_sources()
            inventory = {
                name: hashlib.sha256(data).hexdigest()
                for name, data in reviewed.items()
            }
            if spec.install_reviewed:
                sources = {
                    name: base64.b64encode(data).decode("ascii")
                    for name, data in reviewed.items()
                }
        elif spec.install_reviewed:
            raise ManagedBridgeProvisionError("managed_bridge_identity_mismatch")
        client = self._factory()
        attachment = None
        try:
            expected = spec.provisioning
            DockerManagedContainerAdapter._validate_managed_network(client)
            image = client.inspect_image(expected.image_id)
            if image.get("Id") != expected.image_id:
                raise ManagedBridgeProvisionError("managed_bridge_identity_mismatch")
            image_config = image["Config"]
            summaries = client.containers(
                all=True,
                filters={
                    "label": [
                        "io.kodi-lab.managed=true",
                        f"io.kodi-lab.instance-id={expected.managed_instance_id}",
                    ]
                },
            )
            if len(summaries) != 1 or summaries[0]["Id"] != spec.container_id:
                raise ManagedBridgeProvisionError("managed_bridge_identity_mismatch")

            def validate() -> None:
                inspected = client.inspect_container(spec.container_id)
                DockerManagedContainerAdapter._validate_provisioned_container(
                    inspected,
                    expected_container_id=spec.container_id,
                    spec=expected,
                    image_config=image_config,
                )
                if inspected["State"]["Status"] != "running":
                    raise ManagedBridgeProvisionError("managed_bridge_not_running")

            validate()
            created = client.exec_create(
                spec.container_id,
                ["python3", "-c", ESTABLISH_PROGRAM],
                stdin=True,
                stdout=True,
                stderr=False,
                tty=False,
                privileged=False,
                user=f"{expected.runtime_uid}:{expected.runtime_gid}",
            )
            attachment = client.exec_start(created["Id"], socket=True)
            transport = getattr(attachment, "_sock", attachment)
            transport.settimeout(5)
            transport.sendall(
                (
                    json.dumps(
                        {
                            "target_id": expected.managed_instance_id,
                            "secret": base64.urlsafe_b64encode(spec.secret)
                            .rstrip(b"=")
                            .decode(),
                            "approved": approved_bridge_build(spec.kodi_release),
                            **(
                                {"reviewed_inventory": inventory}
                                if inventory is not None
                                else {}
                            ),
                            **(
                                {"reviewed_sources": sources}
                                if sources is not None
                                else {}
                            ),
                        }
                    )
                    + "\n"
                ).encode()
            )

            deadline = time.monotonic() + 5

            def exact(count: int) -> bytes:
                data = b""
                while len(data) < count:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ManagedBridgeProvisionError(
                            "managed_bridge_establishment_failed"
                        )
                    transport.settimeout(remaining)
                    part = transport.recv(count - len(data))
                    if not part:
                        raise ManagedBridgeProvisionError(
                            "managed_bridge_establishment_failed"
                        )
                    data += part
                return data

            output = b""
            while b"\n" not in output:
                header = exact(8)
                size = struct.unpack(">I", header[4:])[0]
                if header[0] != 1 or size > 1024 or len(output) + size > 1024:
                    raise ManagedBridgeProvisionError(
                        "managed_bridge_establishment_failed"
                    )
                output += exact(size)
            result = json.loads(output)
            bridge_id = str(UUID(result["bridge_id"]))
            validate()
            return bridge_id
        except Exception as error:
            raise ManagedBridgeProvisionError(
                "managed_bridge_establishment_failed"
            ) from error
        finally:
            if attachment is not None:
                attachment.close()
            client.close()
