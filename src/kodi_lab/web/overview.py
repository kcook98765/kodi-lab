"""Small recorded-state dashboard. Never observes a broker or a remote Kodi."""

from collections import Counter
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.domain import TargetType
from kodi_lab.storage.models import (
    ExternalBridgePairingRow,
    ExternalKodiTargetRow,
    McpClientRow,
    TargetRow,
)
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
    with sessions() as session:
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
        production=ConnectionSummary(total=len(production), **production_counts),
        development=ConnectionSummary(
            total=development_counts.total(), **development_counts
        ),
        clients=clients,
        enabled_clients=enabled,
    )
