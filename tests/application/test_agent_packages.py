"""Original package test intake is private and distinct from editable projects."""

import base64
import hashlib
from collections.abc import Callable
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from kodi_lab.application.agent_packages import (
    AgentPackageError,
    AgentPackageService,
    PackageImportInput,
)
from kodi_lab.application.mcp_administration import (
    READ_ONLY_INSPECTION_PRESET_ID,
    McpAuthenticationService,
)
from kodi_lab.application.native_zip_installations import NativeZipRequest
from kodi_lab.broker.operations import ObserveManagedKodiRuntimeResult
from kodi_lab.development.addon_zip import AddonZipValidationError, ValidatedAddonZip
from kodi_lab.kodi.addons import KodiAddonInventoryRecord
from kodi_lab.mcp.authorization import McpAuthorizationError
from kodi_lab.storage.models import (
    AgentPackageRow,
    McpClientTargetGrantRow,
    NativeZipInstallationRow,
)
from tests.application.test_addon_zip_installations import factory as factory
from tests.application.test_native_zip_installations import (
    Fixture,
)
from tests.application.test_native_zip_installations import (
    native as native_fixture,
)
from tests.development.test_addon_zip import ADDON_ID, make_zip
from tests.development.test_repository_zip import repository_zip

native = native_fixture


def operation(payload: bytes) -> PackageImportInput:
    return PackageImportInput(
        request_id=uuid4(),
        archive_base64=base64.b64encode(payload).decode(),
        archive_sha256=hashlib.sha256(payload).hexdigest(),
        provenance_reference="agent-reviewed checkout@0123456789abcdef",
    )


def setup(e: Fixture) -> AgentPackageService:
    e.admin.set_managed_lab_role(e.principal.client_id, True)
    e.admin.assign_client_preset(e.principal.client_id, READ_ONLY_INSPECTION_PRESET_ID)
    principal = McpAuthenticationService(e.sessions, clock=e.clock).authenticate(
        e.token
    )
    assert principal is not None
    e.principal = principal
    with e.sessions.begin() as db:
        grant = db.get(
            McpClientTargetGrantRow,
            (str(e.principal.client_id), str(e.request.target_id)),
        )
        assert grant is not None
        db.delete(grant)
    return AgentPackageService(e.sessions, e.root)


def authority(e: Fixture) -> Callable[[Session], None]:
    return lambda session: e.authorization.require_managed_lab_role(
        session, e.principal
    )


@pytest.mark.parametrize("repository", [False, True])
def test_separate_original_intake_and_native_receipt_exact_replay(
    native: Fixture, repository: bool
) -> None:
    e = native
    service = setup(e)
    data = repository_zip() if repository else make_zip()
    request = operation(data)
    accepted = service.import_package(
        e.principal.client_id, request, authorize=authority(e)
    )
    assert accepted.state == "accepted"
    assert (
        service.import_package(e.principal.client_id, request, authorize=authority(e))
        == accepted
    )
    with e.sessions() as db:
        assert db.scalar(select(func.count()).select_from(AgentPackageRow)) == 1
        assert (
            db.scalar(select(func.count()).select_from(NativeZipInstallationRow)) == 0
        )
    native_request = NativeZipRequest(
        request_id=uuid4(),
        target_id=e.request.target_id,
        artifact_id=accepted.artifact_id,
    )
    first = e.service.request(e.principal, native_request)
    assert first.source_url is not None and first.state == "requested"
    assert (
        e.service.request(e.principal, native_request).operation_id
        == first.operation_id
    )
    assert e.service.verify(e.principal, first.operation_id).state == "unverified"
    addon = accepted.addon_id
    assert addon is not None
    fetched, _ = e.service.package(
        first.operation_id, "10.250.0.2", name=f"{addon}-{accepted.addon_version}.zip"
    )
    assert fetched == data
    e.inventory.items = (
        KodiAddonInventoryRecord(
            addon,
            "Package",
            accepted.addon_version or "",
            True,
            "xbmc.addon.repository" if repository else "xbmc.python.pluginsource",
        ),
    )
    assert e.service.verify(e.principal, first.operation_id).state == "unverified"
    e.broker.destination = "exact_payload_present"
    assert e.service.verify(e.principal, first.operation_id).state == "completed"
    assert not {"start", "apply", "stop"}.intersection(e.broker.calls)


def test_cross_agent_status_and_direct_native_endpoint_refuse_private_package(
    native: Fixture,
) -> None:
    e = native
    service = setup(e)
    accepted = service.import_package(
        e.principal.client_id, operation(make_zip()), authorize=authority(e)
    )
    other = e.admin.create_client(
        display_name="Another coding agent",
        preset_id=READ_ONLY_INSPECTION_PRESET_ID,
        trusted_managed_lab=True,
    )
    principal = McpAuthenticationService(e.sessions).authenticate(
        e.admin.issue_credential(other.id).token
    )
    assert principal is not None
    with pytest.raises(AgentPackageError, match="not_found_or_not_authorized"):
        service.status(
            other.id,
            accepted.artifact_id,
            authorize=lambda db: e.authorization.require_managed_lab_role(
                db, principal
            ),
        )
    with pytest.raises(
        McpAuthorizationError, match="package_not_found_or_not_authorized"
    ):
        e.service.request(
            principal,
            NativeZipRequest(
                request_id=uuid4(),
                target_id=e.request.target_id,
                artifact_id=accepted.artifact_id,
            ),
        )
    with e.sessions() as db:
        assert (
            db.scalar(select(func.count()).select_from(NativeZipInstallationRow)) == 0
        )


@pytest.mark.parametrize(
    "defect", ["digest", "traversal", "native", "dependency", "oversized"]
)
def test_invalid_package_never_publishes_or_installs(
    native: Fixture, defect: str
) -> None:
    e = native
    service = setup(e)
    extra: tuple[tuple[str, bytes], ...] = ()
    dependency = ""
    if defect == "traversal":
        extra = (("../escape.py", b"# refused"),)
    elif defect == "native":
        extra = ((f"{ADDON_ID}/binary.so", b"\x7fELF"),)
    elif defect == "dependency":
        dependency = '<import addon="native.unreviewed" version="1.0.0" />'
    elif defect == "oversized":
        extra = ((f"{ADDON_ID}/huge.txt", b"x" * (1024 * 1024 + 1)),)
    request = operation(make_zip(extra=extra, dependency=dependency))
    if defect == "digest":
        request = request.model_copy(update={"archive_sha256": "0" * 64})
    with pytest.raises((AgentPackageError, AddonZipValidationError)):
        service.import_package(e.principal.client_id, request, authorize=authority(e))
    with e.sessions() as db:
        assert (
            db.scalar(select(func.count()).select_from(NativeZipInstallationRow)) == 0
        )
        assert not any(
            row.state == "accepted" for row in db.scalars(select(AgentPackageRow))
        )
    assert not {"start", "apply", "stop"}.intersection(e.broker.calls)


def test_interrupted_publish_is_retained_and_replay_does_not_repeat(
    native: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    e = native
    service = setup(e)
    request = operation(make_zip())

    def interrupted(*_args: object) -> None:
        raise KeyboardInterrupt("fixture process interrupted")

    monkeypatch.setattr(
        "kodi_lab.application.agent_packages.validate_and_publish_addon_zip",
        interrupted,
    )
    with pytest.raises(KeyboardInterrupt):
        service.import_package(e.principal.client_id, request, authorize=authority(e))
    repeated = service.import_package(
        e.principal.client_id, request, authorize=authority(e)
    )
    assert (
        repeated.state == "reserved"
        and repeated.next_action == "inspect_original_request"
    )
    assert (
        service.status(
            e.principal.client_id, repeated.artifact_id, authorize=authority(e)
        )
        == repeated
    )


@pytest.mark.parametrize("revoked", ["role", "credential"])
def test_publication_rechecks_current_authority(
    native: Fixture, monkeypatch: pytest.MonkeyPatch, revoked: str
) -> None:
    e = native
    service = setup(e)
    from kodi_lab.development.addon_zip import validate_and_publish_addon_zip

    original = validate_and_publish_addon_zip

    def revoke(root: Path, identity: UUID) -> ValidatedAddonZip:
        result = original(root, identity)
        if revoked == "role":
            e.admin.set_managed_lab_role(e.principal.client_id, False)
        else:
            e.admin.revoke_credential(e.principal.client_id, e.principal.credential_id)
        return result

    monkeypatch.setattr(
        "kodi_lab.application.agent_packages.validate_and_publish_addon_zip", revoke
    )
    with pytest.raises(McpAuthorizationError):
        service.import_package(
            e.principal.client_id, operation(make_zip()), authorize=authority(e)
        )
    with e.sessions() as db:
        assert {row.state for row in db.scalars(select(AgentPackageRow))} == {"failed"}


def test_package_quota_and_conflicting_replay_are_bounded(native: Fixture) -> None:
    e = native
    service = setup(e)
    request = operation(make_zip())
    service.import_package(e.principal.client_id, request, authorize=authority(e))
    with pytest.raises(AgentPackageError, match="request_id_conflict"):
        service.import_package(
            e.principal.client_id,
            request.model_copy(update={"provenance_reference": "changed"}),
            authorize=authority(e),
        )
    for _ in range(3):
        service.import_package(
            e.principal.client_id, operation(make_zip()), authorize=authority(e)
        )
    with pytest.raises(AgentPackageError, match="quota_exceeded"):
        service.import_package(
            e.principal.client_id, operation(make_zip()), authorize=authority(e)
        )


@pytest.mark.parametrize("external", ["external_development", "external_production"])
def test_role_native_delivery_rejects_external_even_with_stale_managed_metadata(
    native: Fixture, external: str
) -> None:
    from kodi_lab.domain import TargetType
    from kodi_lab.storage.models import TargetRow

    e = native
    service = setup(e)
    accepted = service.import_package(
        e.principal.client_id, operation(make_zip()), authorize=authority(e)
    )
    with e.sessions.begin() as db:
        row = db.get(TargetRow, str(e.request.target_id))
        assert row is not None
        row.target_type = TargetType(external)
    before = list(e.broker.calls)
    with pytest.raises(McpAuthorizationError, match="not_found_or_not_authorized"):
        e.service.request(
            e.principal,
            NativeZipRequest(
                request_id=uuid4(),
                target_id=e.request.target_id,
                artifact_id=accepted.artifact_id,
            ),
        )
    assert e.broker.calls == before


def test_native_consumer_fences_reset_until_exact_stopped_proof(
    native: Fixture,
) -> None:
    from datetime import timedelta
    from typing import cast

    from kodi_lab.application.managed_resources import (
        ManagedResourceProtected,
        ManagedResourceService,
    )
    from kodi_lab.application.runtime_lifecycle import ManagedRuntimeLifecycleService
    from kodi_lab.broker.docker_inventory import ContainerRuntimeState
    from kodi_lab.domain import InstanceRuntimeState
    from kodi_lab.storage.models import ManagedInstanceRow

    e = native
    setup(e)
    delivery = e.service.request(e.principal, e.request)
    # A real valid GET supplies evidence the consumer may have started installing.
    e.service.package(
        delivery.operation_id, "10.250.0.2", name="repository.fixture-1.0.0.zip"
    )
    e.clock.now += timedelta(minutes=16)
    e.service.sweep()
    resources = ManagedResourceService(
        e.sessions,
        e.broker,
        cast(ManagedRuntimeLifecycleService, object()),
        clock=e.clock,
    )
    # Even a stale DB row saying stopped cannot waive native uncertainty.
    with e.sessions.begin() as db:
        instance = db.get(ManagedInstanceRow, str(e.request.target_id))
        assert instance is not None
        instance.runtime_state = InstanceRuntimeState.STOPPED
    with (
        e.sessions() as db,
        pytest.raises(ManagedResourceProtected, match="native_installation_pending"),
    ):
        resources._guard(db, e.request.target_id, reason="reset", actor=None)
    e.broker.state = ContainerRuntimeState.STOPPED
    with e.sessions() as db:
        resources._guard(db, e.request.target_id, reason="reset", actor=None)


@pytest.mark.parametrize("timing", ["before_request", "before_reservation"])
def test_native_delivery_rechecks_pending_reset_before_receipt(
    native: Fixture, monkeypatch: pytest.MonkeyPatch, timing: str
) -> None:
    from kodi_lab.storage.models import ManagedCleanResetRow, ManagedInstanceRow
    from tests.application.test_addon_zip_installations import CONTAINER_ID

    e = native
    setup(e)

    def reserve_reset() -> None:
        now = e.clock().isoformat(timespec="microseconds").replace("+00:00", "Z")
        with e.sessions.begin() as db:
            instance = db.get(ManagedInstanceRow, str(e.request.target_id))
            assert instance is not None
            db.add(
                ManagedCleanResetRow(
                    id=str(uuid4()),
                    client_id=str(e.principal.client_id),
                    request_id=str(uuid4()),
                    target_id=str(e.request.target_id),
                    generation="omega",
                    restart=False,
                    runtime_revision_id=instance.runtime_revision_id,
                    image_id="sha256:" + "5" * 64,
                    old_container_id=CONTAINER_ID,
                    expected_state_revision=instance.runtime_state_revision,
                    phase="uncertain",
                    code="fixture_uncertain_stop",
                    step="stop",
                    stop_id=str(uuid4()),
                    remove_id=str(uuid4()),
                    provision_id=str(uuid4()),
                    start_id=str(uuid4()),
                    created_at=now,
                    updated_at=now,
                )
            )

    if timing == "before_request":
        reserve_reset()
    else:
        observe = e.service._observe

        def interrupted(
            target: str, row: NativeZipInstallationRow | None = None
        ) -> ObserveManagedKodiRuntimeResult:
            result = observe(target, row)
            reserve_reset()
            return result

        monkeypatch.setattr(e.service, "_observe", interrupted)
    with pytest.raises(McpAuthorizationError, match="active_operation_requires_review"):
        e.service.request(e.principal, e.request)
    with e.sessions() as db:
        assert (
            db.scalar(select(func.count()).select_from(NativeZipInstallationRow)) == 0
        )
