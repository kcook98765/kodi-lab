"""Attach the qualified RFB tunnel to the broker-owned working profile."""

from __future__ import annotations

import os
from pathlib import Path

from kodi_lab.broker.novnc import (
    DisplayLane,
    DockerViewTunnel,
    ViewBinding,
    ViewRequest,
)
from kodi_lab.broker.socket_path import SocketPathConfig

from .controller import Controller
from .protocol import Refusal
from .runtime import docker


def display_lane(
    controller: Controller, path: Path, frontend_uid: int, frontend_gid: int
) -> DisplayLane:
    runtime = controller.runtime

    def admit(request: ViewRequest) -> DockerViewTunnel:
        interactive = request.mode == "interactive"
        with runtime.exclusive():
            if request.instance != runtime.target:
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
            proof=lambda exec_id: (
                exec_id is None or helper_exited(binding.container, exec_id)
            ),
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


def helper_exited(container_id: str, exec_id: str) -> bool:
    # The helper owns its X input fence until exit. If the exec completed,
    # the tunnel's own exec_inspect check is sufficient. A removed container
    # is also definitive. Other Docker errors keep the input gate occupied.
    from docker import APIClient  # type: ignore[import-untyped]
    from docker.errors import NotFound  # type: ignore[import-untyped]

    with APIClient(base_url="unix:///var/run/docker.sock", timeout=1) as client:
        try:
            return client.exec_inspect(exec_id).get("Running") is False
        except NotFound:
            return not bool(
                docker("ps", "-aq", "--no-trunc", "--filter", "id=" + container_id)
            )
