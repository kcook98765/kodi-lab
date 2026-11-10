"""Small recorded-state dashboard. Never observes a broker or a remote Kodi."""

from collections import Counter
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.domain import TargetType
from kodi_lab.storage.managed_version_slots import managed_version_slots
from kodi_lab.storage.models import (
    ExternalBridgePairingRow,
    ExternalKodiTargetRow,
    ManagedCleanResetRow,
    ManagedInstanceRow,
    ManagedVersionOperationRow,
    McpClientRow,
    RuntimeRecipeInstallationRow,
    TargetRow,
)
from kodi_lab.storage.repositories import ManagedRuntimeProjectionRepository
from kodi_lab.web.kodi_views import KodiViews


@dataclass(frozen=True)
class ConnectionSummary:
    total: int
    connected: int = 0
    disconnected: int = 0
    attention: int = 0
    unknown: int = 0


@dataclass(frozen=True)
class OverviewSummary:
    managed_setup_started: bool
    prepared: int
    supported: int
    running: tuple[str, ...]
    managed_attention: int
    production: ConnectionSummary
    development: ConnectionSummary
    clients: int
    enabled_clients: int


def overview_summary(
    sessions: sessionmaker[Session], production_views: KodiViews
) -> OverviewSummary:
    # Reuse the household page's recorded trust evaluator, including incomplete
    # setup and invalidated Bridge relationships. It performs no network I/O.
    production = production_views.list()
    production_counts: Counter[str] = Counter(
        "attention" if kodi.needs_review else kodi.status for kodi in production
    )
    slots = managed_version_slots()
    ids = [str(slot.canonical_target_id) for slot in slots]
    with sessions() as session:
        prepared = 0
        managed_setup_started = False
        attention: set[str] = set()
        for slot in slots:
            installation = session.scalar(
                select(RuntimeRecipeInstallationRow)
                .where(RuntimeRecipeInstallationRow.generation == slot.generation)
                .order_by(RuntimeRecipeInstallationRow.created_at.desc())
                .limit(1)
            )
            if installation is not None:
                managed_setup_started = True
                prepared += installation.state == "ready"
                if installation.state == "failed":
                    attention.add(str(slot.canonical_target_id))
        managed = {
            target_id: state
            for target_id, state in session.execute(
                select(ManagedInstanceRow.target_id, ManagedInstanceRow.runtime_state)
                .join(TargetRow, TargetRow.id == ManagedInstanceRow.target_id)
                .where(
                    TargetRow.target_type == TargetType.MANAGED, TargetRow.id.in_(ids)
                )
            )
        }
        # Stopped profiles normally have warning health; that alone is not an
        # error. Running profiles with unsuccessful readiness do need review.
        attention.update(
            session.scalars(
                select(TargetRow.id).where(
                    TargetRow.id.in_(managed),
                    (TargetRow.health == "error")
                    | (
                        (TargetRow.health == "warning")
                        & TargetRow.id.in_(
                            [
                                key
                                for key, state in managed.items()
                                if state == "running"
                            ]
                        )
                    ),
                )
            )
        )
        projections = ManagedRuntimeProjectionRepository(session)
        attention.update(
            target_id
            for target_id in managed
            if projections.has_blocking_runtime_reconciliation(UUID(target_id))
        )
        for model in (ManagedVersionOperationRow, ManagedCleanResetRow):
            attention.update(
                session.scalars(
                    select(model.target_id).where(
                        model.target_id.in_(ids), model.phase == "uncertain"
                    )
                )
            )
        development_counts: Counter[str] = Counter()
        for target, bridge in session.execute(
            select(ExternalKodiTargetRow, ExternalBridgePairingRow)
            .join(TargetRow, TargetRow.id == ExternalKodiTargetRow.target_id)
            .outerjoin(
                ExternalBridgePairingRow,
                ExternalBridgePairingRow.target_id == ExternalKodiTargetRow.target_id,
            )
            .where(
                TargetRow.target_type == TargetType.EXTERNAL_DEVELOPMENT,
                ExternalKodiTargetRow.administrative_state != "deactivated",
            )
        ):
            if (
                target.verification_state != "verified"
                or target.observation_state
                in {"authentication_failed", "capability_missing"}
                or (
                    bridge is not None
                    and (
                        bridge.pairing_state in {"needs_reverification", "revoked"}
                        or bridge.reachability_state
                        in {
                            "identity_mismatch",
                            "authentication_failed",
                            "protocol_unsupported",
                        }
                    )
                )
            ):
                state = "attention"
            elif (
                target.administrative_state == "disabled"
                or target.observation_state == "unreachable"
                or (bridge is not None and bridge.reachability_state == "unreachable")
            ):
                state = "disconnected"
            elif target.observation_state == "reachable":
                state = "connected"
            else:
                state = "unknown"
            development_counts[state] += 1
        clients = session.scalar(select(func.count()).select_from(McpClientRow)) or 0
        enabled = (
            session.scalar(
                select(func.count())
                .select_from(McpClientRow)
                .where(McpClientRow.enabled)
            )
            or 0
        )
    return OverviewSummary(
        managed_setup_started=managed_setup_started or bool(managed),
        prepared=prepared,
        supported=len(slots),
        running=tuple(
            slot.generation.title()
            for slot in slots
            if managed.get(str(slot.canonical_target_id)) == "running"
        ),
        managed_attention=len(attention),
        production=ConnectionSummary(total=len(production), **production_counts),
        development=ConnectionSummary(
            total=development_counts.total(), **development_counts
        ),
        clients=clients,
        enabled_clients=enabled,
    )
