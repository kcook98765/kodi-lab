#!/usr/bin/env python3
"""Install a fresh Kodi Lab bundle, or manage that installation without rebuilding."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import platform
import re
import runpy
import shlex
import shutil
import socket
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

SCHEMA = "kodi-lab-host-installation-v1"
MANAGED_NETWORK = "kodi-lab-managed-v1"
MIN_FREE_BYTES = 8 * 1024**3
COMPOSE_COMMANDS = {"config", "exec", "logs", "ps", "run", "stop", "up"}


def environment() -> dict[str, str]:
    # Shell settings must not override the verified bundle's .env or project.
    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("COMPOSE_", "KODI_LAB_"))
    }


def run(command: list[str], cwd: Path, *, capture: bool = False) -> str:
    result = subprocess.run(
        command,
        cwd=cwd,
        env=environment(),
        text=True,
        capture_output=capture,
        check=True,
    )
    return result.stdout.strip() if capture else ""


def host_kind() -> str:
    if sys.platform != "linux" or sys.version_info[:2] != (3, 12):
        raise ValueError("run with Python 3.12 inside Ubuntu 24.04")
    release = platform.freedesktop_os_release()
    if (
        release.get("ID") != "ubuntu"
        or release.get("VERSION_ID") != "24.04"
        or platform.machine().lower() not in {"x86_64", "amd64"}
        or os.geteuid() == 0
    ):
        raise ValueError("use ordinary-user Ubuntu 24.04 on x86_64")
    kernel = platform.release().lower()
    if "microsoft" in kernel and "wsl2" not in kernel:
        raise ValueError("WSL1 is unsupported; convert the distribution to WSL2")
    return "windows-wsl2" if "wsl2" in kernel else "ubuntu"


def engine(cwd: Path, host: str) -> str:
    endpoint = os.environ.get("DOCKER_HOST")
    if os.environ.get("DOCKER_CONTEXT") or not endpoint:
        endpoint = run(
            ["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"],
            cwd,
            capture=True,
        )
    if endpoint != "unix:///var/run/docker.sock":
        raise ValueError("select the local unix:///var/run/docker.sock Docker engine")
    info = json.loads(
        run(["docker", "info", "--format", "{{json .}}"], cwd, capture=True)
    )
    desktop = "docker desktop" in info.get("OperatingSystem", "").lower()
    if (
        info.get("OSType") != "linux"
        or info.get("Architecture") not in {"x86_64", "amd64"}
        or not isinstance(info.get("ID"), str)
        or not info["ID"]
        or any("rootless" in item for item in info.get("SecurityOptions", []))
        or desktop != (host == "windows-wsl2")
    ):
        raise ValueError(
            "use rootful Linux amd64 Docker; WSL requires Docker Desktop integration"
        )
    version = run(["docker", "compose", "version", "--short"], cwd, capture=True)
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)(?:[-+].*)?", version)
    if match is None or tuple(map(int, match.groups())) < (2, 24, 4):
        raise ValueError("Docker Compose 2.24.4 or newer is required")
    return str(info["ID"])


def installation_path(value: str, source: Path | None = None) -> Path:
    path = Path(value).expanduser().absolute()
    if path.resolve() != path or str(path).startswith("/mnt/") or path == Path("/"):
        raise ValueError("use a canonical Linux filesystem directory outside /mnt")
    if source and (path.is_relative_to(source) or source.is_relative_to(path)):
        raise ValueError("installation directory and source checkout must be separate")
    return path


def available_port(address: str, port: int) -> None:
    with socket.socket() as probe:
        probe.bind((address, port))


def check_networks(source: Path) -> None:
    names = run(
        ["docker", "network", "ls", "--format", "{{.Name}}"], source, capture=True
    )
    if MANAGED_NETWORK in names.splitlines():
        raise ValueError(
            "managed network already exists; use the existing installation "
            "or upgrade guide"
        )
    routes = json.loads(
        run(["ip", "-j", "-4", "route", "show", "table", "all"], source, capture=True)
    )
    claimed = [
        item["dst"] for item in routes if item.get("dst") not in {None, "default"}
    ]
    ids = run(["docker", "network", "ls", "-q"], source, capture=True).split()
    if ids:
        networks = json.loads(
            run(["docker", "network", "inspect", *ids], source, capture=True)
        )
        claimed.extend(
            entry["Subnet"]
            for network in networks
            for entry in (network.get("IPAM") or {}).get("Config") or []
            if entry.get("Subnet")
        )
    managed = ipaddress.ip_network("10.250.0.0/24")
    if any(
        managed.overlaps(ipaddress.ip_network(value, strict=False)) for value in claimed
    ):
        raise ValueError("10.250.0.0/24 overlaps a host route or Docker network")


def compose(root: Path, data: dict[str, Any], *, managed: bool = False) -> list[str]:
    command = [
        "docker",
        "compose",
        "--project-name",
        data["project"],
        "--project-directory",
        str(root / "bundle"),
        "--env-file",
        str(root / "bundle/.env"),
        "-f",
        str(root / "bundle/compose.release.yaml"),
    ]
    if data["host"] == "windows-wsl2":
        command += ["-f", str(root / "compose.windows.yaml")]
    if managed:
        command += ["-f", str(root / "bundle/compose.lab.yaml")]
    return [*command, "--profile", "lab"]


def save(root: Path, data: dict[str, Any], stage: str) -> None:
    data["completed_stage"] = stage
    temporary = root / "INSTALLATION.json.tmp"
    temporary.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    temporary.replace(root / "INSTALLATION.json")


def windows_overlay(port: int) -> str:
    # !override replaces the LAN binding; a plain list would append a second port.
    return (
        "services:\n  repository:\n    ports: !override\n"
        f'      - "127.0.0.1:{port}:8110"\n'
    )


def probe(web_port: int) -> None:
    origin = f"http://127.0.0.1:{web_port}"
    opener = build_opener(ProxyHandler({}))
    with opener.open(origin + "/login", timeout=5) as response:
        if response.status != 200:
            raise ValueError("Web login readiness failed")
    request = Request(
        origin + "/mcp",
        headers={
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2026-07-28",
        },
    )
    try:
        with opener.open(request, timeout=5):
            raise ValueError("MCP unexpectedly accepted an anonymous request")
    except HTTPError as error:
        if error.code != 401 or not error.headers.get(
            "WWW-Authenticate", ""
        ).startswith("Bearer"):
            raise ValueError("MCP bearer readiness failed") from error


def load(root: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((root / "INSTALLATION.json").read_text())
    if (
        data.get("schema") != SCHEMA
        or not re.fullmatch(r"kodi-lab-[0-9a-f]{12}", data.get("project", ""))
        or data.get("host") != host_kind()
        or data.get("engine_id") != engine(root, data["host"])
    ):
        raise ValueError("installation receipt, host or Docker engine differs")
    bundle_hash = hashlib.sha256((root / "bundle/BUNDLE.json").read_bytes()).hexdigest()
    if bundle_hash != data.get("bundle_sha256"):
        raise ValueError("bundle identity differs from installation receipt")
    manifest = json.loads((root / "bundle/BUNDLE.json").read_text())
    tool = root / "bundle/linux-v1-bundle"
    tool_hash = "sha256:" + hashlib.sha256(tool.read_bytes()).hexdigest()
    if (
        manifest.get("source_commit") != data.get("source_commit")
        or manifest["files"]["linux-v1-bundle"]["sha256"] != tool_hash
    ):
        raise ValueError("bundle source or verifier identity differs")
    if not (root / "bundle/.env").is_file():
        raise ValueError("bundle is not configured; inspect the partial installation")
    if data["host"] == "windows-wsl2" and (
        (root / "compose.windows.yaml").read_text()
        != windows_overlay(data["repository_local_port"])
    ):
        raise ValueError("Windows repository override differs")
    run(
        [
            sys.executable,
            str(root / "bundle/linux-v1-bundle"),
            "verify",
            str(root / "bundle"),
        ],
        root,
    )
    values = dict(
        line.split("=", 1)
        for line in (root / "bundle/.env").read_text().splitlines()
        if line and not line.startswith("#")
    )
    expected = {
        "KODI_LAB_PUBLISHED_PORT": str(data["web_port"]),
        "KODI_LAB_REPOSITORY_BIND": data["repository_bind"],
        "KODI_LAB_REPOSITORY_PORT": str(data["repository_port"]),
        "KODI_LAB_REPOSITORY_BASE_URL": data["repository_base_url"],
    }
    if any(values.get(key) != value for key, value in expected.items()):
        raise ValueError("site configuration differs from installation receipt")
    return data


def install(args: argparse.Namespace) -> None:
    source = Path(__file__).resolve().parent.parent
    host = host_kind()
    root = installation_path(args.directory, source)
    if root.exists() or root.is_symlink() or not root.parent.is_dir():
        raise ValueError(
            "choose a new installation directory with an existing parent; "
            "partial installs are preserved"
        )
    if str(source).startswith("/mnt/"):
        raise ValueError("clone into the Linux filesystem, outside /mnt")
    for executable in ("docker", "git", "uv", "ip", "sha256sum", "bash"):
        if shutil.which(executable) is None:
            raise ValueError(f"install prerequisite: {executable}")
    if run(["git", "status", "--porcelain"], source, capture=True):
        raise ValueError("source checkout must be a clean commit")
    source_commit = run(["git", "rev-parse", "HEAD"], source, capture=True)
    repository_bind = (
        args.repository_bind or input("Server's private LAN IPv4 address: ").strip()
    )
    repository_url = (
        args.repository_base_url or f"http://{repository_bind}:{args.repository_port}"
    )
    bundle_tool = runpy.run_path(str(source / "scripts/linux-v1-bundle"))
    bundle_tool["validate_repository_config"](
        repository_bind, args.repository_port, repository_url
    )
    ports = [args.web_port, args.repository_port]
    if host == "windows-wsl2":
        ports.append(args.repository_local_port)
    if len(set(ports)) != len(ports) or any(
        not 1024 <= value <= 65535 for value in ports
    ):
        raise ValueError("choose distinct unprivileged ports (1024 through 65535)")
    engine_id = engine(source, host)
    check_networks(source)
    available_port("127.0.0.1", args.web_port)
    available_port(
        "127.0.0.1" if host == "windows-wsl2" else repository_bind,
        args.repository_local_port if host == "windows-wsl2" else args.repository_port,
    )
    if shutil.disk_usage(root.parent).free < MIN_FREE_BYTES:
        raise ValueError(
            "need at least 8 GiB free before building; "
            "Docker and Kodi need additional space"
        )
    data = {
        "schema": SCHEMA,
        "host": host,
        "engine_id": engine_id,
        "project": "kodi-lab-" + uuid.uuid4().hex[:12],
        "source_commit": source_commit,
        "web_port": args.web_port,
        "repository_bind": repository_bind,
        "repository_port": args.repository_port,
        "repository_base_url": repository_url,
        "repository_local_port": args.repository_local_port,
    }
    root.mkdir(mode=0o700)
    shutil.copyfile(Path(__file__), root / "kodi-lab.py")
    save(root, data, "created")
    if host == "windows-wsl2":
        (root / "compose.windows.yaml").write_text(
            windows_overlay(args.repository_local_port)
        )
    try:
        run(["uv", "sync", "--locked", "--no-dev"], source)
        run(
            [
                "bash",
                str(source / "scripts/stage-linux-v1-bundle"),
                str(root / "bundle"),
            ],
            source,
        )
        if (
            json.loads((root / "bundle/BUNDLE.json").read_text()).get("source_commit")
            != source_commit
        ):
            raise ValueError("bundle source commit changed during installation")
        data["bundle_sha256"] = hashlib.sha256(
            (root / "bundle/BUNDLE.json").read_bytes()
        ).hexdigest()
        save(root, data, "staged")
        tool = [sys.executable, str(root / "bundle/linux-v1-bundle")]
        run([*tool, "verify", str(root / "bundle")], root)
        run(
            [
                "docker",
                "image",
                "load",
                "--input",
                str(root / "bundle/kodi-lab-application-images.tar"),
            ],
            root,
        )
        save(root, data, "images_loaded")
        run(
            [
                *tool,
                "configure",
                str(root / "bundle"),
                "--port",
                str(args.web_port),
                "--repository-bind",
                repository_bind,
                "--repository-port",
                str(args.repository_port),
                "--repository-base-url",
                repository_url,
            ],
            root,
        )
        save(root, data, "configured")
        if engine(source, host) != engine_id:
            raise ValueError("Docker engine changed during installation")
        check_networks(source)
        command = compose(root, data)
        run([*command, "config", "--quiet"], root)
        steps = [
            (
                "repository_initialized",
                [
                    "--profile",
                    "repository-init",
                    "run",
                    "--rm",
                    "--no-deps",
                    "repository-init",
                ],
            ),
            (
                "repository_verified",
                [
                    "run",
                    "--rm",
                    "--no-deps",
                    "--entrypoint",
                    "kodi-lab-repository",
                    "repository",
                    "verify",
                ],
            ),
            (
                "database_initialized",
                [
                    "run",
                    "--rm",
                    "--no-deps",
                    "--entrypoint",
                    "kodi-lab-admin",
                    "web",
                    "initialize",
                ],
            ),
            (
                "password_set",
                [
                    "run",
                    "--rm",
                    "--no-deps",
                    "--entrypoint",
                    "kodi-lab-admin",
                    "web",
                    "set-password",
                ],
            ),
            (
                "network_ready",
                [
                    "run",
                    "--rm",
                    "--no-deps",
                    "--entrypoint",
                    "kodi-lab-runtime",
                    "broker",
                    "network-ensure",
                ],
            ),
        ]
        for stage, arguments in steps:
            run([*command, *arguments], root)
            save(root, data, stage)
        run(
            [
                *compose(root, data, managed=True),
                "up",
                "-d",
                "--wait",
                "--no-build",
                "web",
                "broker",
                "repository",
            ],
            root,
        )
        save(root, data, "services_started")
        probe(args.web_port)
        save(root, data, "ready")
    except (Exception, KeyboardInterrupt):
        print(
            f"Installation preserved at {root}; "
            f"last completed stage: {data['completed_stage']}.\n"
            "Do not rerun initialization blindly. See the installation/recovery guide.",
            file=sys.stderr,
        )
        raise
    print(f"Open http://127.0.0.1:{args.web_port}/login and use your chosen password.")
    print(
        "Manage with: "
        + shlex.join(
            ["python3", str(root / "kodi-lab.py"), "status", "--directory", str(root)]
        )
    )
    if host == "windows-wsl2":
        print(
            "Repository backend is loopback-only on port "
            f"{args.repository_local_port}. Configure the documented Windows LAN "
            "forwarding before using it from Kodi devices."
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=("install", "start", "stop", "status", "compose")
    )
    parser.add_argument("--directory", required=True)
    parser.add_argument("--repository-bind")
    parser.add_argument("--repository-base-url")
    parser.add_argument("--repository-port", type=int, default=18120)
    parser.add_argument("--repository-local-port", type=int, default=18121)
    parser.add_argument("--web-port", type=int, default=18100)
    args, extra = parser.parse_known_args()
    try:
        if args.action == "compose":
            extra = extra[1:] if extra[:1] == ["--"] else extra
            if not extra or extra[0] not in COMPOSE_COMMANDS:
                raise ValueError(
                    "compose requires a supported service command after --"
                )
            if any(
                value.split("=", 1)[0]
                in {
                    "-p",
                    "--project-name",
                    "-f",
                    "--file",
                    "--project-directory",
                    "--env-file",
                    "--profile",
                }
                or value.startswith(("-p", "-f"))
                for value in extra
            ):
                raise ValueError("Compose project and configuration are pinned")
        elif extra:
            raise ValueError("unexpected installer arguments")
        if args.action == "install":
            install(args)
        else:
            root = installation_path(args.directory)
            data = load(root)
            if args.action == "start" and data["completed_stage"] != "ready":
                raise ValueError(
                    "partial installation: inspect completed_stage "
                    "and recover explicitly"
                )
            arguments = {
                "start": [
                    "up",
                    "-d",
                    "--wait",
                    "--no-build",
                    "web",
                    "broker",
                    "repository",
                ],
                "stop": ["stop", "web", "broker", "repository"],
                "status": ["ps"],
                "compose": extra,
            }[args.action]
            run([*compose(root, data, managed=True), *arguments], root)
            if args.action == "start":
                probe(data["web_port"])
    except (ValueError, OSError, EOFError, subprocess.CalledProcessError) as error:
        print(f"Kodi Lab setup stopped: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
