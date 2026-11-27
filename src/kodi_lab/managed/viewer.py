"""Attach the qualified RFB tunnel to the broker-owned working profile."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

from kodi_lab.broker.novnc import (
    VIEW_HELPER,
    DisplayLane,
    DockerViewTunnel,
    ViewBinding,
    ViewRequest,
)
from kodi_lab.broker.socket_path import SocketPathConfig

from .controller import Controller
from .protocol import Refusal
from .runtime import docker

MAX_NATIVE_INPUT_PROBES = 3


def display_lane(
    controller: Controller, path: Path, frontend_uid: int, frontend_gid: int
) -> DisplayLane:
    def admit(request: ViewRequest) -> DockerViewTunnel:
        interactive = request.mode == "interactive"
        with controller.omega.exclusive():
            runtime = next(
                (
                    item
                    for item in controller.runtimes.values()
                    if item.target == request.instance
                ),
                None,
            )
            if runtime is None:
                raise Refusal("managed_view_target_invalid")
            obj = runtime.inspect()
            if not obj or not obj["State"]["Running"]:
                raise Refusal("kodi_stopped")
            runtime.check_live_profile(obj)
            binding = ViewBinding(
                container=obj["Id"],
                image=runtime.image,
                started=obj["State"]["StartedAt"],
                identity=(runtime.read_pointer(),),
            )
            if interactive:
                assert request.attempt is not None
                controller.begin_input(request.attempt)

        def valid() -> bool:
            # Retired containers close the attached exec. The controller also
            # revokes input before any managed stop or ownership release.
            return not interactive or (
                controller.input_attempt == request.attempt
                and not controller.input_revoked
                and controller.owner() == "admin"
            )

        return DockerViewTunnel(
            binding,
            request,
            valid,
            proof=helper_exit_proof(binding, interactive=interactive),
            finished=(
                (
                    lambda terminated: controller.finish_input(
                        request.attempt, terminated
                    )
                )
                if interactive
                else None
            ),
        )

    return DisplayLane(
        SocketPathConfig(
            path.parent,
            path,
            directory_owner_uid=os.getuid(),
            directory_group_gid=frontend_gid,
            socket_owner_uid=os.getuid(),
            socket_group_gid=frontend_gid,
        ),
        frontend_uid,
        admit,
    )


def helper_exit_proof(
    binding: ViewBinding, *, interactive: bool
) -> Callable[[str | None], bool]:
    # A clean interactive exit neutralizes keys and buttons. After a failed
    # exit retry the read-only probe briefly for transient cleanup races.
    # If input stays unproven, keep the fence until the incarnation retires.
    from docker import APIClient  # type: ignore[import-untyped]
    from docker.errors import NotFound  # type: ignore[import-untyped]

    probes_left = MAX_NATIVE_INPUT_PROBES

    def proven(exec_id: str | None) -> bool:
        nonlocal probes_left
        if exec_id is None:
            return True
        with APIClient(base_url="unix:///var/run/docker.sock", timeout=1) as client:
            try:
                exec_state = client.exec_inspect(exec_id)
                if exec_state.get("Running") is not False:
                    return False
                if not interactive or exec_state.get("ExitCode") == 0:
                    return True
                state = client.inspect_container(binding.container)["State"]
                if state["Running"] is False or state["StartedAt"] != binding.started:
                    return True
                if probes_left == 0:
                    return False
                probes_left -= 1
                probe = client.exec_create(
                    binding.container,
                    cmd=[
                        "/usr/bin/python3",
                        "-I",
                        "-B",
                        VIEW_HELPER,
                        "--probe-control",
                    ],
                    user="1000:1000",
                    stdin=False,
                    stdout=False,
                    stderr=False,
                    tty=False,
                    privileged=False,
                )["Id"]
                client.exec_start(probe)
                observation = client.exec_inspect(probe)
                return observation["Running"] is False and bool(
                    observation["ExitCode"] == 0
                )
            except NotFound:
                # No matching container means the old display and its input are gone.
                return not bool(
                    docker(
                        "ps", "-aq", "--no-trunc", "--filter", "id=" + binding.container
                    )
                )

    return proven
