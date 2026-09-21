"""Closed Phase 14 production-policy values."""

from enum import StrEnum


class ProductionDesignationState(StrEnum):
    PENDING_NATIVE_IDENTITY = "pending_native_identity"
    PENDING_CONFIRMATION = "pending_confirmation"
    CONFIRMED = "confirmed"
    INVALIDATED = "invalidated"
    DEACTIVATED = "deactivated"
