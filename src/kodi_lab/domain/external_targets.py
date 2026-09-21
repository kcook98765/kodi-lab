"""Closed Phase 13 external-development target values."""

from __future__ import annotations

from enum import StrEnum


class ExternalProtocolKind(StrEnum):
    JSONRPC_HTTP = "jsonrpc_http"
    JSONRPC_HTTPS = "jsonrpc_https"


class ExternalAuthenticationMode(StrEnum):
    NONE = "none"
    USERNAME_PASSWORD = "username_password"


class ExternalAdministrativeState(StrEnum):
    DISABLED = "disabled"
    ENABLED = "enabled"
    DEACTIVATED = "deactivated"


class ExternalVerificationState(StrEnum):
    UNVERIFIED = "unverified"
    VERIFIED = "verified"
    NEEDS_REVERIFICATION = "needs_reverification"
    IDENTITY_MISMATCH = "identity_mismatch"


class ExternalObservationState(StrEnum):
    REACHABLE = "reachable"
    UNREACHABLE = "unreachable"
    AUTHENTICATION_FAILED = "authentication_failed"
    CAPABILITY_MISSING = "capability_missing"


class ExternalCapability(StrEnum):
    TARGET_INSPECT = "target_inspect"
    ADDON_INVENTORY = "addon_inventory"
    SEMANTIC_REMOTE = "semantic_remote"
    SCREENSHOT = "screenshot"
    WORKSPACE_DEPLOYMENT = "workspace_deployment"
    FIXED_EXECUTION = "fixed_execution"


EXTERNAL_CAPABILITY_VALUES = frozenset(item.value for item in ExternalCapability)


class ExternalBridgePairingState(StrEnum):
    UNPAIRED = "unpaired"
    PAIRING_PENDING = "pairing_pending"
    PAIRED = "paired"
    NEEDS_REVERIFICATION = "needs_reverification"
    REVOKED = "revoked"


class ExternalBridgeReachabilityState(StrEnum):
    UNKNOWN = "unknown"
    REACHABLE = "reachable"
    UNREACHABLE = "unreachable"
    AUTHENTICATION_FAILED = "authentication_failed"
    IDENTITY_MISMATCH = "identity_mismatch"
    PROTOCOL_UNSUPPORTED = "protocol_unsupported"
