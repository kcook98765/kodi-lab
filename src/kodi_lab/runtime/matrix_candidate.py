"""Offline assembly inputs for the reviewed disposable Matrix 19.5 recipe."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import lzma
import subprocess
import tempfile
from collections import defaultdict
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from importlib.resources import files
from pathlib import Path, PurePosixPath
from typing import TypedDict, cast
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from kodi_lab.runtime.assembly import pack_build_entries, render_apt_image
from kodi_lab.runtime.recipes import (
    MatrixCandidateRecipe,
    ReviewedAptRecipe,
    load_matrix_candidate,
)

_PPA_SIGNER = "04AE50726660A0D55355E966C8B28F649961BE33"
_UBUNTU_SIGNER = "F6ECB3762474EDA9D21B7022871920D1991BC93C"
_MAX_INDEX = 128 * 1024**2


class Blob(TypedDict):
    sha256: str
    size: int


class Release(Blob):
    url: str


class Index(Blob):
    release: str
    path: str


class Package(Blob):
    index: str
    path: str
    name: str
    version: str
    architecture: str


class Closure(TypedDict):
    releases: dict[str, Release]
    indexes: dict[str, Index]
    packages: list[Package]
    base_inventory: list[list[str]]
    installed_inventory: list[list[str]]


class MatrixAcquisitionError(ValueError):
    """The sealed acquisition differs from the reviewed identity."""


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _check_blob(payload: bytes, pin: Blob, name: str) -> None:
    if len(payload) != pin["size"] or _digest(payload) != pin["sha256"]:
        raise MatrixAcquisitionError(f"reviewed artifact differs: {name}")


def _load() -> tuple[ReviewedAptRecipe, MatrixCandidateRecipe, Closure, bytes]:
    reviewed = load_matrix_candidate()
    recipe = reviewed.recipe
    assert isinstance(recipe, MatrixCandidateRecipe)
    location = files("kodi_lab.runtime").joinpath(
        "assembly_assets", "matrix-19.5-candidate"
    )
    raw = location.joinpath("package-closure.json").read_bytes()
    ppa_key = location.joinpath("team-kodi-archive.gpg").read_bytes()
    if (
        _digest(raw) != recipe.closure_sha256
        or _digest(ppa_key) != recipe.ppa_keyring_sha256
    ):
        raise MatrixAcquisitionError("reviewed Matrix closure or PPA keyring differs")
    closure = cast(Closure, json.loads(raw))
    return reviewed, recipe, closure, ppa_key


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> None:
        raise MatrixAcquisitionError("archive redirect refused")


def _download(url: str, pin: Blob) -> bytes:
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    with opener.open(url, timeout=45) as response:
        payload = response.read(pin["size"] + 1)
    if not isinstance(payload, bytes):
        raise MatrixAcquisitionError("archive response is not bytes")
    _check_blob(payload, pin, url)
    return payload


def _signatures(payload: bytes, keyring: bytes, signer: str) -> str:
    with tempfile.TemporaryDirectory(prefix="kodi-lab-matrix-gpg.") as root:
        key = Path(root) / "archive.gpg"
        key.write_bytes(keyring)
        reply = subprocess.run(
            ["gpgv", "--keyring", str(key), "--status-fd", "2", "--output", "-", "-"],
            input=payload,
            capture_output=True,
            check=True,
            timeout=15,
        )
    if not any(
        line.startswith(f"[GNUPG:] VALIDSIG {signer} ")
        for line in reply.stderr.decode().splitlines()
    ):
        raise MatrixAcquisitionError("expected archive signer missing")
    return reply.stdout.decode()


def _index_pins(release: str) -> dict[str, Blob]:
    rows = release.split("\nSHA256:\n", 1)[1].splitlines()
    pins: dict[str, Blob] = {}
    for row in rows:
        if not row.startswith(" "):
            break
        digest, size, name = row.split()
        if name in pins:
            raise MatrixAcquisitionError("duplicate signed index path")
        pins[name] = {"sha256": digest, "size": int(size)}
    return pins


def _index_rows(index: Index, payload: bytes) -> Iterator[dict[str, str]]:
    compressed = io.BytesIO(payload)
    open_index = gzip.open if index["path"].endswith(".gz") else lzma.open
    with open_index(compressed, "rb") as decompressed:
        raw = decompressed.read(_MAX_INDEX + 1)
    if len(raw) > _MAX_INDEX:
        raise MatrixAcquisitionError("package index exceeds its decoded size bound")
    for group in raw.decode().split("\n\n"):
        yield {
            key: value
            for line in group.splitlines()
            if line and not line[0].isspace() and ": " in line
            for key, value in (line.split(": ", 1),)
        }


def _match_packages(index: Index, payload: bytes, packages: list[Package]) -> None:
    expected = {package["path"]: package for package in packages}
    seen: set[str] = set()
    for entry in _index_rows(index, payload):
        path = entry.get("Filename", "")
        if path not in expected:
            continue
        package = expected[path]
        if path in seen or (
            entry["Package"],
            entry["Version"],
            entry["Architecture"],
            entry["SHA256"],
            int(entry["Size"]),
        ) != (
            package["name"],
            package["version"],
            package["architecture"],
            package["sha256"],
            package["size"],
        ):
            raise MatrixAcquisitionError(f"signed package record differs: {path}")
        seen.add(path)
    if seen != expected.keys():
        raise MatrixAcquisitionError("reviewed package missing from signed index")


def acquire(archive_directory: Path, ubuntu_keyring: Path) -> None:
    """Verify both archives' signed metadata and fetch only the sealed package set."""
    _, recipe, closure, ppa_key = _load()
    ubuntu_key = ubuntu_keyring.read_bytes()
    if "sha256:" + _digest(ubuntu_key) != recipe.provider.keyring_sha256:
        raise MatrixAcquisitionError("Ubuntu keyring differs from reviewed base")
    releases: dict[str, dict[str, Blob]] = {}
    for name, release in closure["releases"].items():
        key, signer = (
            (ppa_key, _PPA_SIGNER) if name == "ppa" else (ubuntu_key, _UBUNTU_SIGNER)
        )
        text = _signatures(_download(release["url"], release), key, signer)
        releases[name] = _index_pins(text)

    by_index: dict[str, list[Package]] = defaultdict(list)
    for package in closure["packages"]:
        by_index[package["index"]].append(package)
    for name, index in closure["indexes"].items():
        signed_pin = releases[index["release"]].get(index["path"])
        if signed_pin != {"sha256": index["sha256"], "size": index["size"]}:
            raise MatrixAcquisitionError(f"compressed package index differs: {name}")
        release_url = closure["releases"][index["release"]]["url"]
        url = release_url.removesuffix("InRelease") + index["path"]
        _match_packages(index, _download(url, index), by_index[name])

    archive_directory.mkdir(mode=0o700)

    def obtain(package: Package) -> None:
        index = closure["indexes"][package["index"]]
        url = closure["releases"][index["release"]]["url"].split("dists/", 1)[0]
        payload = _download(url + package["path"], package)
        target = archive_directory / PurePosixPath(package["path"]).name
        with target.open("xb") as stream:
            stream.write(payload)

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(obtain, closure["packages"]))


def _inventory_bytes(rows: list[list[str]]) -> bytes:
    return "".join("\t".join(row) + "\n" for row in rows).encode()


def build_context(archive_directory: Path) -> bytes:
    """Use the reviewed r4 assets and the reverified closure in a networkless build."""
    reviewed, recipe, closure, _ = _load()
    entries = dict(reviewed.assets)
    for package in closure["packages"]:
        name = PurePosixPath(package["path"]).name
        with (archive_directory / name).open("rb") as stream:
            payload = stream.read(package["size"] + 1)
        _check_blob(payload, package, name)
        entries[f"matrix-input/packages/{name}"] = payload

    entries["matrix-input/base.tsv"] = _inventory_bytes(closure["base_inventory"])
    entries["matrix-input/installed.tsv"] = _inventory_bytes(
        closure["installed_inventory"]
    )
    entries["matrix-input/closure.sha256"] = f"{recipe.closure_sha256}\n".encode()
    inventory = (
        "dpkg-query -W -f='${Package}\\t${Version}\\t${Architecture}\\n' "
        "| LC_ALL=C sort"
    )
    commands = [
        "set -eu",
        'test "$(dpkg --print-architecture)" = amd64',
        'test -z "$(dpkg --print-foreign-architectures)"',
        f"{inventory} > /tmp/base.tsv",
        "cmp /tmp/base.tsv /reviewed-matrix/base.tsv",
        "rm -f /etc/apt/sources.list /etc/apt/sources.list.d/*",
        "install -d /reviewed-matrix/empty-sources",
        "touch /reviewed-matrix/empty.list",
        # APT's file transport stages local archives even with networking disabled.
        "apt-get -o Dir::Etc::sourcelist=/reviewed-matrix/empty.list "
        "-o Dir::Etc::sourceparts=/reviewed-matrix/empty-sources "
        "--no-remove --no-install-recommends -y install "
        "/reviewed-matrix/packages/*.deb",
        "kodi --version | grep -F '20221224-f8fdeb6b1b'",
        "install -d -m 0755 /usr/share/kodi-lab",
        f"{inventory} > /usr/share/kodi-lab/installed-packages.tsv",
        "cmp /usr/share/kodi-lab/installed-packages.tsv /reviewed-matrix/installed.tsv",
        "install -m 0444 /reviewed-matrix/closure.sha256 /usr/share/kodi-lab/",
        "rm -rf /reviewed-matrix /tmp/base.tsv /var/cache/apt/archives/*.deb",
    ]
    entries["Dockerfile"] = render_apt_image(
        reviewed, commands, "COPY matrix-input/ /reviewed-matrix/"
    ).encode()
    return pack_build_entries(entries)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    download = actions.add_parser("acquire")
    download.add_argument("archive_directory", type=Path)
    download.add_argument("ubuntu_keyring", type=Path)
    context = actions.add_parser("context")
    context.add_argument("archive_directory", type=Path)
    context.add_argument("output", type=Path)
    args = parser.parse_args()
    if args.action == "acquire":
        acquire(args.archive_directory, args.ubuntu_keyring)
    else:
        payload = build_context(args.archive_directory)
        with args.output.open("xb") as output:
            output.write(payload)


if __name__ == "__main__":
    main()
