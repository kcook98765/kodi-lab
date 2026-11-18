"""Parsing uses Kodi's actual formats and retains uncertain/localized labels."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from kodi_lab.domain import ExternalProtocolKind
from kodi_lab.kodi.external import (
    ExternalEndpointPolicy,
    ExternalProbeError,
    NativeExternalKodiAdapter,
)
from kodi_lab.kodi.system_info import (
    INFO_BOOLEANS,
    INFO_LABELS,
    MAX_RAW,
    collect_system_info,
)


def snapshot(labels: object, booleans: object = None) -> Any:
    return collect_system_info(
        lambda method, params: labels if method == "XBMC.GetInfoLabels" else booleans
    )


@pytest.mark.parametrize(
    ("label", "raw", "name", "value", "unit"),
    [
        ("System.Memory(total)", "2048MB", "memory_total", 2048 * 1048576, "bytes"),
        (
            "System.TotalSpace",
            "123 MB insgesamt",
            "storage_total",
            123 * 1048576,
            "bytes",
        ),
        ("System.FreeSpacePercent", "42 % frei", "storage_free_percent", 42, "percent"),
        ("System.CpuFrequency", " 2400 MHz", "cpu_frequency", 2.4e9, "Hz"),
        ("System.CpuFrequency", "1.5 GHz", "cpu_frequency", 1.5e9, "Hz"),
        ("System.CPUTemperature", "-5°C", "cpu_temperature", -5, "celsius"),
        ("System.GPUTemperature", "122 °F", "gpu_temperature", 50, "celsius"),
        ("System.BatteryLevel", "0%", "battery_level", 0, "percent"),
        ("System.Uptime", "2 days, 3 hours, 4 minutes", "uptime", 183840, "seconds"),
        ("System.ScreenWidth", "1920", "display_width", 1920, "pixels"),
    ],
)
def test_documented_formats(
    label: str, raw: str, name: str, value: float, unit: str
) -> None:
    metric = snapshot({label: raw}).metrics[name]
    assert metric.availability == "available"
    assert metric.value == value and metric.unit == unit
    assert metric.raw == raw.strip()


@pytest.mark.parametrize(
    "raw",
    [
        "1,25 GHz",
        "24,5°C",
        "1 Stunde, 2 Minuten",
        "17%",
        "#0: 12% truncated",
        "#0: 1% #0: 2%",
    ],
)
def test_uncertain_formats_are_preserved_without_fabricated_numbers(raw: str) -> None:
    result = snapshot({"System.CpuUsage": raw}).metrics["cpu_usage"]
    assert result.value is None and result.raw == raw
    assert result.normalization == "raw_only"


def test_cpu_is_indexed_and_never_an_aggregate() -> None:
    metric = snapshot({"System.CpuUsage": "#0: 1.5% #1: 100% #9: 0.0%"}).metrics[
        "cpu_usage"
    ]
    assert metric.value == {"0": 1.5, "1": 100.0, "9": 0.0}
    assert metric.unit == "percent_per_core"
    assert "aggregate" not in metric.model_dump_json()


@pytest.mark.parametrize("raw", ["", None, "N/A", "Not available"])
def test_missing_is_not_zero(raw: object) -> None:
    metric = snapshot({"System.BatteryLevel": raw}).metrics["battery_level"]
    assert metric.availability == "not_reported" and metric.value is None


@pytest.mark.parametrize(
    "raw",
    [
        True,
        12,
        [],
        "a" * (MAX_RAW + 1),
        "bad\ntext",
        "-2%",
        "101%",
        "NaN",
        "inf",
        "\ud800",
    ],
)
def test_invalid_fields_do_not_discard_good_fields(raw: object) -> None:
    value = snapshot({"System.BatteryLevel": raw, "System.BuildVersion": "21.3"})
    assert value.status == "partial"
    assert value.metrics["battery_level"].availability == "invalid"
    assert value.metrics["battery_level"].value is None
    assert value.metrics["kodi_version"].value == "21.3"


def test_empty_unsupported_invalid_and_strict_booleans() -> None:
    cases: list[tuple[object, str]] = [
        ({}, "not_reported"),
        (None, "unsupported"),
        ([], "invalid"),
    ]
    for labels, expected in cases:
        value = snapshot(labels, {})
        assert value.status == "unavailable"
        assert value.metrics["memory_total"].availability == expected
    value = snapshot({}, {"System.HasNetwork": False, "System.CanReboot": "false"})
    assert value.metrics["network_connected"].value is False
    assert value.metrics["can_reboot"].availability == "invalid"


def test_closed_requests_cardinality_and_result_bounds() -> None:
    calls = []

    def call(method: str, params: dict[str, Any] | None) -> object:
        calls.append((method, params))
        # Even maximum-length permitted raw strings stay inside the MCP limit
        # when multibyte strings are refused by the final response guard.
        return (
            {label: "21.3" for label in INFO_LABELS}
            if method.endswith("Labels")
            else {label: False for label in INFO_BOOLEANS}
        )

    value = collect_system_info(call)
    assert calls == [
        ("XBMC.GetInfoLabels", {"labels": list(INFO_LABELS)}),
        ("XBMC.GetInfoBooleans", {"booleans": list(INFO_BOOLEANS)}),
    ]
    assert len(value.metrics) == len(INFO_LABELS) + len(INFO_BOOLEANS)
    assert len(value.model_dump_json().encode()) < 20000
    assert "Network.MacAddress" not in INFO_LABELS
    assert all("path" not in label.lower() for label in INFO_LABELS)


@pytest.mark.parametrize("error", [-32601, -32602, -32603, [], {}, True])
def test_optional_methods_only_ignore_explicit_unsupported_errors(
    error: object,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["method"].endswith("Labels"):
            return httpx.Response(
                200, json={"jsonrpc": "2.0", "id": body["id"], "error": {"code": error}}
            )
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": body["id"],
                "result": {"System.HasNetwork": True},
            },
        )

    endpoint = ExternalEndpointPolicy(
        allowed_ports=(8080,), test_loopback_enabled=True
    ).validate(
        protocol_kind=ExternalProtocolKind.JSONRPC_HTTP, host="127.0.0.1", port=8080
    )
    adapter = NativeExternalKodiAdapter(
        endpoint, None, transport=httpx.MockTransport(handler)
    )
    if error not in (-32601, -32602):
        with pytest.raises(ExternalProbeError, match="invalid_kodi_response"):
            adapter.system_info()
    else:
        result = adapter.system_info()
        assert result.metrics["kodi_version"].availability == "unsupported"
        assert result.metrics["network_connected"].value is True


def test_many_cores_and_hostile_strings_remain_bounded() -> None:
    # No inferred count/aggregate, no truncation into valid-looking percentages.
    cores = " ".join(f"#{i}: 1.0%" for i in range(256))
    value = snapshot({"System.CpuUsage": cores})
    readings = value.metrics["cpu_usage"].value
    assert isinstance(readings, dict) and len(readings) == 256
    assert (
        snapshot({"System.CpuUsage": cores + " #256: 1.0%"}).metrics["cpu_usage"].value
        is None
    )
    labels = {label: "😀" * 40 for label in INFO_LABELS}
    labels["System.CpuUsage"] = cores
    booleans = {label: False for label in INFO_BOOLEANS}
    result = snapshot(labels, booleans)
    assert len(json.dumps(result.model_dump(mode="json")).encode()) < 64 * 1024
