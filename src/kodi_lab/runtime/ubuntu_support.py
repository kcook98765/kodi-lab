"""Exact, wheel-reviewed Ubuntu support closure; no runtime APT resolution.

The review manifest binds packages to Ubuntu-signed index hashes. Subsequent
acquisition verifies each reviewed DEB hash; unavailable pins fail closed. This
is deliberately independent of the unchanged signed_distro_apt provider.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from importlib import resources
from urllib.parse import urlsplit
from uuid import UUID

from kodi_lab.runtime.ostree import AcquiredOstree

SUPPORT_SHA256 = "1dd0391796d618a541d39a1817f0ac3074fe4ff350dd7474c6e41dae0d0870d1"
BASE_IMAGE = (
    "ubuntu:26.04@sha256:"
    "e5a4d6262ab5dbc25a85e60550dd7c87fd41a74fe43881534ed8288b2a7a3f8d"
)
ASSET_DIRECTORY = "assembly_assets/piers-22.0-rc1-r2"
HISTORICAL_SUPPORT_SHA256 = (
    "05afddc1b4174d4a5b3e472173ebbfc04fb0ff5c4630a3b784e7e186de058f29"
)
SNAPSHOT_PREFIX = "/ubuntu/20261001T000000Z/pool/"
SNAPSHOT_PACKAGES = frozenset({"ca-certificates", "tzdata", "xserver-common", "xvfb"})
MAX_SUPPORT_BYTES = 128 * 1024**2
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_NAME = re.compile(r"[a-z][a-z0-9+.-]{0,63}\Z")
_VERSION = re.compile(r"[0-9][A-Za-z0-9.+:~_-]{0,95}\Z")
_FILE = re.compile(r"[a-z0-9][a-zA-Z0-9.+_~%-]{0,191}\.deb\Z")


class SupportError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SupportPackage:
    name: str
    version: str
    architecture: str
    archive_name: str
    url: str
    sha256: str
    size: int
    index: str


@dataclass(frozen=True, slots=True)
class SupportClosure:
    raw: bytes
    packages: tuple[SupportPackage, ...]
    base_inventory: tuple[tuple[str, str, str], ...]
    final_inventory: tuple[tuple[str, str, str], ...]

    @property
    def digest(self) -> str:
        return "sha256:" + hashlib.sha256(self.raw).hexdigest()


def _parse(raw: bytes) -> SupportClosure:
    if len(raw) > 128 * 1024:
        raise SupportError("support_manifest_exceeded")
    data = json.loads(raw)
    if set(data) != {
        "schema_version",
        "base_image",
        "platform",
        "distribution",
        "suite",
        "keyring_sha256",
        "signed_metadata",
        "indexes",
        "packages",
        "base_inventory",
        "final_inventory",
        "requested",
        "removed_packages",
    }:
        raise SupportError("unexpected_support_metadata")
    if (
        data["schema_version"] != "piers-ubuntu-support-v1"
        or data["base_image"] != BASE_IMAGE
        or data["platform"] != "linux/amd64"
        or data["suite"] != "resolute"
        or data["distribution"] != "Ubuntu"
        or not _SHA.fullmatch(data["keyring_sha256"])
    ):
        raise SupportError("wrong_support_identity")
    if not data["signed_metadata"] or any(
        x["signers"] != ["F6ECB3762474EDA9D21B7022871920D1991BC93C"]
        or not _SHA.fullmatch(x["sha256"])
        for x in data["signed_metadata"]
    ):
        raise SupportError("incorrect_support_trust")
    indexes = {x["filename"] for x in data["indexes"]}
    packages = tuple(SupportPackage(**x) for x in data["packages"])
    if not 1 <= len(packages) <= 128 or len({p.name for p in packages}) != len(
        packages
    ):
        raise SupportError("invalid_support_closure")
    for p in packages:
        parsed = urlsplit(p.url)
        if (
            not _NAME.fullmatch(p.name)
            or not _VERSION.fullmatch(p.version)
            or p.architecture not in ("amd64", "all")
            or not _FILE.fullmatch(p.archive_name)
            or not p.archive_name.startswith(p.name + "_")
            or not p.archive_name.endswith("_" + p.architecture + ".deb")
            or parsed.scheme != "https"
            or not (
                (
                    parsed.netloc == "archive.ubuntu.com"
                    and parsed.path.startswith("/ubuntu/pool/")
                )
                or (
                    parsed.netloc == "snapshot.ubuntu.com"
                    and parsed.path.startswith(SNAPSHOT_PREFIX)
                    and p.name in SNAPSHOT_PACKAGES
                )
            )
            or parsed.path.rsplit("/", 1)[-1] != p.archive_name
            or any(part in (".", "..", "") for part in parsed.path[1:].split("/"))
            or parsed.query
            or parsed.fragment
            or not _SHA.fullmatch(p.sha256)
            or type(p.size) is not int
            or not 0 < p.size <= MAX_SUPPORT_BYTES
            or p.index not in indexes
            or p.name in ("ostree", "gnupg", "curl", "gcc", "g++", "make", "mesa-utils")
        ):
            raise SupportError("invalid_support_package")
    if sum(p.size for p in packages) > MAX_SUPPORT_BYTES:
        raise SupportError("support_payload_exceeded")
    if not set(data["requested"]) <= {p.name for p in packages}:
        raise SupportError("missing_support_package")
    base = tuple(tuple(x) for x in data["base_inventory"])
    final = tuple(tuple(x) for x in data["final_inventory"])
    expected = {x[0]: x for x in base}
    expected.update({p.name: (p.name, p.version, p.architecture) for p in packages})
    if data["removed_packages"] != ["apt", "gpgv", "libapt-pkg7.0", "ubuntu-keyring"]:
        raise SupportError("unexpected_support_removal")
    for name in data["removed_packages"]:
        expected.pop(name)
    if final != tuple(sorted(expected.values())):
        raise SupportError("incorrect_final_inventory")
    return SupportClosure(raw, packages, base, final)


def load_support_closure(*, revision: int = 2) -> SupportClosure:
    if type(revision) is not int or revision not in (1, 2):
        raise SupportError("unsupported_support_revision")
    directory = (
        ASSET_DIRECTORY if revision == 2 else "assembly_assets/piers-22.0-rc1-r1"
    )
    expected = SUPPORT_SHA256 if revision == 2 else HISTORICAL_SUPPORT_SHA256
    raw = (
        resources.files("kodi_lab.runtime")
        .joinpath(directory, "ubuntu-support.json")
        .read_bytes()
    )
    if hashlib.sha256(raw).hexdigest() != expected:
        raise SupportError("support_manifest_digest_mismatch")
    try:
        return _parse(raw)
    except (KeyError, ValueError, TypeError) as error:
        raise SupportError("invalid_support_manifest") from error


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, req: object, fp: object, code: int, msg: str, headers: object, newurl: str
    ) -> None:
        raise SupportError("support_redirect_rejected")


def download_support(acquired: AcquiredOstree, operation_id: UUID) -> dict[str, bytes]:
    """Only the fixed reviewed closure; nothing is cached on disk or persisted."""
    acquired.assert_handoff(operation_id)
    result = fetch_support_packages()
    acquired.assert_handoff(operation_id)
    return result


def fetch_support_packages() -> dict[str, bytes]:
    """Fixed wheel-owned inputs only; usable in the socketless network helper."""
    closure = load_support_closure()
    deadline = time.monotonic() + 600

    def obtain(p: SupportPackage) -> tuple[str, bytes]:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect()
        )
        request = urllib.request.Request(
            p.url, headers={"User-Agent": "Kodi-Lab-reviewed-support"}
        )
        try:
            with opener.open(
                request, timeout=min(30, max(0.1, deadline - time.monotonic()))
            ) as response:
                payload = bytearray()
                digest = hashlib.sha256()
                while chunk := response.read(1024**2):
                    if (
                        time.monotonic() > deadline
                        or len(payload) + len(chunk) > p.size
                    ):
                        raise SupportError("support_download_bound")
                    digest.update(chunk)
                    payload.extend(chunk)
        except SupportError:
            raise
        except Exception as error:
            raise SupportError("pinned_support_unavailable") from error
        if len(payload) != p.size or digest.hexdigest() != p.sha256:
            raise SupportError("support_artifact_mismatch")
        return "support/" + p.archive_name, bytes(payload)

    # Fixed bounded concurrency; each task still uses the same global deadline,
    # exact size/hash and immutable closed URL. Never resolve replacement DEBs.
    with ThreadPoolExecutor(max_workers=4) as pool:
        result = dict(pool.map(obtain, closure.packages))
    return result


def validate_support_entries(entries: dict[str, bytes]) -> None:
    closure = load_support_closure()
    if set(entries) != {"support/" + p.archive_name for p in closure.packages}:
        raise SupportError("support_closure_mismatch")
    for p in closure.packages:
        payload = entries["support/" + p.archive_name]
        if len(payload) != p.size or hashlib.sha256(payload).hexdigest() != p.sha256:
            raise SupportError("support_artifact_mismatch")
