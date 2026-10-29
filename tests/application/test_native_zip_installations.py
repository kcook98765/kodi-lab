from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.addon_zip_installations import (
    AddonZipInstallationCoordinator,
    AddonZipInstallationUnsupportedError,
)
from kodi_lab.application.mcp_administration import (
    ADDON_DEVELOPMENT_PRESET_ID,
    McpAdministrationService,
    McpAuthenticationService,
    McpPrincipal,
)
from kodi_lab.application.native_zip_installations import (
    DELIVERY_LIFETIME,
    NativeZipError,
    NativeZipInstallationService,
    NativeZipRequest,
)
from kodi_lab.broker.addon_zips import (
    AddonZipInstallationStateError,
    SqliteAddonZipInstallationResolver,
)
from kodi_lab.broker.operations import (
    AddonZipDestinationResult,
    NativeZipObservationParams,
)
from kodi_lab.domain import AddonZipArtifact
from kodi_lab.kodi.addons import KodiAddonInventoryRecord, KodiAddonInventoryResult
from kodi_lab.kodi.readiness import KodiApplicationIdentity
from kodi_lab.mcp.authorization import (
    McpAuthorizationError,
    McpAuthorizationService,
    TargetTechnicalAuthority,
)
from kodi_lab.mcp.registry import TargetClass, build_mcp_tool_registry
from kodi_lab.storage.models import (
    AddonZipArtifactRow,
    ManagedInstanceRow,
    McpClientTargetGrantRow,
    NativeZipInstallationRow,
    TargetRow,
)
from kodi_lab.storage.repositories import AddonZipArtifactRepository
from tests.application.test_addon_zip_installations import (
    INSTANCE_ID,
    REVISION_ID,
    FakeBroker,
    factory,  # noqa: F401
)
from tests.development.test_addon_zip import publish
from tests.development.test_repository_zip import REPO_ID, repository_zip


class Clock:
    def __init__(self) -> None:
        self.now = datetime.now(UTC)
        self.ticks = 0.0

    def monotonic(self) -> float:
        return self.ticks

    def __call__(self) -> datetime:
        return self.now


class Authority:
    def resolve(self, _target: UUID) -> TargetTechnicalAuthority:
        return TargetTechnicalAuthority(
            TargetClass.INTERNAL_LAB,
            frozenset({"addon_inventory"}),
            frozenset({"running", "ready", "reconciled"}),
        )


class Inventory:
    items: tuple[KodiAddonInventoryRecord, ...] = ()

    def get_inventory(self) -> KodiAddonInventoryResult:
        return KodiAddonInventoryResult(
            KodiApplicationIdentity("Kodi", 21, 3, "stable"), self.items
        )


class NativeBroker(FakeBroker):
    def __init__(self, sessions: sessionmaker[Session], root: Path) -> None:
        super().__init__()
        self.resolver = SqliteAddonZipInstallationResolver(sessions, data_root=root)
        self.switched = False

    def observe_native_zip_installation(
        self, operation_id: UUID
    ) -> AddonZipDestinationResult:
        self._fail("native_readback")
        spec, container = self.resolver.resolve_native_observation(str(operation_id))
        return AddonZipDestinationResult(
            managed_instance_id=spec.managed_instance_id,
            artifact_id=spec.artifact_id,
            installation_operation_id=str(operation_id),
            runtime_revision_id=spec.runtime_revision_id,
            container_id="b" * 64 if self.switched else container,
            destination_state=self.destination,
            matched=self.destination == "exact_payload_present",
        )


@dataclass
class Fixture:
    sessions: sessionmaker[Session]
    root: Path
    service: NativeZipInstallationService
    broker: NativeBroker
    inventory: Inventory
    admin: McpAdministrationService
    principal: McpPrincipal
    token: str
    request: NativeZipRequest
    clock: Clock
    authorization: McpAuthorizationService


@pytest.fixture
def native(factory: sessionmaker[Session], tmp_path: Path) -> Fixture:  # noqa: F811
    root = tmp_path / "native-data"
    root.mkdir(mode=0o700)
    identity, validated = publish(root, repository_zip())
    clock = Clock()
    artifact = AddonZipArtifact(
        id=identity,
        logical_label="Native fixture",
        sha256_digest=validated.sha256_digest,
        compressed_size=validated.compressed_size,
        total_uncompressed_bytes=validated.total_uncompressed_bytes,
        member_count=validated.member_count,
        addon_id=validated.addon_id,
        addon_name=validated.addon_name,
        addon_version=validated.addon_version,
        extension_classification=validated.extension_classification,
        dependencies=validated.dependencies,
        payload_manifest_digest=validated.payload_manifest_digest,
        imported_at=clock(),
    )
    with factory.begin() as session:
        AddonZipArtifactRepository(session).create(artifact)
    admin = McpAdministrationService(factory, clock=clock)
    preset = admin.create_custom_preset(
        display_name="Native ZIP explicit opt-in",
        template_preset_id=ADDON_DEVELOPMENT_PRESET_ID,
        permissions=frozenset({"addon.install"}),
    )
    client = admin.create_client(display_name="Native ZIP client", preset_id=preset.id)
    admin.assign_target_preset(INSTANCE_ID, preset.id)
    admin.grant_target(client.id, INSTANCE_ID)
    issued = admin.issue_credential(client.id, lifetime=timedelta(days=1))
    principal = McpAuthenticationService(factory, clock=clock).authenticate(
        issued.token
    )
    assert principal is not None
    authorization = McpAuthorizationService(
        factory, build_mcp_tool_registry(), targets=Authority(), clock=clock
    )
    broker = NativeBroker(factory, root)
    inventory = Inventory()
    service = NativeZipInstallationService(
        factory,
        broker,
        authorization,
        data_root=root,
        base_url="http://lab.example.org:8100",
        inventory_factory=lambda _o, _p: inventory,
        clock=clock,
        monotonic=clock.monotonic,
        resolve=lambda _host: ("8.8.8.8",),
    )
    return Fixture(
        factory,
        root,
        service,
        broker,
        inventory,
        admin,
        principal,
        issued.token,
        NativeZipRequest(
            request_id=uuid4(), target_id=INSTANCE_ID, artifact_id=identity
        ),
        clock,
        authorization,
    )


def recognized() -> KodiAddonInventoryRecord:
    return KodiAddonInventoryRecord(
        REPO_ID, "Fixture Repository", "1.0.0", True, "xbmc.addon.repository"
    )


def test_native_install_requires_kodi_confirmation_and_exact_readback(
    native: Fixture,
) -> None:
    state = native.service.request(native.principal, native.request)
    assert state.state == "requested" and state.source_url is not None
    assert (
        native.service.verify(native.principal, state.operation_id).state
        == "unverified"
    )
    data, name = native.service.package(
        state.operation_id, "10.250.0.2", name=f"{REPO_ID}-1.0.0.zip"
    )
    assert data == repository_zip() and name.endswith(".zip")
    assert (
        native.service.verify(native.principal, state.operation_id).state
        == "unverified"
    )
    native.inventory.items = (recognized(),)
    # A recognized ID/version without exact payload must remain unverified.
    assert (
        native.service.verify(native.principal, state.operation_id).state
        == "unverified"
    )
    native.broker.destination = "exact_payload_present"
    result = native.service.verify(native.principal, state.operation_id)
    assert result.state == "completed" and result.source_url is None
    assert "apply" not in native.broker.calls and "start" not in native.broker.calls
    with pytest.raises(NativeZipError):
        native.service.package(state.operation_id, "10.250.0.2")


def test_expiry_revokes_delivery_and_leaves_no_staging_copy(native: Fixture) -> None:
    before = set(native.root.rglob("*"))
    state = native.service.request(native.principal, native.request)
    native.clock.now += DELIVERY_LIFETIME + timedelta(seconds=1)
    result = native.service.get(native.principal, state.operation_id)
    assert result.state == "unverified" and result.source_url is None
    assert result.failure_reason == "native_installation_timed_out"
    with pytest.raises(NativeZipError):
        native.service.package(state.operation_id, "10.250.0.2")
    assert set(native.root.rglob("*")) == before


def test_preflight_failure_releases_delivery(native: Fixture) -> None:
    native.broker.fail_on = "native_readback"
    with pytest.raises(RuntimeError):
        native.service.request(native.principal, native.request)
    with native.sessions() as session:
        row = session.scalar(select(NativeZipInstallationRow))
        assert row is not None and row.state == "failed" and not row.stage_ready


def test_readonly_and_ungranted_clients_cannot_install(native: Fixture) -> None:
    readonly = native.admin.create_custom_preset(
        display_name="Read only",
        template_preset_id=ADDON_DEVELOPMENT_PRESET_ID,
        permissions=frozenset({"target.inspect"}),
    )
    native.admin.assign_client_preset(native.principal.client_id, readonly.id)
    with pytest.raises(McpAuthorizationError, match="permission_denied"):
        native.service.request(native.principal, native.request)
    assert native.broker.calls == []
    native.admin.assign_client_preset(
        native.principal.client_id, native.principal.preset_id
    )
    native.admin.remove_target_grant(native.principal.client_id, INSTANCE_ID)
    with pytest.raises(
        McpAuthorizationError, match="target_not_found_or_not_authorized"
    ):
        native.service.request(native.principal, native.request)
    assert native.broker.calls == []


def test_other_35_targets_remain_inaccessible(native: Fixture) -> None:
    ids = [UUID(int=6000 + index) for index in range(35)]
    with native.sessions.begin() as session:
        for identity in ids:
            session.add(
                TargetRow(
                    id=str(identity),
                    name=str(identity),
                    target_type="managed",
                    kodi_version="21.3",
                    availability="unavailable",
                    health="warning",
                    connection_state="disconnected",
                )
            )
        session.flush()
        for identity in ids:
            session.add(
                ManagedInstanceRow(
                    target_id=str(identity),
                    instance_type="disposable",
                    runtime_revision_id=str(REVISION_ID),
                    runtime_state="stopped",
                    start_on_host_boot=False,
                )
            )
    for identity in ids:
        native.admin.assign_target_preset(identity, native.principal.preset_id)
        with pytest.raises(McpAuthorizationError):
            native.service.request(
                native.principal,
                native.request.model_copy(update={"target_id": identity}),
            )
    with native.sessions() as session:
        assert len(session.scalars(select(TargetRow)).all()) == 36
        assert [
            row.target_id for row in session.scalars(select(McpClientTargetGrantRow))
        ] == [str(INSTANCE_ID)]
    assert native.broker.calls == []


def test_replay_and_delivery_are_bound_to_target_and_owner(native: Fixture) -> None:
    state = native.service.request(native.principal, native.request)
    assert (
        native.service.request(native.principal, native.request).operation_id
        == state.operation_id
    )
    with pytest.raises(NativeZipError):
        native.service.package(state.operation_id, "10.250.0.11")
    other = native.admin.create_client(
        display_name="Other", preset_id=native.principal.preset_id
    )
    native.admin.grant_target(other.id, INSTANCE_ID)
    other_principal = native.principal.__class__(
        client_id=other.id,
        credential_id=native.principal.credential_id,
        preset_id=other.preset.id,
        permissions=frozenset({"addon.install"}),
    )
    with pytest.raises(McpAuthorizationError):
        native.service.get(other_principal, state.operation_id)
    with pytest.raises(NativeZipError, match="request_id_conflict"):
        native.service.request(
            native.principal, native.request.model_copy(update={"artifact_id": uuid4()})
        )
    native.admin.remove_target_grant(native.principal.client_id, INSTANCE_ID)
    with pytest.raises(McpAuthorizationError):
        native.service.package(state.operation_id, "10.250.0.2")


def test_wrong_broker_identity_never_completes(native: Fixture) -> None:
    state = native.service.request(native.principal, native.request)
    native.service.package(
        state.operation_id, "10.250.0.2", name=f"{REPO_ID}-1.0.0.zip"
    )
    native.inventory.items = (recognized(),)
    native.broker.destination = "exact_payload_present"
    native.broker.switched = True
    assert (
        native.service.verify(native.principal, state.operation_id).state
        == "unverified"
    )


def test_repository_cannot_use_video_materialization(native: Fixture) -> None:
    with pytest.raises(AddonZipInstallationUnsupportedError):
        AddonZipInstallationCoordinator(native.sessions).queue(
            INSTANCE_ID, native.request.artifact_id
        )
    with pytest.raises(AddonZipInstallationStateError):
        native.broker.resolver.resolve_native_observation(str(uuid4()))
    assert set(NativeZipObservationParams.model_json_schema()["properties"]) == {
        "operation_id"
    }


def test_delivery_detects_original_zip_tampering(native: Fixture) -> None:
    state = native.service.request(native.principal, native.request)
    original = (
        native.root
        / "addon-zips/artifacts"
        / str(native.request.artifact_id)
        / "original.zip"
    )
    os.chmod(original, 0o600)
    original.write_bytes(b"changed")
    os.chmod(original, 0o400)
    with pytest.raises(NativeZipError, match="artifact_changed"):
        native.service.package(
            state.operation_id, "10.250.0.2", name=f"{REPO_ID}-1.0.0.zip"
        )


def test_native_permission_is_explicit_and_does_not_expand_mcp_catalog(
    native: Fixture,
) -> None:
    registry = build_mcp_tool_registry()
    assert len(registry.names()) == 31
    assert registry.discoverable(frozenset({"addon.install"})) == ()
    presets = native.admin.list_presets()
    assert all("addon.install" not in p.permissions for p in presets if p.builtin)


def test_target_policy_inspection_is_not_installation_authority(
    native: Fixture,
) -> None:
    readonly = native.admin.create_custom_preset(
        display_name="Target inspection only",
        template_preset_id=ADDON_DEVELOPMENT_PRESET_ID,
        permissions=frozenset({"target.inspect"}),
    )
    native.admin.assign_target_preset(INSTANCE_ID, readonly.id)
    with pytest.raises(McpAuthorizationError, match="permission_denied"):
        native.service.request(native.principal, native.request)
    assert native.broker.calls == []


def test_revocation_rejects_delivery_and_repository_cannot_use_video_resolver(
    native: Fixture,
) -> None:
    state = native.service.request(native.principal, native.request)
    with pytest.raises(AddonZipInstallationStateError):
        native.broker.resolver.resolve(
            str(INSTANCE_ID),
            str(native.request.artifact_id),
            str(state.operation_id),
            "a" * 64,
        )
    native.admin.revoke_credential(
        native.principal.client_id, native.principal.credential_id
    )
    with pytest.raises(McpAuthorizationError, match="permission_denied"):
        native.service.package(state.operation_id, "10.250.0.2")


@pytest.mark.parametrize(
    "field,value",
    [
        ("compressed_size", 16 * 1024 * 1024 + 1),
        ("member_count", 4097),
        ("total_uncompressed_bytes", 64 * 1024 * 1024 + 1),
    ],
)
def test_native_delivery_limits_do_not_expand_with_import_limits(
    native: Fixture, field: str, value: int
) -> None:
    with native.sessions.begin() as session:
        row = session.get(AddonZipArtifactRow, str(native.request.artifact_id))
        assert row is not None
        setattr(row, field, value)
    with pytest.raises(NativeZipError, match="size_exceeded"):
        native.service.request(native.principal, native.request)
    with native.sessions() as session:
        operation = session.scalar(select(NativeZipInstallationRow))
        assert (
            operation is not None
            and operation.state == "failed"
            and not operation.stage_ready
        )


def test_pending_target_cannot_accumulate_parallel_delivery_leases(
    native: Fixture,
) -> None:
    state = native.service.request(native.principal, native.request)
    with pytest.raises(NativeZipError, match="target_busy"):
        native.service.request(
            native.principal, native.request.model_copy(update={"request_id": uuid4()})
        )
    assert native.service.get(native.principal, state.operation_id).state == "requested"


def test_concurrent_request_replay_resolves_one_operation(
    native: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading
    from concurrent.futures import ThreadPoolExecutor

    barrier = threading.Barrier(2)
    observe = native.service._observe

    def synchronized(target: str, row: NativeZipInstallationRow | None = None):  # type: ignore[no-untyped-def]
        if row is None:
            barrier.wait(timeout=5)
        return observe(target, row)

    monkeypatch.setattr(native.service, "_observe", synchronized)
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(
            workers.map(
                lambda _n: native.service.request(native.principal, native.request),
                range(2),
            )
        )
    assert results[0].operation_id == results[1].operation_id
    assert native.broker.calls.count("native_readback") == 1


def test_late_verifier_cannot_downgrade_completed(
    native: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading
    from concurrent.futures import ThreadPoolExecutor

    state = native.service.request(native.principal, native.request)
    native.service.package(
        state.operation_id, "10.250.0.2", name=f"{REPO_ID}-1.0.0.zip"
    )
    native.inventory.items = (recognized(),)
    native.broker.destination = "exact_payload_present"
    entered, release = threading.Event(), threading.Event()
    destination = native.service._destination

    def delayed(row: NativeZipInstallationRow) -> AddonZipDestinationResult:
        result = destination(row)
        if threading.current_thread() is not threading.main_thread():
            entered.set()
            assert release.wait(timeout=5)
            return result.model_copy(
                update={"matched": False, "destination_state": "absent"}
            )
        return result

    monkeypatch.setattr(native.service, "_destination", delayed)
    with ThreadPoolExecutor(max_workers=1) as worker:
        late = worker.submit(
            native.service.verify, native.principal, state.operation_id
        )
        assert entered.wait(timeout=5)
        assert (
            native.service.verify(native.principal, state.operation_id).state
            == "completed"
        )
        release.set()
        assert late.result(timeout=5).state == "completed"


def test_verification_crossing_expiry_never_completes(
    native: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = native.service.request(native.principal, native.request)
    native.service.package(
        state.operation_id, "10.250.0.2", name=f"{REPO_ID}-1.0.0.zip"
    )
    native.inventory.items = (recognized(),)
    native.broker.destination = "exact_payload_present"
    destination = native.service._destination

    def expire(row: NativeZipInstallationRow) -> AddonZipDestinationResult:
        result = destination(row)
        native.clock.now += DELIVERY_LIFETIME + timedelta(seconds=1)
        native.service.sweep()
        return result

    monkeypatch.setattr(native.service, "_destination", expire)
    result = native.service.verify(native.principal, state.operation_id)
    assert result.state == "unverified" and result.source_url is None


def test_runtime_change_during_package_read_is_rejected(
    native: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    import kodi_lab.application.native_zip_installations as implementation

    state = native.service.request(native.principal, native.request)
    read = implementation.original_zip
    observe = native.broker.observe_managed_kodi_runtime
    changed = False

    def replace_after_read(root: Path, artifact: AddonZipArtifact) -> bytes:
        nonlocal changed
        data = read(root, artifact)
        changed = True
        return data

    def current(*, managed_instance_id: str, expected_container_id: str):  # type: ignore[no-untyped-def]
        value = observe(
            managed_instance_id=managed_instance_id,
            expected_container_id=expected_container_id,
        )
        return value.model_copy(update={"container_id": "b" * 64}) if changed else value

    monkeypatch.setattr(implementation, "original_zip", replace_after_read)
    monkeypatch.setattr(native.broker, "observe_managed_kodi_runtime", current)
    with pytest.raises(NativeZipError):
        native.service.package(
            state.operation_id, "10.250.0.2", name=f"{REPO_ID}-1.0.0.zip"
        )


def test_dns_timeouts_keep_outstanding_work_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import socket
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor

    import kodi_lab.application.native_zip_installations as implementation

    with ThreadPoolExecutor(max_workers=4) as workers:
        monkeypatch.setattr(implementation, "_DNS_WORKERS", workers)
        release = threading.Event()
        calls = 0

        def blocked(*args, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal calls
            calls += 1
            assert release.wait(timeout=5)
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]

        monkeypatch.setattr(socket, "getaddrinfo", blocked)
        monkeypatch.setattr(implementation, "DNS_TIMEOUT_SECONDS", 0.01)
        try:
            for _ in range(4):
                with pytest.raises(NativeZipError, match="dns_timed_out"):
                    implementation.resolve_native_repository_host(
                        "packages.example.org"
                    )
            with pytest.raises(NativeZipError, match="dns_busy"):
                implementation.resolve_native_repository_host("packages.example.org")
            assert calls == 4
        finally:
            release.set()
        monkeypatch.setattr(implementation, "DNS_TIMEOUT_SECONDS", 2)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                assert implementation.resolve_native_repository_host(
                    "packages.example.org"
                ) == ("8.8.8.8",)
                break
            except NativeZipError:
                time.sleep(0.01)
        else:
            pytest.fail("DNS worker capacity did not recover")


def test_confirmation_refreshes_enabled_state_after_payload_observation(
    native: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = native.service.request(native.principal, native.request)
    native.service.package(
        state.operation_id, "10.250.0.2", name=f"{REPO_ID}-1.0.0.zip"
    )
    native.inventory.items = (recognized(),)
    native.broker.destination = "exact_payload_present"
    destination = native.service._destination

    def disable_during_readback(
        row: NativeZipInstallationRow,
    ) -> AddonZipDestinationResult:
        result = destination(row)
        native.inventory.items = (
            KodiAddonInventoryRecord(
                REPO_ID, "Fixture", "1.0.0", False, "xbmc.addon.repository"
            ),
        )
        return result

    monkeypatch.setattr(native.service, "_destination", disable_during_readback)
    assert (
        native.service.verify(native.principal, state.operation_id).state
        == "unverified"
    )


def test_expiry_is_sampled_after_competing_sqlite_writer(
    native: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from sqlalchemy import event

    state = native.service.request(native.principal, native.request)
    native.service.package(
        state.operation_id, "10.250.0.2", name=f"{REPO_ID}-1.0.0.zip"
    )
    native.inventory.items = (recognized(),)
    native.broker.destination = "exact_payload_present"
    observed, resume, waiting = threading.Event(), threading.Event(), threading.Event()
    destination = native.service._destination

    def paused(row: NativeZipInstallationRow) -> AddonZipDestinationResult:
        result = destination(row)
        observed.set()
        assert resume.wait(timeout=5)
        return result

    def before_sql(_connection, _cursor, statement, _parameters, _context, _many):  # type: ignore[no-untyped-def]
        if threading.current_thread() is not threading.main_thread() and (
            statement == "BEGIN IMMEDIATE"
            or statement.startswith("UPDATE native_zip_installations")
        ):
            waiting.set()

    monkeypatch.setattr(native.service, "_destination", paused)
    engine = native.sessions.kw["bind"]
    with ThreadPoolExecutor(max_workers=1) as workers:
        pending = workers.submit(
            native.service.verify, native.principal, state.operation_id
        )
        assert observed.wait(timeout=5)
        event.listen(engine, "before_cursor_execute", before_sql)
        try:
            with native.sessions.begin() as writer:
                writer.connection().exec_driver_sql("BEGIN IMMEDIATE")
                resume.set()
                assert waiting.wait(timeout=5)
                native.clock.now += DELIVERY_LIFETIME + timedelta(seconds=1)
            result = pending.result(timeout=5)
            assert result.state == "unverified" and result.source_url is None
        finally:
            resume.set()
            event.remove(engine, "before_cursor_execute", before_sql)


def test_slow_payload_observation_cannot_confirm_from_stale_bytes(
    native: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = native.service.request(native.principal, native.request)
    native.service.package(
        state.operation_id, "10.250.0.2", name=f"{REPO_ID}-1.0.0.zip"
    )
    native.inventory.items = (recognized(),)
    native.broker.destination = "exact_payload_present"
    observe = native.service._destination

    def delayed(row: NativeZipInstallationRow) -> AddonZipDestinationResult:
        result = observe(row)
        native.clock.ticks += 16
        return result

    monkeypatch.setattr(native.service, "_destination", delayed)
    result = native.service.verify(native.principal, state.operation_id)
    assert (
        result.state == "unverified"
        and result.failure_reason == "native_confirmation_stale"
    )
