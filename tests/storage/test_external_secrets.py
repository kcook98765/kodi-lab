from __future__ import annotations

import base64
import stat
from pathlib import Path
from uuid import uuid4

import pytest

from kodi_lab.storage.secrets import (
    ExternalCredentialStore,
    ExternalKodiCredential,
    SecretStoreError,
    decode_master_key,
)


def test_secret_store_encrypts_with_exact_permissions_and_round_trips(
    tmp_path: Path,
) -> None:
    root = tmp_path / "secrets"
    target_id = uuid4()
    store = ExternalCredentialStore(root, master_key=b"k" * 32, key_version=7)
    reference = store.write(target_id, ExternalKodiCredential("alice", "sensitive"))
    path = root / f"{reference}.json"
    raw = path.read_bytes()
    assert b"alice" not in raw
    assert b"sensitive" not in raw
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert store.read(target_id, reference) == ExternalKodiCredential(
        "alice", "sensitive"
    )


def test_secret_store_wrong_or_missing_key_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "secrets"
    target_id = uuid4()
    reference = ExternalCredentialStore(
        root, master_key=b"a" * 32, key_version=1
    ).write(target_id, ExternalKodiCredential("alice", "sensitive"))
    with pytest.raises(SecretStoreError, match="cannot be decrypted"):
        ExternalCredentialStore(root, master_key=b"b" * 32, key_version=1).read(
            target_id, reference
        )
    with pytest.raises(SecretStoreError, match="master key"):
        ExternalCredentialStore(root, master_key=None, key_version=1).read(
            target_id, reference
        )
    with pytest.raises(SecretStoreError, match="cannot be decrypted"):
        ExternalCredentialStore(root, master_key=b"a" * 32, key_version=2).read(
            target_id, reference
        )


def test_secret_rotation_uses_new_reference_and_retirement_is_exact(
    tmp_path: Path,
) -> None:
    root = tmp_path / "secrets"
    target_id = uuid4()
    store = ExternalCredentialStore(root, master_key=b"k" * 32, key_version=1)
    first = store.write(target_id, ExternalKodiCredential("alice", "one"))
    second = store.write(target_id, ExternalKodiCredential("alice", "two"))
    assert first != second
    assert (root / f"{first}.json").read_bytes() != (
        root / f"{second}.json"
    ).read_bytes()
    store.retire(first)
    assert not (root / f"{first}.json").exists()
    assert store.read(target_id, second).password == "two"


def test_master_key_is_explicit_256_bit_base64url() -> None:
    encoded = base64.urlsafe_b64encode(b"k" * 32).rstrip(b"=").decode()
    assert decode_master_key(encoded) == b"k" * 32
    assert decode_master_key(None) is None
    with pytest.raises(SecretStoreError):
        decode_master_key("short")
    with pytest.raises(SecretStoreError):
        decode_master_key("!" * 43)
