"""Recoverable AEAD secret files for external Kodi credentials."""

from __future__ import annotations

import base64
import json
import os
import stat
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAX_SECRET_FILE_BYTES = 16_384


class SecretStoreError(RuntimeError):
    """Fail-closed secret configuration or storage error."""


@dataclass(frozen=True, slots=True)
class ExternalKodiCredential:
    username: str
    password: str

    def __post_init__(self) -> None:
        if not self.username or len(self.username) > 256:
            raise ValueError("username must contain 1-256 characters")
        if not self.password or len(self.password) > 1024:
            raise ValueError("password must contain 1-1024 characters")


def decode_master_key(value: str | None) -> bytes | None:
    """Decode exactly one deployment-provided 256-bit base64url key."""

    if value is None:
        return None
    try:
        encoded = value.strip().encode("ascii")
        encoded += b"=" * (-len(encoded) % 4)
        raw = base64.b64decode(encoded, altchars=b"-_", validate=True)
    except (UnicodeEncodeError, ValueError) as error:
        raise SecretStoreError("external target master key is invalid") from error
    if len(raw) != 32:
        raise SecretStoreError("external target master key must decode to 32 bytes")
    return raw


class ExternalCredentialStore:
    """UUID-addressed, no-follow, atomic AES-256-GCM credential storage."""

    def __init__(
        self,
        root: Path,
        *,
        master_key: bytes | None,
        key_version: int,
    ) -> None:
        self._root = root
        self._master_key = master_key
        self._key_version = key_version

    @property
    def available(self) -> bool:
        return self._master_key is not None

    def write(self, target_id: UUID, credential: ExternalKodiCredential) -> UUID:
        key = self._require_key()
        reference = uuid4()
        plaintext = json.dumps(
            {"username": credential.username, "password": credential.password},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        nonce = os.urandom(12)
        associated = self._associated_data(target_id, reference, self._key_version)
        ciphertext = AESGCM(key).encrypt(nonce, plaintext, associated)
        document = json.dumps(
            {
                "format": 1,
                "key_version": self._key_version,
                "nonce": base64.urlsafe_b64encode(nonce).decode("ascii"),
                "ciphertext": base64.urlsafe_b64encode(ciphertext).decode("ascii"),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        if len(document) > MAX_SECRET_FILE_BYTES:
            raise SecretStoreError("encrypted credential is too large")
        directory_fd = self._open_directory(create=True)
        temporary = f".{uuid4()}.tmp"
        final = f"{reference}.json"
        try:
            try:
                os.stat(final, dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise SecretStoreError("secret reference collision")
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory_fd,
            )
            try:
                with os.fdopen(descriptor, "wb", closefd=False) as handle:
                    handle.write(document)
                    handle.flush()
                    os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.replace(
                temporary,
                final,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
            os.fsync(directory_fd)
        except Exception:
            with suppress(FileNotFoundError):
                os.unlink(temporary, dir_fd=directory_fd)
            raise
        finally:
            os.close(directory_fd)
        return reference

    def read(self, target_id: UUID, reference: UUID) -> ExternalKodiCredential:
        key = self._require_key()
        directory_fd = self._open_directory(create=False)
        try:
            descriptor = os.open(
                f"{reference}.json",
                os.O_RDONLY | os.O_NOFOLLOW,
                dir_fd=directory_fd,
            )
            try:
                metadata = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or stat.S_IMODE(metadata.st_mode) != 0o600
                ):
                    raise SecretStoreError("secret file metadata is unsafe")
                chunks: list[bytes] = []
                remaining = MAX_SECRET_FILE_BYTES + 1
                while remaining:
                    chunk = os.read(descriptor, remaining)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                document = b"".join(chunks)
                if len(document) > MAX_SECRET_FILE_BYTES:
                    raise SecretStoreError("secret file is too large")
            finally:
                os.close(descriptor)
        except (FileNotFoundError, OSError) as error:
            raise SecretStoreError("encrypted credential is unavailable") from error
        finally:
            os.close(directory_fd)
        try:
            raw = json.loads(document)
            if not isinstance(raw, dict) or set(raw) != {
                "format",
                "key_version",
                "nonce",
                "ciphertext",
            }:
                raise ValueError
            if (
                raw["format"] != 1
                or not isinstance(raw["key_version"], int)
                or isinstance(raw["key_version"], bool)
                or raw["key_version"] != self._key_version
            ):
                raise ValueError
            nonce = base64.urlsafe_b64decode(raw["nonce"])
            ciphertext = base64.urlsafe_b64decode(raw["ciphertext"])
            associated = self._associated_data(target_id, reference, raw["key_version"])
            plaintext = AESGCM(key).decrypt(nonce, ciphertext, associated)
            credential = json.loads(plaintext)
            if not isinstance(credential, dict) or set(credential) != {
                "username",
                "password",
            }:
                raise ValueError
            return ExternalKodiCredential(**credential)
        except (InvalidTag, ValueError, TypeError, json.JSONDecodeError) as error:
            raise SecretStoreError(
                "encrypted credential cannot be decrypted"
            ) from error

    def retire(self, reference: UUID) -> None:
        """Remove one exact UUID secret without following attacker-controlled links."""

        directory_fd = self._open_directory(create=False)
        try:
            name = f"{reference}.json"
            metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if not stat.S_ISREG(metadata.st_mode):
                raise SecretStoreError("secret file metadata is unsafe")
            os.unlink(name, dir_fd=directory_fd)
            os.fsync(directory_fd)
        except FileNotFoundError:
            return
        finally:
            os.close(directory_fd)

    def _require_key(self) -> bytes:
        if self._master_key is None:
            raise SecretStoreError("external target master key is unavailable")
        return self._master_key

    def _open_directory(self, *, create: bool) -> int:
        if create:
            with suppress(FileExistsError):
                self._root.mkdir(mode=0o700, parents=True)
        try:
            metadata = self._root.lstat()
        except FileNotFoundError as error:
            raise SecretStoreError("secret directory is unavailable") from error
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise SecretStoreError("secret directory metadata is unsafe")
        try:
            return os.open(self._root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError as error:
            raise SecretStoreError("secret directory is unavailable") from error

    @staticmethod
    def _associated_data(target_id: UUID, reference: UUID, key_version: int) -> bytes:
        value = f"kodi-lab-external-credential\0{target_id}\0{reference}\0{key_version}"
        return value.encode()
