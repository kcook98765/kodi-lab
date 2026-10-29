"""Managed lifecycle owns establishment, invalidation and credential retirement."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.broker.client import BrokerClient
from kodi_lab.broker.docker_inventory import (
    ContainerHealthState,
    ContainerRuntimeState,
    KodiProcessState,
)
from kodi_lab.broker.managed_bridge_trust import approved_bridge_build
from kodi_lab.broker.operations import ObserveManagedKodiRuntimeResult
from kodi_lab.config import Settings
from kodi_lab.domain import ExternalProtocolKind, RuntimeExecutionProfile, TargetType
from kodi_lab.kodi.bridge import (
    BridgeIdentity,
    ExternalBridgeClient,
    ExternalBridgeError,
)
from kodi_lab.kodi.external import ValidatedExternalEndpoint
from kodi_lab.kodi.observation_bridge import ObservationBridgeClient
from kodi_lab.kodi.readiness import (
    KodiJsonRpcReadinessAdapter,
    _BoundedHttpTransport,
    _HttpTransport,
)
from kodi_lab.storage.models import ManagedBridgeTrustRow, ManagedInstanceRow, TargetRow
from kodi_lab.storage.repositories import RuntimeExecutionProfileRepository
from kodi_lab.storage.secrets import ExternalBridgeSecretStore


class ManagedBridgeTrustError(RuntimeError):
    pass


def identity_json(identity: BridgeIdentity) -> str:
    value = {**asdict(identity), "bridge_id": str(identity.bridge_id)}
    if identity.kodi_release is None:
        value.pop("kodi_release")
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
    )


def require_approved_identity(
    identity: BridgeIdentity, kodi_version: str, *, established: bool = False
) -> None:
    builds = [approved_bridge_build(kodi_version)]
    if established:
        builds.append(approved_bridge_build(kodi_version, settings_predecessor=True))
        builds.append(approved_bridge_build(kodi_version, recent=True))
        builds.append(approved_bridge_build(kodi_version, predecessor=True))
        builds.append(approved_bridge_build(kodi_version, previous=True))
        builds.append(approved_bridge_build(kodi_version, historical=True))
        builds.append(approved_bridge_build(kodi_version, retained=True))
        builds.append(approved_bridge_build(kodi_version, legacy=True))
    if (
        not any(
            all(
                getattr(identity, field) == approved[field]
                for field in (
                    "addon_id",
                    "addon_version",
                    "protocol_version",
                    "build_fingerprint",
                )
            )
            for approved in builds
        )
        or ("-" in kodi_version and identity.kodi_release is None)
        or (identity.kodi_release or identity.kodi_version) != kodi_version
        or "ui_observation" not in identity.capabilities
    ):
        raise ManagedBridgeTrustError("managed_bridge_identity_mismatch")


def invalidate_managed_bridge_trust(
    session: Session,
    target_id: UUID,
    *,
    secrets: ExternalBridgeSecretStore | None = None,
    revoked: bool = False,
) -> None:
    """Retire authority before replacement. A rollback after retirement fails closed."""
    row = session.get(ManagedBridgeTrustRow, str(target_id))
    if row is None:
        return
    reference = row.secret_reference
    row.state = "revoked" if revoked else "invalidated"
    row.secret_reference = None
    row.generation += 1
    row.invalidated_at = (
        datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    )
    session.flush()
    if reference:
        store = (
            secrets
            or session.info.get("managed_bridge_secrets")
            or ExternalBridgeSecretStore(
                Settings().data_root / "secrets/external-bridges",
                master_key=None,
                key_version=1,
            )
        )
        store.retire(UUID(reference))


class ManagedBridgeTrustService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        broker: BrokerClient,
        secrets: ExternalBridgeSecretStore,
        *,
        client: ExternalBridgeClient | None = None,
    ) -> None:
        from kodi_lab.application.ui_state import ObservationGate

        self.sessions, self.broker, self.secrets = sessions, broker, secrets
        sessions.configure(
            info={**sessions.kw.get("info", {}), "managed_bridge_secrets": secrets}
        )
        self.client = client or ObservationBridgeClient()
        self._gate = ObservationGate(timeout=10)
        self.transport: _HttpTransport = _BoundedHttpTransport()

    def close(self) -> None:
        if isinstance(self.client, ObservationBridgeClient):
            self.client.close()

    def _admit(
        self, target_id: UUID
    ) -> tuple[ObserveManagedKodiRuntimeResult, RuntimeExecutionProfile]:
        with self.sessions() as session:
            target = session.get(TargetRow, str(target_id))
            instance = session.get(ManagedInstanceRow, str(target_id))
            if (
                target is None
                or target.target_type is not TargetType.MANAGED
                or instance is None
            ):
                raise ManagedBridgeTrustError("managed_bridge_target_unavailable")
            profile = RuntimeExecutionProfileRepository(session).get(
                UUID(instance.runtime_revision_id)
            )
        inventory = self.broker.managed_container_inventory(
            managed_instance_id=str(target_id)
        )
        if (
            profile is None
            or len(inventory.containers) != 1
            or inventory.containers[0].managed_instance_id != str(target_id)
            or inventory.containers[0].health_state is not ContainerHealthState.HEALTHY
        ):
            raise ManagedBridgeTrustError("managed_bridge_target_unavailable")
        observed = self.broker.observe_managed_kodi_runtime(
            managed_instance_id=str(target_id),
            expected_container_id=inventory.containers[0].container_id,
        )
        if (
            observed.managed_instance_id != str(target_id)
            or observed.container_id != inventory.containers[0].container_id
            or observed.jsonrpc_port != profile.jsonrpc_port
            or observed.runtime_revision_id != str(profile.runtime_revision_id)
            or observed.runtime_state is not ContainerRuntimeState.RUNNING
            or observed.process_state is not KodiProcessState.RUNNING
        ):
            raise ManagedBridgeTrustError("managed_bridge_identity_mismatch")
        from kodi_lab.storage.retained_development import (
            require_retained_container,
            retained_targets,
        )

        with self.sessions() as session:
            if retained_targets(session):
                require_retained_container(
                    session,
                    self.broker.managed_container_inventory().containers,
                    str(target_id),
                    running=True,
                    observed_container_id=observed.container_id,
                )
        identity = KodiJsonRpcReadinessAdapter(
            observed, profile, transport=self.transport
        ).get_application_identity()
        if (
            identity.name != profile.expected_application_name
            or identity.kodi_release != profile.kodi_release
        ):
            raise ManagedBridgeTrustError("managed_bridge_identity_mismatch")
        return observed, profile

    def _call(
        self,
        observed: ObserveManagedKodiRuntimeResult,
        profile: RuntimeExecutionProfile,
        method: str,
        params: dict[str, object],
    ) -> object:
        return KodiJsonRpcReadinessAdapter(
            observed, profile, transport=self.transport
        )._fixed_request("managed-bridge-lifecycle", method, params)["result"]

    @staticmethod
    def _endpoint(
        observed: ObserveManagedKodiRuntimeResult,
    ) -> ValidatedExternalEndpoint:
        return ValidatedExternalEndpoint(
            ExternalProtocolKind.JSONRPC_HTTP,
            observed.ipv4_address,
            observed.jsonrpc_port,
        )

    def _invalidate_if_current(self, row: ManagedBridgeTrustRow) -> None:
        with self.sessions.begin() as session:
            current = session.get(ManagedBridgeTrustRow, row.target_id)
            if (
                current is not None
                and current.generation == row.generation
                and current.secret_reference == row.secret_reference
            ):
                invalidate_managed_bridge_trust(
                    session,
                    UUID(row.target_id),
                    secrets=self.secrets,
                )

    def _require_unchanged(
        self, row: ManagedBridgeTrustRow, observed: ObserveManagedKodiRuntimeResult
    ) -> None:
        fresh = self.broker.observe_managed_kodi_runtime(
            managed_instance_id=row.target_id,
            expected_container_id=observed.container_id,
        )
        with self.sessions() as session:
            current = session.get(ManagedBridgeTrustRow, row.target_id)
            if (
                fresh.container_id != row.container_id
                or fresh.runtime_revision_id != row.runtime_revision_id
                or current is None
                or current.state != "active"
                or current.generation != row.generation
                or current.secret_reference != row.secret_reference
                or current.identity_json != row.identity_json
            ):
                raise ManagedBridgeTrustError("managed_bridge_identity_mismatch")

    def _resident_identity(
        self,
        observed: ObserveManagedKodiRuntimeResult,
        *,
        bridge_port: int,
        target_id: UUID,
        bridge_id: UUID,
        secret: bytes,
    ) -> BridgeIdentity:
        deadline = time.monotonic() + 5
        while True:
            try:
                return self.client.identity(
                    self._endpoint(observed),
                    bridge_port=bridge_port,
                    target_id=target_id,
                    bridge_id=bridge_id,
                    secret=secret,
                )
            except ExternalBridgeError as error:
                if error.code != "bridge_unreachable" or time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)  # bounded resident-service readiness polling

    def revoke(self, target_id: UUID) -> None:
        with self._gate.enter(target_id), self.sessions.begin() as session:
            invalidate_managed_bridge_trust(
                session, target_id, secrets=self.secrets, revoked=True
            )

    def prepare(self, target_id: UUID, *, replace: bool = False) -> bool:
        """Managed lifecycle only. Observation and reconnect never call this.

        Missing/disabled Bridge is optional. Active same-container trust is
        authenticated and reused. Revocation/drift needs explicit replacement.
        """
        with self._gate.enter(target_id):
            from kodi_lab.storage.retained_development import retained_targets

            with self.sessions() as session:
                retained = bool(retained_targets(session))
            if not retained:
                return self._prepare(target_id, replace=replace)
            return self._owned_prepare(target_id, replace=replace)

    def prepare_background(self, target_id: UUID) -> bool:
        """Automatic readiness is owned in both legacy and retained mode.

        This also excludes enabling retained policy over an old live worker.
        Direct legacy synchronous preparation retains its existing contract.
        """
        with self._gate.enter(target_id):
            return self._owned_prepare(target_id, replace=False)

    def _owned_prepare(self, target_id: UUID, *, replace: bool) -> bool:
        """Background work shares durable lifecycle ownership through cleanup.

        Stop/selection cannot pass while credentials/profile changes are still
        possible. Interruption leaves typed lifecycle reconciliation evidence.
        Synchronous reviewed staging/resume already hold their caller's lease.
        """
        from kodi_lab.application.lifecycle_operations import (
            ManagedInstanceLifecycleOperationCoordinator,
        )
        from kodi_lab.domain import (
            ManagedInstanceLifecycleOperationState,
            ManagedInstanceSemanticState,
        )
        from kodi_lab.storage.retained_development import require_retained_container

        def admission(session: Session) -> None:
            require_retained_container(
                session,
                self.broker.managed_container_inventory().containers,
                str(target_id),
                running=True,
            )

        operations = ManagedInstanceLifecycleOperationCoordinator(self.sessions)
        handle = operations.begin_bridge_preparation(target_id, precondition=admission)
        mutated = False

        def mutation_started() -> None:
            nonlocal mutated
            mutated = True

        try:
            result = self._prepare(
                target_id, replace=replace, mutation_started=mutation_started
            )
        except BaseException:
            operations.terminalize(
                handle,
                state=(
                    ManagedInstanceLifecycleOperationState.RECONCILIATION_REQUIRED
                    if mutated
                    else ManagedInstanceLifecycleOperationState.FAILED
                ),
                resulting_state=ManagedInstanceSemanticState.AMBIGUOUS,
                failure_reason="Resident Bridge preparation did not complete.",
            )
            raise
        operations.terminalize(
            handle,
            state=ManagedInstanceLifecycleOperationState.SUCCEEDED,
            resulting_state=ManagedInstanceSemanticState.RUNNING_USABLE,
        )
        return result

    def resume_reviewed(self, target_id: UUID, *, expected_generation: int) -> None:
        """Explicit lifecycle continuation; prepared intent is not staging proof."""
        with self._gate.enter(target_id):
            if not self._prepare(
                target_id,
                replace=False,
                expected_prepared_generation=expected_generation,
            ):
                raise ManagedBridgeTrustError("managed_bridge_installation_incomplete")

    def stage_reviewed(self, target_id: UUID) -> None:
        """Explicit lifecycle admission only; restart must confirm resident identity."""
        with self._gate.enter(target_id):
            if not self._prepare(target_id, replace=True, install_reviewed=True):
                raise ManagedBridgeTrustError("managed_bridge_establishment_failed")

    def status(self, target_id: UUID) -> dict[str, str | int | bool | None]:
        with self.sessions() as session:
            instance = session.get(ManagedInstanceRow, str(target_id))
            profile = (
                RuntimeExecutionProfileRepository(session).get(
                    UUID(instance.runtime_revision_id)
                )
                if instance
                else None
            )
            if profile is None or profile.kodi_release.canonical not in {
                "20.5",
                "21.3",
                "22.0-rc.1",
            }:
                raise ManagedBridgeTrustError("managed_bridge_candidate_unavailable")
            row = session.get(ManagedBridgeTrustRow, str(target_id))
            return {
                "state": row.state if row else "uninstalled",
                "generation": row.generation if row else 0,
                "resume_available": bool(
                    row and row.state == "prepared" and row.reviewed_installation
                ),
                "addon_version": approved_bridge_build(profile.kodi_release.canonical)[
                    "addon_version"
                ],
            }

    def _prepare(
        self,
        target_id: UUID,
        *,
        replace: bool,
        install_reviewed: bool = False,
        expected_prepared_generation: int | None = None,
        mutation_started: Callable[[], None] | None = None,
    ) -> bool:
        observed, profile = self._admit(target_id)
        if install_reviewed and profile.kodi_release.canonical not in {
            "20.5",
            "21.3",
            "22.0-rc.1",
        }:
            raise ManagedBridgeTrustError("managed_bridge_candidate_unavailable")
        with self.sessions() as session:
            row = session.get(ManagedBridgeTrustRow, str(target_id))
            if row is not None:
                session.expunge(row)
        if expected_prepared_generation is not None and (
            row is None
            or row.state != "prepared"
            or not row.reviewed_installation
            or row.generation != expected_prepared_generation
            or row.container_id != observed.container_id
            or row.runtime_revision_id != observed.runtime_revision_id
            or not row.secret_reference
        ):
            raise ManagedBridgeTrustError("managed_bridge_resume_unavailable")
        if row is not None and row.state == "active" and not replace:
            try:
                if (
                    row.container_id != observed.container_id
                    or row.runtime_revision_id != observed.runtime_revision_id
                    or not row.secret_reference
                    or not row.bridge_id
                ):
                    raise ManagedBridgeTrustError("managed_bridge_identity_mismatch")
                identity = self._resident_identity(
                    observed,
                    bridge_port=row.bridge_port,
                    target_id=target_id,
                    bridge_id=UUID(row.bridge_id),
                    secret=self.secrets.read(
                        target_id, UUID(row.secret_reference), "request_authentication"
                    ),
                )
                require_approved_identity(
                    identity, profile.kodi_release.canonical, established=True
                )
                if identity_json(identity) != row.identity_json:
                    raise ManagedBridgeTrustError("managed_bridge_identity_mismatch")
                self._require_unchanged(row, observed)
                return True
            except ExternalBridgeError as error:
                if error.code == "bridge_unreachable":
                    raise ManagedBridgeTrustError(
                        "managed_bridge_unavailable"
                    ) from error
                if mutation_started is not None:
                    mutation_started()
                self._invalidate_if_current(row)
                raise ManagedBridgeTrustError(
                    "managed_bridge_identity_mismatch"
                ) from error
            except Exception as error:
                if mutation_started is not None:
                    mutation_started()
                self._invalidate_if_current(row)
                raise ManagedBridgeTrustError(
                    "managed_bridge_identity_mismatch"
                ) from error
        if (
            row is not None
            and row.state in {"invalidated", "revoked"}
            and row.container_id == observed.container_id
            and not replace
        ):
            raise ManagedBridgeTrustError("managed_bridge_reestablishment_required")
        try:
            details = self._call(
                observed,
                profile,
                "Addons.GetAddonDetails",
                {
                    "addonid": "service.kodi_lab_bridge",
                    "properties": ["enabled", "version"],
                },
            )
        except Exception:
            if not install_reviewed:
                return False
            details = None
        if not isinstance(details, dict) or not isinstance(details.get("addon"), dict):
            if not install_reviewed:
                return False
            addon = None
        else:
            addon = details["addon"]
        if addon is not None and (
            (
                addon.get("enabled") is not True
                and not replace
                and not (
                    row
                    and row.state == "prepared"
                    and (
                        profile.kodi_release.canonical == "22.0-rc.1"
                        or row.reviewed_installation
                    )
                )
            )
            or (
                not install_reviewed
                and addon.get("version")
                != approved_bridge_build(profile.kodi_release.canonical)[
                    "addon_version"
                ]
            )
            or addon.get("addonid") != "service.kodi_lab_bridge"
        ):
            return False
        if mutation_started is not None:
            mutation_started()
        reference = self.secrets.write(
            target_id, "request_authentication", os.urandom(32)
        )
        now = (
            datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
        )
        generation = 1 if row is None else row.generation + 1
        try:
            with self.sessions.begin() as session:
                current = session.get(ManagedBridgeTrustRow, str(target_id))
                if (current is None) != (row is None) or (
                    current is not None
                    and row is not None
                    and current.generation != row.generation
                ):
                    raise ManagedBridgeTrustError("managed_bridge_identity_mismatch")
                if current is not None:
                    invalidate_managed_bridge_trust(
                        session, target_id, secrets=self.secrets
                    )
                    session.delete(current)
                    session.flush()
                session.add(
                    ManagedBridgeTrustRow(
                        target_id=str(target_id),
                        runtime_revision_id=observed.runtime_revision_id,
                        container_id=observed.container_id,
                        bridge_id=None,
                        bridge_port=8765,
                        secret_reference=str(reference),
                        identity_json=None,
                        state="prepared",
                        generation=generation,
                        prepared_at=now,
                        reviewed_installation=install_reviewed
                        or bool(
                            row
                            and row.state == "prepared"
                            and row.reviewed_installation
                        ),
                    )
                )
            if addon is not None:
                self._call(
                    observed,
                    profile,
                    "Addons.SetAddonEnabled",
                    {"addonid": "service.kodi_lab_bridge", "enabled": False},
                )
            result = self.broker.establish_managed_bridge_trust(
                managed_instance_id=str(target_id),
                expected_container_id=observed.container_id,
                generation=generation,
                **({"install_reviewed": True} if install_reviewed else {}),
            )
            if install_reviewed:
                # Prepared is not trusted. Existing lifecycle restart + prepare
                # must confirm exact resident identity before the anchor is active.
                return True
            # Enable only after the broker proves provisioned authenticated state.
            # A failed partial write must not start ordinary unpaired onboarding.
            self._call(
                observed,
                profile,
                "Addons.SetAddonEnabled",
                {"addonid": "service.kodi_lab_bridge", "enabled": True},
            )
            secret = self.secrets.read(target_id, reference, "request_authentication")
            identity = self._resident_identity(
                observed,
                bridge_port=8765,
                target_id=target_id,
                bridge_id=UUID(result.bridge_id),
                secret=secret,
            )
            require_approved_identity(identity, profile.kodi_release.canonical)
            current_observed = self.broker.observe_managed_kodi_runtime(
                managed_instance_id=str(target_id),
                expected_container_id=observed.container_id,
            )
            if (
                current_observed.container_id != observed.container_id
                or current_observed.runtime_revision_id != observed.runtime_revision_id
            ):
                raise ManagedBridgeTrustError("managed_bridge_identity_mismatch")
            with self.sessions.begin() as session:
                current = session.get(ManagedBridgeTrustRow, str(target_id))
                if (
                    current is None
                    or current.state != "prepared"
                    or current.generation != generation
                    or current.secret_reference != str(reference)
                ):
                    raise ManagedBridgeTrustError("managed_bridge_identity_mismatch")
                current.bridge_id = str(identity.bridge_id)
                current.identity_json = identity_json(identity)
                current.established_at = (
                    datetime.now(UTC)
                    .isoformat(timespec="microseconds")
                    .replace("+00:00", "Z")
                )
                current.state = "active"
            return True
        except Exception as error:
            with self.sessions.begin() as session:
                current = session.get(ManagedBridgeTrustRow, str(target_id))
                if current is not None and current.secret_reference == str(reference):
                    invalidate_managed_bridge_trust(
                        session, target_id, secrets=self.secrets
                    )
            self.secrets.retire(reference)
            raise ManagedBridgeTrustError(
                "managed_bridge_establishment_failed"
            ) from error
