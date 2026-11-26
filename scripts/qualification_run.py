"""Run or retire one engine-owned Linux qualification environment."""

from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, suppress
from datetime import date
from pathlib import Path
from typing import Any
from uuid import uuid4

from scripts.test_scratch import process_start_ticks

TMP = Path("/tmp")
MAX_LOG = 256 * 1024
MAX_ARTIFACTS = 64
MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
OWNER = "qualification-run.json"
MANIFEST = "evidence-sha256.json"
RESULT = "closeout.json"
DOCKER_ALIAS = Path("/var/run/docker.sock")
EVIDENCE = (
    "outcome.json",
    "endpoint.json",
    "inventory.json",
    "workload.log",
    "docker.log",
    "containerd.log",
)
Doc = dict[str, Any]


def output(*args: str) -> str:
    return subprocess.check_output(args, text=True, timeout=20).strip()


def read(path: Path) -> Doc:
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError(f"Unsafe receipt: {path.name}")
    result: Doc = json.loads(path.read_text())
    return result


def write(path: Path, value: object) -> None:
    stage = path.with_name("." + uuid4().hex)
    with stage.open("x") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    stage.replace(path)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def owned_path(path: Path, uid: int, *, traversable: bool = False) -> os.stat_result:
    observed = path.lstat()
    if (
        path != path.resolve(strict=True)
        or not stat.S_ISDIR(observed.st_mode)
        or observed.st_uid != uid
        or observed.st_mode & (0o066 if traversable else 0o077)
    ):
        raise ValueError("Expected an exact private run directory")
    return observed


@contextmanager
def lock(evidence: Path) -> Iterator[None]:
    owned_path(evidence, os.getuid())
    fd = os.open(evidence / ".run.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "r+") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def stamp(pid: int) -> Doc:
    ticks = process_start_ticks(pid)
    if ticks is None:
        raise ValueError("Owned process exited before its identity could be saved")
    return {"pid": pid, "start_ticks": ticks, "boot": boot_id()}


def boot_id() -> str:
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def live(identity: Doc) -> bool:
    return bool(
        identity["boot"] == boot_id()
        and process_start_ticks(identity["pid"]) == identity["start_ticks"]
    )


def socket_identity(path: Path, process: Doc) -> tuple[int, int]:
    if not live(process):
        raise ValueError("Daemon process was replaced")
    observed = path.stat()
    if not stat.S_ISSOCK(observed.st_mode):
        raise ValueError("Expected a Unix listener")
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(2)
        client.connect(str(path))
        pid, uid, _ = struct.unpack(
            "3i", client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        )
    if pid != process["pid"] or uid != 0:
        raise ValueError("Unix listener is not the recorded daemon")
    return observed.st_dev, observed.st_ino


def namespaces(pid: int) -> tuple[str, str]:
    return (os.readlink(f"/proc/{pid}/ns/mnt"), os.readlink(f"/proc/{pid}/ns/net"))


def require_private_mounts() -> None:
    for line in Path("/proc/self/mountinfo").read_bytes().splitlines():
        optional = line.partition(b" - ")[0].split()[6:]
        if any(
            field.startswith((b"shared:", b"master:", b"propagate_from:"))
            for field in optional
        ):
            raise ValueError("Qualification mount propagation is not private")


def docker(root: Path, *args: str) -> str:
    return output(
        "docker",
        "--config",
        str(root / "docker-config"),
        "--host",
        "unix://" + str(root / "docker.sock"),
        *args,
    )


def verify(root: Path, record: Doc, *, alias: bool) -> Doc:
    outer, inner = record["dockerd"], record["containerd"]
    docker_socket, ctr_socket = root / "docker.sock", root / "ctr.sock"
    docker_identity = socket_identity(docker_socket, outer)
    ctr_identity = socket_identity(ctr_socket, inner)
    mount_ns, net_ns = namespaces(outer["pid"])
    host_mount, host_net = namespaces(1)
    if (
        mount_ns == host_mount
        or net_ns == host_net
        or namespaces(inner["pid"]) != (mount_ns, net_ns)
    ):
        raise ValueError("Daemon mount or network namespace is shared")
    outer_args = Path(f"/proc/{outer['pid']}/cmdline").read_bytes().decode().split("\0")
    for option, expected in {
        "--config-file": str(root / "daemon.json"),
        "--host": "unix://" + str(docker_socket),
        "--data-root": str(root / "docker-data"),
        "--exec-root": str(root / "docker-exec"),
        "--containerd": str(ctr_socket),
        "--containerd-namespace": record["namespace"],
        "--containerd-plugins-namespace": record["namespace"] + "-plugins",
    }.items():
        if (
            option not in outer_args
            or outer_args[outer_args.index(option) + 1] != expected
        ):
            raise ValueError(f"Docker launch identity changed: {option}")
    ctr_config = root / "containerd.toml"
    inner_args = Path(f"/proc/{inner['pid']}/cmdline").read_bytes().decode().split("\0")
    if inner_args[1:3] != [
        "--config",
        str(ctr_config),
    ] or ctr_config.read_text() != containerd_config(root):
        raise ValueError("Containerd root or state identity changed")
    # Confirm the observed established connection, in the private network namespace.
    rows = [
        line.split()
        for line in output(
            "nsenter",
            "--target",
            str(outer["pid"]),
            "--net",
            "ss",
            "-xnpH",
            "state",
            "established",
        ).splitlines()
    ]
    clients = {
        (row[4], row[6])
        for row in rows
        if len(row) > 7 and f"pid={outer['pid']}," in "".join(row[7:])
    }
    if not any(
        len(row) > 7
        and row[3] == str(ctr_socket)
        and f"pid={inner['pid']}," in "".join(row[7:])
        and (row[6], row[4]) in clients
        for row in rows
    ):
        raise ValueError("No established peer from Docker to the recorded containerd")
    platform = json.loads(docker(root, "info", "--format", "{{json .}}"))
    engine = platform["ID"]
    if (
        not engine
        or platform["DockerRootDir"] != str(root / "docker-data")
        or (record.get("engine") is not None and record["engine"] != engine)
    ):
        raise ValueError("Docker Engine identity or root changed")
    ctr_names = {
        row.split()[0]
        for row in output(
            "ctr", "--address", str(ctr_socket), "namespaces", "list"
        ).splitlines()[1:]
    }
    if not ctr_names <= {
        record["namespace"],
        record["namespace"] + "-plugins",
    }:
        raise ValueError(f"Unexpected containerd namespaces: {sorted(ctr_names)}")
    if alias and socket_identity(DOCKER_ALIAS, outer) != docker_identity:
        raise ValueError("The child worker socket alias has a different listener")
    return {
        "engine": engine,
        "version": platform["ServerVersion"],
        "docker_socket": str(docker_socket),
        "docker_socket_identity": docker_identity,
        "containerd_socket": str(ctr_socket),
        "containerd_socket_identity": ctr_identity,
        "namespaces": sorted(ctr_names),
        "mount_namespace": mount_ns,
        "network_namespace": net_ns,
        "worker_alias_verified": alias,
    }


def containerd_config(root: Path) -> str:
    return (
        'version = 3\ndisabled_plugins = ["io.containerd.cri.v1.images", '
        '"io.containerd.cri.v1.runtime", "io.containerd.grpc.v1.cri"]\n'
        f'root = "{root}/ctr-data"\nstate = "{root}/ctr-state"\n'
        f'[grpc]\n  address = "{root}/ctr.sock"\n'
    )


def tail(source: Path, destination: Path) -> None:
    with source.open("rb") as stream:
        stream.seek(max(0, stream.seek(0, os.SEEK_END) - MAX_LOG))
        destination.write_bytes(stream.read(MAX_LOG))


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def evidence_verified(evidence: Path) -> bool:
    manifest = read(evidence / MANIFEST)
    required = set(EVIDENCE) | {OWNER}
    extra = set(manifest) - required
    if not required <= manifest.keys() or len(extra) > MAX_ARTIFACTS:
        return False
    if extra and (
        not extra
        <= {f"artifacts/{path.name}" for path in (evidence / "artifacts").iterdir()}
        or (evidence / "artifacts").is_symlink()
    ):
        return False
    return all(
        stat.S_ISREG((evidence / name).lstat().st_mode)
        and digest(evidence / name) == expected
        for name, expected in manifest.items()
    )


def artifact_files(root: Path, uid: int) -> Iterator[tuple[str, bytes]]:
    remaining = MAX_ARTIFACT_BYTES
    inbox = root / "artifact-inbox"
    for count, item in enumerate(inbox.iterdir()):
        if count >= MAX_ARTIFACTS or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}", item.name
        ):
            raise ValueError("Unsupported qualification artifact inventory")
        fd = os.open(item, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as artifact:
            info = os.fstat(artifact.fileno())
            if not (
                stat.S_ISREG(info.st_mode)
                and info.st_uid == uid
                and info.st_nlink == 1
                and 0 <= info.st_size <= remaining
            ):
                raise ValueError("Artifact ownership, type or size is unsupported")
            data = artifact.read(info.st_size + 1)
            if len(data) != info.st_size:
                raise ValueError("Qualification artifact changed during readback")
        remaining -= len(data)
        yield item.name, data


def export_artifacts(root: Path, evidence: Path, uid: int) -> list[str]:
    destination = evidence / "artifacts"
    destination.mkdir(mode=0o700, exist_ok=True)
    owned_path(destination, 0)
    exported: list[str] = []
    for name, data in artifact_files(root, uid):
        path = destination / name
        temporary = destination / ("." + uuid4().hex)
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        exported.append(f"artifacts/{name}")
    fd = os.open(destination, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    return exported


def require_current_artifacts(
    root: Path, evidence: Path, uid: int, *, already_removing: bool
) -> None:
    if already_removing and not (root / "artifact-inbox").exists():
        return
    preserved = {
        name: value
        for name, value in read(evidence / MANIFEST).items()
        if name.startswith("artifacts/")
    }
    current = {
        f"artifacts/{name}": hashlib.sha256(data).hexdigest()
        for name, data in artifact_files(root, uid)
    }
    if preserved != current:
        raise ValueError("Qualification artifact inbox changed after evidence readback")


def inventory(root: Path) -> Doc:
    return {
        "containers": docker(
            root, "container", "ls", "--all", "--no-trunc", "--format", "{{json .}}"
        ).splitlines(),
        "networks": docker(
            root, "network", "ls", "--no-trunc", "--format", "{{json .}}"
        ).splitlines(),
        "volumes": docker(root, "volume", "ls", "--format", "{{json .}}").splitlines(),
        "images": docker(
            root, "image", "ls", "--all", "--no-trunc", "--format", "{{json .}}"
        ).splitlines(),
    }


def check_root(record: Doc, *, retiring: bool) -> Path:
    root = Path(record["root"])
    if root.parent != TMP or not re.fullmatch(r"klq\.[a-z0-9_]{8}", root.name):
        raise ValueError("Run root is outside the private qualification namespace")
    observed = owned_path(root, 0, traversable=True)
    if [observed.st_dev, observed.st_ino] != record["root_identity"]:
        raise ValueError("Run root was replaced")
    marker = root / "run-id"
    if retiring and not marker.exists() and not any(root.iterdir()):
        return root
    if not stat.S_ISREG(marker.lstat().st_mode) or marker.read_text() != record["run"]:
        raise ValueError("Run root marker was replaced")
    return root


def signal_owned(identity: Doc, sig: int) -> None:
    if live(identity):
        # PID descriptors bind the signal to the recorded incarnation.
        with os.fdopen(os.pidfd_open(identity["pid"]), "rb") as process:
            if not live(identity):
                raise ValueError("Process identity changed")
            signal.pidfd_send_signal(process.fileno(), sig)


def wait_gone(identity: Doc) -> None:
    for _ in range(100):
        if not live(identity):
            return
        time.sleep(0.1)
    raise ValueError("Owned daemon did not exit")


def require_inactive_run(root: Path, endpoint: Doc) -> None:
    for name in ("docker.sock", "ctr.sock"):
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(1)
            try:
                client.connect(str(root / name))
            except OSError as error:
                if error.errno not in (errno.ENOENT, errno.ECONNREFUSED):
                    raise
            else:
                raise ValueError("Qualification root still has an active daemon")
    for process in Path("/proc").iterdir():
        if not process.name.isdecimal() or int(process.name) == os.getpid():
            continue
        try:
            mount, net = namespaces(int(process.name))
            argv = (process / "cmdline").read_bytes().split(b"\0")
        except (FileNotFoundError, ProcessLookupError):
            continue
        if mount == endpoint["mount_namespace"] or net == endpoint["network_namespace"]:
            raise ValueError("Qualification namespaces still have an active process")
        encoded = os.fsencode(root)
        if any(arg == encoded or arg.startswith(encoded + b"/") for arg in argv):
            raise ValueError("A process still references the qualification root")


def retire_root(root: Path) -> None:
    mount = os.fsencode(root)
    for line in Path("/proc/self/mountinfo").read_bytes().splitlines():
        point = line.split()[4]
        if point == mount or point.startswith(mount + b"/"):
            raise ValueError("Qualification root still has a mounted descendant")
    marker = root / "run-id"
    inbox = root / "artifact-inbox"
    if inbox.exists():
        inbox.rename(root / ".retiring-artifacts")
    for child in root.iterdir():
        if child != marker:
            subprocess.run(
                ["rm", "-r", "--one-file-system", "--", str(child)],
                check=True,
                timeout=60,
            )
    marker.unlink(missing_ok=True)
    root.rmdir()


def closeout(
    evidence: Path,
    children: Sequence[subprocess.Popen[bytes]] = (),
    *,
    release_hold: bool = False,
) -> bool:
    record = read(evidence / OWNER)
    if record.get("owner_uid") != int(os.environ["SUDO_UID"]):
        raise ValueError("Only the recorded run owner can close this qualification")
    hold = evidence / "hold.json"
    if hold.exists():
        if not release_hold:
            write(evidence / RESULT, {"state": "retained", "run": record["run"]})
            return False
        write(evidence / "released-hold.json", read(hold))
        hold.unlink()
    prior = read(evidence / RESULT) if (evidence / RESULT).is_file() else {}
    candidate = Path(record["root"])
    if (
        prior.get("run") == record["run"]
        and prior.get("state") in {"removing", "retired"}
        and not candidate.exists()
    ):
        if not evidence_verified(evidence):
            raise ValueError("Evidence readback failed")
        write(evidence / RESULT, {"state": "retired", "run": record["run"]})
        return True
    retiring = prior.get("run") == record["run"] and prior.get("state") in {
        "retiring",
        "removing",
    }
    already_removing = retiring and prior.get("state") == "removing"
    root = check_root(record, retiring=retiring)
    if record.get("workload") and record["workload"]["boot"] == boot_id():
        try:
            os.killpg(record["workload"]["pid"], 0)
        except ProcessLookupError:
            pass
        else:
            raise ValueError("The recorded workload group is still active")
    daemons = (record["dockerd"], record["containerd"])
    still_live = [live(process) for process in daemons]
    if all(still_live):
        checked = verify(root, record, alias=False)
        write(evidence / "teardown-endpoint.json", checked)
        if not (evidence / MANIFEST).exists():
            collect(evidence, root, record, None)
    else:
        if not retiring:
            raise ValueError(
                "Recorded daemon identity is incomplete without verified teardown"
            )
        checked = read(evidence / "teardown-endpoint.json")
        for process, active, name in zip(
            daemons, still_live, ("docker", "containerd"), strict=True
        ):
            if active and (
                namespaces(process["pid"])
                != (checked["mount_namespace"], checked["network_namespace"])
                or list(socket_identity(Path(checked[f"{name}_socket"]), process))
                != checked[f"{name}_socket_identity"]
            ):
                raise ValueError(
                    "Remaining daemon no longer matches the verified teardown"
                )
    if not evidence_verified(evidence):
        raise ValueError("Evidence readback failed")
    if not already_removing:
        write(evidence / RESULT, {"state": "retiring", "run": record["run"]})
    # Shutdown targets the exact daemon incarnations whose endpoints were checked.
    for process, child in zip(daemons, (*children, None, None), strict=False):
        signal_owned(process, signal.SIGTERM)
        if child is not None:
            child.wait(timeout=12)
        wait_gone(process)
    require_inactive_run(root, checked)
    require_current_artifacts(
        root, evidence, record["owner_uid"], already_removing=already_removing
    )
    write(evidence / RESULT, {"state": "removing", "run": record["run"]})
    retire_root(root)
    write(evidence / RESULT, {"state": "retired", "run": record["run"]})
    return True


def collect(evidence: Path, root: Path, record: Doc, result: int | None) -> None:
    write(
        evidence / "outcome.json",
        {
            "exit_code": result,
            "run": record["run"],
            "state": "interrupted" if result is None else "finished",
        },
    )
    write(evidence / "inventory.json", inventory(root))
    with (evidence / "workload.log").open("ab"):
        pass
    for name in ("docker.log", "containerd.log"):
        tail(root / name, evidence / name)
    for name in EVIDENCE:
        with (evidence / name).open("rb") as stream:
            os.fsync(stream.fileno())
    artifacts = export_artifacts(root, evidence, record["owner_uid"])
    write(
        evidence / MANIFEST,
        {name: digest(evidence / name) for name in (*EVIDENCE, OWNER, *artifacts)},
    )


def start_process(args: list[str], log: Path) -> subprocess.Popen[bytes]:
    with log.open("wb") as stream:
        # Keep evidence private without imposing that umask on unpacked
        # container root filesystems, which must support image-configured users.
        return subprocess.Popen(
            args, stdout=stream, stderr=subprocess.STDOUT, umask=0o022
        )


def wait_ready(*args: str) -> None:
    for _ in range(80):
        try:
            output(*args)
            return
        except subprocess.CalledProcessError:
            time.sleep(0.15)
    raise ValueError("Disposable daemon did not become ready")


def workload(
    root: Path, evidence: Path, record: Doc, command: list[str], uid: int, gid: int
) -> int:
    env = os.environ.copy()
    for name in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "BUILDX_CONFIG"):
        env.pop(name, None)
    env["DOCKER_HOST"] = "unix://" + str(DOCKER_ALIAS)
    env["DOCKER_CONFIG"] = str(root / "client")
    env["KODI_LAB_QUALIFICATION_ARTIFACTS"] = str(root / "artifact-inbox")
    read_fd, write_fd = os.pipe()
    gate_script = """import os, sys
fd = int(sys.argv[1])
marker = os.read(fd, 1)
os.close(fd)
if marker != b"1":
    sys.exit(125)
os.execvp(sys.argv[2], sys.argv[2:])
"""
    with (
        os.fdopen(read_fd, "rb") as inherited,
        subprocess.Popen(
            [
                "setpriv",
                f"--reuid={uid}",
                f"--regid={gid}",
                "--init-groups",
                sys.executable,
                "-B",
                "-c",
                gate_script,
                str(inherited.fileno()),
                *command,
            ],
            pass_fds=(inherited.fileno(),),
            cwd=record["worktree"],
            env=env,
            start_new_session=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        ) as process,
        os.fdopen(write_fd, "wb", buffering=0) as gate,
        (evidence / "workload.log").open("wb") as log,
    ):
        inherited.close()
        record["workload"] = stamp(process.pid)
        write(evidence / OWNER, record)
        gate.write(b"1")
        gate.close()
        assert process.stdout is not None
        pipe = process.stdout.fileno()
        remaining = MAX_LOG
        try:
            for chunk in iter(lambda: os.read(pipe, 32768), b""):
                log.write(chunk[:remaining])
                remaining = max(0, remaining - len(chunk))
            return process.wait()
        except (KeyboardInterrupt, SystemExit):
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
            raise


def qualify(source: Path, evidence: Path, command: list[str]) -> int:
    mount, net = namespaces(os.getpid())
    host_mount, host_net = namespaces(1)
    if os.getuid() != 0 or mount == host_mount or net == host_net:
        raise ValueError("Run in separate private Linux mount and network namespaces")
    require_private_mounts()
    uid, gid = int(os.environ["SUDO_UID"]), int(os.environ["SUDO_GID"])
    subprocess.run(["ip", "link", "set", "lo", "up"], check=True, timeout=10)
    source = source.resolve(strict=True)
    git = (
        "setpriv",
        f"--reuid={uid}",
        f"--regid={gid}",
        "--init-groups",
        "git",
        "-c",
        "core.attributesFile=/dev/null",
        "-c",
        "core.excludesFile=/dev/null",
        "--no-optional-locks",
        "-C",
        str(source),
    )
    if output(*git, "status", "--porcelain"):
        raise ValueError("Record a clean worktree for the qualification run")
    commit, tree = output(*git, "rev-parse", "HEAD", "HEAD^{tree}").splitlines()
    free = shutil.disk_usage(TMP)
    if free.free < max(free.total // 10, 12 * 1024**3):
        raise ValueError(
            "Qualification filesystem is below the storage warning threshold"
        )
    root = Path(tempfile.mkdtemp(prefix="klq.", dir=TMP))
    os.chmod(
        root, 0o711
    )  # Traverse for the workload; ownership identity below is private.
    run_id = uuid4().hex
    record: Doc = {
        "run": run_id,
        "root": str(root),
        "root_identity": [root.stat().st_dev, root.stat().st_ino],
        "worktree": str(source),
        "source_commit": commit,
        "source_tree": tree,
        "owner_uid": uid,
        "supervisor": stamp(os.getpid()),
        "namespace": "klq-" + run_id,
    }
    (root / "run-id").write_text(run_id)
    cfg = root / "containerd.toml"
    cfg.write_text(containerd_config(root))
    (root / "daemon.json").write_text("{}")
    for name in ("docker-config", "client", "artifact-inbox"):
        (root / name).mkdir(mode=0o700)
    for name in ("client", "artifact-inbox"):
        os.chown(root / name, uid, gid)
    write(evidence / OWNER, record)
    inner = start_process(["containerd", "--config", str(cfg)], root / "containerd.log")
    outer: subprocess.Popen[bytes] | None = None
    proven = False
    mounted = False
    result = 1
    try:
        record["containerd"] = stamp(inner.pid)
        write(evidence / OWNER, record)
        wait_ready("ctr", "--address", str(root / "ctr.sock"), "version")
        outer = start_process(
            [
                "dockerd",
                "--config-file",
                str(root / "daemon.json"),
                "--host",
                "unix://" + str(root / "docker.sock"),
                "--data-root",
                str(root / "docker-data"),
                "--exec-root",
                str(root / "docker-exec"),
                "--pidfile",
                str(root / "engine.pid"),
                "--containerd",
                str(root / "ctr.sock"),
                "--containerd-namespace",
                record["namespace"],
                "--containerd-plugins-namespace",
                record["namespace"] + "-plugins",
                "--storage-driver",
                "overlayfs",
                "--bridge",
                "none",
                "--iptables=false",
                "--ip6tables=false",
                "--ip-forward=false",
                "--ip-masq=false",
                "--group",
                "docker",
            ],
            root / "docker.log",
        )
        record["dockerd"] = stamp(outer.pid)
        write(evidence / OWNER, record)
        wait_ready(
            "docker",
            "--host",
            "unix://" + str(root / "docker.sock"),
            "info",
            "--format",
            "{{.ID}}",
        )
        endpoint = verify(root, record, alias=False)
        record["engine"] = endpoint["engine"]
        write(evidence / OWNER, record)
        subprocess.run(
            ["mount", "--bind", str(root / "docker.sock"), str(DOCKER_ALIAS)],
            check=True,
            timeout=10,
        )
        mounted = True
        write(evidence / "endpoint.json", verify(root, record, alias=True))
        proven = True
        try:
            result = workload(root, evidence, record, command, uid, gid)
        except KeyboardInterrupt:
            result = 130
        return result
    finally:
        if mounted:
            subprocess.run(["umount", str(DOCKER_ALIAS)], check=True, timeout=10)
        if proven and outer is not None:
            collect(evidence, root, record, result)
            closeout(evidence, (outer, inner))
        elif not proven:
            # Only process handles created by this invocation are stopped
            # before endpoint proof exists. Keep the setup evidence and root.
            for child in (outer, inner):
                if child is not None and child.poll() is None:
                    child.terminate()
                    child.wait(timeout=12)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("run", "closeout"))
    parser.add_argument("evidence", type=Path)
    parser.add_argument("--source", type=Path, default=Path.cwd())
    parser.add_argument(
        "--hold", nargs=4, metavar=("OWNER", "REASON", "SCOPE", "REVIEW_DATE")
    )
    arguments = sys.argv[1:]
    separator = arguments.index("--") if "--" in arguments else len(arguments)
    parser.add_argument("--release-hold", action="store_true")
    options = parser.parse_args(arguments[:separator])
    command = arguments[separator + 1 :]
    evidence = options.evidence.absolute()
    os.umask(0o077)
    if os.getuid() != 0:
        parser.error("Qualification supervision must run as root")
    if options.action == "run":
        if not command or options.release_hold:
            parser.error("Run requires a workload after --")
        evidence.mkdir(mode=0o700)
    with lock(evidence):
        if options.action == "closeout":
            if command or options.hold:
                parser.error("Closeout accepts an exact evidence directory")
            return 0 if closeout(evidence, release_hold=options.release_hold) else 2
        if options.hold:
            owner, reason, scope, review = options.hold
            if not all(value.strip() for value in options.hold):
                parser.error("A hold requires an owner, reason, scope and review date")
            date.fromisoformat(review)
            write(
                evidence / "hold.json",
                {
                    "owner": owner,
                    "reason": reason,
                    "scope": scope,
                    "review_date": review,
                },
            )
        signal.signal(signal.SIGTERM, signal.getsignal(signal.SIGINT))
        return qualify(options.source, evidence, command)


if __name__ == "__main__":
    sys.exit(main())
