"""Installed-artifact entry point for the restricted Linux broker."""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import socket
import stat
import sys
from multiprocessing import Process
from pathlib import Path
from threading import Event, Thread

from kodi_lab.broker.client import BrokerClient
from kodi_lab.broker.errors import (
    BrokerAuthorizationError,
    BrokerStartupError,
    BrokerUnavailableError,
)
from kodi_lab.broker.server import BrokerServer, ProductionBrokerConfiguration
from kodi_lab.managed.broker import serve
from kodi_lab.managed.runtime import docker
from kodi_lab.viewer_diagnostics import configure_diagnostics


class _AuditFormatter(logging.Formatter):
    """Keep the broker's bounded semantic audit fields in container logs."""

    def format(self, record: logging.LogRecord) -> str:
        fields = {
            key: value
            for key, value in vars(record).items()
            if key.startswith("broker_")
        }
        return json.dumps(
            {"event": record.getMessage(), **fields},
            sort_keys=True,
            separators=(",", ":"),
        )


def _configure_logging() -> None:
    configure_diagnostics()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    audit = logging.getLogger("kodi_lab.broker.audit")
    audit.setLevel(logging.INFO)
    audit.propagate = False
    handler = logging.StreamHandler()
    handler.setFormatter(_AuditFormatter())
    audit.addHandler(handler)


def _healthcheck() -> int:
    """Prove a live listener responds, including its rejection of root."""

    try:
        BrokerClient().ping()
    except BrokerAuthorizationError as error:
        # The broker container runs the check as root. The exact-UID broker
        # must reject that peer after accepting and answering the connection.
        return 0 if error.code == "unauthorized_peer" else 1
    except BrokerUnavailableError:
        return 1
    except Exception:
        return 1
    # An accepted root request would mean the production identity gate drifted.
    return 1


MANAGED_STATE = Path("/var/lib/kodi-lab-managed")
KODI_UID = 1000


def _managed_volume_source() -> Path:
    own = json.loads(docker("inspect", "--type", "container", socket.gethostname()))[0]
    if own["Config"].get("Entrypoint") != ["kodi-lab-broker"]:
        raise BrokerStartupError("managed broker container identity is invalid")
    mounts = [
        mount for mount in own["Mounts"] if mount["Destination"] == str(MANAGED_STATE)
    ]
    if len(mounts) != 1:
        raise BrokerStartupError("managed state volume is required")
    mount = mounts[0]
    name = mount.get("Name")
    if (
        mount.get("Type") != "volume"
        or mount.get("RW") is not True
        or not isinstance(name, str)
        or re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", name) is None
    ):
        raise BrokerStartupError("managed state must be a writable named volume")
    volume = json.loads(docker("volume", "inspect", name))[0]
    source = mount.get("Source")
    if (
        volume.get("Driver") != "local"
        or volume.get("Options")
        or not isinstance(source, str)
        or source != volume.get("Mountpoint")
        or re.fullmatch(r"/[a-zA-Z0-9_./-]+", source) is None
        or ".." in Path(source).parts
        or not source.endswith(f"/volumes/{name}/_data")
    ):
        raise BrokerStartupError("managed state volume has an unexpected host path")
    return Path(source)


def _start_managed(
    root: Path, path: Path, web_uid: int, web_gid: int, daemon_root: Path
) -> None:
    # Own only the separate managed state volume. Keep Web/agent credentials in
    # the application and never inherit the Web state directory as authority.
    os.setgroups([web_gid, Path("/var/run/docker.sock").stat().st_gid])
    os.setgid(KODI_UID)
    os.setuid(KODI_UID)
    serve(root, path, web_uid, web_gid, daemon_root)


def _managed_process() -> Process:
    daemon_root = _managed_volume_source()
    config = ProductionBrokerConfiguration.resolve()
    runtime_directory = config.socket.runtime_directory / "managed"
    runtime_directory.mkdir(mode=0o750, exist_ok=True)
    state = MANAGED_STATE.lstat()
    runtime = runtime_directory.lstat()
    if (
        not stat.S_ISDIR(state.st_mode)
        or state.st_uid not in {0, KODI_UID}
        or stat.S_IMODE(state.st_mode) != 0o700
        or not stat.S_ISDIR(runtime.st_mode)
        or runtime.st_uid not in {0, KODI_UID}
        or runtime.st_mode & 0o027
    ):
        raise BrokerStartupError("managed directories have unsafe metadata")
    os.chown(MANAGED_STATE, KODI_UID, KODI_UID)
    os.chown(runtime_directory, KODI_UID, config.socket.directory_group_gid)
    return Process(
        target=_start_managed,
        name="managed-controller",
        args=(
            MANAGED_STATE,
            runtime_directory / "control.sock",
            config.authorization.expected_uid,
            config.socket.socket_group_gid,
            daemon_root,
        ),
    )


def main() -> int:
    """Run the closed production broker until SIGTERM or SIGINT."""

    if sys.argv[1:] == ["--healthcheck"]:
        return _healthcheck()
    if sys.argv[1:]:
        print("usage: kodi-lab-broker [--healthcheck]", file=sys.stderr)
        return 2

    _configure_logging()
    stopped = Event()

    def stop(_signum: int, _frame: object) -> None:
        stopped.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    managed: Process | None = None
    try:
        server = BrokerServer.production()
        server.open()
        managed = _managed_process()
        managed.start()

        def watch() -> None:
            assert managed is not None
            managed.join()
            stopped.set()

        Thread(target=watch, name="managed-monitor", daemon=True).start()
        server.serve_forever(stopped)
        if managed.exitcode is not None:
            return 1
    except BrokerStartupError as error:
        logging.getLogger(__name__).error("broker startup rejected: %s", error)
        return 1
    except Exception:
        logging.getLogger(__name__).exception("broker stopped unexpectedly")
        return 1
    finally:
        if managed is not None and managed.is_alive():
            managed.terminate()
            managed.join(timeout=6)
    return 0
