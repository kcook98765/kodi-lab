"""Inactive, worker-only acquisition of the three reviewed RC1 OSTree trees.

No installation, Docker, database, approval, or runtime execution authority lives
here. The constructor's private root and runner are worker dependencies, never
request parameters. Acquisition always starts a fresh repository; persistent
cache reuse is deliberately not supported in this phase.
"""

from __future__ import annotations

import base64
import configparser
import ctypes
import fcntl
import hashlib
import json
import os
import posixpath
import re
import selectors
import shutil
import signal
import stat
import subprocess
import threading
import time
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field, replace
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Literal, Protocol, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from kodi_lab.runtime.recipes import (
    FLATHUB_KEY_SHA256,
    FLATHUB_PRIMARY_KEY,
    FLATHUB_SIGNING_SUBKEY,
    KODI_RC1_SOURCE_REVISION,
    InactiveOstreeDefinition,
    OstreeComponent,
    load_inactive_ostree_recipe,
)

_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
_DIR_FLAGS = _READ_FLAGS | os.O_DIRECTORY
MAX_METADATA_BYTES = 128 * 1024
MAX_OUTPUT_BYTES = 1024 * 1024
MAX_STAGE_BYTES = 6 * 1024**3
MAX_STAGE_ENTRIES = 100_000
MIN_FREE_BYTES = 8 * 1024**3
MAX_ACQUISITION_SECONDS = 1800
_KEY_RESOURCE_DIGEST = (
    "163ff6edafe98f855c733c245371f5dc9cec052b4fa5f0ecad7b7885d328fae9"
)
_CRITICAL_FILES = {
    "app": {
        "files/lib/kodi/kodi.bin": (
            "2862331a8a047f929d8019987a518527fcb45330082435bec7b7756141cf688f"
        ),
        "files/manifest.json": (
            "e53b4d6eea30b1e01d6c3507f6b016eebf1c5b4ac6b0b654ed1a98ffe43650ee"
        ),
        "files/share/metainfo/tv.kodi.Kodi.metainfo.xml": (
            "60fe82f6f36510992c2a56b8e55fd7b860ffc7b9bd188835dab45c13ef962f6a"
        ),
    },
    "platform": {
        "files/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2": (
            "6b887f408400b77f99a5c0e99f380c84264eef29156c191713b3343189cb0391"
        ),
        "files/lib/x86_64-linux-gnu/libtag.so.2.3.1": (
            "2f65f918dc718e5e1190f7dc9a1efb2b0aeefea42a31cfcce8cc2373b5b851ee"
        ),
        "files/manifest.json": (
            "e27bd9e2966a47541356f069ae29e7bf2f4946baa8047b1fc09b43f1d34c62fc"
        ),
    },
    "gl": {
        "files/lib/libEGL_mesa.so.0.0.0": (
            "0c0bc7e58b50fb381d72027d056194f7cfd73676df8a46510c7841585af6b49b"
        ),
    },
}


class OstreeAcquisitionError(RuntimeError):
    """A bounded, non-secret failure code; never an upstream command transcript."""


class OstreeAcquisitionBusy(OstreeAcquisitionError):
    pass


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    output: bytes


class CommandRunner(Protocol):
    def run(
        self, argv: tuple[str, ...], *, stage: Path, timeout: float
    ) -> CommandResult: ...


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def _directory(path: Path) -> int:
    """Open every ancestor without following a symlink, including the root."""

    if not path.is_absolute() or any(x in (".", "..") for x in path.parts):
        raise OstreeAcquisitionError("unsafe_staging_path")
    fd = os.open("/", _DIR_FLAGS)
    try:
        for part in path.parts[1:]:
            next_fd = os.open(part, _DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


def _read_file(path: Path, *, maximum: int = MAX_METADATA_BYTES) -> bytes:
    parent = _directory(path.parent)
    try:
        fd = os.open(path.name, _READ_FLAGS, dir_fd=parent)
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_nlink != 1
                or info.st_size > maximum
            ):
                raise OstreeAcquisitionError("unsafe_staged_file")
            data = bytearray()
            while chunk := os.read(fd, min(65536, maximum + 1 - len(data))):
                data.extend(chunk)
                if len(data) > maximum:
                    raise OstreeAcquisitionError("staged_file_too_large")
            return bytes(data)
        finally:
            os.close(fd)
    finally:
        os.close(parent)


def _write_file(path: Path, data: bytes) -> None:
    parent = _directory(path.parent)
    try:
        fd = os.open(
            path.name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent,
        )
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
    finally:
        os.close(parent)


def _stage_quota(stage: Path) -> None:
    total = count = 0
    for directory, folders, files in os.walk(stage, followlinks=False):
        for name in (*folders, *files):
            path = Path(directory) / name
            try:
                info = path.lstat()
            except FileNotFoundError:
                # OSTree atomically moves completed downloads out of repo/tmp.
                # This monitor is a quota estimate, never integrity evidence.
                # All final objects/checkouts are subsequently verified strictly.
                if path.is_relative_to(stage / "repo" / "tmp"):
                    continue
                raise
            count += 1
            total += info.st_size
            if count > MAX_STAGE_ENTRIES or total > MAX_STAGE_BYTES:
                raise OstreeAcquisitionError("staging_quota_exceeded")


def _cleanup_owned_gpg_agents(stage: Path) -> tuple[int, ...]:
    """GPGME can leave detached agents; signal only this operation's namespace.

    pidfds prevent signalling a reused PID. No agent outside the generated
    operation path is adopted, and no host GPG home or socket is contacted.
    """

    if (
        not stage.is_absolute()
        or ".." in stage.parts
        or re.fullmatch(r"ostree-[0-9a-f]{32}", stage.name) is None
    ):
        raise OstreeAcquisitionError("unsafe_agent_cleanup_scope")
    stopped = []
    prefix = str(stage) + "/"
    for entry in Path("/proc").iterdir():
        if not entry.name.isdecimal():
            continue
        pid_fd = None
        try:
            pid_fd = os.pidfd_open(int(entry.name))
            # Agents are non-dumpable: /proc directory ownership and exe access
            # do not reflect their effective UID. Read the kernel's status.
            with (entry / "status").open("rb") as stream:
                status = stream.read(16384).decode("ascii")
            identities = next(
                (
                    line.split()[1:]
                    for line in status.splitlines()
                    if line.startswith("Uid:")
                ),
                [],
            )
            if identities != [str(os.geteuid())] * 4:
                continue
            with (entry / "cmdline").open("rb") as stream:
                raw = stream.read(16385)
            if len(raw) > 16384 or not raw.endswith(b"\x00"):
                continue
            arguments = raw.decode("utf-8").split("\x00")
            if (
                arguments[0] not in ("gpg-agent", "/usr/bin/gpg-agent")
                or "--homedir" not in arguments
            ):
                continue
            home = arguments[arguments.index("--homedir") + 1]
            if not home.startswith(prefix) or posixpath.normpath(home) != home:
                continue
            # Record the exact identifier before removing the owned process.
            stopped.append(int(entry.name))
            signal.pidfd_send_signal(pid_fd, signal.SIGTERM)
            with selectors.DefaultSelector() as selector:
                selector.register(pid_fd, selectors.EVENT_READ)
                if not selector.select(timeout=2):
                    signal.pidfd_send_signal(pid_fd, signal.SIGKILL)
                    if not selector.select(timeout=2):
                        raise OstreeAcquisitionError("owned_agent_cleanup_failed")
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            # Process disappeared, or belongs to another inaccessible identity.
            continue
        finally:
            if pid_fd is not None:
                os.close(pid_fd)
    return tuple(stopped)


class BoundedCommandRunner:
    """Fixed tools, empty inherited environment, process-group cancellation."""

    def run(
        self, argv: tuple[str, ...], *, stage: Path, timeout: float
    ) -> CommandResult:
        if argv[0] not in ("/usr/bin/ostree", "/usr/bin/gpg", "/usr/bin/gpgv"):
            raise OstreeAcquisitionError("unreviewed_tool")
        environment = {
            "PATH": "/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "HOME": str(stage / "home"),
            "GNUPGHOME": str(stage / "gnupg"),
            "TMPDIR": str(stage / "tmp"),
            "GIO_USE_VFS": "local",
        }
        command = (
            "/usr/bin/prlimit",
            "--as=2147483648",
            "--fsize=536870912",
            "--nofile=256",
            "--cpu=900",
            "--",
            *argv,
        )
        output = bytearray()
        started = last_quota = time.monotonic()
        with subprocess.Popen(
            command,
            cwd=stage,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            umask=0o077,
        ) as process:
            assert process.stdout is not None
            try:
                os.set_blocking(process.stdout.fileno(), False)
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout, selectors.EVENT_READ)
                    while selector.get_map():
                        now = time.monotonic()
                        if now - started > timeout:
                            raise OstreeAcquisitionError("acquisition_timeout")
                        if now - last_quota > 2:
                            _stage_quota(stage)
                            last_quota = now
                        for key, _ in selector.select(timeout=0.1):
                            chunk = os.read(key.fd, 65536)
                            if not chunk:
                                selector.unregister(key.fd)
                            else:
                                output.extend(chunk)
                                if len(output) > MAX_OUTPUT_BYTES:
                                    raise OstreeAcquisitionError(
                                        "command_output_exceeded"
                                    )
                process.wait(timeout=max(0.1, timeout - (time.monotonic() - started)))
                _stage_quota(stage)
                return CommandResult(process.returncode, bytes(output))
            except BaseException:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                raise


@lru_cache(maxsize=1)
def _glib() -> ctypes.CDLL:
    """Use GLib's canonical GVariant decoder, not a hand-written binary parser."""

    lib = ctypes.CDLL("libglib-2.0.so.0")
    pointer = ctypes.c_void_p
    definitions = {
        "g_variant_type_new": ([ctypes.c_char_p], pointer),
        "g_variant_type_free": ([pointer], None),
        "g_variant_new_from_data": (
            [pointer, pointer, ctypes.c_size_t, ctypes.c_int, pointer, pointer],
            pointer,
        ),
        "g_variant_is_normal_form": ([pointer], ctypes.c_int),
        "g_variant_get_child_value": ([pointer, ctypes.c_size_t], pointer),
        "g_variant_n_children": ([pointer], ctypes.c_size_t),
        "g_variant_lookup_value": ([pointer, ctypes.c_char_p, pointer], pointer),
        "g_variant_get_type_string": ([pointer], ctypes.c_char_p),
        "g_variant_get_string": ([pointer, pointer], ctypes.c_char_p),
        "g_variant_get_size": ([pointer], ctypes.c_size_t),
        "g_variant_get_data": ([pointer], pointer),
        "g_variant_unref": ([pointer], None),
    }
    for name, (arguments, result) in definitions.items():
        method = getattr(lib, name)
        method.argtypes = arguments
        method.restype = result
    return lib


@contextmanager
def _variant(data: bytes, kind: bytes) -> Iterator[int]:
    if not 1 <= len(data) <= MAX_METADATA_BYTES:
        raise OstreeAcquisitionError("invalid_variant_size")
    lib = _glib()
    variant_type = lib.g_variant_type_new(kind)
    buffer = ctypes.create_string_buffer(data)
    pointer = cast(
        int,
        lib.g_variant_new_from_data(variant_type, buffer, len(data), False, None, None),
    )
    lib.g_variant_type_free(variant_type)
    try:
        if not pointer or not lib.g_variant_is_normal_form(pointer):
            raise OstreeAcquisitionError("invalid_variant")
        yield pointer
    finally:
        if pointer:
            lib.g_variant_unref(pointer)


@contextmanager
def _lookup(parent: int, key: bytes, kind: bytes) -> Iterator[int]:
    lib = _glib()
    pointer = cast(int, lib.g_variant_lookup_value(parent, key, None))
    try:
        if not pointer or lib.g_variant_get_type_string(pointer) != kind:
            raise OstreeAcquisitionError("invalid_commit_metadata")
        yield pointer
    finally:
        if pointer:
            lib.g_variant_unref(pointer)


def _text(parent: int, key: bytes) -> str:
    with _lookup(parent, key, b"s") as pointer:
        return cast(bytes, _glib().g_variant_get_string(pointer, None)).decode("utf-8")


def _ref_bindings(parent: int) -> tuple[str, ...]:
    lib = _glib()
    with _lookup(parent, b"ostree.ref-binding", b"as") as pointer:
        count = lib.g_variant_n_children(pointer)
        if count != 1:
            raise OstreeAcquisitionError("invalid_ref_binding")
        child = lib.g_variant_get_child_value(pointer, 0)
        try:
            return (cast(bytes, lib.g_variant_get_string(child, None)).decode(),)
        finally:
            lib.g_variant_unref(child)


def detached_signatures(data: bytes) -> tuple[bytes, ...]:
    """Decode bounded detached signature metadata using its OSTree GVariant type."""

    lib = _glib()
    signatures = []
    with (
        _variant(data, b"a{sv}") as metadata,
        _lookup(metadata, b"ostree.gpgsigs", b"aay") as pointer,
    ):
        count = lib.g_variant_n_children(pointer)
        if not 1 <= count <= 4:
            raise OstreeAcquisitionError("missing_or_excess_signatures")
        for index in range(count):
            child = lib.g_variant_get_child_value(pointer, index)
            try:
                size = lib.g_variant_get_size(child)
                if not 1 <= size <= 65536:
                    raise OstreeAcquisitionError("invalid_signature_size")
                signatures.append(ctypes.string_at(lib.g_variant_get_data(child), size))
            finally:
                lib.g_variant_unref(child)
    return tuple(signatures)


def signed_component_metadata(data: bytes, component: OstreeComponent) -> str:
    """Validate content identity and signed ref/collection/architecture bindings."""

    if _digest(data) != component.commit:
        raise OstreeAcquisitionError("commit_digest_mismatch")
    lib = _glib()
    with _variant(data, b"(a{sv}aya(say)sstayay)") as commit:
        related = lib.g_variant_get_child_value(commit, 2)
        try:
            if lib.g_variant_n_children(related) != 0:
                raise OstreeAcquisitionError("unexpected_related_component")
        finally:
            lib.g_variant_unref(related)
        metadata = lib.g_variant_get_child_value(commit, 0)
        try:
            if (
                _ref_bindings(metadata) != (component.ref,)
                or _text(metadata, b"xa.ref") != component.ref
                or _text(metadata, b"ostree.collection-binding") != component.collection
                or component.ref.split("/")[2] != "x86_64"
            ):
                raise OstreeAcquisitionError("commit_binding_mismatch")
            with _lookup(metadata, b"ostree.collection-refs-binding", b"a(ss)") as refs:
                if lib.g_variant_n_children(refs) != 1:
                    raise OstreeAcquisitionError("collection_ref_binding_mismatch")
                pair = lib.g_variant_get_child_value(refs, 0)
                children = [lib.g_variant_get_child_value(pair, i) for i in range(2)]
                try:
                    binding = tuple(
                        cast(bytes, lib.g_variant_get_string(c, None)).decode()
                        for c in children
                    )
                    if binding != (component.collection, component.ref):
                        raise OstreeAcquisitionError("collection_ref_binding_mismatch")
                finally:
                    for child in children:
                        lib.g_variant_unref(child)
                    lib.g_variant_unref(pair)
            embedded = _text(metadata, b"xa.metadata")
            if _digest(embedded.encode()) != component.metadata_sha256:
                raise OstreeAcquisitionError("component_metadata_mismatch")
            return embedded
        finally:
            lib.g_variant_unref(metadata)


def trusted_key_bytes() -> bytes:
    raw = (
        resources.files("kodi_lab.runtime")
        .joinpath("trust", "flathub-2026.json")
        .read_bytes()
    )
    if len(raw) > 8192 or _digest(raw) != _KEY_RESOURCE_DIGEST:
        raise OstreeAcquisitionError("trust_resource_mismatch")
    value = json.loads(raw)
    if (
        set(value) != {"format", "key_base64"}
        or value["format"] != "openpgp-public-key-v1"
    ):
        raise OstreeAcquisitionError("invalid_trust_resource")
    key = base64.b64decode(value["key_base64"], validate=True)
    if _digest(key) != FLATHUB_KEY_SHA256:
        raise OstreeAcquisitionError("trust_key_mismatch")
    return key


def _validate_key_listing(output: bytes) -> None:
    primary: list[str] = []
    subkeys: list[str] = []
    kind = ""
    for line in output.decode("utf-8").splitlines():
        fields = line.split(":")
        if fields[0] in ("pub", "sub"):
            kind = fields[0]
        elif fields[0] == "fpr" and len(fields) > 9:
            (primary if kind == "pub" else subkeys).append(fields[9])
    if primary != [FLATHUB_PRIMARY_KEY] or subkeys != [FLATHUB_SIGNING_SUBKEY]:
        raise OstreeAcquisitionError("trust_fingerprint_mismatch")


def _validate_signature_status(result: CommandResult) -> None:
    signatures = []
    for line in result.output.decode("utf-8").splitlines():
        if line.startswith("[GNUPG:] VALIDSIG "):
            fields = line.split()
            if len(fields) != 12:
                raise OstreeAcquisitionError("invalid_signature_status")
            signatures.append((fields[2], fields[-1]))
        elif line.startswith("[GNUPG:] ") and line.split()[1] in {
            "BADSIG",
            "ERRSIG",
            "EXPSIG",
            "EXPKEYSIG",
            "REVKEYSIG",
            "NO_PUBKEY",
            "NODATA",
            "FAILURE",
        }:
            raise OstreeAcquisitionError("signature_invalid")
    if result.returncode != 0 or signatures != [
        (FLATHUB_SIGNING_SUBKEY, FLATHUB_PRIMARY_KEY)
    ]:
        raise OstreeAcquisitionError("signature_invalid")


class ComponentEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    role: Literal["app", "platform", "gl"]
    ref: str = Field(max_length=128)
    commit: str = Field(pattern=r"^[0-9a-f]{64}$")
    collection: str = Field(max_length=64)
    architecture: Literal["x86_64"] = "x86_64"
    primary_key: Literal["6E5C05D979C76DAF93C081354184DD4D907A7CAE"] = (
        FLATHUB_PRIMARY_KEY
    )
    signing_subkey: Literal["54A6CDDD8919FB204200D8AC562702E9E3ED7EE8"] = (
        FLATHUB_SIGNING_SUBKEY
    )
    signature_result: Literal["passed"] = "passed"
    detached_metadata_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    metadata_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    inventory_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    entry_count: int = Field(ge=1, le=MAX_STAGE_ENTRIES)
    content_bytes: int = Field(ge=1, le=MAX_STAGE_BYTES)


class OstreeVerificationEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["flathub-ostree-verification-v1"] = (
        "flathub-ostree-verification-v1"
    )
    provider: Literal["flathub_ostree"] = "flathub_ostree"
    recipe_id: Literal["piers-22.0-rc1-r1"] = "piers-22.0-rc1-r1"
    recipe_revision: Literal[1] = 1
    recipe_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    kodi_release: Literal["22.0-rc.1"] = "22.0-rc.1"
    kodi_source_revision: Literal["28ea2eac1eb7af8fdcbd2672d933ce87f594be79"] = (
        KODI_RC1_SOURCE_REVISION
    )
    platform: Literal["linux/amd64"] = "linux/amd64"
    installation_status: Literal["inactive"] = "inactive"
    scope: Literal["three-reviewed-ostree-components"] = (
        "three-reviewed-ostree-components"
    )
    ostree_integrity: Literal["passed"] = "passed"
    artifact_closure: Literal["verified"] = "verified"
    components: tuple[ComponentEvidence, ComponentEvidence, ComponentEvidence]

    def canonical_bytes(self) -> bytes:
        result = _canonical(self.model_dump(mode="json"))
        if len(result) > 16384:
            raise OstreeAcquisitionError("verification_evidence_exceeded")
        return result


def _metadata_config(value: str, component: OstreeComponent) -> None:
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    parser.read_string(value)
    section = "Application" if component.role == "app" else "Runtime"
    expected_name = component.ref.split("/")[1]
    if (
        parser.get(section, "name") != expected_name
        or parser.get(section, "runtime") != "org.freedesktop.Platform/x86_64/26.08"
        or parser.get(section, "sdk") != "org.freedesktop.Sdk/x86_64/26.08"
    ):
        raise OstreeAcquisitionError("component_dependency_mismatch")
    if (
        component.role == "gl"
        and parser.get("ExtensionOf", "ref")
        != "runtime/org.freedesktop.Platform/x86_64/26.08"
    ):
        raise OstreeAcquisitionError("graphics_dependency_mismatch")
    if component.role == "app" and parser.get("Application", "command") != "kodi":
        raise OstreeAcquisitionError("application_command_mismatch")


def _safe_link(relative: str, target: str, role: str) -> None:
    if not target or len(target) > 4096:
        raise OstreeAcquisitionError("unsafe_payload_symlink")
    if target.startswith("/"):
        # These reviewed FDO links are retained as data, never dereferenced on
        # the host. All other absolute links fail. Contents remain OSTree-pinned.
        if (
            role != "platform"
            or ".." in target.split("/")
            or not (
                target
                in (
                    "/usr/bin/python3",
                    "/usr/share/xkeyboard-config-2",
                    "/usr/share/zoneinfo/UTC",
                )
                or target.startswith(
                    (
                        "/etc/pki/",
                        "/usr/share/licenses/common/LICENSE_",
                        "/usr/share/alsa/alsa.conf.d/",
                        "/usr/lib/x86_64-linux-gnu/GL/",
                    )
                )
            )
        ):
            raise OstreeAcquisitionError("unsafe_payload_symlink")
    else:
        resolved = posixpath.normpath(
            posixpath.join(posixpath.dirname(relative), target)
        )
        if resolved == ".." or resolved.startswith("../"):
            raise OstreeAcquisitionError("unsafe_payload_symlink")


def _inventory(root: Path, component: OstreeComponent) -> tuple[str, int, int]:
    digest = hashlib.sha256()
    found: dict[str, str] = {}
    count = size = 0

    def walk(fd: int, prefix: str) -> None:
        nonlocal count, size
        with os.scandir(fd) as scan:
            names = sorted(entry.name for entry in scan)
        for name in names:
            relative = f"{prefix}{name}"
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if info.st_uid != os.geteuid() or info.st_mode & (
                stat.S_ISUID | stat.S_ISGID
            ):
                raise OstreeAcquisitionError("unsafe_payload_owner_or_mode")
            count += 1
            if count > MAX_STAGE_ENTRIES:
                raise OstreeAcquisitionError("payload_inventory_exceeded")
            if stat.S_ISDIR(info.st_mode):
                digest.update(
                    _canonical([relative, "directory", stat.S_IMODE(info.st_mode)])
                    + b"\n"
                )
                child = os.open(name, _DIR_FLAGS, dir_fd=fd)
                try:
                    walk(child, relative + "/")
                finally:
                    os.close(child)
            elif stat.S_ISLNK(info.st_mode):
                target = os.readlink(name, dir_fd=fd)
                _safe_link(relative, target, component.role)
                digest.update(_canonical([relative, "symlink", target]) + b"\n")
            elif stat.S_ISREG(info.st_mode):
                child = os.open(name, _READ_FLAGS, dir_fd=fd)
                content_hash = hashlib.sha256()
                actual_size = 0
                try:
                    opened = os.fstat(child)
                    if opened.st_nlink != 1 or (opened.st_dev, opened.st_ino) != (
                        info.st_dev,
                        info.st_ino,
                    ):
                        raise OstreeAcquisitionError("unsafe_payload_file")
                    while chunk := os.read(child, 1024 * 1024):
                        actual_size += len(chunk)
                        size += len(chunk)
                        if size > MAX_STAGE_BYTES:
                            raise OstreeAcquisitionError("payload_inventory_exceeded")
                        content_hash.update(chunk)
                    if actual_size != info.st_size:
                        raise OstreeAcquisitionError(
                            "payload_changed_during_verification"
                        )
                finally:
                    os.close(child)
                found[relative] = content_hash.hexdigest()
                digest.update(
                    _canonical(
                        [
                            relative,
                            "file",
                            stat.S_IMODE(info.st_mode),
                            actual_size,
                            content_hash.hexdigest(),
                        ]
                    )
                    + b"\n"
                )
            else:
                raise OstreeAcquisitionError("unsupported_payload_entry")

    fd = _directory(root)
    try:
        walk(fd, "")
    finally:
        os.close(fd)
    for path, expected in _CRITICAL_FILES[component.role].items():
        if found.get(path) != expected:
            raise OstreeAcquisitionError("critical_content_mismatch")
    if found.get("metadata") != component.metadata_sha256:
        raise OstreeAcquisitionError("checkout_metadata_mismatch")
    return digest.hexdigest(), count, size


def _application_release(root: Path) -> None:
    manifest = json.loads(
        _read_file(root / "files/manifest.json", maximum=MAX_OUTPUT_BYTES)
    )
    if (
        manifest.get("runtime") != "org.freedesktop.Platform"
        or manifest.get("runtime-version") != "26.08"
    ):
        raise OstreeAcquisitionError("application_runtime_mismatch")
    modules = [
        m
        for m in manifest.get("modules", [])
        if isinstance(m, dict) and m.get("name") == "kodi"
    ]
    if len(modules) != 1:
        raise OstreeAcquisitionError("missing_kodi_source_metadata")
    sources = [
        s
        for s in modules[0].get("sources", [])
        if isinstance(s, dict) and s.get("type") == "git"
    ]
    if (
        len(sources) != 1
        or sources[0].get("url") != "https://github.com/xbmc/xbmc.git"
        or sources[0].get("commit") != KODI_RC1_SOURCE_REVISION
    ):
        raise OstreeAcquisitionError("kodi_source_revision_mismatch")
    xml = _read_file(
        root / "files/share/metainfo/tv.kodi.Kodi.metainfo.xml",
        maximum=MAX_OUTPUT_BYTES,
    )
    if b"<!DOCTYPE" in xml or b"<!ENTITY" in xml:
        raise OstreeAcquisitionError("invalid_release_xml")
    tree = ET.fromstring(xml)
    releases = tree.findall("releases/release")
    if (
        tree.findtext("id") != "tv.kodi.Kodi"
        or not releases
        or releases[0].get("version") != "22.0~rc1-Piers"
        or releases[0].get("type") != "development"
    ):
        raise OstreeAcquisitionError("kodi_release_metadata_mismatch")


@dataclass(slots=True)
class _AcquisitionLease:
    operation_id: UUID
    stage_identity: tuple[int, int]
    evidence_digest: str
    active: bool = True
    assembly_claimed: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


@dataclass(frozen=True, slots=True)
class AcquiredOstree:
    stage: Path
    evidence: OstreeVerificationEvidence
    _lease: _AcquisitionLease | None = field(default=None, repr=False, compare=False)

    def claim_assembly(self, operation_id: UUID) -> None:
        """One assembly consumer per lease, including concurrent callers."""
        self.assert_handoff(operation_id)
        assert self._lease is not None
        with self._lease.lock:
            self.assert_handoff(operation_id)
            if self._lease.assembly_claimed:
                raise OstreeAcquisitionError("assembly_handoff_already_consumed")
            self._lease.assembly_claimed = True

    def assert_handoff(self, operation_id: UUID) -> None:
        """Only live operation-owned acquisition results can enter assembly.

        This process-local lease is not an installation approval or an artifact
        signature. Assembly still hashes the bytes it actually hands to Docker.
        """
        lease = self._lease
        if (
            lease is None
            or not lease.active
            or lease.operation_id != operation_id
            or self.stage.name != f"ostree-{operation_id.hex}"
            or lease.evidence_digest != _digest(self.evidence.canonical_bytes())
            or self.evidence.recipe_digest != load_inactive_ostree_recipe().digest
        ):
            raise OstreeAcquisitionError("invalid_acquisition_handoff")
        fd = _directory(self.stage)
        try:
            info = os.fstat(fd)
            if (
                (info.st_dev, info.st_ino) != lease.stage_identity
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o700
            ):
                raise OstreeAcquisitionError("invalid_acquisition_handoff")
        finally:
            os.close(fd)

    def component_path(self, role: Literal["app", "platform", "gl"]) -> Path:
        if role not in ("app", "platform", "gl"):
            raise OstreeAcquisitionError("unknown_component")
        return self.stage / "checkout" / role


class OstreeAcquirer:
    """One fixed recipe, one private operation directory, one acquisition lock."""

    def __init__(
        self, worker_root: Path, *, runner: CommandRunner | None = None
    ) -> None:
        self._root = worker_root
        self._runner = runner or BoundedCommandRunner()
        self._definition = load_inactive_ostree_recipe()

    @contextmanager
    def acquire(self, operation_id: UUID) -> Iterator[AcquiredOstree]:
        """Yield verified staging only; clean our operation on success or failure."""

        root_fd = lock_fd = None
        created = locked = False
        lease: _AcquisitionLease | None = None
        name = f"ostree-{operation_id.hex}"
        stage = self._root / name
        try:
            root_fd = _directory(self._root)
            root = os.fstat(root_fd)
            if root.st_uid != os.geteuid() or stat.S_IMODE(root.st_mode) != 0o700:
                raise OstreeAcquisitionError("unsafe_worker_staging_root")
            lock_fd = os.open(
                "ostree-acquisition.lock",
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=root_fd,
            )
            info = os.fstat(lock_fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_uid != root.st_uid
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise OstreeAcquisitionError("unsafe_acquisition_lock")
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except BlockingIOError as error:
                raise OstreeAcquisitionBusy("acquisition_busy") from error
            if shutil.disk_usage(self._root).free < MIN_FREE_BYTES:
                raise OstreeAcquisitionError("insufficient_staging_space")
            try:
                os.mkdir(name, mode=0o700, dir_fd=root_fd)
                created = True
            except FileExistsError as error:
                raise OstreeAcquisitionError("partial_staging_not_reusable") from error
            for directory in ("home", "gnupg", "tmp", "checkout", "signatures"):
                (stage / directory).mkdir(mode=0o700)
            result = self._acquire(stage)
            stage_fd = _directory(stage)
            try:
                info = os.fstat(stage_fd)
                lease = _AcquisitionLease(
                    operation_id,
                    (info.st_dev, info.st_ino),
                    _digest(result.evidence.canonical_bytes()),
                )
            finally:
                os.close(stage_fd)
            yield replace(result, _lease=lease)
        except OstreeAcquisitionError:
            raise
        except Exception as error:
            raise OstreeAcquisitionError("acquisition_failed") from error
        finally:
            if lease is not None:
                lease.active = False
            try:
                if created and root_fd is not None:
                    try:
                        _cleanup_owned_gpg_agents(stage)
                    finally:
                        shutil.rmtree(name, dir_fd=root_fd)
            finally:
                if lock_fd is not None:
                    if locked:
                        fcntl.flock(lock_fd, fcntl.LOCK_UN)
                    os.close(lock_fd)
                if root_fd is not None:
                    os.close(root_fd)

    def _acquire(self, stage: Path) -> AcquiredOstree:
        definition: InactiveOstreeDefinition = self._definition
        deadline = time.monotonic() + MAX_ACQUISITION_SECONDS
        repo = stage / "repo"

        def run(argv: tuple[str, ...], *, code: str = "verification_failed") -> bytes:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise OstreeAcquisitionError("acquisition_timeout")
            result = self._runner.run(argv, stage=stage, timeout=min(900, remaining))
            if result.returncode != 0 or len(result.output) > MAX_OUTPUT_BYTES:
                raise OstreeAcquisitionError(code)
            return result.output

        key = stage / "flathub.gpg"
        _write_file(key, trusted_key_bytes())
        listing = run(
            (
                "/usr/bin/gpg",
                "--batch",
                "--no-options",
                "--no-autostart",
                "--with-colons",
                "--show-keys",
                str(key),
            )
        )
        _validate_key_listing(listing)
        ostree = ("/usr/bin/ostree", f"--repo={repo}")
        run((*ostree, "init", "--mode=archive"))
        for remote, url in (
            ("flathub-beta", "https://dl.flathub.org/beta-repo/"),
            ("flathub", "https://dl.flathub.org/repo/"),
        ):
            run(
                (
                    *ostree,
                    "remote",
                    "add",
                    f"--gpg-import={key}",
                    "--set=gpg-verify=true",
                    "--set=gpg-verify-summary=true",
                    remote,
                    url,
                )
            )
        for component in definition.recipe.provider.components:
            remote = "flathub-beta" if component.role == "app" else "flathub"
            run(
                (
                    *ostree,
                    "pull",
                    "--depth=0",
                    "--http-header=User-Agent=Kodi-Lab-reviewed-OSTree",
                    remote,
                    f"{component.ref}@{component.commit}",
                ),
                code="pinned_artifact_unavailable",
            )
        run((*ostree, "fsck"), code="ostree_integrity_failed")
        evidence = []
        for component in definition.recipe.provider.components:
            object_path = repo / "objects" / component.commit[:2]
            commit_path = object_path / (component.commit[2:] + ".commit")
            detached = _read_file(object_path / (component.commit[2:] + ".commitmeta"))
            commit = _read_file(commit_path)
            embedded = signed_component_metadata(commit, component)
            _metadata_config(embedded, component)
            for index, signature in enumerate(detached_signatures(detached)):
                signature_path = stage / "signatures" / f"{component.role}-{index}.sig"
                _write_file(signature_path, signature)
                result = self._runner.run(
                    (
                        "/usr/bin/gpgv",
                        "--homedir",
                        str(stage / "gnupg"),
                        "--keyring",
                        str(key),
                        "--status-fd=1",
                        str(signature_path),
                        str(commit_path),
                    ),
                    stage=stage,
                    timeout=min(30, max(0.1, deadline - time.monotonic())),
                )
                _validate_signature_status(result)
            checkout = stage / "checkout" / component.role
            run(
                (
                    *ostree,
                    "checkout",
                    "--user-mode",
                    "--force-copy",
                    component.commit,
                    str(checkout),
                )
            )
            if component.role == "app":
                _application_release(checkout)
            inventory, count, size = _inventory(checkout, component)
            evidence.append(
                ComponentEvidence(
                    role=component.role,
                    ref=component.ref,
                    commit=component.commit,
                    collection=component.collection,
                    detached_metadata_sha256=_digest(detached),
                    metadata_sha256=component.metadata_sha256,
                    inventory_sha256=inventory,
                    entry_count=count,
                    content_bytes=size,
                )
            )
        if len(evidence) != 3:
            raise OstreeAcquisitionError("artifact_closure_incomplete")
        proof = OstreeVerificationEvidence(
            recipe_digest=definition.digest,
            components=(evidence[0], evidence[1], evidence[2]),
        )
        _write_file(stage / "verification.json", proof.canonical_bytes())
        return AcquiredOstree(stage, proof)
