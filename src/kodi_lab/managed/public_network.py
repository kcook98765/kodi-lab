"""Short-lived network resources for the explicitly selected public test."""

from __future__ import annotations

import json
import time
from importlib.resources import files
from typing import TYPE_CHECKING

from .protocol import Document, Refusal

if TYPE_CHECKING:
    from .runtime import Runtime

ISOLATED = "com.docker.network.bridge.gateway_mode_ipv4"


class PublicNetwork:
    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime
        self.session = runtime.root / "public-test"
        self.policy = runtime.root / "public-relay"
        self.name = runtime.network_name + "-public"
        self.relay = self.name + "-relay"
        self.feed = self.name + "-feed"
        self.feed_dir = runtime.root / "public-feed"

    def resource(self) -> Document | None:
        return self.runtime.inspect(self.relay)

    def prepare(self) -> tuple[str, str]:
        from .runtime import LABEL, atomic, docker

        runtime = self.runtime
        found = docker("network", "ls", "-q", "--filter", f"name=^{self.name}$")
        if not found:
            docker(
                "network",
                "create",
                "--internal",
                "--opt",
                f"{ISOLATED}=isolated",
                "--label",
                f"{LABEL}={runtime.target}",
                self.name,
            )
        network = json.loads(docker("network", "inspect", self.name))[0]
        if (
            network["Driver"] != "bridge"
            or not network["Internal"]
            or network["EnableIPv6"]
            or network["Options"].get(ISOLATED) != "isolated"
            or network["Labels"].get(LABEL) != str(runtime.target)
        ):
            raise Refusal("public_network_identity")
        current = self.resource()
        if not current:
            self.policy.mkdir(mode=0o700, exist_ok=True)
            atomic(
                self.policy / "relay.py",
                files(__package__).joinpath("public_relay.py").read_bytes(),
            )
            docker(
                "create",
                *runtime.args(),
                "--name",
                self.relay,
                "--network",
                "bridge",
                "--read-only",
                "--memory",
                "96m",
                "--pids-limit",
                "32",
                "--sysctl",
                "net.ipv4.ip_forward=0",
                "--mount",
                f"type=bind,src={runtime.daemon_path(self.policy)},dst=/policy,readonly",
                "--entrypoint",
                "/usr/bin/python3",
                runtime.image,
                "-u",
                "-B",
                "/policy/relay.py",
            )
            docker("network", "connect", self.name, self.relay)
            current = self.resource()
        if current is None:
            raise Refusal("public_relay_identity")
        addresses = current["NetworkSettings"]["Networks"]
        # Docker allocates the default bridge endpoint when the container starts.
        if not current["State"]["Running"]:
            # The relay waits briefly for the configuration, written once both
            # endpoints have been assigned by the Engine.
            (self.policy / "relay.json").unlink(missing_ok=True)
            docker("start", self.relay)
            current = self.resource()
            if current is None:
                raise Refusal("public_relay_identity")
            addresses = current["NetworkSettings"]["Networks"]
            if (
                set(addresses) != {"bridge", self.name}
                or current["HostConfig"]["PortBindings"]
            ):
                raise Refusal("public_relay_identity")
            expiration = float(self.session.read_text())
            if expiration <= time.time():
                raise Refusal("public_test_expired")
            data = {
                "control": addresses["bridge"]["IPAddress"],
                "gateway": addresses["bridge"]["Gateway"],
                "internal": addresses[self.name]["IPAddress"],
                "subnet": network["IPAM"]["Config"][0]["Subnet"],
                "deadline": expiration,
            }
            atomic(self.policy / "relay.json", json.dumps(data).encode())
        if (
            set(addresses) != {"bridge", self.name}
            or current["HostConfig"]["PortBindings"]
        ):
            raise Refusal("public_relay_identity")
        self.feed_dir.mkdir(mode=0o700, exist_ok=True)
        return self.name, str(addresses[self.name]["IPAddress"])

    def address(self, actual: str) -> str:
        from .runtime import atomic

        current = self.resource()
        if current is None or not current["State"]["Running"]:
            raise Refusal("public_relay_unavailable")
        path = self.policy / "kodi-ip"
        if not path.exists() or path.read_text() != actual:
            atomic(path, actual.encode())
        return str(current["NetworkSettings"]["Networks"]["bridge"]["IPAddress"])

    def remove(self) -> None:
        from .runtime import docker

        current = self.resource()
        if current is not None:
            docker("rm", "-f", current["Id"])
        runtime = self.runtime
        kodi = runtime.inspect()
        if kodi is not None and kodi["State"]["Running"]:
            # Only the disposable profile is eligible. The normal Stop path
            # has already been requested or startup failed.
            runtime.check_live_profile(kodi)
            docker("stop", "--time", "2", kodi["Id"])
        if (feed := runtime.inspect(self.feed)) is not None:
            docker("rm", "-f", feed["Id"])
        self.session.unlink(missing_ok=True)
