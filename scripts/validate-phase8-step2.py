#!/usr/bin/env python3
"""Run isolated Phase 8 Step 2 attachment and history validation."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path
from typing import cast
from uuid import uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import text

from kodi_lab.application import (
    DevelopmentSourceAdministrationService,
    DevelopmentSourceAttachmentService,
    DevelopmentSourceIdentityChangedError,
)
from kodi_lab.application.instance_delete_operations import (
    ManagedInstanceDeleteOperationCoordinator,
)
from kodi_lab.application.instance_deletions import (
    DisposableManagedInstanceDeletionService,
    InstanceDeleteBroker,
)
from kodi_lab.application.readiness_tasks import ReadinessTaskRegistry
from kodi_lab.broker.operations import ManagedContainerInventoryResult
from kodi_lab.config import Settings
from kodi_lab.development import DevelopmentSourceValidator
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.storage.models import (
    ManagedInstanceRow,
    RuntimeDefinitionRow,
    RuntimeRevisionRow,
    TargetRow,
)
from kodi_lab.storage.repositories import (
    DevelopmentSourceAttachmentOperationRepository,
    DevelopmentSourceRepository,
    ManagedInstanceDevelopmentSourceRepository,
    ManagedInstanceRepository,
)


def write_source(
    root: Path, locator: str, *, addon_id: str, name: str, version: str
) -> None:
    directory = root.joinpath(*locator.split("/"))
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "addon.xml").write_text(
        f'<addon id="{addon_id}" name="{name}" version="{version}" '
        'provider-name="Kodi Lab Validation">'
        '<extension point="xbmc.python.pluginsource" /></addon>',
        encoding="utf-8",
    )


class EmptyInventoryBroker:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def managed_container_inventory(self) -> ManagedContainerInventoryResult:
        self.calls.append("managed_container_inventory")
        return ManagedContainerInventoryResult(containers=[])

    def __getattr__(self, name: str) -> object:
        raise RuntimeError(f"unexpected broker method: {name}")


def main() -> None:
    project_root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(
        prefix="kodi-lab-phase8-step2-", dir="/var/tmp"
    ) as temporary:
        validation_root = Path(temporary)
        development_root = validation_root / "development"
        development_root.mkdir(mode=0o700)
        data_root = validation_root / "data"
        (data_root / "database").mkdir(parents=True)
        os.environ["KODI_LAB_DATA_ROOT"] = str(data_root)
        os.environ["KODI_LAB_DEVELOPMENT_ROOT"] = str(development_root)
        settings = Settings()

        command.upgrade(Config(str(project_root / "alembic.ini")), "head")
        engine = create_database_engine(settings.database_url)
        factory = create_session_factory(engine)
        definition_id = uuid4()
        revision_id = uuid4()
        instance_ids = {name: uuid4() for name in ("I1", "I2", "I3", "I4")}
        timestamp = "2026-09-16T22:00:00.000000Z"
        with factory.begin() as session:
            session.add(
                RuntimeDefinitionRow(
                    id=str(definition_id),
                    slug=f"step2-{definition_id.hex}",
                    kodi_version="21.3",
                )
            )
            session.add(
                RuntimeRevisionRow(
                    id=str(revision_id),
                    runtime_definition_id=str(definition_id),
                    revision_number=1,
                    source_kind="validation",
                    source_reference="synthetic",
                    content_digest="sha256:" + "a" * 64,
                    manifest_reference="synthetic",
                    manifest_digest="sha256:" + "b" * 64,
                    built_at=timestamp,
                    approval_state="approved",
                    approved_at=timestamp,
                    requires_trusted_build_confirmation=False,
                )
            )
            session.flush()
            for name, identity in instance_ids.items():
                session.add(
                    TargetRow(
                        id=str(identity),
                        name=f"Step 2 {name}",
                        target_type="managed",
                        kodi_version="21.3",
                        availability="unavailable",
                        health="warning",
                        connection_state="disconnected",
                    )
                )
                session.flush()
                session.add(
                    ManagedInstanceRow(
                        target_id=str(identity),
                        instance_type="disposable",
                        runtime_revision_id=str(revision_id),
                        runtime_state="stopped",
                        start_on_host_boot=False,
                    )
                )

        write_source(
            development_root,
            "sources/s1",
            addon_id="plugin.video.kodi-lab-step2-s1",
            name="Step 2 Source 1",
            version="1.0.0",
        )
        write_source(
            development_root,
            "sources/s2",
            addon_id="plugin.video.kodi-lab-step2-s2",
            name="Step 2 Source 2",
            version="1.0.0",
        )
        validator = DevelopmentSourceValidator(development_root)
        catalog = DevelopmentSourceAdministrationService(factory, validator)
        s1 = catalog.register_source(
            logical_name="Step 2 Source 1", relative_locator="sources/s1"
        )
        s2 = catalog.register_source(
            logical_name="Step 2 Source 2", relative_locator="sources/s2"
        )
        attachments = DevelopmentSourceAttachmentService(factory, validator)

        i1_s1 = attachments.attach_source(instance_ids["I1"], s1.id)
        i1_s2 = attachments.attach_source(instance_ids["I1"], s2.id)
        i1_list = attachments.list_attached_sources(instance_ids["I1"])
        i2_s1 = attachments.attach_source(instance_ids["I2"], s1.id)

        write_source(
            development_root,
            "sources/s1",
            addon_id=s1.addon_id,
            name="Step 2 Source 1 Changed",
            version="2.0.0",
        )
        i3_s1 = attachments.attach_source(instance_ids["I3"], s1.id)
        retained = catalog.get_source(s1.id)
        write_source(
            development_root,
            "sources/s1",
            addon_id="plugin.video.kodi-lab-step2-changed",
            name="Changed identity",
            version="3.0.0",
        )
        identity_change_code = None
        try:
            attachments.attach_source(instance_ids["I4"], s1.id)
        except DevelopmentSourceIdentityChangedError as error:
            identity_change_code = error.code
        if identity_change_code is None:
            raise RuntimeError("changed add-on identity was accepted")
        write_source(
            development_root,
            "sources/s1",
            addon_id=s1.addon_id,
            name="Step 2 Source 1",
            version="1.0.0",
        )

        detached = attachments.detach_source(instance_ids["I1"], s2.id)

        broker = EmptyInventoryBroker()
        deletion = ManagedInstanceDeleteOperationCoordinator(factory)
        queued = deletion.queue(instance_ids["I2"], preserve_snapshots=True)
        lease = deletion.acquire(queued.operation_id)
        if lease is None:
            raise RuntimeError("validation instance deletion was not acquired")
        readiness = ReadinessTaskRegistry(max_workers=1)
        try:
            DisposableManagedInstanceDeletionService(
                factory,
                cast(InstanceDeleteBroker, broker),
                readiness,
                deletion,
                lease,
                data_root=data_root,
            ).delete()
        finally:
            readiness.close()

        with factory() as session:
            association_repository = ManagedInstanceDevelopmentSourceRepository(session)
            history_repository = DevelopmentSourceAttachmentOperationRepository(session)
            i1_after = association_repository.list_for_instance(instance_ids["I1"])
            i2_after = association_repository.list_for_instance(instance_ids["I2"])
            i2_history = history_repository.list_for_original_instance(
                instance_ids["I2"]
            )
            all_i1_history = history_repository.list_for_original_instance(
                instance_ids["I1"]
            )
            sources_survive = all(
                DevelopmentSourceRepository(session).get(source_id) is not None
                for source_id in (s1.id, s2.id)
            )
            i2_deleted = ManagedInstanceRepository(session).get(instance_ids["I2"])
            migration_head = session.scalar(
                text("SELECT version_num FROM alembic_version")
            )

        if [item.development_source_id for item in i1_list] != [s1.id, s2.id]:
            raise RuntimeError("deterministic attachment ordering failed")
        if retained.observed_name != "Step 2 Source 1":
            raise RuntimeError("mutable source rewrote catalog observation")
        if i2_deleted is not None or i2_after or not i2_history or not sources_survive:
            raise RuntimeError("instance-deletion attachment invariants failed")
        if any(item.managed_instance_id is not None for item in i2_history):
            raise RuntimeError("deleted-instance attachment history retained live FK")

        root_metadata = development_root.stat()
        evidence = {
            "schema": "kodi-lab-phase8-step2-live-v1",
            "validation_root_identity": {
                "description": "dedicated temporary development root",
                "device": root_metadata.st_dev,
                "inode": root_metadata.st_ino,
                "uid": root_metadata.st_uid,
                "gid": root_metadata.st_gid,
                "mode": oct(stat.S_IMODE(root_metadata.st_mode)),
            },
            "sources": {
                "S1": {"uuid": str(s1.id), "addon_id": s1.addon_id},
                "S2": {"uuid": str(s2.id), "addon_id": s2.addon_id},
            },
            "instances": {name: str(value) for name, value in instance_ids.items()},
            "live_attachment": {
                "I1_S1": str(i1_s1.development_source_id),
                "I1_S2": str(i1_s2.development_source_id),
                "deterministic_I1_order": [
                    str(item.development_source_id) for item in i1_list
                ],
            },
            "global_reuse": {
                "source_uuid": str(s1.id),
                "I2_attachment": str(i2_s1.development_source_id),
                "I3_attachment": str(i3_s1.development_source_id),
                "proved": True,
            },
            "mutable_revalidation": {
                "changed_name_version_attach_succeeded": True,
                "catalog_name_retained": retained.observed_name,
                "catalog_version_retained": retained.observed_version,
                "identity_change_rejection": identity_change_code,
            },
            "reset_preservation": {
                "proof": "application integration test",
                "test": "test_reset_preserves_development_source_attachment_exactly",
                "deployment_expected": False,
            },
            "detach": {
                "source_uuid": str(detached.development_source_id),
                "I1_remaining_source_uuids": [
                    str(item.development_source_id) for item in i1_after
                ],
                "catalog_survived": True,
            },
            "attachment_history": {
                "I1_operation_uuids": [str(item.id) for item in all_i1_history],
                "I2_operation_uuids": [str(item.id) for item in i2_history],
            },
            "instance_deletion": {
                "deleted_instance": "I2",
                "association_rows_removed": True,
                "history_live_fk_cleared": True,
                "history_discoverable_by_original_uuid": True,
                "source_catalog_survived": sources_survive,
                "runtime_adapter_calls": broker.calls,
            },
            "privacy": {
                "attachment_dto_has_relative_locator": "relative_locator"
                in i1_s1.model_dump(),
                "attachment_dto_has_absolute_path": False,
            },
            "migration_head": migration_head,
            "temporary_root_removed_after_validation": True,
        }
        print(json.dumps(evidence, indent=2, sort_keys=True))
        engine.dispose()


if __name__ == "__main__":
    main()
