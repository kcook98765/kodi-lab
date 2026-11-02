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
import tempfile
import uuid
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

SCHEMA = "kodi-lab-host-installation-v1"
SITE_SCHEMA = "kodi-lab-successor-site-v1"
# Run inside the verified successor image, using its existing startup checks.
# This preflight never migrates or records an installer-owned readiness state.
SUCCESSOR_CHECK = """\
from kodi_lab.config import Settings
from kodi_lab.remote_lifecycle import validate, verify_candidate_transition_completion

settings = Settings(_env_file=None)
root, url = settings.data_root, settings.database_url
validate(root, url, require_head=True, require_marker=True)
if verify_candidate_transition_completion(root, url) is None:
    raise ValueError("successor requires a completed repository candidate transition")
"""
# Official caddy:2.11.7-alpine, linux/amd64 manifest (not a mutable tag).
CADDY_IMAGE = (
    "docker.io/library/caddy:2.11.7-alpine@sha256:"
    "04913b1da43339a40ad1ea87827d70a576ac5aa6cf674c6e24fdca675887533a"
)
MANAGED_NETWORK = "kodi-lab-managed-v1"
MIN_FREE_BYTES = 8 * 1024**3
SOURCE_REPOSITORY = "https://github.com/kcook98765/kodi-lab.git"
COMPOSE_COMMANDS = {"config", "exec", "logs", "ps", "run", "stop", "up"}
GIT_DIRECTORY_OVERRIDES = {
    "GIT_DIR",
    "GIT_COMMON_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
}


def environment() -> dict[str, str]:
    # Shell settings must not override the verified bundle's .env or project.
    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("COMPOSE_", "KODI_LAB_"))
        and key not in GIT_DIRECTORY_OVERRIDES
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


def ingress_config(subnet: str, port: int, routes: list[str]) -> dict[str, Any]:
    network = ipaddress.ip_network(subnet)
    private = [
        ipaddress.IPv4Network(value)
        for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
    ]
    if (
        not isinstance(network, ipaddress.IPv4Network)
        or str(network) != subnet
        or network.prefixlen != 29
        or not any(network.subnet_of(item) for item in private)
        or type(port) is not int
        or not 1024 <= port <= 65535
        or not routes
    ):
        raise ValueError(
            "Caddy requires a canonical RFC1918 IPv4 /29, port and Windows routes"
        )
    normalized = sorted({str(ipaddress.IPv4Network(value)) for value in routes})
    if any(ipaddress.IPv4Network(value).prefixlen == 0 for value in normalized):
        raise ValueError("omit the default Windows route; supply all specific routes")
    for value in [*normalized, "10.250.0.0/24"]:
        if network.overlaps(ipaddress.ip_network(value)):
            raise ValueError(
                "HTTPS ingress overlaps Windows routes or the managed network"
            )
    return {
        "subnet": subnet,
        "gateway": str(network[1]),
        "caddy_ip": str(network[2]),
        "web_ip": str(network[3]),
        "port": port,
        "image": CADDY_IMAGE,
        "windows_routes": normalized,
    }


def ingress_options(args: argparse.Namespace, host: str) -> dict[str, Any] | None:
    proxy = getattr(args, "web_https_proxy", None)
    subnet = getattr(args, "https_ingress_subnet", None)
    port = getattr(args, "https_port", None)
    routes = getattr(args, "windows_route", None)
    if proxy is None:
        if subnet is not None or port is not None or routes:
            raise ValueError("HTTPS options require --web-https-proxy caddy")
        return None
    if proxy != "caddy" or host != "windows-wsl2" or subnet is None:
        raise ValueError("Caddy ingress requires Windows WSL2 and an explicit subnet")
    return ingress_config(subnet, port if port is not None else 18443, routes or [])


def caddyfile(config: dict[str, Any]) -> str:
    return (
        "{\n\tadmin off\n\tauto_https disable_redirects\n"
        "\tdefault_sni 127.0.0.1\n"
        "\tskip_install_trust\n\tservers {\n\t\tprotocols h1 h2\n\t}\n}\n"
        "https://127.0.0.1:8443 {\n\ttls internal\n"
        f"\treverse_proxy {config['web_ip']}:8100\n}}\n"
    )


def windows_overlay(port: int, ingress: dict[str, Any] | None = None) -> str:
    # !override replaces the LAN binding; a plain list would append a second port.
    result = (
        "services:\n  repository:\n    ports: !override\n"
        f'      - "127.0.0.1:{port}:8110"\n'
    )
    if ingress is None:
        return result
    # The lab overlay merges managed/default membership into this map.
    return result + (
        "  web:\n    environment:\n"
        f'      FORWARDED_ALLOW_IPS: "{ingress["caddy_ip"]}"\n'
        "    networks:\n      default: {}\n      https_ingress:\n"
        f"        ipv4_address: {ingress['web_ip']}\n"
        "  caddy:\n"
        f"    image: {CADDY_IMAGE}\n"
        '    user: "10003:10003"\n    read_only: true\n'
        "    cap_drop: [ALL]\n    security_opt: [no-new-privileges:true]\n"
        # The official executable has cap_net_bind_service set. A regular copy
        # drops that file capability, allowing execution with an empty bounding
        # set. The listener uses 8443 and needs no capabilities.
        "    tmpfs:\n"
        "      - /tmp:rw,exec,nosuid,nodev,size=128m,mode=700,uid=10003,gid=10003\n"
        '    entrypoint: ["/bin/sh", "-ec", '
        + json.dumps(
            "cp /usr/bin/caddy /tmp/caddy\n"
            "exec /tmp/caddy run --config /etc/caddy/Caddyfile --adapter caddyfile"
        )
        + "]\n"
        "    healthcheck:\n"
        '      test: ["CMD", "curl", "--fail", "--silent", "--cacert", '
        '"/data/caddy/pki/authorities/local/root.crt", '
        '"https://127.0.0.1:8443/login"]\n'
        "      interval: 3s\n      timeout: 3s\n      retries: 20\n"
        "      start_period: 5s\n"
        "    ports:\n"
        f'      - "127.0.0.1:{ingress["port"]}:8443/tcp"\n'
        "    volumes:\n      - ../Caddyfile:/etc/caddy/Caddyfile:ro\n"
        "      - caddy_data:/data\n      - caddy_config:/config\n"
        "    networks:\n      https_ingress:\n"
        f"        ipv4_address: {ingress['caddy_ip']}\n"
        "networks:\n  https_ingress:\n"
        "    name: ${COMPOSE_PROJECT_NAME}-https-ingress\n"
        "    driver: bridge\n    internal: false\n    ipam:\n      config:\n"
        f"        - subnet: {ingress['subnet']}\n"
        f"          gateway: {ingress['gateway']}\n"
        "volumes:\n  caddy_data: {}\n  caddy_config: {}\n"
    )


def validate_ingress(root: Path, data: dict[str, Any]) -> None:
    config = data.get("https_ingress")
    if config is None:
        if (root / "Caddyfile").exists() or "https_ingress_identity" in data:
            raise ValueError("unrecorded Caddy configuration")
        return
    if (
        data["host"] != "windows-wsl2"
        or config
        != ingress_config(config["subnet"], config["port"], config["windows_routes"])
        or config["port"]
        in {data["web_port"], data["repository_port"], data["repository_local_port"]}
        or (root / "Caddyfile").read_text() != caddyfile(config)
        or data.get("https_ingress_identity") != ingress_identity(data, config)
    ):
        raise ValueError("HTTPS ingress configuration differs")


def ingress_identity(data: dict[str, Any], config: dict[str, Any]) -> dict[str, str]:
    return {
        "caddyfile_sha256": hashlib.sha256(caddyfile(config).encode()).hexdigest(),
        "overlay_sha256": hashlib.sha256(
            windows_overlay(data["repository_local_port"], config).encode()
        ).hexdigest(),
        "ca_volume": data["project"] + "_caddy_data",
        "config_volume": data["project"] + "_caddy_config",
    }


def check_ingress_network(root: Path, data: dict[str, Any]) -> None:
    config = data.get("https_ingress")
    if config is None:
        return
    subnet = ipaddress.ip_network(config["subnet"])
    site_env = root / "bundle/.env"
    if site_env.is_file():
        for line in site_env.read_text().splitlines():
            if line.startswith("KODI_LAB_TEMPORARY_NETWORK_POOL="):
                pool = line.split("=", 1)[1]
                if pool and subnet.overlaps(ipaddress.ip_network(pool)):
                    raise ValueError(
                        "HTTPS ingress overlaps the temporary-network pool"
                    )
    # WSL cannot infer Windows/VPN routes from its Linux route table. Refresh
    # them at every activation, including a sign-in task using the Python helper.
    current_routes = json.loads(
        run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "ConvertTo-Json -Compress -InputObject @((Get-NetRoute "
                "-AddressFamily IPv4 -ErrorAction Stop).DestinationPrefix | "
                "Where-Object {$_ -ne '0.0.0.0/0'})",
            ],
            root,
            capture=True,
        )
    )
    ingress_config(config["subnet"], config["port"], current_routes)
    name = data["project"] + "-https-ingress"
    ids = run(["docker", "network", "ls", "-q"], root, capture=True).split()
    networks = (
        json.loads(run(["docker", "network", "inspect", *ids], root, capture=True))
        if ids
        else []
    )
    own_bridge = None
    for network in networks:
        if network["Name"] == name:
            if (
                network.get("Driver") != "bridge"
                or network.get("Internal") is not False
                or network.get("EnableIPv6")
                or network.get("Labels", {}).get("com.docker.compose.project")
                != data["project"]
                or network.get("IPAM", {}).get("Config")
                != [{"Subnet": config["subnet"], "Gateway": config["gateway"]}]
            ):
                raise ValueError("HTTPS ingress network identity differs")
            own_bridge = "br-" + network["Id"][:12]
            for container_id, endpoint in (network.get("Containers") or {}).items():
                container = json.loads(
                    run(["docker", "inspect", container_id], root, capture=True)
                )[0]
                labels = container["Config"].get("Labels") or {}
                service = labels.get("com.docker.compose.service")
                if (
                    labels.get("com.docker.compose.project") != data["project"]
                    or service not in {"web", "caddy"}
                    or endpoint["IPv4Address"].split("/")[0] != config[service + "_ip"]
                    or (
                        service == "caddy"
                        and set(container["NetworkSettings"]["Networks"]) != {name}
                    )
                ):
                    raise ValueError("unexpected HTTPS ingress member or address")
            continue
        for entry in (network.get("IPAM") or {}).get("Config") or []:
            if entry.get("Subnet") and subnet.overlaps(
                ipaddress.ip_network(entry["Subnet"])
            ):
                raise ValueError("HTTPS ingress overlaps a Docker network")
    routes = json.loads(
        run(["ip", "-j", "-4", "route", "show", "table", "all"], root, capture=True)
    )
    for route in routes:
        if (
            route.get("dst") not in {None, "default"}
            and subnet.overlaps(ipaddress.ip_network(route["dst"], strict=False))
            and route.get("dev") != own_bridge
        ):
            raise ValueError("HTTPS ingress overlaps a Linux route")


def services(data: dict[str, Any]) -> list[str]:
    return [
        "web",
        "broker",
        "repository",
        *(["caddy"] if data.get("https_ingress") else []),
    ]


def probe(web_port: int) -> None:
    origin = f"http://127.0.0.1:{web_port}"
    opener = build_opener(ProxyHandler({}))
    with opener.open(origin + "/login", timeout=5) as response:
        if response.status != 200:
            raise ValueError("Web login readiness failed")
    request = Request(
        origin + "/mcp",
        method="POST",
        headers={
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": "2026-07-28",
            "Mcp-Method": "tools/list",
        },
        data=json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 0,
                "method": "tools/list",
                "params": {
                    "_meta": {
                        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                        "io.modelcontextprotocol/clientCapabilities": {},
                        "io.modelcontextprotocol/clientInfo": {
                            "name": "kodi-lab-installer",
                            "version": "1",
                        },
                    }
                },
            }
        ).encode("utf-8"),
    )
    try:
        with opener.open(request, timeout=5):
            raise ValueError("MCP unexpectedly accepted an anonymous request")
    except HTTPError as error:
        challenge = error.headers.get("WWW-Authenticate", "")
        if error.code != 401 or challenge.split(" ", 1)[0].lower() != "bearer":
            raise ValueError("MCP bearer readiness failed") from error


def load(root: Path) -> dict[str, Any]:
    receipt = root / "INSTALLATION.json"
    if (root / "SITE.json").exists():
        if receipt.exists():
            raise ValueError("ambiguous installation and successor site records")
        receipt = root / "SITE.json"
    data: dict[str, Any] = json.loads(receipt.read_text())
    if (
        data.get("schema") != (SITE_SCHEMA if receipt.name == "SITE.json" else SCHEMA)
        or not re.fullmatch(r"kodi-lab-[0-9a-f]{12}", data.get("project", ""))
        or data.get("host") != host_kind()
        or data.get("engine_id") != engine(root, data["host"])
    ):
        raise ValueError("installation receipt, host or Docker engine differs")
    if receipt.name == "SITE.json" and hashlib.sha256(
        Path(data["previous_receipt"]).read_bytes()
    ).hexdigest() != data.get("previous_receipt_sha256"):
        raise ValueError("original installation evidence differs")
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
    validate_ingress(root, data)
    if data["host"] == "windows-wsl2" and (
        (root / "compose.windows.yaml").read_text()
        != windows_overlay(data["repository_local_port"], data.get("https_ingress"))
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


def listener_preflight(root: Path, data: dict[str, Any]) -> None:
    bind = "127.0.0.1" if data["host"] == "windows-wsl2" else data["repository_bind"]
    run(
        [
            sys.executable,
            str(root / "bundle/linux-v1-bundle"),
            "preflight",
            str(root / "bundle"),
            "--repository-bind",
            bind,
        ],
        root,
    )


def successor_preflight(root: Path, data: dict[str, Any]) -> None:
    if data.get("schema") != SITE_SCHEMA:
        return
    previous = data
    seen: set[Path] = set()
    same_bundle = True
    while previous.get("schema") == SITE_SCHEMA:
        receipt = Path(previous["previous_receipt"])
        if receipt in seen:
            raise ValueError("successor receipt ancestry contains a cycle")
        seen.add(receipt)
        body = receipt.read_bytes()
        if hashlib.sha256(body).hexdigest() != previous["previous_receipt_sha256"]:
            raise ValueError("successor receipt ancestry differs")
        previous = json.loads(body)
        same_bundle = previous["bundle_sha256"] == data["bundle_sha256"] and same_bundle
    if previous.get("schema") != SCHEMA or previous.get("completed_stage") != "ready":
        raise ValueError("successor has no completed installation ancestor")
    if same_bundle:
        return  # Every verified ancestor uses the proposed application bundle.
    run(
        [
            *compose(root, data),
            "run",
            "--rm",
            "--no-deps",
            "--user",
            "10001:10001",
            "--env",
            "KODI_LAB_REPOSITORY_DATA_ROOT=/srv/kodi-lab-repository",
            "--entrypoint",
            "python",
            "repository-transition",
            "-c",
            SUCCESSOR_CHECK,
        ],
        root,
    )


def bootstrap(args: argparse.Namespace) -> None:
    """A downloaded copy can obtain a clean checkout without knowing its layout."""
    host = host_kind()
    ingress = ingress_options(args, host)
    destination = installation_path(args.directory)
    if (
        destination.exists()
        or destination.is_symlink()
        or not destination.parent.is_dir()
    ):
        raise ValueError("choose a new installation directory with an existing parent")
    if args.revision != "main" and not re.fullmatch(r"[0-9a-f]{40}", args.revision):
        raise ValueError("revision must be main or a full lowercase Git commit ID")
    for executable in ("git", "uv", "docker", "bash", "ip", "sha256sum"):
        if shutil.which(executable) is None:
            raise ValueError(f"install prerequisite: {executable}")
    engine(destination.parent, host)
    check_networks(destination.parent)
    source_parent = installation_path(
        str(
            Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share")))
            / "kodi-lab/sources"
        )
    )
    if destination.is_relative_to(source_parent) or source_parent.is_relative_to(
        destination
    ):
        raise ValueError("source storage and installation directory must be separate")
    source_parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    checkout = Path(tempfile.mkdtemp(prefix="checkout-", dir=source_parent))
    print(f"Source checkout retained at {checkout}", flush=True)
    run(["git", "init", "--quiet", str(checkout)], source_parent)
    run(["git", "remote", "add", "origin", SOURCE_REPOSITORY], checkout)
    run(["git", "fetch", "--depth", "1", "origin", args.revision], checkout)
    run(["git", "checkout", "--quiet", "--detach", "FETCH_HEAD"], checkout)
    commit = run(["git", "rev-parse", "HEAD"], checkout, capture=True)
    if not re.fullmatch(r"[0-9a-f]{40}", commit) or (
        args.revision != "main" and args.revision != commit
    ):
        raise ValueError("downloaded source commit differs from requested revision")
    print(f"Installing source commit {commit}", flush=True)
    command = [
        sys.executable,
        str(checkout / "scripts/install.py"),
        "install",
        "--directory",
        str(destination),
        "--web-port",
        str(args.web_port),
        "--repository-port",
        str(args.repository_port),
        "--repository-local-port",
        str(args.repository_local_port),
    ]
    if args.repository_bind:
        command += ["--repository-bind", args.repository_bind]
    if args.repository_base_url:
        command += ["--repository-base-url", args.repository_base_url]
    if ingress:
        command += [
            "--web-https-proxy",
            "caddy",
            "--https-ingress-subnet",
            ingress["subnet"],
            "--https-port",
            str(ingress["port"]),
        ]
        for route in ingress["windows_routes"]:
            command += ["--windows-route", route]
    run(command, checkout)


def install(args: argparse.Namespace) -> None:
    source = Path(__file__).resolve().parent.parent
    host = host_kind()
    ingress = ingress_options(args, host)
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
    if ingress:
        ports.append(ingress["port"])
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
    if ingress:
        data["https_ingress"] = ingress
        data["https_ingress_identity"] = ingress_identity(data, ingress)
        check_ingress_network(source, data)
        available_port("127.0.0.1", ingress["port"])
    root.mkdir(mode=0o700)
    shutil.copyfile(Path(__file__), root / "kodi-lab.py")
    save(root, data, "created")
    if host == "windows-wsl2":
        (root / "compose.windows.yaml").write_text(
            windows_overlay(args.repository_local_port, ingress)
        )
        if ingress:
            (root / "Caddyfile").write_text(caddyfile(ingress))
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
        listener_preflight(root, data)
        if engine(source, host) != engine_id:
            raise ValueError("Docker engine changed during installation")
        check_networks(source)
        check_ingress_network(root, data)
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
                *services(data),
            ],
            root,
        )
        save(root, data, "services_started")
        check_ingress_network(root, data)
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
    if ingress:
        print(
            f"After manual CA trust, open https://127.0.0.1:{ingress['port']}/login. "
            "See the Windows HTTPS guide."
        )
    else:
        print(
            f"Open http://127.0.0.1:{args.web_port}/login and use your chosen password."
        )
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


def configure_successor(args: argparse.Namespace) -> None:
    """Bind site files to a staged successor without upgrading application state."""
    root = installation_path(args.directory)
    previous = installation_path(args.previous_installation)
    old = load(previous)
    if old["host"] != "windows-wsl2" or old["completed_stage"] not in {
        "ready",
        "successor_site_configured",
    }:
        raise ValueError(
            "successor configuration requires a completed Windows installation"
        )
    if (
        root == previous
        or root.is_relative_to(previous)
        or previous.is_relative_to(root)
    ):
        raise ValueError(
            "successor site must be separate from the previous installation"
        )
    if not root.is_dir() or set(path.name for path in root.iterdir()) != {"bundle"}:
        raise ValueError(
            "successor directory must contain only the new verified bundle/"
        )
    ingress = ingress_options(args, old["host"])
    manifest = json.loads((root / "bundle/BUNDLE.json").read_text())
    # Check the verifier against the manifest before executing it, as load() does.
    tool = root / "bundle/linux-v1-bundle"
    if (
        manifest["files"]["linux-v1-bundle"]["sha256"]
        != "sha256:" + hashlib.sha256(tool.read_bytes()).hexdigest()
    ):
        raise ValueError("successor verifier identity differs")
    run([sys.executable, str(tool), "verify", str(root / "bundle")], root)

    # Carry every configured site/runtime value; only application identities change.
    def values(path: Path) -> dict[str, str]:
        return dict(
            line.split("=", 1)
            for line in path.read_text().splitlines()
            if line and not line.startswith("#")
        )

    changed_images = {
        "KODI_LAB_REMOTE_IMAGE",
        "KODI_LAB_BROKER_IMAGE",
        "KODI_LAB_REPOSITORY_BUNDLE_SHA256",
    }
    if {
        key: value
        for key, value in values(previous / "bundle/.env").items()
        if key not in changed_images
    } != {
        key: value
        for key, value in values(root / "bundle/.env").items()
        if key not in changed_images
    }:
        raise ValueError("successor must preserve all site and runtime configuration")
    previous_receipt = previous / (
        "SITE.json" if old["schema"] == SITE_SCHEMA else "INSTALLATION.json"
    )
    data = {
        **old,
        "schema": SITE_SCHEMA,
        "source_commit": manifest["source_commit"],
        "bundle_sha256": hashlib.sha256(
            (root / "bundle/BUNDLE.json").read_bytes()
        ).hexdigest(),
        "previous_receipt": str(previous_receipt),
        "previous_receipt_sha256": hashlib.sha256(
            previous_receipt.read_bytes()
        ).hexdigest(),
        "completed_stage": "successor_site_configured",
    }
    data.pop("https_ingress", None)
    data.pop("https_ingress_identity", None)
    if ingress:
        data["https_ingress"] = ingress
        data["https_ingress_identity"] = ingress_identity(data, ingress)
        check_ingress_network(root, data)
    (root / "compose.windows.yaml").write_text(
        windows_overlay(data["repository_local_port"], ingress)
    )
    if ingress:
        (root / "Caddyfile").write_text(caddyfile(ingress))
    shutil.copyfile(Path(__file__), root / "kodi-lab.py")
    (root / "SITE.json").write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    load(root)
    print(
        "Successor site configured; no application upgrade or service activation "
        "performed. Follow the candidate-transition and administrator-upgrade "
        "guide before start."
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=(
            "bootstrap",
            "install",
            "configure-successor",
            "start",
            "stop",
            "status",
            "compose",
        ),
    )
    parser.add_argument("--directory")
    parser.add_argument("--revision", default="main")
    parser.add_argument("--repository-bind")
    parser.add_argument("--repository-base-url")
    parser.add_argument("--repository-port", type=int, default=18120)
    parser.add_argument("--repository-local-port", type=int, default=18121)
    parser.add_argument("--web-port", type=int, default=18100)
    parser.add_argument("--web-https-proxy", choices=("caddy",))
    parser.add_argument("--https-ingress-subnet")
    parser.add_argument("--https-port", type=int)
    parser.add_argument("--windows-route", action="append")
    parser.add_argument("--previous-installation")
    args, extra = parser.parse_known_args()
    try:
        if args.previous_installation and args.action != "configure-successor":
            raise ValueError("--previous-installation requires configure-successor")
        if args.action not in {"install", "bootstrap", "configure-successor"} and any(
            (
                args.web_https_proxy,
                args.https_ingress_subnet,
                args.https_port is not None,
                args.windows_route,
                args.previous_installation,
            )
        ):
            raise ValueError(
                "site options apply only to installation or successor configuration"
            )
        if args.action == "configure-successor" and not args.previous_installation:
            raise ValueError("--previous-installation is required")
        if not args.directory:
            if args.action != "bootstrap":
                raise ValueError("--directory is required for this action")
            args.directory = str(Path.home() / "kodi-lab-install")
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
        if args.action == "bootstrap":
            bootstrap(args)
        elif args.action == "install":
            install(args)
        elif args.action == "configure-successor":
            configure_successor(args)
        else:
            root = installation_path(args.directory)
            data = load(root)
            if args.action == "start" and data["completed_stage"] not in {
                "ready",
                "successor_site_configured",
            }:
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
                    *services(data),
                ],
                "stop": ["stop", *services(data)],
                "status": ["ps"],
                "compose": extra,
            }[args.action]
            if args.action == "start":
                listener_preflight(root, data)
            activating = arguments[0] in {"up", "run"}
            if activating:
                check_ingress_network(root, data)
            if arguments[0] == "up":
                successor_preflight(root, data)
            run([*compose(root, data, managed=True), *arguments], root)
            if activating:
                check_ingress_network(root, data)
            if args.action == "start":
                probe(data["web_port"])
    except (
        ValueError,
        KeyError,
        TypeError,
        OSError,
        EOFError,
        subprocess.CalledProcessError,
    ) as error:
        print(f"Kodi Lab setup stopped: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
