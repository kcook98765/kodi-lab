"""Fixed, bounded Kodi diagnostics. No caller-selected labels or host probes."""

from __future__ import annotations

import math
import re
from datetime import UTC, datetime
from typing import Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field

from kodi_lab.kodi.ui_state_native import UiRpc

MAX_RAW = 160
MAX_CPU_RAW = 8192
MAX_CORES = 256


type Normalization = Literal["exact", "rounded", "raw_only", "unavailable"]
type MetricValue = str | float | bool | dict[str, float] | None
type MetricScope = Literal["kodi_reported", "kodi_process", "kodi_default_drive"]


class _MetricIdentity(TypedDict):
    label: str
    scope: MetricScope


class SystemMetric(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    availability: Literal["available", "not_reported", "unsupported", "invalid"]
    source: Literal["native_jsonrpc"] = "native_jsonrpc"
    label: str
    scope: MetricScope = "kodi_reported"
    value: MetricValue = None
    unit: str | None = None
    raw: str | None = None
    normalization: Normalization = "unavailable"


class SystemInfo(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal[1] = 1
    observed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    status: Literal["complete", "partial", "unavailable"]
    scope: Literal["container_view", "unknown"] = "unknown"
    metrics: dict[str, SystemMetric]


# kind determines parsing, never request selection. MB in these Kodi labels is
# binary (1048576 bytes), rounded down by Kodi, even though its suffix says MB.
_LABELS: dict[str, tuple[str, str]] = {
    "kodi_version": ("System.BuildVersion", "text"),
    "kodi_version_short": ("System.BuildVersionShort", "text"),
    "kodi_version_code": ("System.BuildVersionCode", "text"),
    "kodi_git": ("System.BuildVersionGit", "text"),
    "kodi_build_date": ("System.BuildDate", "text"),
    "os_version": ("System.OSVersionInfo", "text"),
    "cpu_usage": ("System.CpuUsage", "cores"),
    "cpu_frequency": ("System.CpuFrequency", "frequency"),
    "cpu_temperature": ("System.CPUTemperature", "temperature"),
    "gpu_temperature": ("System.GPUTemperature", "temperature"),
    "fan_speed": ("System.FanSpeed", "percent"),
    "memory_total": ("System.Memory(total)", "memory"),
    "memory_used": ("System.Memory(used)", "memory"),
    "memory_free": ("System.Memory(free)", "memory"),
    "memory_used_percent": ("System.Memory(used.percent)", "percent"),
    "memory_free_percent": ("System.Memory(free.percent)", "percent"),
    "storage_total": ("System.TotalSpace", "storage"),
    "storage_used": ("System.UsedSpace", "storage"),
    "storage_free": ("System.FreeSpace", "storage"),
    "storage_used_percent": ("System.UsedSpacePercent", "storage_percent"),
    "storage_free_percent": ("System.FreeSpacePercent", "storage_percent"),
    "display_width": ("System.ScreenWidth", "pixels"),
    "display_height": ("System.ScreenHeight", "pixels"),
    "display_resolution": ("System.ScreenResolution", "text"),
    "display_mode": ("System.ScreenMode", "text"),
    "display_encoder": ("System.VideoEncoderInfo", "text"),
    "display_hdr_types": ("System.SupportedHDRTypes", "text"),
    "render_fps": ("System.FPS", "fps"),
    "network_ip": ("Network.IPAddress", "text"),
    "network_gateway": ("Network.GatewayAddress", "text"),
    "network_subnet": ("Network.SubnetMask", "text"),
    "network_dns1": ("Network.DNS1Address", "text"),
    "network_dns2": ("Network.DNS2Address", "text"),
    "network_dhcp": ("Network.DHCPAddress", "text"),
    "network_link": ("Network.LinkState", "text"),
    "network_internet": ("System.InternetState", "text"),
    "uptime": ("System.Uptime", "uptime"),
    "total_uptime": ("System.TotalUptime", "uptime"),
    "battery_level": ("System.BatteryLevel", "percent"),
}
_BOOLEANS = {
    "network_connected": "System.HasNetwork",
    "cpu_usage_supported": "System.SupportsCPUUsage",
    "can_powerdown": "System.CanPowerDown",
    "can_suspend": "System.CanSuspend",
    "can_hibernate": "System.CanHibernate",
    "can_reboot": "System.CanReboot",
    **{
        f"platform_{name.lower()}": f"System.Platform.{name}"
        for name in (
            "Linux",
            "Windows",
            "UWP",
            "Darwin",
            "OSX",
            "IOS",
            "TVOS",
            "Android",
            "WebOS",
        )
    },
}
INFO_LABELS = tuple(label for label, _ in _LABELS.values())
INFO_BOOLEANS = tuple(_BOOLEANS.values())
_NUMBER = r"[+-]?[0-9]+(?:\.[0-9]+)?"


def _normalize(raw: str, kind: str) -> tuple[MetricValue, str | None, Normalization]:
    if kind == "text":
        return raw, None, "exact"
    if kind == "cores":
        # Linux reports indexed cores; Windows/Darwin can report one percentage.
        # Neither is silently interpreted as aggregate CPU use.
        matches = list(re.finditer(r"#(\d+):\s*(" + _NUMBER + r")%", raw))
        if matches and not re.sub(r"#\d+:\s*" + _NUMBER + r"%", "", raw).strip():
            values = {m[1]: float(m[2]) for m in matches}
            if len(values) == len(matches) <= MAX_CORES and all(
                0 <= v <= 100 for v in values.values()
            ):
                return values, "percent_per_core", "rounded"
        return None, None, "raw_only"
    if kind == "uptime":
        # Kodi uses localized day/hour/minute words. Only convert known English.
        match = re.fullmatch(
            r"(?:(\d+) days?, )?(?:(\d+) hours?, )?(\d+) minutes?", raw, re.I
        )
        if match:
            days, hours, minutes = (int(v or 0) for v in match.groups())
            return (
                float((days * 1440 + hours * 60 + minutes) * 60),
                "seconds",
                "rounded",
            )
        return None, "seconds", "raw_only"
    suffixes = {
        "frequency": r"\s*(MHz|GHz|kHz|Hz)",
        "temperature": r"\s*°?\s*([CF])",
        "memory": r"\s*(MB)",
        "storage": r"\s*(MB)(?:\s+[^\d]*)?",
        "percent": r"\s*%?",
        "storage_percent": r"\s*%(?:\s+[^\d]*)?",
        "pixels": "",
        "fps": "",
    }
    match = re.fullmatch(r"(" + _NUMBER + ")" + suffixes[kind], raw, re.I)
    units = {
        "frequency": "Hz",
        "temperature": "celsius",
        "memory": "bytes",
        "storage": "bytes",
        "percent": "percent",
        "storage_percent": "percent",
        "pixels": "pixels",
        "fps": "frames_per_second",
    }
    unit = units[kind]
    if not match:
        return None, unit, "raw_only"
    value = float(match[1])
    if kind == "frequency":
        value *= {"hz": 1, "khz": 1000, "mhz": 1e6, "ghz": 1e9}[match[2].lower()]
    elif kind == "temperature" and match[2].upper() == "F":
        value = (value - 32) * 5 / 9
    elif kind in {"memory", "storage"}:
        value *= 1048576
    if (
        not math.isfinite(value)
        or (kind != "temperature" and value < 0)
        or (unit == "percent" and value > 100)
        or (kind == "temperature" and not -273.15 <= value <= 1000)
    ):
        return None, unit, "unavailable"
    return (
        value,
        unit,
        "rounded"
        if kind in {"memory", "storage", "frequency", "temperature", "fps"}
        else "exact",
    )


def _metric(label: str, kind: str, values: object) -> SystemMetric:
    base: _MetricIdentity = {
        "label": label,
        "scope": "kodi_default_drive"
        if kind.startswith("storage")
        else "kodi_process"
        if kind == "uptime"
        else "kodi_reported",
    }
    if values is None:
        return SystemMetric(**base, availability="unsupported")
    if not isinstance(values, dict):
        return SystemMetric(**base, availability="invalid")
    raw = values.get(label)
    if raw is None or raw == "":
        return SystemMetric(**base, availability="not_reported")
    if kind == "boolean":
        return SystemMetric(
            **base,
            availability="available" if type(raw) is bool else "invalid",
            value=raw if type(raw) is bool else None,
            normalization="exact" if type(raw) is bool else "unavailable",
        )
    if (
        not isinstance(raw, str)
        or any(ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF for c in raw)
        or len(raw.encode("utf-8")) > (MAX_CPU_RAW if kind == "cores" else MAX_RAW)
    ):
        return SystemMetric(**base, availability="invalid")
    raw = raw.strip()
    if not raw or raw.lower() in {
        "n/a",
        "na",
        "unknown",
        "not available",
        "unavailable",
        "?",
    }:
        return SystemMetric(**base, availability="not_reported", raw=raw or None)
    if kind != "text" and raw.lower() in {"nan", "inf", "+inf", "-inf", "infinity"}:
        return SystemMetric(**base, availability="invalid", raw=raw)
    value, unit, normalization = _normalize(raw, kind)
    return SystemMetric(
        **base,
        availability="available" if normalization != "unavailable" else "invalid",
        value=value,
        unit=unit,
        raw=raw,
        normalization=normalization,
    )


def collect_system_info(call: UiRpc) -> SystemInfo:
    """Two bounded requests. Transport, auth and identity errors propagate."""
    labels = call("XBMC.GetInfoLabels", {"labels": list(INFO_LABELS)})
    booleans = call("XBMC.GetInfoBooleans", {"booleans": list(INFO_BOOLEANS)})
    metrics = {
        name: _metric(label, kind, labels) for name, (label, kind) in _LABELS.items()
    }
    metrics.update(
        {name: _metric(label, "boolean", booleans) for name, label in _BOOLEANS.items()}
    )
    available = sum(m.availability == "available" for m in metrics.values())
    return SystemInfo(
        status="complete"
        if available == len(metrics)
        else "partial"
        if available
        else "unavailable",
        metrics=metrics,
    )
