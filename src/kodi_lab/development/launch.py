"""Manifest-only eligibility for a selected managed GUI entry point."""

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from kodi_lab.development.sources import DevelopmentSourceValidationResult


class ManifestLaunchEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    ordinal: int = Field(ge=0, le=63)
    extension_point: str = Field(max_length=128)
    library: str | None = Field(max_length=256)
    kind: Literal["script", "plugin"] | None
    eligible: bool
    reason: (
        Literal[
            "unsupported_extension",
            "unsafe_library",
            "multiple_plugin_extensions",
            "unsupported_plugin_content",
        ]
        | None
    )
    requires_exact_managed_deployment: bool = True


def safe_launch_library(value: str | None) -> bool:
    return bool(
        value
        and len(value) <= 256
        and re.fullmatch(r"[A-Za-z0-9_.\-/]+\.py", value)
        and all(part not in {"", ".", ".."} for part in value.split("/"))
    )


def manifest_launch_entries(
    descriptor: DevelopmentSourceValidationResult,
) -> tuple[ManifestLaunchEntry, ...]:
    plugin_count = sum(
        item.point == "xbmc.python.pluginsource" for item in descriptor.extensions
    )
    result = []
    for ordinal, item in enumerate(descriptor.extensions[:64]):
        kind: Literal["script", "plugin"] | None = (
            "script"
            if item.point == "xbmc.python.script"
            else "plugin"
            if item.point == "xbmc.python.pluginsource"
            else None
        )
        reason: (
            Literal[
                "unsupported_extension",
                "unsafe_library",
                "multiple_plugin_extensions",
                "unsupported_plugin_content",
            ]
            | None
        ) = None
        if kind is None:
            reason = "unsupported_extension"
        elif not safe_launch_library(item.library):
            reason = "unsafe_library"
        elif kind == "plugin" and plugin_count != 1:
            reason = "multiple_plugin_extensions"
        elif kind == "plugin" and (
            len(item.provides) != 1
            or item.provides[0] not in {"video", "audio", "image", "executable"}
        ):
            reason = "unsupported_plugin_content"
        result.append(
            ManifestLaunchEntry(
                ordinal=ordinal,
                extension_point=item.point,
                library=item.library
                if item.library is not None and len(item.library) <= 256
                else None,
                kind=kind,
                eligible=reason is None,
                reason=reason,
            )
        )
    return tuple(result)
