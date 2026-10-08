"""Recoverable AEAD secret files for external Kodi credentials."""

from __future__ import annotations

import base64
import json
import os
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.storage.models import ExternalSecretRetirementRow

MAX_SECRET_FILE_BYTES = 16_384


class SecretStoreError(RuntimeError):
    """Fail-closed secret configuration or storage error."""


def queue_external_secret_retirement(
    session: Session,
    *,
    store_kind: Literal["bridge", "kodi"],
    target_id: UUID,
    reference: UUID,
) -> None:
    """Journal an unpublished reference or atomically remove live ownership."""
    key = (store_kind, str(reference))
    existing = session.get(ExternalSecretRetirementRow, key)
    if existing is not None:
        if existing.target_id != str(target_id):
            raise SecretStoreError("secret retirement target mismatch")
        return
    session.add(
        ExternalSecretRetirementRow(
            store_kind=store_kind,
            reference=str(reference),
            target_id=str(target_id),
            created_at=datetime.now(UTC)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z"),
        )
    )


@contextmanager
def external_secret_publication(
    sessions: sessionmaker[Session],
    *,
    store_kind: Literal["bridge", "kodi"],
    target_id: UUID,
    write: Callable[[UUID], object] | None,
    retire: Callable[[UUID], None],
    validate: Callable[[Session], object] | None = None,
) -> Iterator[tuple[Session, UUID | None]]:
    """Journal before materializing, then atomically transfer to durable ownership.

    The caller publishes the reference in the yielded writer transaction. No
    network work belongs here. Process loss leaves the previously committed
    obligation; rollback never depends on successful secret deletion.
    """
    reference = None if write is None else uuid4()
    if reference is not None:
        with sessions.begin() as session:
            queue_external_secret_retirement(
                session,
                store_kind=store_kind,
                target_id=target_id,
                reference=reference,
            )
    try:
        with sessions.begin() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            if validate is not None:
                validate(session)
            pending = None
            if reference is not None:
                pending = session.get(
                    ExternalSecretRetirementRow, (store_kind, str(reference))
                )
                # Cleanup can win between registration and writer acquisition.
                # A removed obligation must never be rematerialized.
                if pending is None or pending.target_id != str(target_id):
                    raise SecretStoreError("secret publication was retired")
                assert write is not None
                write(reference)
            yield session, reference
            session.flush()
            if pending is not None:
                session.delete(pending)
    except Exception:
        if reference is not None:
            retire_pending_external_secrets(
                sessions,
                store_kind=store_kind,
                target_id=target_id,
                retire=retire,
                reference=reference,
            )
        raise


def retire_pending_external_secrets(
    sessions: sessionmaker[Session],
    *,
    store_kind: Literal["bridge", "kodi"],
    target_id: UUID,
    retire: Callable[[UUID], None],
    reference: UUID | None = None,
) -> None:
    """Delete exact files before clearing their durable deletion obligations."""
    with sessions() as session:
        query = select(ExternalSecretRetirementRow.reference).where(
            ExternalSecretRetirementRow.store_kind == store_kind,
            ExternalSecretRetirementRow.target_id == str(target_id),
        )
        if reference is not None:
            query = query.where(ExternalSecretRetirementRow.reference == str(reference))
        references = session.scalars(query).all()
    for value in references:
        with sessions.begin() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            pending = session.get(ExternalSecretRetirementRow, (store_kind, value))
            if pending is None or pending.target_id != str(target_id):
                continue
            # Serialize deletion with publication and recheck after acquiring
            # the writer lock: an earlier snapshot may now have a live owner.
            retire(UUID(value))
            session.execute(
                delete(ExternalSecretRetirementRow).where(
                    ExternalSecretRetirementRow.store_kind == store_kind,
                    ExternalSecretRetirementRow.target_id == str(target_id),
                    ExternalSecretRetirementRow.reference == value,
                )
            )


def _retire_secret_files(directory_fd: int, reference: UUID) -> None:
    for name in (f"{reference}.json", f".{reference}.tmp"):
        try:
            metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise SecretStoreError("secret file metadata is unsafe")
        os.unlink(name, dir_fd=directory_fd)
    # Even an already-missing file needs this: an earlier unlink may have
    # succeeded while its directory fsync failed before acknowledgement.
    os.fsync(directory_fd)


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

    def write(
        self,
        target_id: UUID,
        credential: ExternalKodiCredential,
        *,
        reference: UUID | None = None,
    ) -> UUID:
        key = self._require_key()
        reference = reference or uuid4()
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
        temporary = f".{reference}.tmp"
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
        """Delete final and interrupted temporary files for one exact reference."""
        try:
            directory_fd = self._open_directory(create=False)
        except SecretStoreError as error:
            if isinstance(error.__cause__, FileNotFoundError):
                return
            raise
        try:
            _retire_secret_files(directory_fd, reference)
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


class ExternalBridgeSecretStore:
    """The Slice 1 AEAD/no-follow facility specialized for 256-bit bridge keys."""

    _PURPOSES = frozenset({"pairing_token", "request_authentication"})

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

    def write(
        self,
        target_id: UUID,
        purpose: str,
        secret: bytes,
        *,
        reference: UUID | None = None,
    ) -> UUID:
        if purpose not in self._PURPOSES or len(secret) != 32:
            raise SecretStoreError("bridge secret shape is invalid")
        key = self._require_key()
        reference = reference or uuid4()
        nonce = os.urandom(12)
        ciphertext = AESGCM(key).encrypt(
            nonce,
            secret,
            self._associated_data(target_id, reference, purpose, self._key_version),
        )
        document = json.dumps(
            {
                "format": 1,
                "key_version": self._key_version,
                "purpose": purpose,
                "nonce": base64.urlsafe_b64encode(nonce).decode("ascii"),
                "ciphertext": base64.urlsafe_b64encode(ciphertext).decode("ascii"),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        if len(document) > MAX_SECRET_FILE_BYTES:
            raise SecretStoreError("encrypted bridge secret is too large")
        reference_name = f"{reference}.json"
        directory_fd = self._open_directory(create=True)
        temporary = f".{reference}.tmp"
        try:
            try:
                os.stat(reference_name, dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise SecretStoreError("bridge secret reference collision")
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
                reference_name,
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

    def read(self, target_id: UUID, reference: UUID, purpose: str) -> bytes:
        if purpose not in self._PURPOSES:
            raise SecretStoreError("bridge secret purpose is invalid")
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
            raise SecretStoreError("encrypted bridge secret is unavailable") from error
        finally:
            os.close(directory_fd)
        try:
            raw = json.loads(document)
            if not isinstance(raw, dict) or set(raw) != {
                "format",
                "key_version",
                "purpose",
                "nonce",
                "ciphertext",
            }:
                raise ValueError
            if (
                raw["format"] != 1
                or raw["key_version"] != self._key_version
                or raw["purpose"] != purpose
            ):
                raise ValueError
            secret = AESGCM(key).decrypt(
                base64.urlsafe_b64decode(raw["nonce"]),
                base64.urlsafe_b64decode(raw["ciphertext"]),
                self._associated_data(
                    target_id, reference, purpose, raw["key_version"]
                ),
            )
            if len(secret) != 32:
                raise ValueError
            return secret
        except (InvalidTag, ValueError, TypeError, json.JSONDecodeError) as error:
            raise SecretStoreError(
                "encrypted bridge secret cannot be decrypted"
            ) from error

    def retire(self, reference: UUID) -> None:
        """Delete final and interrupted temporary files for one exact reference."""
        try:
            directory_fd = self._open_directory(create=False)
        except SecretStoreError as error:
            if isinstance(error.__cause__, FileNotFoundError):
                return
            raise
        try:
            _retire_secret_files(directory_fd, reference)
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
        return os.open(self._root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)

    @staticmethod
    def _associated_data(
        target_id: UUID, reference: UUID, purpose: str, key_version: int
    ) -> bytes:
        return (
            f"kodi-lab-external-bridge\0{purpose}\0{target_id}\0{reference}\0{key_version}"
        ).encode()
