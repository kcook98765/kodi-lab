"""Crash, commit, deletion and serialization boundaries on real SQLite/AEAD."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import Engine, event, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.external_targets import ExternalTargetAdministrationService
from kodi_lab.domain import ExternalAuthenticationMode, ExternalProtocolKind
from kodi_lab.kodi.external import ExternalEndpointPolicy
from kodi_lab.storage import metadata
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.storage.models import (
    ExternalBridgePairingRow,
    ExternalSecretRetirementRow,
)
from kodi_lab.storage.secrets import (
    ExternalBridgeSecretStore,
    ExternalCredentialStore,
    ExternalKodiCredential,
    SecretStoreError,
    external_secret_publication,
    queue_external_secret_retirement,
    retire_pending_external_secrets,
)


@pytest.fixture
def storage(
    tmp_path: Path,
) -> tuple[Engine, sessionmaker[Session], ExternalBridgeSecretStore, UUID]:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'publication.db'}")
    metadata.create_all(engine)
    sessions = create_session_factory(engine)
    targets = ExternalTargetAdministrationService(
        sessions,
        endpoint_policy=ExternalEndpointPolicy(allowed_ports=(8080,)),
        credential_store=ExternalCredentialStore(
            tmp_path / "native",
            master_key=b"k" * 32,
            key_version=1,
        ),
    )
    target = targets.register(
        logical_name="Publication fixture",
        protocol_kind=ExternalProtocolKind.JSONRPC_HTTP,
        host="192.168.50.20",
        port=8080,
        authentication_mode=ExternalAuthenticationMode.NONE,
        configured_capabilities={"target_inspect"},
    )
    with sessions.begin() as session:
        session.add(
            ExternalBridgePairingRow(
                target_id=str(target.target_id),
                pairing_state="unpaired",
                reachability_state="unknown",
                observed_capabilities_json="[]",
                verified_capabilities_json="[]",
            )
        )
    return (
        engine,
        sessions,
        ExternalBridgeSecretStore(
            tmp_path / "bridge",
            master_key=b"k" * 32,
            key_version=1,
        ),
        target.target_id,
    )


# Abrupt os._exit bypasses all Python rollback/cleanup handlers. Reopening the
# database and store proves recovery does not rely on an exception catch.
CRASH_WRITER = """
import os, sys
from uuid import UUID
from sqlalchemy import event
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.storage.models import ExternalBridgePairingRow
from kodi_lab.storage.secrets import (
    ExternalBridgeSecretStore, external_secret_publication,
)
from pathlib import Path
url, root, target, boundary = sys.argv[1:]
engine = create_database_engine(url)
sessions = create_session_factory(engine)
store = ExternalBridgeSecretStore(Path(root), master_key=b"k"*32, key_version=1)
target_id = UUID(target)
if boundary == "registration":
    def before(conn, cursor, statement, *args):
        if statement.startswith("INSERT INTO external_secret_retirements"):
            os._exit(73)
    event.listen(engine, "before_cursor_execute", before)
if boundary == "transferred":
    def after(conn, cursor, statement, *args):
        if statement.startswith("DELETE FROM external_secret_retirements"):
            os._exit(73)
    event.listen(engine, "after_cursor_execute", after)
replace = os.replace
def interrupted_replace(*args, **kwargs):
    if boundary == "temporary":
        os._exit(73)
    replace(*args, **kwargs)
    if boundary == "renamed":
        os._exit(73)
os.replace = interrupted_replace
def write(reference):
    if boundary == "registered":
        os._exit(73)
    store.write(target_id, "request_authentication", b"x"*32, reference=reference)
    if boundary == "written":
        os._exit(73)
with external_secret_publication(
    sessions, store_kind="bridge", target_id=target_id,
    write=write, retire=store.retire,
) as (session, reference):
    row = session.get(ExternalBridgePairingRow, target)
    row.bridge_secret_reference = str(reference)
    row.observed_bridge_id = "00000000-0000-4000-8000-000000000001"
    row.pairing_state = "pairing_pending"
    session.flush()
    if boundary == "owner_flushed":
        os._exit(73)
os._exit(73)
"""


@pytest.mark.parametrize(
    "boundary",
    [
        "registration",
        "registered",
        "temporary",
        "renamed",
        "written",
        "owner_flushed",
        "transferred",
        "committed",
    ],
)
def test_process_exit_leaves_durable_owner_or_retirement(
    storage: tuple[Engine, sessionmaker[Session], ExternalBridgeSecretStore, UUID],
    boundary: str,
) -> None:
    engine, _, store, target_id = storage
    url = str(engine.url)
    engine.dispose()
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            CRASH_WRITER,
            url,
            str(store._root),
            str(target_id),
            boundary,
        ],
        capture_output=True,
        check=False,
        timeout=20,
    )
    assert result.returncode == 73, result.stderr.decode()
    reopened = create_database_engine(url)
    sessions = create_session_factory(reopened)
    try:
        with sessions() as session:
            owner = session.get(ExternalBridgePairingRow, str(target_id))
            assert owner is not None
            pending = session.scalars(select(ExternalSecretRetirementRow)).all()
            if boundary == "committed":
                assert owner.bridge_secret_reference is not None and pending == []
                reference = UUID(owner.bridge_secret_reference)
            elif boundary == "registration":
                assert owner.bridge_secret_reference is None and pending == []
                assert not store._root.exists()
                return
            else:
                assert owner.bridge_secret_reference is None and len(pending) == 1
                reference = UUID(pending[0].reference)
        retire_pending_external_secrets(
            sessions,
            store_kind="bridge",
            target_id=target_id,
            retire=store.retire,
        )
        if boundary == "committed":
            assert (
                store.read(target_id, reference, "request_authentication") == b"x" * 32
            )
        else:
            assert not list(store._root.glob("*")) if store._root.exists() else True
            with sessions() as session:
                assert session.scalars(select(ExternalSecretRetirementRow)).all() == []
    finally:
        reopened.dispose()


def test_publication_commit_failure_rolls_back_owner_and_deletes_secret(
    storage: tuple[Engine, sessionmaker[Session], ExternalBridgeSecretStore, UUID],
) -> None:
    engine, sessions, store, target_id = storage
    commits = 0
    references: list[UUID] = []

    def fail_commit(connection: Any) -> None:
        nonlocal commits
        commits += 1
        if commits == 2:
            raise SQLAlchemyError("injected pairing commit failure")

    def write(reference: UUID) -> UUID:
        references.append(reference)
        return store.write(
            target_id, "request_authentication", b"x" * 32, reference=reference
        )

    event.listen(engine, "commit", fail_commit)
    try:
        with (
            pytest.raises(SQLAlchemyError, match="pairing commit"),
            external_secret_publication(
                sessions,
                store_kind="bridge",
                target_id=target_id,
                write=write,
                retire=store.retire,
            ) as (session, reference),
        ):
            row = session.get(ExternalBridgePairingRow, str(target_id))
            assert row is not None
            row.bridge_secret_reference = str(reference)
    finally:
        event.remove(engine, "commit", fail_commit)
    with sessions() as session:
        row = session.get(ExternalBridgePairingRow, str(target_id))
        assert row is not None and row.bridge_secret_reference is None
        assert session.scalars(select(ExternalSecretRetirementRow)).all() == []
    assert len(references) == 1
    assert list(store._root.iterdir()) == []


@pytest.mark.parametrize("native", [False, True])
def test_unlink_directory_fsync_failure_retains_obligation_until_durable_retry(
    storage: tuple[Engine, sessionmaker[Session], ExternalBridgeSecretStore, UUID],
    monkeypatch: pytest.MonkeyPatch,
    native: bool,
) -> None:
    _, sessions, bridge, target_id = storage
    store: ExternalBridgeSecretStore | ExternalCredentialStore = bridge
    kind = "bridge"
    if native:
        kind = "kodi"
        store = ExternalCredentialStore(
            bridge._root, master_key=b"k" * 32, key_version=1
        )
        reference = store.write(target_id, ExternalKodiCredential("alice", "password"))
    else:
        reference = bridge.write(target_id, "request_authentication", b"x" * 32)
    with sessions.begin() as session:
        queue_external_secret_retirement(
            session,
            store_kind="kodi" if native else "bridge",
            target_id=target_id,
            reference=reference,
        )
    fsync = os.fsync
    syncs = 0

    def failed(fd: int) -> None:
        nonlocal syncs
        assert stat.S_ISDIR(os.fstat(fd).st_mode)
        syncs += 1
        raise OSError("injected deletion directory fsync failure")

    monkeypatch.setattr(os, "fsync", failed)
    for _ in range(2):
        with pytest.raises(OSError, match="fsync"):
            retire_pending_external_secrets(
                sessions,
                store_kind="kodi" if native else "bridge",
                target_id=target_id,
                retire=store.retire,
            )
        assert not (store._root / f"{reference}.json").exists()
        with sessions() as session:
            assert (
                session.get(ExternalSecretRetirementRow, (kind, str(reference)))
                is not None
            )
    monkeypatch.setattr(os, "fsync", fsync)
    retire_pending_external_secrets(
        sessions,
        store_kind="kodi" if native else "bridge",
        target_id=target_id,
        retire=store.retire,
    )
    assert syncs == 2
    with sessions() as session:
        assert session.scalars(select(ExternalSecretRetirementRow)).all() == []


def test_stale_cleanup_snapshot_cannot_delete_newly_owned_secret(
    storage: tuple[Engine, sessionmaker[Session], ExternalBridgeSecretStore, UUID],
) -> None:
    engine, sessions, store, target_id = storage
    selected = threading.Event()
    release = threading.Event()
    calls = 0

    def snapshot_pause(*args: Any) -> None:
        if args[2].startswith("SELECT external_secret_retirements.reference"):
            selected.set()
            assert release.wait(5)

    def retire(reference: UUID) -> None:
        nonlocal calls
        calls += 1
        store.retire(reference)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with external_secret_publication(
            sessions,
            store_kind="bridge",
            target_id=target_id,
            write=lambda reference: store.write(
                target_id,
                "request_authentication",
                b"x" * 32,
                reference=reference,
            ),
            retire=store.retire,
        ) as (session, reference):
            assert reference is not None
            event.listen(engine, "after_cursor_execute", snapshot_pause)
            cleanup = pool.submit(
                retire_pending_external_secrets,
                sessions,
                store_kind="bridge",
                target_id=target_id,
                retire=retire,
            )
            assert selected.wait(5)
            row = session.get(ExternalBridgePairingRow, str(target_id))
            assert row is not None
            row.bridge_secret_reference = str(reference)
            # Allow cleanup's read cursor to finish before writer commit, but
            # its deletion must wait on this publication's SQLite writer lock.
            release.set()
        cleanup.result(timeout=5)
    event.remove(engine, "after_cursor_execute", snapshot_pause)
    assert calls == 0
    assert store.read(target_id, reference, "request_authentication") == b"x" * 32


def test_cleanup_winning_before_materialization_prevents_republication(
    storage: tuple[Engine, sessionmaker[Session], ExternalBridgeSecretStore, UUID],
) -> None:
    _engine, sessions, store, target_id = storage
    commits = 0
    writes = 0

    def clean_after_registration(*args: Any) -> None:
        nonlocal commits
        commits += 1
        if commits == 1:
            retire_pending_external_secrets(
                sessions,
                store_kind="bridge",
                target_id=target_id,
                retire=store.retire,
            )

    def write(reference: UUID) -> UUID:
        nonlocal writes
        writes += 1
        return store.write(
            target_id, "request_authentication", b"x" * 32, reference=reference
        )

    event.listen(sessions, "after_commit", clean_after_registration)
    try:
        with (
            pytest.raises(SecretStoreError, match="retired"),
            external_secret_publication(
                sessions,
                store_kind="bridge",
                target_id=target_id,
                write=write,
                retire=store.retire,
            ),
        ):
            pytest.fail("retired reference was republished")
    finally:
        event.remove(sessions, "after_commit", clean_after_registration)
    assert writes == 0
    assert not store._root.exists()
    with sessions() as session:
        assert session.scalars(select(ExternalSecretRetirementRow)).all() == []


@pytest.mark.parametrize("boundary", ["file_fsync", "rename", "directory_fsync"])
def test_secret_write_failure_and_failed_cleanup_retain_exact_reference(
    storage: tuple[Engine, sessionmaker[Session], ExternalBridgeSecretStore, UUID],
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    _, sessions, store, target_id = storage
    fsync, replace, unlink = os.fsync, os.replace, os.unlink
    references: list[UUID] = []

    def write(reference: UUID) -> UUID:
        references.append(reference)
        return store.write(
            target_id, "request_authentication", b"x" * 32, reference=reference
        )

    def fail_sync(fd: int) -> None:
        directory = stat.S_ISDIR(os.fstat(fd).st_mode)
        if (boundary == "directory_fsync" and directory) or (
            boundary == "file_fsync" and not directory
        ):
            raise OSError("injected secret write fsync failure")
        fsync(fd)

    def fail_rename(*args: Any, **kwargs: Any) -> None:
        if boundary == "rename":
            raise OSError("injected secret rename failure")
        replace(*args, **kwargs)

    def fail_delete(*args: Any, **kwargs: Any) -> None:
        raise OSError("injected mandatory deletion failure")

    monkeypatch.setattr(os, "fsync", fail_sync)
    monkeypatch.setattr(os, "replace", fail_rename)
    monkeypatch.setattr(os, "unlink", fail_delete)
    with (
        pytest.raises(OSError),
        external_secret_publication(
            sessions,
            store_kind="bridge",
            target_id=target_id,
            write=write,
            retire=store.retire,
        ),
    ):
        pytest.fail("failed secret write was published")
    (reference,) = references
    with sessions() as session:
        owner = session.get(ExternalBridgePairingRow, str(target_id))
        assert owner is not None and owner.bridge_secret_reference is None
        assert (
            session.get(ExternalSecretRetirementRow, ("bridge", str(reference)))
            is not None
        )
    assert {p.name for p in store._root.iterdir()} == {
        f"{reference}.json" if boundary == "directory_fsync" else f".{reference}.tmp"
    }
    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "replace", replace)
    monkeypatch.setattr(os, "unlink", unlink)
    retire_pending_external_secrets(
        sessions,
        store_kind="bridge",
        target_id=target_id,
        retire=store.retire,
    )
    assert list(store._root.iterdir()) == []


def test_lost_publication_commit_response_preserves_committed_owner(
    storage: tuple[Engine, sessionmaker[Session], ExternalBridgeSecretStore, UUID],
) -> None:
    _, sessions, store, target_id = storage
    commits = 0
    reference: UUID | None = None

    def lost_response(*args: Any) -> None:
        nonlocal commits
        commits += 1
        if commits == 2:
            raise SQLAlchemyError("lost committed publication response")

    event.listen(sessions, "after_commit", lost_response)
    try:
        with (
            pytest.raises(SQLAlchemyError),
            external_secret_publication(
                sessions,
                store_kind="bridge",
                target_id=target_id,
                write=lambda reference: store.write(
                    target_id,
                    "request_authentication",
                    b"x" * 32,
                    reference=reference,
                ),
                retire=store.retire,
            ) as (session, reference),
        ):
            owner = session.get(ExternalBridgePairingRow, str(target_id))
            assert owner is not None
            owner.bridge_secret_reference = str(reference)
    finally:
        event.remove(sessions, "after_commit", lost_response)
    assert reference is not None
    with sessions() as session:
        owner = session.get(ExternalBridgePairingRow, str(target_id))
        assert owner is not None and owner.bridge_secret_reference == str(reference)
        assert session.scalars(select(ExternalSecretRetirementRow)).all() == []
    assert store.read(target_id, reference, "request_authentication") == b"x" * 32
