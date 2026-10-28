"""Bounded native qualification entrypoint; deliberately absent from defaults.

The candidate image embeds one operator-created immutable pin file. Ordinary
broker/Web launchers never enable this prototype. No listener publishes RFB.
"""

from __future__ import annotations

import json
import signal
import stat
import sys
import time
from dataclasses import replace
from pathlib import Path
from threading import Event
from uuid import UUID, uuid4

import docker  # type: ignore[import-untyped]
import uvicorn

from kodi_lab.application.novnc import CatalogViewAuthority
from kodi_lab.application.novnc_control import HumanViewerOwnership
from kodi_lab.broker.client import BrokerClient
from kodi_lab.broker.docker_inventory import DockerManagedContainerAdapter
from kodi_lab.broker.novnc import (
    DisplayLane,
    DockerViewTunnel,
    ViewBinding,
    ViewRequest,
)
from kodi_lab.broker.provisioning import SqliteProvisioningResolver
from kodi_lab.broker.server import BrokerServer, ProductionBrokerConfiguration
from kodi_lab.config import Settings
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.web.app import create_app
from kodi_lab.web.novnc import PrototypeViewer
from kodi_lab.web.novnc_protocol import BoundedWebSocketProtocol


def main() -> None:
    if sys.argv[1:] not in (["web"], ["broker"]):
        raise SystemExit(
            "usage: python -m kodi_lab.runtime.novnc_qualification web|broker"
        )
    pin_path = Path("/usr/share/kodi-lab/novnc-phase1-pin.json")
    metadata = pin_path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != 0
        or stat.S_IMODE(metadata.st_mode) != 0o444
        or metadata.st_size > 4096
    ):
        raise ValueError("qualification pin unavailable")
    pin = json.loads(pin_path.read_text())
    expires = pin["expires"]
    if not time.time() < expires <= time.time() + 7200:
        raise ValueError("qualification pin expired")
    instance = UUID(pin["instance"])
    binding = ViewBinding(
        pin["container"], pin["image"], pin["started"], tuple(pin["identity"])
    )
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    engine = create_database_engine(settings.database_url)
    sessions = create_session_factory(engine)
    catalog = CatalogViewAuthority(sessions, {instance: binding})
    ownership = HumanViewerOwnership(sessions)
    broker_id = str(uuid4())

    def authority(target: UUID) -> ViewBinding:
        if time.time() >= expires:
            raise ValueError("qualification expired")
        return catalog.resolve(target)

    if sys.argv[1] == "broker":
        configuration = ProductionBrokerConfiguration.resolve()
        resolver = SqliteProvisioningResolver(sessions)

        def native_proof(
            container: str, exec_id: str | None = None, interactive: bool = False
        ) -> bool:
            client = docker.APIClient(version="auto", timeout=1)
            try:
                actual = client.inspect_container(container)
                if actual["Id"] != container or actual["State"]["Paused"]:
                    return False
                if not actual["State"]["Running"]:
                    return True
                if exec_id is not None and client.exec_inspect(exec_id)["Running"]:
                    return False
                check = client.exec_create(
                    container,
                    cmd=[
                        "/usr/bin/python3",
                        "-I",
                        "-B",
                        "/usr/local/lib/kodi-lab-novnc-view.py",
                        "--probe-control" if interactive else "--probe",
                    ],
                    user="1000:1000",
                    stdout=True,
                    stderr=False,
                    tty=False,
                    privileged=False,
                )
                client.exec_start(check["Id"], stream=False, tty=False)
                state = client.exec_inspect(check["Id"])
                return bool(not state["Running"] and state["ExitCode"] == 0)
            except Exception:
                return False
            finally:
                client.close()

        ownership.recover(broker_id, native_proof)

        def admit(request: ViewRequest) -> DockerViewTunnel:
            if request.attempt is None or request.rate != 2:
                raise ValueError("human Web viewer admission required")
            selected = authority(request.instance)
            attempt = request.attempt
            client = docker.APIClient(version="auto", timeout=1)
            try:
                spec = resolver.resolve(str(request.instance))
                image = client.inspect_image(selected.image)
                ownership.claim(
                    request.instance,
                    attempt,
                    request.owner,
                    selected,
                    request.mode,
                    broker_id,
                )
            except BaseException:
                client.close()
                ownership.finish(
                    request.instance,
                    attempt,
                    broker_id,
                    native_proof(selected.container),
                )
                raise

            def valid() -> bool:
                try:
                    if (
                        not ownership.valid(request.instance, attempt, broker_id)
                        or authority(request.instance) != selected
                    ):
                        return False
                    actual = client.inspect_container(selected.container)
                    DockerManagedContainerAdapter._validate_provisioned_container(
                        actual,
                        expected_container_id=selected.container,
                        spec=spec,
                        image_config=image["Config"],
                    )
                    return bool(
                        actual["State"]["Running"]
                        and actual["State"]["Health"]["Status"] == "healthy"
                        and actual["State"]["StartedAt"] == selected.started
                        and actual["Image"] == selected.image
                    )
                except Exception:
                    return False

            return DockerViewTunnel(
                selected,
                request,
                valid,
                client=client,
                started=lambda exec_id: ownership.started(
                    request.instance, attempt, broker_id, exec_id
                ),
                proof=lambda exec_id: native_proof(
                    selected.container,
                    exec_id,
                    interactive=request.mode == "interactive",
                ),
                finished=lambda proven: ownership.finish(
                    request.instance, attempt, broker_id, proven
                ),
            )

        lane = DisplayLane(
            replace(
                configuration.socket, socket_path=settings.runtime_root / "display.sock"
            ),
            configuration.authorization.expected_uid,
            admit,
            reconcile=ownership.expire_pending,
        )
        server = BrokerServer(
            socket_config=configuration.socket,
            authorization=configuration.authorization,
            display_lane=lane,
        )
        stopped = Event()
        signal.signal(signal.SIGTERM, lambda *_: stopped.set())
        signal.signal(signal.SIGINT, lambda *_: stopped.set())
        try:
            server.serve_forever(stopped)
        finally:
            print(
                json.dumps(
                    [
                        {
                            "exec": r.exec_id,
                            "terminated": r.terminated,
                            "up": r.bytes_up,
                            "down": r.bytes_down,
                            "startup_ms": r.startup_ms,
                            "teardown_ms": r.teardown_ms,
                            "reason": r.reason,
                        }
                        for r in lane.results
                    ]
                ),
                flush=True,
            )
    else:
        viewer = PrototypeViewer(
            settings.runtime_root / "display.sock", authority, ownership=ownership
        )
        app = create_app(
            settings=settings,
            session_factory=sessions,
            broker=BrokerClient(settings.runtime_root / "broker.sock"),
            novnc_viewer=viewer,
        )
        uvicorn.run(
            app,
            host=settings.effective_web_bind_host,
            port=settings.web_port,
            ws=BoundedWebSocketProtocol,
            ws_max_size=256,
            ws_max_queue=1,
            proxy_headers=True,
            forwarded_allow_ips=pin["proxy_ip"],
            access_log=False,
        )


if __name__ == "__main__":
    main()
