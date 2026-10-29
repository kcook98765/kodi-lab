from __future__ import annotations

import json
import logging

import pytest

from kodi_lab.development.measurements import DeploymentMeasurement


def test_counts_actual_stream_bytes_and_failed_phase(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Alembic may disable an existing logger before this test runs.
    monkeypatch.setattr(
        logging.getLogger("kodi_lab.deployment.measurement"), "disabled", False
    )
    measurement = DeploymentMeasurement("operation", "copy_sync")
    assert list(measurement.chunks("readback", [b"abc", b"de"])) == [b"abc", b"de"]

    def fail() -> None:
        raise ValueError("private file content")

    with pytest.raises(ValueError):
        measurement.run("source_scan", fail)
    with caplog.at_level(logging.INFO, logger="kodi_lab.deployment.measurement"):
        measurement.emit()
    text = caplog.records[-1].getMessage().removeprefix("deployment measurement ")
    document = json.loads(text)
    assert document["counts"] == {"readback": 5}
    assert document["completed"] is False
    assert 0 <= document["phases_seconds"]["source_scan"] <= document["total_seconds"]
    assert "private file content" not in text


def test_log_sink_failure_cannot_change_mutation_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise OSError("log sink unavailable")

    monkeypatch.setattr(
        logging.getLogger("kodi_lab.deployment.measurement"), "info", fail
    )
    measurement = DeploymentMeasurement("operation", "copy_sync")
    measurement.completed = True
    measurement.emit()
    assert measurement.completed is True
