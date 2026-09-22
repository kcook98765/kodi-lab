"""Shared fail-closed state and resource controls for production inspection."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Callable, Hashable
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.orm import Session

from kodi_lab.domain import (
    EXTERNAL_CAPABILITY_VALUES,
    ExternalAdministrativeState,
    ExternalVerificationState,
    ProductionDesignationState,
    TargetType,
)
from kodi_lab.storage.models import (
    ExternalKodiTargetObservationRow,
    ExternalKodiTargetRow,
    ExternalProductionPolicyRow,
    TargetRow,
)

PRODUCTION_INSPECTION_PRESET_ID = UUID("11000000-0000-4000-8000-000000000005")
PRODUCTION_INSPECTION_PRESET_NAME = "Production Inspection"


class ProductionInspectionError(PermissionError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class ProductionInspectionState:
    target_id: UUID
    logical_name: str
    identity_generation: int
    kodi_name: str
    kodi_version: str
    protocol_version: str
    capabilities: frozenset[str]


def production_observation_digest(
    row: ExternalKodiTargetRow, observation: ExternalKodiTargetObservationRow
) -> str:
    encoded = json.dumps(
        {
            "addresses": json.loads(observation.addresses_json),
            "capabilities": json.loads(observation.verified_capabilities_json),
            "completed_at": observation.completed_at,
            "host": row.host,
            "kodi_name": observation.kodi_name,
            "kodi_version": observation.kodi_version,
            "observation_id": observation.id,
            "port": row.port,
            "protocol_kind": row.protocol_kind.value,
            "protocol_version": observation.protocol_version,
            "target_id": observation.target_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def evaluate_production_inspection_state(
    session: Session,
    target_id: UUID,
    *,
    required_capabilities: frozenset[str],
) -> ProductionInspectionState:
    """Evaluate the current confirmed production target state without I/O."""

    target = session.get(TargetRow, str(target_id))
    if target is None or target.target_type is not TargetType.EXTERNAL_PRODUCTION:
        raise ProductionInspectionError("target_not_found_or_not_authorized")
    row = session.get(ExternalKodiTargetRow, str(target_id))
    policy = session.get(ExternalProductionPolicyRow, str(target_id))
    if row is None or policy is None:
        raise ProductionInspectionError("production_policy_not_confirmed")
    if row.administrative_state is not ExternalAdministrativeState.ENABLED:
        raise ProductionInspectionError("target_disabled")
    if row.verification_state is ExternalVerificationState.IDENTITY_MISMATCH:
        raise ProductionInspectionError("identity_mismatch")
    if row.verification_state is ExternalVerificationState.NEEDS_REVERIFICATION:
        raise ProductionInspectionError("needs_reverification")
    if row.verification_state is not ExternalVerificationState.VERIFIED:
        raise ProductionInspectionError("needs_reverification")
    if policy.designation_state is not ProductionDesignationState.CONFIRMED:
        raise ProductionInspectionError("production_policy_not_confirmed")
    if policy.identity_generation < 1:
        raise ProductionInspectionError("needs_reverification")
    if (
        policy.confirmed_observation_id is None
        or policy.confirmed_observation_digest is None
    ):
        raise ProductionInspectionError("production_policy_not_confirmed")
    observation = session.get(
        ExternalKodiTargetObservationRow, policy.confirmed_observation_id
    )
    if (
        observation is None
        or observation.target_id != str(target_id)
        or observation.outcome != "reachable"
        or observation.error_code is not None
        or observation.kodi_name is None
        or observation.kodi_version is None
        or observation.protocol_version is None
        or row.pending_observation_id != observation.id
    ):
        raise ProductionInspectionError("needs_reverification")
    try:
        configured = _closed_set(
            row.configured_capabilities_json, allowed=EXTERNAL_CAPABILITY_VALUES
        )
        verified = _closed_set(
            row.verified_capabilities_json, allowed=EXTERNAL_CAPABILITY_VALUES
        )
        observed_capabilities = _closed_set(
            observation.verified_capabilities_json,
            allowed=EXTERNAL_CAPABILITY_VALUES,
        )
        addresses = _closed_set(row.verified_addresses_json)
        observed_addresses = _closed_set(observation.addresses_json)
        digest = production_observation_digest(row, observation)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise ProductionInspectionError("needs_reverification") from error
    if (
        verified != observed_capabilities
        or addresses != observed_addresses
        or addresses != frozenset({row.host})
        or row.verified_kodi_name != observation.kodi_name
        or row.verified_kodi_version != observation.kodi_version
        or row.observed_kodi_name != observation.kodi_name
        or row.observed_kodi_version != observation.kodi_version
        or row.observed_protocol_version != observation.protocol_version
        or target.kodi_version != observation.kodi_version
        or digest != policy.confirmed_observation_digest
    ):
        raise ProductionInspectionError("needs_reverification")
    capabilities = configured.intersection(verified)
    if not required_capabilities.issubset(capabilities):
        raise ProductionInspectionError("capability_unavailable")
    return ProductionInspectionState(
        target_id=target_id,
        logical_name=target.name,
        identity_generation=policy.identity_generation,
        kodi_name=observation.kodi_name,
        kodi_version=observation.kodi_version,
        protocol_version=observation.protocol_version,
        capabilities=capabilities,
    )


def _closed_set(value: str, *, allowed: frozenset[str] | None = None) -> frozenset[str]:
    parsed = json.loads(value)
    if (
        not isinstance(parsed, list)
        or any(not isinstance(item, str) for item in parsed)
        or len(parsed) != len(set(parsed))
    ):
        raise ValueError("invalid closed string set")
    result = frozenset(parsed)
    if allowed is not None and not result <= allowed:
        raise ValueError("unknown closed string set member")
    return result


@dataclass(slots=True)
class _Bucket:
    tokens: float
    updated_at: float
    seen_at: float


class ProductionReadRateLimiter:
    """Three bounded server-identity keyed production read limiter maps."""

    def __init__(
        self,
        *,
        maximum_entries: int = 1024,
        idle_seconds: float = 15 * 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if maximum_entries < 1 or idle_seconds <= 0:
            raise ValueError("invalid production read limiter bounds")
        self._maximum_entries = maximum_entries
        self._idle_seconds = idle_seconds
        self._clock = clock
        self._guard = threading.Lock()
        self._maps: dict[str, dict[Hashable, _Bucket]] = {
            "target_list": {},
            "target_status": {},
            "addon_list": {},
        }

    def require_target_list(self, client_id: UUID) -> None:
        self._require("target_list", client_id, rate=2.0, burst=2)

    def require_target_read(
        self, tool_name: str, client_id: UUID, target_id: UUID
    ) -> None:
        if tool_name == "target_status":
            self._require(tool_name, (client_id, target_id), rate=1 / 5.0, burst=1)
        elif tool_name == "addon_list":
            self._require(tool_name, (client_id, target_id), rate=1 / 30.0, burst=1)

    def _require(self, name: str, key: Hashable, *, rate: float, burst: int) -> None:
        now = self._clock()
        with self._guard:
            buckets = self._maps[name]
            self._prune_idle(buckets, now)
            bucket = buckets.get(key)
            if bucket is None:
                if len(buckets) >= self._maximum_entries:
                    raise ProductionInspectionError("rate_limited")
                bucket = _Bucket(float(burst), now, now)
                buckets[key] = bucket
            else:
                elapsed = max(0.0, now - bucket.updated_at)
                bucket.tokens = min(float(burst), bucket.tokens + elapsed * rate)
                bucket.updated_at = now
                bucket.seen_at = now
            if bucket.tokens < 1.0:
                raise ProductionInspectionError("rate_limited")
            bucket.tokens -= 1.0

    def _prune_idle(self, buckets: dict[Hashable, _Bucket], now: float) -> None:
        for key, bucket in tuple(buckets.items()):
            if now - bucket.seen_at >= self._idle_seconds:
                del buckets[key]
