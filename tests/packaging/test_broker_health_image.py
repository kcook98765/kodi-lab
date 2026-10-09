"""Opt-in isolated acceptance of the installed broker and its Docker watchdog."""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

import docker  # type: ignore[import-untyped]
import pytest

IMAGE = os.environ.get("KODI_LAB_TEST_BROKER_HEALTH_IMAGE")
EVIDENCE = os.environ.get("KODI_LAB_TEST_BROKER_HEALTH_EVIDENCE")
pytestmark = pytest.mark.skipif(not IMAGE, reason="requires the built broker image")


def _record(name: str, evidence: dict[str, Any]) -> None:
    if EVIDENCE:
        (Path(EVIDENCE) / name).write_text(json.dumps(evidence, indent=2) + "\n")


def _wait_health(container: Any, wanted: str, seconds: float) -> dict[str, Any]:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        container.reload()
        assert container.status == "running", container.logs().decode()
        health: dict[str, Any] = container.attrs["State"]["Health"]
        if health["Status"] == wanted:
            return health
        time.sleep(0.5)
    pytest.fail(f"isolated broker did not become {wanted}: {container.attrs['State']}")


def _isolated(client: Any, image: str, **kwargs: Any) -> Any:
    return client.containers.run(
        image,
        detach=True,
        network_mode="none",
        read_only=True,
        tmpfs={"/run/kodi-lab": "rw,noexec,nosuid,size=1m,mode=0750,uid=0,gid=10001"},
        cap_drop=["ALL"],
        cap_add=["CHOWN", "DAC_OVERRIDE", "FOWNER"],
        security_opt=["no-new-privileges:true"],
        labels={"io.kodi-lab.test": "broker-healthcheck"},
        **kwargs,
    )


def test_installed_broker_is_healthy_and_preserves_exact_uid_boundary() -> None:
    client = docker.from_env()
    container = None
    try:
        image = client.images.get(IMAGE)
        assert image.attrs["Config"]["Entrypoint"] == ["kodi-lab-broker"]
        watchdog = image.attrs["Config"]["Healthcheck"]
        assert watchdog["Test"] == ["CMD", "kodi-lab-broker-healthcheck"]
        assert watchdog["Timeout"] == 3_000_000_000
        assert watchdog["Interval"] == 10_000_000_000
        assert watchdog["Retries"] == 3
        assert watchdog["StartPeriod"] == 15_000_000_000
        container = _isolated(client, image.id)
        health = _wait_health(container, "healthy", 45)
        assert container.attrs["HostConfig"]["NetworkMode"] == "none"
        assert not container.attrs["HostConfig"].get("Binds")
        assert not container.attrs["HostConfig"].get("PortBindings")
        timings = []
        for _ in range(3):
            started = time.monotonic()
            result = container.exec_run(["kodi-lab-broker-healthcheck"])
            elapsed = time.monotonic() - started
            assert result.exit_code == 0, result.output.decode()
            assert elapsed < 1.0
            timings.append(elapsed)
        code = """import hashlib,json,pathlib,sys
from kodi_lab import broker_health
assert 'kodi_lab.broker' not in sys.modules
assert not any(n.startswith('kodi_lab.broker.') for n in sys.modules)
assert not {'sqlalchemy','pydantic','docker'} & {n.split('.')[0] for n in sys.modules}
p=pathlib.Path(broker_health.__file__)
print(json.dumps({'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'file':str(p)}))
"""
        installed = container.exec_run(["python", "-I", "-c", code])
        assert installed.exit_code == 0, installed.output.decode()
        expected = Path(__file__).parents[2] / "src/kodi_lab/broker_health.py"
        assert (
            json.loads(installed.output)["sha256"]
            == hashlib.sha256(expected.read_bytes()).hexdigest()
        )
        authorized = container.exec_run(
            [
                "python",
                "-c",
                """import json
from kodi_lab.broker.client import BrokerClient
c=BrokerClient()
print(json.dumps({'ping':c.ping().model_dump(),'info':c.broker_info().model_dump(),'capabilities':c.capabilities().model_dump()}))
""",
            ],
            user="10001:10001",
        )
        assert authorized.exit_code == 0, authorized.output.decode()
        normal = json.loads(authorized.output)
        assert normal["ping"] == {"status": "healthy", "protocol_version": 1}
        assert "ping" in normal["capabilities"]["operations"]
        root = container.exec_run(
            [
                "python",
                "-c",
                """from kodi_lab.broker.client import BrokerClient
from kodi_lab.broker.errors import BrokerAuthorizationError
try:
 BrokerClient().capabilities()
except BrokerAuthorizationError as error:
 assert error.code=='unauthorized_peer'
 print(error.code)
else:
 raise AssertionError('root was granted capabilities')
""",
            ],
        )
        assert root.exit_code == 0 and root.output.strip() == b"unauthorized_peer"
        nonroot = container.exec_run(
            ["kodi-lab-broker-healthcheck"], user="10001:10001"
        )
        assert nonroot.exit_code == 1
        _record(
            "docker-functional.json",
            {
                "image": image.id,
                "container": container.id,
                "health": health,
                "watchdog": watchdog,
                "healthcheck_seconds": timings,
                "installed_source": json.loads(installed.output),
                "authorized_operations": normal,
                "root_capabilities_denied": True,
                "nonroot_probe_exit": nonroot.exit_code,
                "production_mounts_or_network": False,
            },
        )
    finally:
        if container is not None:
            container.remove(force=True)
        client.close()


def test_installed_watchdog_rejects_unresponsive_listener() -> None:
    client = docker.from_env()
    container = None
    try:
        image = client.images.get(IMAGE)
        # A real disposable listener accepts ping but never produces a response.
        code = """import socket,time
s=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
s.bind('/run/kodi-lab/broker.sock');s.listen(8)
while True:
 c,_=s.accept()
 with c:
  c.recv(65536)
  time.sleep(3)
"""
        container = _isolated(
            client, image.id, entrypoint="python", command=["-c", code]
        )
        health = _wait_health(container, "unhealthy", 75)
        _record(
            "docker-unresponsive.json",
            {
                "image": image.id,
                "container": container.id,
                "health": health,
                "watchdog": container.attrs["Config"]["Healthcheck"],
                "production_mounts_or_network": False,
            },
        )
        assert health["FailingStreak"] >= 3
        assert all(row["ExitCode"] == 1 for row in health["Log"])
        assert all("response timed out" in row["Output"] for row in health["Log"])
    finally:
        if container is not None:
            container.remove(force=True)
        client.close()
