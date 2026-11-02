"""Opt-in ingress pins one peer, canonical site bytes and installation identity."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from scripts import install
from tests.packaging.test_host_installer import Host, mcp_opener
from tests.packaging.test_linux_v1_bundle import bundle as verified_bundle

bundle = verified_bundle
opener = mcp_opener
ROOT = Path(__file__).resolve().parents[2]
SUBNET = "192.168.241.0/29"
ROUTES = ["192.168.1.0/24", "172.28.0.0/20", "127.0.0.0/8"]


def config() -> dict[str, Any]:
    return install.ingress_config(SUBNET, 18443, ROUTES)


@pytest.mark.parametrize(
    "subnet",
    [
        "*",
        "172.18.0.1",
        "192.168.241.2/29",
        "192.168.241.0/24",
        "0.0.0.0/29",
        "8.8.8.0/29",
        "::1/128",
        "10.250.0.0/29",
        "192.168.1.0/29",
        "proxy",
        "192.168.241.2,172.18.0.1",
    ],
)
def test_broad_noncanonical_gateway_and_overlapping_input_rejected(subnet: str) -> None:
    with pytest.raises(ValueError):
        install.ingress_config(subnet, 18443, ROUTES)


@pytest.mark.parametrize("port", [0, 443, 65536, True, "18443"])
def test_invalid_tls_ports_rejected(port: Any) -> None:
    with pytest.raises(ValueError):
        install.ingress_config(SUBNET, port, ROUTES)


def test_opt_in_requires_windows_subnet_and_route_evidence() -> None:
    cases: list[tuple[dict[str, Any], str]] = [
        ({"web_https_proxy": "caddy", "https_ingress_subnet": SUBNET}, "ubuntu"),
        ({"web_https_proxy": "caddy", "https_ingress_subnet": SUBNET}, "windows-wsl2"),
        ({"https_port": 18443}, "windows-wsl2"),
        ({"web_https_proxy": "caddy", "windows_route": ROUTES}, "windows-wsl2"),
    ]
    for options, host in cases:
        with pytest.raises(ValueError):
            install.ingress_options(argparse.Namespace(**options), host)
    with pytest.raises(ValueError):
        install.ingress_config(SUBNET, 18443, ["0.0.0.0/0"])
    assert install.ingress_options(argparse.Namespace(), "ubuntu") is None
    assert config()["caddy_ip"] == "192.168.241.2"
    assert config()["gateway"] == "192.168.241.1"


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opener: MagicMock) -> Host:
    host = Host(tmp_path / "installation", monkeypatch)
    host.kind = "windows-wsl2"
    base = host.run

    def run(command: list[str], cwd: Path, *, capture: bool = False) -> str:
        if command[0] == "powershell.exe":
            host.commands.append(command)
            return json.dumps(ROUTES)
        return base(command, cwd, capture=capture)

    monkeypatch.setattr(install, "run", run)
    return host


def options(host: Host) -> argparse.Namespace:
    args = host.args()
    args.web_https_proxy = "caddy"
    args.https_ingress_subnet = SUBNET
    args.https_port = 18443
    args.windows_route = ROUTES
    return args


def test_opt_in_receipt_and_lifecycle_preserve_exact_configuration(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    install.install(options(host))
    data = json.loads((host.root / "INSTALLATION.json").read_text())
    assert data["https_ingress"] == config()
    assert (host.root / "Caddyfile").read_text() == install.caddyfile(config())
    assert (host.root / "compose.windows.yaml").read_text() == install.windows_overlay(
        18121, config()
    )
    original = {
        path.name: path.read_bytes() for path in host.root.iterdir() if path.is_file()
    }
    monkeypatch.setattr(install, "load", lambda _: data)
    for action in ("stop", "start"):
        monkeypatch.setattr(
            sys, "argv", ["installer", action, "--directory", str(host.root)]
        )
        assert install.main() == 0
        assert any(
            "caddy" in command and ("up" in command or "stop" in command)
            for command in host.commands
        )
    assert original == {
        path.name: path.read_bytes() for path in host.root.iterdir() if path.is_file()
    }
    assert not any("down" in command or "prune" in command for command in host.commands)


def test_conflicting_tls_port_fails_before_installation(host: Host) -> None:
    args = options(host)
    args.https_port = args.web_port
    with pytest.raises(ValueError, match="distinct"):
        install.install(args)
    assert not host.root.exists()


def test_proxy_failure_retains_partial_installation_and_never_reinitializes(
    host: Host,
) -> None:
    host.fail = "up"
    with pytest.raises(subprocess.CalledProcessError):
        install.install(options(host))
    assert (
        json.loads((host.root / "INSTALLATION.json").read_text())["completed_stage"]
        == "network_ready"
    )
    assert (host.root / "Caddyfile").is_file()
    before = list(host.commands)
    with pytest.raises(ValueError, match="partial installs"):
        install.install(options(host))
    assert host.commands == before


def test_merged_compose_exposes_only_loopback_and_two_ingress_members(
    tmp_path: Path,
) -> None:
    site = tmp_path / "site"
    (site / "bundle").mkdir(parents=True)
    for name in ("compose.release.yaml", "compose.lab.yaml"):
        shutil.copyfile(ROOT / name, site / "bundle" / name)
    (site / "compose.windows.yaml").write_text(install.windows_overlay(18121, config()))
    (site / "Caddyfile").write_text(install.caddyfile(config()))
    (site / "bundle/.env").write_text(
        "KODI_LAB_REMOTE_IMAGE=sha256:" + "a" * 64 + "\n"
        "KODI_LAB_BROKER_IMAGE=sha256:" + "b" * 64 + "\n"
        "KODI_LAB_REPOSITORY_BIND=192.168.1.2\nKODI_LAB_REPOSITORY_PORT=18120\n"
        "KODI_LAB_REPOSITORY_BASE_URL=http://192.168.1.2:18120\n"
        "KODI_LAB_REPOSITORY_BUNDLE_SHA256=" + "c" * 64 + "\n"
    )
    command = install.compose(
        site, {"project": "kodi-lab-012345abcdef", "host": "windows-wsl2"}, managed=True
    )
    rendered = json.loads(
        install.run([*command, "config", "--format", "json"], site, capture=True)
    )
    services = rendered["services"]
    assert {
        name
        for name, service in services.items()
        if "https_ingress" in service.get("networks", {})
    } == {"web", "caddy"}
    assert set(services["caddy"]["networks"]) == {"https_ingress"}
    assert set(services["web"]["networks"]) == {"default", "managed", "https_ingress"}
    assert services["web"]["environment"]["FORWARDED_ALLOW_IPS"] == "192.168.241.2"
    assert rendered["networks"]["https_ingress"].get("internal", False) is False
    assert (
        rendered["networks"]["https_ingress"]["name"]
        == "kodi-lab-012345abcdef-https-ingress"
    )
    caddy = services["caddy"]
    assert caddy["image"] == install.CADDY_IMAGE
    assert caddy["user"] == "10003:10003" and caddy["cap_drop"] == ["ALL"]
    assert caddy["read_only"] and caddy["security_opt"] == ["no-new-privileges:true"]
    assert caddy["ports"][0]["host_ip"] == "127.0.0.1"
    assert caddy["ports"][0]["published"] == "18443"
    assert caddy["ports"][0]["target"] == 8443 and len(caddy["ports"]) == 1
    assert {mount["target"] for mount in caddy["volumes"]} == {
        "/data",
        "/config",
        "/etc/caddy/Caddyfile",
    }
    assert next(m for m in caddy["volumes"] if m["target"] == "/etc/caddy/Caddyfile")[
        "source"
    ] == str(site / "Caddyfile")
    assert {
        name
        for name, service in services.items()
        if any(
            m["target"] == "/var/run/docker.sock" for m in service.get("volumes", [])
        )
    } == {"broker"}
    for name in ("web", "repository"):
        assert all(port["host_ip"] == "127.0.0.1" for port in services[name]["ports"])


def installed_bundle(bundle: Path, root: Path) -> dict[str, Any]:
    root.mkdir()
    shutil.copytree(
        bundle,
        root / "bundle",
        ignore=shutil.ignore_patterns("installed", "old", "successor"),
    )
    manifest = json.loads((root / "bundle/BUNDLE.json").read_text())
    images = manifest["application"]["images"]
    bundle_hash = hashlib.sha256((root / "bundle/BUNDLE.json").read_bytes()).hexdigest()
    (root / "bundle/.env").write_text(
        f"KODI_LAB_REMOTE_IMAGE={images['web']['saved_image_id']}\n"
        f"KODI_LAB_BROKER_IMAGE={images['broker']['saved_image_id']}\n"
        "KODI_LAB_PUBLISHED_PORT=18100\nKODI_LAB_REPOSITORY_BIND=192.168.2.20\n"
        "KODI_LAB_REPOSITORY_PORT=18120\nKODI_LAB_REPOSITORY_BASE_URL=http://192.168.2.20:18120\n"
        f"KODI_LAB_REPOSITORY_BUNDLE_SHA256={bundle_hash}\n"
    )
    return {
        "schema": install.SCHEMA,
        "host": "windows-wsl2",
        "engine_id": "engine-one",
        "project": "kodi-lab-012345abcdef",
        "source_commit": manifest["source_commit"],
        "bundle_sha256": bundle_hash,
        "web_port": 18100,
        "repository_bind": "192.168.2.20",
        "repository_port": 18120,
        "repository_local_port": 18121,
        "repository_base_url": "http://192.168.2.20:18120",
    }


@pytest.mark.parametrize(
    "tamper", ["overlay", "caddyfile", "peer", "image", "gateway", "port", "omit"]
)
def test_real_verifier_rejects_ingress_or_receipt_tampering(
    bundle: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tamper: str
) -> None:
    root = tmp_path / "installed"
    data = installed_bundle(bundle, root)
    data["https_ingress"] = config()
    data["https_ingress_identity"] = install.ingress_identity(data, config())
    (root / "compose.windows.yaml").write_text(install.windows_overlay(18121, config()))
    (root / "Caddyfile").write_text(install.caddyfile(config()))
    install.save(root, data, "ready")
    monkeypatch.setattr(install, "host_kind", lambda: "windows-wsl2")
    monkeypatch.setattr(install, "engine", lambda *_: "engine-one")
    assert install.load(root)["https_ingress"] == config()
    if tamper in {"overlay", "caddyfile"}:
        path = root / ("Caddyfile" if tamper == "caddyfile" else "compose.windows.yaml")
        path.write_text(path.read_text() + "# drift\n")
    else:
        if tamper == "omit":
            del data["https_ingress"]
        else:
            data["https_ingress"][
                {
                    "peer": "caddy_ip",
                    "image": "image",
                    "gateway": "gateway",
                    "port": "port",
                }[tamper]
            ] = {
                "peer": "*",
                "image": "caddy:latest",
                "gateway": "192.168.241.2",
                "port": 18100,
            }[tamper]
        install.save(root, data, "ready")
    with pytest.raises(ValueError):
        install.load(root)


@pytest.mark.parametrize("disable", [False, True])
def test_successor_record_preserves_old_receipt_project_and_bundle_bytes(
    bundle: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, disable: bool
) -> None:
    old = tmp_path / "old"
    data = installed_bundle(bundle, old)
    (old / "compose.windows.yaml").write_text(install.windows_overlay(18121))
    install.save(old, data, "ready")
    old_receipt = (old / "INSTALLATION.json").read_bytes()
    successor = tmp_path / "successor"
    successor.mkdir()
    shutil.copytree(old / "bundle", successor / "bundle")
    before = {
        str(p.relative_to(successor)): p.read_bytes()
        for p in (successor / "bundle").rglob("*")
        if p.is_file()
    }
    monkeypatch.setattr(install, "host_kind", lambda: "windows-wsl2")
    monkeypatch.setattr(install, "engine", lambda *_: "engine-one")
    monkeypatch.setattr(install, "check_ingress_network", lambda *_: None)
    args = argparse.Namespace(
        directory=str(successor),
        previous_installation=str(old),
        web_https_proxy=None if disable else "caddy",
        https_ingress_subnet=None if disable else SUBNET,
        windows_route=None if disable else ROUTES,
    )
    install.configure_successor(args)
    new = install.load(successor)
    assert new["schema"] == install.SITE_SCHEMA and new["project"] == data["project"]
    assert new["previous_receipt_sha256"] == hashlib.sha256(old_receipt).hexdigest()
    assert new.get("https_ingress") == (None if disable else config())
    assert not (successor / "INSTALLATION.json").exists()
    assert (old / "INSTALLATION.json").read_bytes() == old_receipt
    assert before == {
        str(p.relative_to(successor)): p.read_bytes()
        for p in (successor / "bundle").rglob("*")
        if p.is_file()
    }
    (old / "INSTALLATION.json").write_bytes(old_receipt + b" ")
    with pytest.raises(ValueError, match="original installation evidence"):
        install.load(successor)


@pytest.mark.parametrize(
    "claim", ["docker", "linux", "windows", "members", "gateway", "external"]
)
def test_activation_rejects_overlap_and_existing_network_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, claim: str
) -> None:
    data: dict[str, Any] = {
        "project": "kodi-lab-012345abcdef",
        "https_ingress": config(),
    }
    network: dict[str, Any] = {
        "Name": data["project"] + "-https-ingress",
        "Id": "a" * 64,
        "Driver": "bridge",
        "Internal": False,
        "Labels": {"com.docker.compose.project": data["project"]},
        "IPAM": {"Config": [{"Subnet": SUBNET, "Gateway": config()["gateway"]}]},
        "Containers": {},
    }
    if claim == "docker":
        network["Name"] = "unrelated"
    if claim == "gateway":
        network["IPAM"]["Config"][0]["Gateway"] = config()["caddy_ip"]
    if claim == "external":
        network["Internal"] = True
    if claim == "members":
        network["Containers"] = {"bad": {"IPv4Address": config()["caddy_ip"] + "/29"}}

    def run(command: list[str], cwd: Path, *, capture: bool = False) -> str:
        if command[0] == "powershell.exe":
            return json.dumps([SUBNET] if claim == "windows" else ROUTES)
        if command[:3] == ["docker", "network", "ls"]:
            return "network-id"
        if command[:3] == ["docker", "network", "inspect"]:
            return json.dumps([network])
        if command[:2] == ["docker", "inspect"]:
            return json.dumps(
                [{"Config": {"Labels": {}}, "NetworkSettings": {"Networks": {}}}]
            )
        return json.dumps([{"dst": SUBNET, "dev": "vpn0"}] if claim == "linux" else [])

    monkeypatch.setattr(install, "run", run)
    with pytest.raises(ValueError):
        install.check_ingress_network(tmp_path, data)


def test_reserved_temporary_network_pool_is_not_available_for_ingress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "bundle").mkdir()
    (tmp_path / "bundle/.env").write_text(
        "KODI_LAB_TEMPORARY_NETWORK_POOL=192.168.240.0/20\n"
    )
    monkeypatch.setattr(
        install,
        "run",
        lambda *_args, **_kwargs: pytest.fail(
            "overlap must fail before activation queries"
        ),
    )
    with pytest.raises(ValueError, match="temporary-network pool"):
        install.check_ingress_network(tmp_path, {"https_ingress": config()})


def test_previous_installation_option_is_not_silently_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "installer",
            "install",
            "--directory",
            "/unused",
            "--previous-installation",
            "/previous",
        ],
    )
    monkeypatch.setattr(
        install, "host_kind", lambda: pytest.fail("invalid options must stop first")
    )
    assert install.main() == 1


@pytest.mark.parametrize("action", ["start", "compose"])
@pytest.mark.parametrize("completed", [False, True])
def test_successor_up_checks_installed_completion_before_starting_services(
    host: Host, monkeypatch: pytest.MonkeyPatch, action: str, completed: bool
) -> None:
    previous = host.root.parent / "previous-receipt.json"
    previous.write_text(
        json.dumps(
            {
                "schema": install.SCHEMA,
                "completed_stage": "ready",
                "bundle_sha256": "a" * 64,
            }
        )
    )
    data = {
        "schema": install.SITE_SCHEMA,
        "project": "kodi-lab-012345abcdef",
        "host": "windows-wsl2",
        "previous_receipt": str(previous),
        "previous_receipt_sha256": hashlib.sha256(previous.read_bytes()).hexdigest(),
        "bundle_sha256": "b" * 64,
        "completed_stage": "successor_site_configured",
        "web_port": 18100,
        "repository_bind": "192.168.2.20",
    }
    monkeypatch.setattr(install, "load", lambda _: data)
    monkeypatch.setattr(
        sys,
        "argv",
        ["installer", action, "--directory", str(host.root)]
        + (
            ["--", "up", "-d", "web", "broker", "repository"]
            if action == "compose"
            else []
        ),
    )
    if not completed:
        host.fail = "repository-transition"
    assert install.main() == (0 if completed else 1)
    preflight = next(
        command for command in host.commands if "repository-transition" in command
    )
    assert preflight[preflight.index("--user") + 1] == "10001:10001"
    assert preflight[preflight.index("--entrypoint") + 1] == "python"
    assert (
        preflight[preflight.index("--env") + 1]
        == "KODI_LAB_REPOSITORY_DATA_ROOT=/srv/kodi-lab-repository"
    )
    assert preflight[-1] == install.SUCCESSOR_CHECK
    up = [command for command in host.commands if "up" in command]
    assert bool(up) == completed
    if completed:
        assert host.commands.index(preflight) < host.commands.index(up[0])
    assert not any(
        "upgrade" in command or "initialize" in command for command in host.commands
    )


def test_same_bundle_proxy_rollback_starts_without_transition_or_migration(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    previous = host.root.parent / "previous-receipt.json"
    previous.write_text(
        json.dumps(
            {
                "schema": install.SCHEMA,
                "completed_stage": "ready",
                "bundle_sha256": "a" * 64,
            }
        )
    )
    data = {
        "schema": install.SITE_SCHEMA,
        "project": "kodi-lab-012345abcdef",
        "host": "windows-wsl2",
        "previous_receipt": str(previous),
        "previous_receipt_sha256": hashlib.sha256(previous.read_bytes()).hexdigest(),
        "bundle_sha256": "a" * 64,
        "completed_stage": "successor_site_configured",
        "web_port": 18100,
        "repository_bind": "192.168.2.20",
    }
    monkeypatch.setattr(install, "load", lambda _: data)
    monkeypatch.setattr(
        sys, "argv", ["installer", "start", "--directory", str(host.root)]
    )
    assert install.main() == 0
    assert any("up" in command for command in host.commands)
    assert not any(
        "repository-transition" in command or "upgrade" in command
        for command in host.commands
    )


@pytest.mark.parametrize(
    "successor,target,needs_preflight",
    [("a", "a", False), ("b", "b", True), ("b", "a", True)],
)
@pytest.mark.parametrize("activated", [False, True])
def test_chained_site_configuration_cannot_bypass_successor_completion(
    host: Host, successor: str, target: str, needs_preflight: bool, activated: bool
) -> None:
    original = host.root.parent / "original.json"
    original.write_text(
        json.dumps(
            {
                "schema": install.SCHEMA,
                "completed_stage": "ready",
                "bundle_sha256": "a" * 64,
            }
        )
    )
    configured = host.root.parent / "configured.json"
    configured.write_text(
        json.dumps(
            {
                "schema": install.SITE_SCHEMA,
                "completed_stage": "successor_site_configured",
                "bundle_sha256": successor * 64,
                "project": "kodi-lab-012345abcdef",
                "host": "windows-wsl2",
                "previous_receipt": str(original),
                "previous_receipt_sha256": hashlib.sha256(
                    original.read_bytes()
                ).hexdigest(),
            }
        )
    )
    # Site receipts remain configuration records even after a successful
    # activation check. A previously activated B must still be seen in A's path.
    if activated:
        install.successor_preflight(host.root, json.loads(configured.read_text()))
        host.commands.clear()
    data = {
        "schema": install.SITE_SCHEMA,
        "project": "kodi-lab-012345abcdef",
        "host": "windows-wsl2",
        "bundle_sha256": target * 64,
        "previous_receipt": str(configured),
        "previous_receipt_sha256": hashlib.sha256(configured.read_bytes()).hexdigest(),
    }
    host.fail = "repository-transition"
    if needs_preflight:
        with pytest.raises(subprocess.CalledProcessError):
            install.successor_preflight(host.root, data)
        assert any("repository-transition" in command for command in host.commands)
    else:
        install.successor_preflight(host.root, data)
        assert not host.commands


def test_successor_ancestry_tampering_fails_before_container_execution(
    host: Host,
) -> None:
    previous = host.root.parent / "previous.json"
    previous.write_text(
        json.dumps(
            {
                "schema": install.SCHEMA,
                "completed_stage": "ready",
                "bundle_sha256": "a" * 64,
            }
        )
    )
    data = {
        "schema": install.SITE_SCHEMA,
        "previous_receipt": str(previous),
        "previous_receipt_sha256": "0" * 64,
        "bundle_sha256": "a" * 64,
    }
    with pytest.raises(ValueError, match="ancestry differs"):
        install.successor_preflight(host.root, data)
    assert not host.commands


def test_altered_older_ancestor_is_rejected_after_bundle_mismatch(host: Host) -> None:
    original = host.root.parent / "original.json"
    original.write_text(
        json.dumps(
            {
                "schema": install.SCHEMA,
                "completed_stage": "ready",
                "bundle_sha256": "a" * 64,
            }
        )
    )
    successor = host.root.parent / "successor.json"
    successor.write_text(
        json.dumps(
            {
                "schema": install.SITE_SCHEMA,
                "bundle_sha256": "b" * 64,
                "previous_receipt": str(original),
                "previous_receipt_sha256": hashlib.sha256(
                    original.read_bytes()
                ).hexdigest(),
            }
        )
    )
    data = {
        "schema": install.SITE_SCHEMA,
        "bundle_sha256": "a" * 64,
        "previous_receipt": str(successor),
        "previous_receipt_sha256": hashlib.sha256(successor.read_bytes()).hexdigest(),
    }
    original.write_text(original.read_text() + "\n")
    with pytest.raises(ValueError, match="ancestry differs"):
        install.successor_preflight(host.root, data)
    assert not host.commands
