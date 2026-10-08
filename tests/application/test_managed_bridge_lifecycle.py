"""Restore/replacement cannot adopt Bridge credentials contained in snapshots."""

from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.snapshot_restores import SnapshotRestoreError
from kodi_lab.broker.docker_inventory import ContainerRuntimeState
from kodi_lab.storage.models import ManagedBridgeTrustRow
from kodi_lab.storage.secrets import ExternalBridgeSecretStore, SecretStoreError
from tests.application.test_snapshot_restores import (
    INSTANCE_ID,
    OLD_ID,
    REVISION_ID,
    FakeBroker,
    service,
)
from tests.application.test_snapshot_restores import factory as factory


@pytest.mark.parametrize("failure", [None, "deprovision", "apply", "invalid_artifact"])
def test_restore_invalidates_before_removal_and_cannot_adopt_snapshot_key(
    factory: sessionmaker[Session],
    tmp_path: Path,
    failure: str | None,
) -> None:
    secrets = ExternalBridgeSecretStore(
        tmp_path / "secrets", master_key=b"x" * 32, key_version=1
    )
    reference = secrets.write(INSTANCE_ID, "request_authentication", b"y" * 32)
    factory.configure(info={"managed_bridge_secrets": secrets})
    with factory.begin() as session:
        session.add(
            ManagedBridgeTrustRow(
                target_id=str(INSTANCE_ID),
                runtime_revision_id=str(REVISION_ID),
                container_id=OLD_ID,
                bridge_id=str(uuid4()),
                bridge_port=8765,
                secret_reference=str(reference),
                identity_json="{}",
                state="active",
                generation=1,
                prepared_at="2026-10-07T00:00:00.000000Z",
                established_at="2026-10-07T00:00:00.000000Z",
            )
        )
    broker = FakeBroker(
        ContainerRuntimeState.STOPPED,
        fail=failure if failure != "invalid_artifact" else None,
    )
    restore = service(factory, broker, invalid=failure == "invalid_artifact")
    if failure:
        with pytest.raises(SnapshotRestoreError):
            restore.restore()
    else:
        restore.restore()
    with factory() as session:
        row = session.get(ManagedBridgeTrustRow, str(INSTANCE_ID))
        assert row is not None
        if failure == "invalid_artifact":
            assert row.state == "active" and secrets.read(
                INSTANCE_ID, UUID(row.secret_reference), "request_authentication"
            )
        else:
            assert (
                row.state == "invalidated"
                and row.secret_reference is None
                and row.generation == 2
            )
            assert (
                row.container_id == OLD_ID
            )  # retain audit pin, never adopt restored profile
            with pytest.raises(SecretStoreError):
                secrets.read(INSTANCE_ID, reference, "request_authentication")
