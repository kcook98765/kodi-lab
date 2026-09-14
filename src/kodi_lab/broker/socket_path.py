"""Fail-closed preparation and cleanup of the broker Unix socket path."""

from __future__ import annotations

import errno
import os
import socket
import stat
from dataclasses import dataclass
from pathlib import Path

from kodi_lab.broker.errors import BrokerStartupError

PRODUCTION_RUNTIME_DIRECTORY = Path("/run/kodi-lab")
PRODUCTION_SOCKET_PATH = PRODUCTION_RUNTIME_DIRECTORY / "broker.sock"


@dataclass(frozen=True, slots=True)
class SocketPathConfig:
    """Expected secure runtime-directory and socket metadata."""

    runtime_directory: Path
    socket_path: Path
    directory_owner_uid: int
    directory_group_gid: int
    socket_owner_uid: int
    socket_group_gid: int
    directory_mode: int = 0o750
    socket_mode: int = 0o660


@dataclass(frozen=True, slots=True)
class _PathIdentity:
    device: int
    inode: int

    @classmethod
    def from_stat(cls, metadata: os.stat_result) -> _PathIdentity:
        return cls(device=metadata.st_dev, inode=metadata.st_ino)


class PreparedUnixListener:
    """A bound listener that owns only its verified socket inode."""

    def __init__(
        self,
        listener: socket.socket,
        config: SocketPathConfig,
        runtime_identity: _PathIdentity,
        socket_identity: _PathIdentity,
    ) -> None:
        self.socket = listener
        self._config = config
        self._runtime_identity = runtime_identity
        self._socket_identity = socket_identity
        self._closed = False

    def close(self) -> None:
        """Close and unlink only the exact socket this object created."""

        if self._closed:
            return
        self._closed = True
        self.socket.close()
        try:
            runtime_stat = os.lstat(self._config.runtime_directory)
            socket_stat = os.lstat(self._config.socket_path)
        except FileNotFoundError:
            return
        if (
            _PathIdentity.from_stat(runtime_stat) == self._runtime_identity
            and _PathIdentity.from_stat(socket_stat) == self._socket_identity
            and _runtime_has_expected_metadata(runtime_stat, self._config)
            and _socket_has_expected_metadata(socket_stat, self._config)
        ):
            self._config.socket_path.unlink()


def _normalized_absolute(path: Path) -> Path:
    return Path(os.path.normpath(os.fspath(path)))


def _validate_config_paths(config: SocketPathConfig) -> None:
    runtime_directory = config.runtime_directory
    socket_path = config.socket_path
    if not runtime_directory.is_absolute() or not socket_path.is_absolute():
        raise BrokerStartupError("broker socket paths must be absolute")
    if _normalized_absolute(runtime_directory) != runtime_directory:
        raise BrokerStartupError("broker runtime directory must be normalized")
    if _normalized_absolute(socket_path) != socket_path:
        raise BrokerStartupError("broker socket path must be normalized")
    if socket_path.parent != runtime_directory:
        raise BrokerStartupError(
            "broker socket must be directly inside runtime directory"
        )


def _validated_runtime_directory(config: SocketPathConfig) -> os.stat_result:
    _validate_config_paths(config)
    try:
        metadata = os.lstat(config.runtime_directory)
    except FileNotFoundError as error:
        raise BrokerStartupError("broker runtime directory does not exist") from error
    if not stat.S_ISDIR(metadata.st_mode):
        raise BrokerStartupError("broker runtime path is not a directory")
    if metadata.st_uid != config.directory_owner_uid:
        raise BrokerStartupError("broker runtime directory has unexpected owner")
    if metadata.st_gid != config.directory_group_gid:
        raise BrokerStartupError("broker runtime directory has unexpected group")
    if stat.S_IMODE(metadata.st_mode) != config.directory_mode:
        raise BrokerStartupError("broker runtime directory has unexpected mode")
    return metadata


def _runtime_has_expected_metadata(
    metadata: os.stat_result, config: SocketPathConfig
) -> bool:
    return (
        stat.S_ISDIR(metadata.st_mode)
        and metadata.st_uid == config.directory_owner_uid
        and metadata.st_gid == config.directory_group_gid
        and stat.S_IMODE(metadata.st_mode) == config.directory_mode
    )


def _socket_has_expected_metadata(
    metadata: os.stat_result, config: SocketPathConfig
) -> bool:
    return (
        stat.S_ISSOCK(metadata.st_mode)
        and metadata.st_uid == config.socket_owner_uid
        and metadata.st_gid == config.socket_group_gid
        and stat.S_IMODE(metadata.st_mode) == config.socket_mode
    )


def _existing_socket_is_live(path: Path) -> bool:
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(0.2)
    try:
        probe.connect(os.fspath(path))
        return True
    except ConnectionRefusedError:
        return False
    except OSError as error:
        if error.errno == errno.ECONNREFUSED:
            return False
        raise BrokerStartupError(
            "existing broker socket could not be verified"
        ) from error
    finally:
        probe.close()


def _remove_verified_stale_socket(
    config: SocketPathConfig,
    runtime_identity: _PathIdentity,
    initial: os.stat_result,
) -> None:
    if not _socket_has_expected_metadata(initial, config):
        raise BrokerStartupError("existing broker socket has unsafe metadata")
    if _existing_socket_is_live(config.socket_path):
        raise BrokerStartupError("another broker is already listening")
    try:
        runtime_current = os.lstat(config.runtime_directory)
        current = os.lstat(config.socket_path)
    except FileNotFoundError as error:
        raise BrokerStartupError(
            "broker socket path changed during validation"
        ) from error
    if (
        _PathIdentity.from_stat(runtime_current) != runtime_identity
        or not _runtime_has_expected_metadata(runtime_current, config)
        or _PathIdentity.from_stat(current) != _PathIdentity.from_stat(initial)
        or not _socket_has_expected_metadata(current, config)
    ):
        raise BrokerStartupError("broker socket path changed during validation")
    config.socket_path.unlink()


def prepare_unix_listener(
    config: SocketPathConfig, *, backlog: int = 16
) -> PreparedUnixListener:
    """Validate the directory, safely clear a stale socket, and bind."""

    runtime_stat = _validated_runtime_directory(config)
    runtime_identity = _PathIdentity.from_stat(runtime_stat)
    try:
        existing = os.lstat(config.socket_path)
    except FileNotFoundError:
        existing = None

    if existing is not None:
        if stat.S_ISLNK(existing.st_mode):
            raise BrokerStartupError("broker socket path must not be a symbolic link")
        if not stat.S_ISSOCK(existing.st_mode):
            raise BrokerStartupError("broker socket path is occupied by a non-socket")
        _remove_verified_stale_socket(config, runtime_identity, existing)

    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    created_identity: _PathIdentity | None = None
    try:
        listener.bind(os.fspath(config.socket_path))
        created = os.lstat(config.socket_path)
        if not stat.S_ISSOCK(created.st_mode):
            raise BrokerStartupError("bound broker path is not a Unix socket")
        created_identity = _PathIdentity.from_stat(created)
        os.chown(
            config.socket_path,
            config.socket_owner_uid,
            config.socket_group_gid,
            follow_symlinks=False,
        )
        os.chmod(config.socket_path, config.socket_mode, follow_symlinks=False)
        secured = os.lstat(config.socket_path)
        if not _socket_has_expected_metadata(secured, config):
            raise BrokerStartupError("broker socket ownership or mode could not be set")
        runtime_after = _validated_runtime_directory(config)
        if _PathIdentity.from_stat(runtime_after) != runtime_identity:
            raise BrokerStartupError("broker runtime directory changed during startup")
        listener.listen(backlog)
        return PreparedUnixListener(
            listener,
            config,
            runtime_identity,
            _PathIdentity.from_stat(secured),
        )
    except Exception:
        listener.close()
        try:
            runtime_candidate = os.lstat(config.runtime_directory)
            candidate = os.lstat(config.socket_path)
        except FileNotFoundError:
            pass
        else:
            if (
                created_identity is not None
                and _PathIdentity.from_stat(runtime_candidate) == runtime_identity
                and _runtime_has_expected_metadata(runtime_candidate, config)
                and _PathIdentity.from_stat(candidate) == created_identity
                and stat.S_ISSOCK(candidate.st_mode)
            ):
                config.socket_path.unlink()
        raise
