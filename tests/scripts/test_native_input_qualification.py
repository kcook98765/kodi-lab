"""Coverage failures must never become native view-only acceptance."""

from typing import Any

import pytest

from scripts.native_input_qualification import validate_refusal


def rows() -> list[dict[str, Any]]:
    return [
        {
            "began": i / 10,
            "at": i / 10 + 0.01,
            "state": {"keys": [], "root_xy": [1, 2], "mask": 0},
        }
        for i in range(20)
    ]


def test_complete_native_coverage() -> None:
    values = rows()
    result = validate_refusal(values, values[0]["state"], 0.2, 1.5)
    assert result["samples"] >= 14
    assert result["max_gap_seconds"] < 0.11


@pytest.mark.parametrize("boundary", [(-1, 1), (1, 3)])
def test_missing_edges_fail(boundary: tuple[float, float]) -> None:
    with pytest.raises(ValueError, match="boundary"):
        validate_refusal(rows(), rows()[0]["state"], *boundary)


def test_slow_native_observation_fails() -> None:
    values = rows()
    values[8]["began"] -= 1
    with pytest.raises(ValueError, match="gap"):
        validate_refusal(values, values[0]["state"], 0.2, 1.5)


def test_sparse_native_coverage_fails() -> None:
    values = rows()
    values = values[:4] + values[12:]
    with pytest.raises(ValueError, match="gap"):
        validate_refusal(values, values[0]["state"], 0.2, 1.5)


@pytest.mark.parametrize("effect", [{"keys": [38]}, {"root_xy": [7, 8]}, {"mask": 256}])
def test_input_effect_fails(effect: dict[str, Any]) -> None:
    values = rows()
    values[8]["state"] = {**values[8]["state"], **effect}
    with pytest.raises(ValueError, match="input effect"):
        validate_refusal(values, values[0]["state"], 0.2, 1.5)


@pytest.mark.parametrize(
    "baseline,current,passed",
    [
        (500, 500, True),
        (100, 150, True),
        (1000, 1150, True),
        (500, 600, True),
        (500, 601, False),
    ],
)
def test_original_compound_latency(
    baseline: float, current: float, passed: bool
) -> None:
    from scripts.native_input_qualification import compound_latency

    assert compound_latency(baseline, current)["passed"] is passed
