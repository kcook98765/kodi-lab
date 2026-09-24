"""Test-only Web automation for the disposable production target lifecycle.

The helper is intentionally narrower than the generic Web client.  It can
register and operate only the fixed Kodi 19 production-profile endpoint, and
it never exposes arbitrary production-target mutation access.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from types import MappingProxyType
from typing import Final, Literal, cast
from uuid import UUID

import httpx

from kodi_lab.production_profile_web_client import ProductionProfileWebClient

PRODUCTION_PROFILE_TARGET_HOST: Final = "10.251.0.19"
PRODUCTION_PROFILE_TARGET_PORT: Final = 8080
PRODUCTION_PROFILE_TARGET_PROTOCOL: Final = "jsonrpc_http"
PRODUCTION_PROFILE_TARGET_NAME: Final = "Disposable Kodi 19.5 production target"
PRODUCTION_PROFILE_CAPABILITIES: Final = (
    "addon_inventory",
    "target_inspect",
)
PRODUCTION_DESIGNATION_ACKNOWLEDGEMENT: Final = (
    "I confirm this is a production Kodi target and mutations may affect real "
    "user state."
)

type TransitionClassification = Literal[
    "initial",
    "native_material",
    "policy_only",
    "no_material_change",
]


class ProductionProfileLifecycleError(RuntimeError):
    """A bounded lifecycle failure that never embeds response or secret data."""

    def __init__(self, code: str, *, status_code: int | None = None) -> None:
        self.code = code
        self.status_code = status_code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ProductionLifecycleTransition:
    """Bounded semantic state returned by one canonical Web operation."""

    operation: str
    target_id: UUID
    generation: int
    policy_revision: int
    classification: TransitionClassification
    lifecycle_state: str
    designation_state: str
    administrative_state: str
    verification_state: str
    observation_id: UUID | None
    kodi_name: str | None
    kodi_version: str | None
    protocol_version: str | None
    configured_capabilities: tuple[str, ...]
    verified_capabilities: tuple[str, ...]


class ProductionProfileLifecycle:
    """Canonical fixed-endpoint production lifecycle over authenticated Web."""

    def __init__(self, web: ProductionProfileWebClient) -> None:
        self._web = web
        self._registered_targets: set[UUID] = set()
        self._latest: dict[UUID, ProductionLifecycleTransition] = {}
        self._transitions: list[ProductionLifecycleTransition] = []

    @property
    def evidence(self) -> tuple[MappingProxyType[str, object], ...]:
        """Return bounded lifecycle evidence without credentials or Web secrets."""

        return tuple(MappingProxyType(asdict(item)) for item in self._transitions)

    def register_external_production(
        self,
        *,
        username: str,
        password: str,
        host: str = PRODUCTION_PROFILE_TARGET_HOST,
        port: int = PRODUCTION_PROFILE_TARGET_PORT,
        protocol_kind: str = PRODUCTION_PROFILE_TARGET_PROTOCOL,
    ) -> ProductionLifecycleTransition:
        """Register only the fixed isolated Kodi fixture with the exact maximum."""

        self._require_fixed_endpoint(host, port, protocol_kind)
        if not username or not password:
            raise ProductionProfileLifecycleError("fixture_credential_missing")
        response = self._web.post_authenticated(
            "/api/production-targets",
            json={
                "logical_name": PRODUCTION_PROFILE_TARGET_NAME,
                "protocol_kind": PRODUCTION_PROFILE_TARGET_PROTOCOL,
                "host": PRODUCTION_PROFILE_TARGET_HOST,
                "port": PRODUCTION_PROFILE_TARGET_PORT,
                "authentication_mode": "username_password",
                "configured_capabilities": list(PRODUCTION_PROFILE_CAPABILITIES),
                "username": username,
                "password": password,
            },
        )
        document = self._document(response, expected_status=201)
        transition = self._transition("register", document, "initial")
        if transition.target_id in self._registered_targets:
            raise ProductionProfileLifecycleError("target_identity_reused")
        self._registered_targets.add(transition.target_id)
        self._record(transition, previous=None)
        return transition

    def observe_native(self, target_id: UUID) -> ProductionLifecycleTransition:
        """Run the human-facing native probe and require exact Kodi 19.5 identity."""

        previous = self._require_registered(target_id)
        response = self._web.post_authenticated(
            f"/api/production-targets/{target_id}/probe", json={}
        )
        transition = self._transition(
            "observe_native",
            self._document(response, expected_status=200),
            "native_material",
        )
        if (
            transition.kodi_name != "Kodi"
            or transition.kodi_version != "19.5"
            or transition.observation_id is None
        ):
            raise ProductionProfileLifecycleError("fixture_identity_mismatch")
        self._record(transition, previous=previous)
        return transition

    def confirm_native_identity(
        self, target_id: UUID, observation_id: UUID
    ) -> ProductionLifecycleTransition:
        """Explicitly confirm the exact observation through the canonical route."""

        previous = self._require_observation(target_id, observation_id)
        response = self._web.post_authenticated(
            f"/api/production-targets/{target_id}/confirm-native",
            json={"confirmed": True, "observation_id": str(observation_id)},
        )
        transition = self._transition(
            "confirm_native_identity",
            self._document(response, expected_status=200),
            "native_material",
        )
        self._record(transition, previous=previous)
        return transition

    def designate_production(
        self, target_id: UUID, observation_id: UUID
    ) -> ProductionLifecycleTransition:
        """Perform the separate literal production-designation ceremony."""

        previous = self._require_observation(target_id, observation_id)
        response = self._web.post_authenticated(
            f"/api/production-targets/{target_id}/confirm-designation",
            json={
                "confirmed": True,
                "observation_id": str(observation_id),
                "acknowledgement": PRODUCTION_DESIGNATION_ACKNOWLEDGEMENT,
            },
        )
        transition = self._transition(
            "designate_production",
            self._document(response, expected_status=200),
            "policy_only",
        )
        self._record(transition, previous=previous)
        return transition

    def enable_production_read(self, target_id: UUID) -> ProductionLifecycleTransition:
        """Enable the designated target without granting any MCP authority."""

        previous = self._require_registered(target_id)
        response = self._web.post_authenticated(
            f"/api/production-targets/{target_id}/enabled",
            json={"enabled": True},
        )
        document = self._document(response, expected_status=200)
        classification: TransitionClassification = (
            "no_material_change"
            if previous.administrative_state == "enabled"
            else "native_material"
        )
        transition = self._transition(
            "enable_production_read", document, classification
        )
        self._record(transition, previous=previous)
        return transition

    @staticmethod
    def _require_fixed_endpoint(host: str, port: int, protocol_kind: str) -> None:
        if (
            host != PRODUCTION_PROFILE_TARGET_HOST
            or port != PRODUCTION_PROFILE_TARGET_PORT
            or protocol_kind != PRODUCTION_PROFILE_TARGET_PROTOCOL
        ):
            raise ProductionProfileLifecycleError("fixture_endpoint_refused")

    def _require_registered(self, target_id: UUID) -> ProductionLifecycleTransition:
        if target_id not in self._registered_targets:
            raise ProductionProfileLifecycleError(
                "target_not_registered_by_lifecycle_harness"
            )
        return self._latest[target_id]

    def _require_observation(
        self, target_id: UUID, observation_id: UUID
    ) -> ProductionLifecycleTransition:
        previous = self._require_registered(target_id)
        if previous.observation_id != observation_id:
            raise ProductionProfileLifecycleError(
                "observation_not_obtained_by_lifecycle_harness"
            )
        return previous

    @staticmethod
    def _document(
        response: httpx.Response, *, expected_status: int
    ) -> dict[str, object]:
        if response.status_code != expected_status:
            code = f"http_{response.status_code}"
            try:
                raw = response.json()
                if isinstance(raw, dict):
                    error = raw.get("error")
                    if isinstance(error, dict) and isinstance(error.get("code"), str):
                        code = cast(str, error["code"])
            except ValueError:
                pass
            raise ProductionProfileLifecycleError(
                code, status_code=response.status_code
            )
        try:
            document = response.json()
        except ValueError as error:
            raise ProductionProfileLifecycleError(
                "lifecycle_response_invalid", status_code=response.status_code
            ) from error
        if not isinstance(document, dict):
            raise ProductionProfileLifecycleError("lifecycle_response_invalid")
        return cast(dict[str, object], document)

    @staticmethod
    def _transition(
        operation: str,
        document: dict[str, object],
        classification: TransitionClassification,
    ) -> ProductionLifecycleTransition:
        policy = document.get("production_policy")
        try:
            target_id = UUID(cast(str, document["target_id"]))
            if document["target_class"] != "external_production":
                raise ValueError
            if (
                document["host"] != PRODUCTION_PROFILE_TARGET_HOST
                or document["port"] != PRODUCTION_PROFILE_TARGET_PORT
                or document["protocol_kind"] != PRODUCTION_PROFILE_TARGET_PROTOCOL
            ):
                raise ValueError
            if not isinstance(policy, dict):
                raise ValueError
            configured = tuple(
                sorted(cast(list[str], document["configured_capabilities"]))
            )
            verified = tuple(sorted(cast(list[str], document["verified_capabilities"])))
            if configured != PRODUCTION_PROFILE_CAPABILITIES:
                raise ValueError
            raw_observation = document.get("pending_observation_id")
            observation_id = (
                None if raw_observation is None else UUID(cast(str, raw_observation))
            )
            transition = ProductionLifecycleTransition(
                operation=operation,
                target_id=target_id,
                generation=cast(int, policy["identity_generation"]),
                policy_revision=cast(int, policy["policy_revision"]),
                classification=classification,
                lifecycle_state=cast(str, policy["lifecycle_state"]),
                designation_state=cast(str, policy["designation_state"]),
                administrative_state=cast(str, document["administrative_state"]),
                verification_state=cast(str, document["verification_state"]),
                observation_id=observation_id,
                kodi_name=cast(str | None, document.get("kodi_name")),
                kodi_version=cast(str | None, document.get("kodi_version")),
                protocol_version=cast(str | None, document.get("protocol_version")),
                configured_capabilities=configured,
                verified_capabilities=verified,
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ProductionProfileLifecycleError(
                "lifecycle_response_invalid"
            ) from error
        if transition.generation != 1 or transition.policy_revision < 1:
            raise ProductionProfileLifecycleError("lifecycle_revision_invalid")
        return transition

    def _record(
        self,
        transition: ProductionLifecycleTransition,
        *,
        previous: ProductionLifecycleTransition | None,
    ) -> None:
        if previous is not None:
            if transition.target_id != previous.target_id:
                raise ProductionProfileLifecycleError("target_identity_changed")
            expected_delta = (
                0 if transition.classification == "no_material_change" else 1
            )
            if transition.policy_revision != previous.policy_revision + expected_delta:
                raise ProductionProfileLifecycleError("unexpected_policy_revision_jump")
            if transition.generation != previous.generation:
                raise ProductionProfileLifecycleError(
                    "unexpected_identity_generation_jump"
                )
        self._latest[transition.target_id] = transition
        self._transitions.append(transition)
