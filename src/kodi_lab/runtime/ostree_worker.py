"""Offline install worker with a socketless, operation-owned acquisition helper.

Only fixed OSTree pull commands and fixed Ubuntu support downloads use the
helper. Verification, leased handoff, sealed context and Docker authority remain
in the existing worker. The helper is removed before assembly can begin.
"""

from __future__ import annotations

import contextlib
import os
import socket
import stat
import sys
import threading
from pathlib import Path
from typing import Any
from uuid import UUID

import docker  # type: ignore[import-untyped]

from kodi_lab.config import Settings
from kodi_lab.runtime.ostree import (
    BoundedCommandRunner,
    CommandResult,
    OstreeAcquirer,
    OstreeAcquisitionError,
    OstreeVerificationEvidence,
    _directory,
    _read_file,
    _stage_quota,
    _write_file,
)
from kodi_lab.runtime.ostree_assembly import (
    OstreeAssemblyEvidence,
    assemble_ostree_image,
)
from kodi_lab.runtime.recipes import load_inactive_ostree_recipe
from kodi_lab.runtime.temporary_networks import (
    create_temporary_network,
    remove_exact_temporary_network,
)
from kodi_lab.runtime.ubuntu_support import (
    SupportError,
    fetch_support_packages,
    load_support_closure,
    validate_support_entries,
)

STAGING_ROOT = Path("/run/kodi-lab/ostree-staging")

# Only these fixed categories cross the socketless helper boundary. No stderr,
# URL, response body or caller-selected error text becomes durable/public state.
SUPPORT_FAILURE_EXITS = {
    71: "pinned_support_unavailable",
    72: "support_artifact_mismatch",
    73: "support_download_bound",
}


def require_support_success(result: CommandResult) -> None:
    if result.returncode:
        raise OstreeAcquisitionError(
            SUPPORT_FAILURE_EXITS.get(result.returncode, "support_acquisition_failed")
        )


class HelperRunner(BoundedCommandRunner):
    def __init__(self, helper: Any) -> None:
        self.helper = helper

    def run(
        self, argv: tuple[str, ...], *, stage: Path, timeout: float
    ) -> CommandResult:
        if argv[0] != "/usr/bin/ostree" or "pull" not in argv:
            return super().run(argv, stage=stage, timeout=timeout)
        # Arguments originate exclusively in OstreeAcquirer, never broker input.
        return self.execute(argv, stage=stage, timeout=timeout)

    def execute(
        self, argv: tuple[str, ...], *, stage: Path, timeout: float
    ) -> CommandResult:
        allowed: set[tuple[str, ...]] = {
            (
                "/usr/bin/ostree",
                f"--repo={stage / 'repo'}",
                "pull",
                "--depth=0",
                "--http-header=User-Agent=Kodi-Lab-reviewed-OSTree",
                "flathub-beta" if c.role == "app" else "flathub",
                f"{c.ref}@{c.commit}",
            )
            for c in load_inactive_ostree_recipe().recipe.provider.components
        }
        operation = stage.name.removeprefix("ostree-")
        allowed.add(
            (
                "/usr/local/bin/python",
                "-m",
                "kodi_lab.runtime.ostree_worker",
                str(UUID(operation)),
            )
        )
        if argv not in allowed or stage.parent != STAGING_ROOT:
            raise OstreeAcquisitionError("unreviewed_helper_command")
        result: list[Any] = []
        error: list[BaseException] = []

        def run() -> None:
            try:
                api = self.helper.client.api
                execution = api.exec_create(
                    self.helper.id,
                    [
                        "/usr/bin/prlimit",
                        "--as=2147483648",
                        "--fsize=536870912",
                        "--nofile=256",
                        "--cpu=900",
                        "--",
                        *argv,
                    ],
                    workdir=str(stage),
                    environment={
                        "HOME": str(stage / "home"),
                        "GNUPGHOME": str(stage / "gnupg"),
                        "TMPDIR": str(stage / "tmp"),
                        "LANG": "C.UTF-8",
                    },
                )
                output = bytearray()
                for chunk in api.exec_start(execution["Id"], stream=True):
                    output.extend(chunk)
                    if len(output) > 1024**2:
                        raise OstreeAcquisitionError("helper_output_exceeded")
                code = api.exec_inspect(execution["Id"])["ExitCode"]
                result.append(CommandResult(code, bytes(output)))
            except BaseException as exc:
                error.append(exc)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        import time

        deadline = time.monotonic() + timeout
        try:
            while thread.is_alive():
                thread.join(timeout=min(1, max(0.01, deadline - time.monotonic())))
                _stage_quota(stage)
                if time.monotonic() > deadline:
                    raise OstreeAcquisitionError("helper_timeout")
            if error or not result or len(result[0].output) > 1024**2:
                raise OstreeAcquisitionError("helper_command_failed")
            return CommandResult(result[0].returncode, result[0].output)
        except BaseException:
            self.helper.kill()
            thread.join(timeout=10)
            raise


def assemble_in_worker(
    operation_id: UUID,
    settings: Settings,
    on_acquired: Any,
) -> tuple[OstreeAssemblyEvidence, OstreeVerificationEvidence]:
    if settings.runtime_root != Path("/run/kodi-lab") or os.geteuid() != 0:
        raise OstreeAcquisitionError("installed_worker_contract_required")
    root_fd = _directory(settings.runtime_root)
    try:
        with contextlib.suppress(FileExistsError):
            os.mkdir(STAGING_ROOT.name, 0o700, dir_fd=root_fd)
    finally:
        os.close(root_fd)
    info = STAGING_ROOT.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != 0
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise OstreeAcquisitionError("unsafe_worker_staging_root")
    client = docker.from_env(timeout=600)
    network = helper = None
    try:
        own = client.api.inspect_container(socket.gethostname())
        image_id = own["Image"]
        if own.get("Config", {}).get("Entrypoint") != ["kodi-lab-runtime"]:
            raise OstreeAcquisitionError("installed_worker_identity_required")
        mounts = [
            m
            for m in own["Mounts"]
            if m["Destination"] == "/run/kodi-lab" and m["Type"] == "volume"
        ]
        if len(mounts) != 1:
            raise OstreeAcquisitionError("worker_lock_volume_required")
        network = create_temporary_network(
            client, settings, operation_id=operation_id, purpose="acquisition"
        )
        helper = client.containers.create(
            image_id,
            name=f"kodi-lab-ostree-acquisition-{operation_id.hex}",
            entrypoint=["/usr/bin/sleep"],
            command=["3600"],
            user="0:0",
            network=network.name,
            read_only=True,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"],
            mem_limit="2g",
            pids_limit=64,
            volumes={mounts[0]["Name"]: {"bind": "/run/kodi-lab", "mode": "rw"}},
            labels={
                "io.kodi-lab.resource": "ostree-acquisition",
                "io.kodi-lab.operation-id": str(operation_id),
            },
        )
        helper.reload()
        attrs = helper.attrs
        host = attrs.get("HostConfig", {})
        actual_mounts = attrs.get("Mounts", [])
        if (
            attrs.get("Image") != image_id
            or attrs.get("Config", {}).get("User") != "0:0"
            or host.get("Privileged")
            or host.get("Devices")
            or host.get("CapAdd")
            or host.get("CapDrop") != ["ALL"]
            or not host.get("ReadonlyRootfs")
            or host.get("SecurityOpt") != ["no-new-privileges:true"]
            or host.get("PortBindings")
            or len(actual_mounts) != 1
            or actual_mounts[0].get("Type") != "volume"
            or actual_mounts[0].get("Name") != mounts[0]["Name"]
            or actual_mounts[0].get("Destination") != "/run/kodi-lab"
            or actual_mounts[0].get("RW") is not True
            or set(attrs.get("NetworkSettings", {}).get("Networks", {}))
            != {network.name}
        ):
            raise OstreeAcquisitionError("acquisition_helper_contract_differs")
        helper.start()
        runner = HelperRunner(helper)
        with OstreeAcquirer(STAGING_ROOT, runner=runner).acquire(
            operation_id
        ) as acquired:
            result = runner.execute(
                (
                    "/usr/local/bin/python",
                    "-m",
                    "kodi_lab.runtime.ostree_worker",
                    str(operation_id),
                ),
                stage=acquired.stage,
                timeout=650,
            )
            require_support_success(result)
            entries = {
                "support/" + p.archive_name: _read_file(
                    acquired.stage / "support" / p.archive_name, maximum=p.size
                )
                for p in load_support_closure().packages
            }
            validate_support_entries(entries)
            # Reap all network helper processes before immutable capture. No
            # concurrent writer retains the mounted staging volume.
            helper.stop(timeout=5)
            helper.remove(force=False)
            helper = None
            remove_exact_temporary_network(client, network)
            network = None
            on_acquired()
            assembly = assemble_ostree_image(
                acquired,
                operation_id=operation_id,
                client=client,
                support_entries=entries,
            )
            return assembly, acquired.evidence
    finally:
        try:
            if helper is not None:
                helper.remove(force=True)
        finally:
            try:
                if network is not None:
                    remove_exact_temporary_network(client, network)
            finally:
                client.close()


def main() -> int:
    """Socketless support fetch; UUID chooses only this fixed private operation."""
    operation = UUID(sys.argv[1])
    if len(sys.argv) != 2 or sys.argv[1] != str(operation):
        return 2
    stage = STAGING_ROOT / ("ostree-" + operation.hex)
    fd = _directory(stage)
    try:
        info = os.fstat(fd)
        if info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o700:
            raise OstreeAcquisitionError("unsafe_support_stage")
        os.mkdir("support", 0o700, dir_fd=fd)
        try:
            entries = fetch_support_packages()
        except SupportError as error:
            return next(
                (
                    status
                    for status, code in SUPPORT_FAILURE_EXITS.items()
                    if str(error) == code
                ),
                1,
            )
        for name, payload in entries.items():
            _write_file(stage / name, payload)
    finally:
        os.close(fd)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
