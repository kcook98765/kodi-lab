"""Live, read-only managed-instance Kodi add-on inventory."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool


class AddonInventoryQueryStatus(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


class AddonInventoryUnavailableReason(StrEnum):
    INSTANCE_NOT_PROVISIONED = "instance_not_provisioned"
    KODI_NOT_RUNNING = "kodi_not_running"
    KODI_NOT_READY = "kodi_not_ready"
    JSONRPC_UNAVAILABLE = "jsonrpc_unavailable"
    UNSUPPORTED_RESPONSE = "unsupported_response"
    MALFORMED_ADDON_DATA = "malformed_addon_data"
    INVENTORY_LIMIT_EXCEEDED = "inventory_limit_exceeded"


class KodiAddonProvenanceClassification(StrEnum):
    DEVELOPMENT_SOURCE_AT_LAST_VERIFIED_COPY = (
        "development_source_at_last_verified_copy"
    )
    PRIOR_VERIFIED_ZIP_INSTALL = "prior_verified_zip_install"
    PRIOR_VERIFIED_REPOSITORY_INSTALL = "prior_verified_repository_install"
    UNKNOWN = "unknown"


class KodiAddonProvenanceEvidenceStatus(StrEnum):
    LAST_OPERATION_VERIFIED = "last_operation_verified"
    STALE = "stale"
    UNKNOWN = "unknown"


class KodiAddonProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    classification: KodiAddonProvenanceClassification
    evidence_status: KodiAddonProvenanceEvidenceStatus
    development_source_id: UUID | None = None
    development_source_logical_name: str | None = Field(default=None, max_length=256)
    addon_zip_logical_label: str | None = Field(default=None, max_length=256)
    repository_source_id: UUID | None = None
    repository_logical_name: str | None = Field(default=None, max_length=120)
    verified_at: datetime | None = None


class KodiAddonInventoryItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    addon_id: str = Field(max_length=128)
    name: str = Field(max_length=256)
    version: str = Field(max_length=128)
    enabled: StrictBool
    kodi_type: str = Field(max_length=128)
    provenance: KodiAddonProvenance
    repository_override_active: StrictBool = False


class ExternalAddonInventory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    instance_id: UUID
    observed_at: datetime
    kodi_release: str = Field(max_length=32)
    query_status: AddonInventoryQueryStatus
    unavailable_reason: AddonInventoryUnavailableReason | None = None
    complete: StrictBool
    items: list[KodiAddonInventoryItem] = Field(max_length=2_048)
