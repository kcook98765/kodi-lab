#!/usr/bin/env python3
"""Disposable reviewed-Omega abrupt-stop/normal-broker-Start qualification.

Run only with an explicitly built revision-2 Omega image ID. This script
creates one UUID-owned managed container and removes only that container.
"""

from __future__ import annotations

import argparse
import time
from uuid import uuid4

import docker
import httpx

from kodi_lab.broker.docker_inventory import (
    ContainerRuntimeState,
    DockerManagedContainerAdapter,
)
from kodi_lab.broker.provisioning import ManagedContainerProvisioningSpec
from kodi_lab.domain.runtime_catalog import PythonBytecodePolicy
from kodi_lab.runtime.recipes import load_reviewed_recipe


def _ready(container: object, *, timeout: float = 75) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        container.reload()  # type: ignore[attr-defined]
        state = container.attrs["State"]  # type: ignore[attr-defined]
        if state["Status"] != "running":
            raise RuntimeError(f"disposable container exited: {state['ExitCode']}")
        networks = container.attrs["NetworkSettings"]["Networks"]  # type: ignore[attr-defined]
        address = networks["kodi-lab-managed-v1"]["IPAddress"]
        if state.get("Health", {}).get("Status") == "healthy":
            try:
                reply = httpx.post(
                    f"http://{address}:8080/jsonrpc",
                    json={"jsonrpc": "2.0", "id": 1, "method": "JSONRPC.Ping"},
                    timeout=2,
                    trust_env=False,
                )
                if reply.status_code == 200 and reply.json().get("result") == "pong":
                    return address
            except (httpx.HTTPError, ValueError):
                pass
        time.sleep(0.5)
    raise RuntimeError("disposable Kodi did not reach JSON-RPC readiness")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("image_id")
    args = parser.parse_args()
    reviewed = load_reviewed_recipe("omega")
    assert reviewed.recipe.recipe_revision == 2
    client = docker.from_env(timeout=30)
    image = client.images.get(args.image_id)
    if (
        image.id != args.image_id
        or image.attrs["Config"]["Labels"].get("io.kodi-lab.recipe-digest")
        != reviewed.digest
    ):
        raise RuntimeError("image does not match current reviewed Omega recipe")
    instance_id, revision_id = uuid4(), uuid4()
    adapter = DockerManagedContainerAdapter()
    spec = ManagedContainerProvisioningSpec(
        managed_instance_id=str(instance_id),
        runtime_revision_id=str(revision_id),
        image_id=image.id,
        container_name=f"kodi-lab-managed-{instance_id.hex}",
        runtime_uid=1000,
        runtime_gid=1000,
        python_bytecode_policy=PythonBytecodePolicy.SUPPRESS,
    )
    owned_id: str | None = None
    try:
        provisioned = adapter.provision_managed_container(spec)
        assert provisioned.created
        owned_id = provisioned.container_id
        container = client.containers.get(owned_id)
        started = adapter.start_managed_container(
            managed_instance_id=str(instance_id),
            expected_container_id=owned_id,
            expected_runtime_state=ContainerRuntimeState.STOPPED,
        )
        assert started.start_performed and started.container_id == owned_id
        address = _ready(container)
        marker = b"gate3r-profile-preserved\n"
        assert (
            container.exec_run(
                [
                    "/bin/sh",
                    "-c",
                    "printf 'gate3r-profile-preserved\\n' > "
                    "/home/kodi/.kodi/gate3r-marker",
                ]
            ).exit_code
            == 0
        )
        container.kill(signal="SIGKILL")
        container.wait(timeout=30)
        container.reload()
        assert container.attrs["State"]["Status"] == "exited"
        changes = {item["Path"] for item in client.api.diff(owned_id)}
        assert "/tmp/.X99-lock" in changes
        assert "/tmp/.X11-unix/X99" in changes
        restarted = adapter.start_managed_container(
            managed_instance_id=str(instance_id),
            expected_container_id=owned_id,
            expected_runtime_state=ContainerRuntimeState.STOPPED,
        )
        assert restarted.start_performed and restarted.container_id == owned_id
        assert _ready(container) == address
        assert container.exec_run(["/usr/bin/pgrep", "-x", "Xvfb"]).exit_code == 0
        assert container.exec_run(["/usr/bin/pgrep", "-x", "kodi.bin"]).exit_code == 0
        archive, _ = container.get_archive("/home/kodi/.kodi/gate3r-marker")
        assert marker in b"".join(archive)
        logs = container.logs().decode(errors="replace")
        assert "removed stale display 99 artifact: /tmp/.X99-lock" in logs
        assert "removed stale display 99 artifact: /tmp/.X11-unix/X99" in logs
        print(
            f"PASS instance={instance_id} revision={revision_id} container={owned_id} "
            f"image={image.id} initial_and_restarted_jsonrpc=pong "
            "same_container=true profile_preserved=true"
        )
    finally:
        if owned_id is not None:
            container = client.containers.get(owned_id)
            container.reload()
            if container.attrs["State"]["Status"] == "running":
                container.stop(timeout=10)
            container.remove(v=False)
        client.close()


if __name__ == "__main__":
    main()
