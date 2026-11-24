"""Operator-only process and crash controls for the disposable Linux qualification."""

import os
import shutil
import socket
import stat
import subprocess
import sys
import sysconfig
import tempfile
from contextlib import contextmanager
from pathlib import Path

from .broker import serve
from .runtime import wait_until

NOBODY = 65534
SOURCE = Path(__file__).resolve().parents[2]


def crashable_broker(root, path):
    fault = root / "qualification-fault"

    def interrupt(event):
        if fault.is_file() and fault.read_text() == event:
            fault.unlink()
            os._exit(86)

    serve(root, path, NOBODY, os.getgid(), interrupt=interrupt)


# Root opens read-only import directories and then relinquishes all elevated IDs
# before importing the gateway. This avoids modifying the owner's home permissions.
FRONTEND = """
import os, runpy, socket, sys
sources, deps, state, endpoint, port = sys.argv[1:]
fds = [os.open(p, os.O_RDONLY | os.O_DIRECTORY) for p in (sources, deps)]
os.chdir("/")
os.setgroups([])
os.setgid(65534)
os.setuid(65534)
assert os.getresuid() == (65534, 65534, 65534) and not os.getgroups()
try:
    os.listdir(state)
except PermissionError:
    pass
else:
    raise AssertionError("gateway can read broker state")
with socket.socket(socket.AF_UNIX) as docker:
    try:
        docker.connect("/var/run/docker.sock")
    except PermissionError:
        pass
    else:
        raise AssertionError("gateway can reach Docker")
print("uid=65534 groups=[] broker_state=denied Docker=denied", flush=True)
sys.path[:0] = [f"/proc/self/fd/{fd}" for fd in fds]
sys.argv = ["server", endpoint, "--port", port]
runpy.run_module("experiments.managed_v2.server", run_name="__main__")
"""


class TrialProcesses:
    def __init__(self, root, endpoint, port, broker_log, server_log):
        self.root = root
        self.endpoint = endpoint
        self.port = port
        self.broker_log = broker_log
        self.server_log = server_log
        self.broker = self.server = None

    def start_broker(self):
        if self.broker and self.broker.poll() is None:
            raise RuntimeError("qualification broker is already running")
        self.endpoint.unlink(missing_ok=True)
        self.broker = subprocess.Popen(
            [
                sys.executable,
                "-B",
                "-m",
                "experiments.managed_v2.native_processes",
                str(self.root),
                str(self.endpoint),
            ],
            stdout=self.broker_log,
            stderr=self.broker_log,
        )

        def bound():
            if self.broker.poll() is not None:
                raise RuntimeError(f"broker exited {self.broker.returncode}")
            return (
                self.endpoint.exists()
                and stat.S_IMODE(self.endpoint.stat().st_mode) == 0o660
            )

        wait_until(bound, 10, "broker_socket_wait")
        subprocess.run(
            ["sudo", "-n", "chgrp", str(NOBODY), str(self.endpoint)],
            check=True,
            timeout=5,
        )

    def start_server(self):
        sources = self.endpoint.parent / "code"
        package = sources / "experiments/managed_v2"
        package.mkdir(parents=True)
        for directory in (sources, package.parent, package):
            directory.chmod(0o755)
        for name in ("__init__", "broker", "protocol", "server"):
            entry = package / (name + ".py")
            shutil.copyfile(SOURCE / "experiments/managed_v2" / entry.name, entry)
            entry.chmod(0o444)
        self.server = subprocess.Popen(
            [
                "sudo",
                "-n",
                "/usr/bin/python3",
                "-B",
                "-c",
                FRONTEND,
                str(sources),
                sysconfig.get_path("purelib"),
                str(self.root),
                str(self.endpoint),
                str(self.port),
            ],
            stdout=self.server_log,
            stderr=self.server_log,
        )

        def listening():
            if self.server.poll() is not None:
                raise RuntimeError(f"gateway exited {self.server.returncode}")
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.25):
                    return True
            except OSError:
                return False

        wait_until(listening, 10, "mcp_server_wait")

    def close(self):
        for process in (self.server, self.broker):
            if process and process.poll() is None:
                process.terminate()
                process.wait(timeout=12)


@contextmanager
def processes(root, port, evidence):
    with (
        tempfile.TemporaryDirectory(prefix="kodi-v2-broker-") as temp,
        (evidence / "server.log").open("wb") as server_log,
        (evidence / "broker.log").open("wb") as broker_log,
    ):
        socket_dir = Path(temp)
        socket_dir.chmod(0o711)
        trial = TrialProcesses(
            root, socket_dir / "broker.sock", port, broker_log, server_log
        )
        try:
            trial.start_broker()
            trial.start_server()
            yield trial
        finally:
            trial.close()


if __name__ == "__main__":
    crashable_broker(Path(sys.argv[1]), Path(sys.argv[2]))
