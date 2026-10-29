"""Bounded source/copy pipeline benchmark using simulated Docker transport.

Run from the checkout with `uv run --locked python -m
scripts.benchmark_workspace_deployment --samples 5`. No Docker daemon, live
Kodi, network, or existing workspace is used. Times include the fake transport's
in-memory tar construction and are NOT native deployment throughput.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import statistics
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

from tests.broker.test_development_source_deployment import (
    ADDON_ID,
    FakeDeploymentDockerClient,
    _apply,
    _prior,
    _spec,
)

CASES = (
    ("small", 50, 1),
    ("typical", 250, 10),
    ("many_files", 1000, 25),
    ("assets", 250, 100),
)


class Measurements(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.documents: list[dict[str, Any]] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.documents.append(
            json.loads(record.getMessage().removeprefix("deployment measurement "))
        )


def benchmark(
    samples: int, parent: str | None, case: str | None = None
) -> dict[str, Any]:
    handler = Measurements()
    logger = logging.getLogger("kodi_lab.deployment.measurement")
    previous_level = logger.level
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    results: list[dict[str, Any]] = []
    cases = tuple(item for item in CASES if case is None or item[0] == case)
    try:
        for name, file_count, size_mib in cases:
            for sample in range(samples):
                with tempfile.TemporaryDirectory(
                    prefix="klbench-", dir=parent
                ) as directory:
                    root = Path(directory)
                    addon = root / "development" / "addon"
                    addon.mkdir(parents=True)
                    descriptor = (
                        f'<addon id="{ADDON_ID}" name="Benchmark" version="1.0.0">'
                        '<extension point="xbmc.python.pluginsource" '
                        'library="file0000.dat">'
                        "<provides>video</provides></extension></addon>"
                    ).encode()
                    (addon / "addon.xml").write_bytes(descriptor)
                    quotient, remainder = divmod(
                        size_mib * 1024**2 - len(descriptor), file_count - 1
                    )
                    for index in range(file_count - 1):
                        (addon / f"file{index:04}.dat").write_bytes(
                            bytes([index % 251]) * (quotient + (index < remainder))
                        )
                    spec = _spec(root)
                    client = FakeDeploymentDockerClient()
                    for change in ("initial", "unchanged", "one_file_edit"):
                        if change == "one_file_edit":
                            edited = addon / "file0000.dat"
                            content = edited.read_bytes()
                            edited.write_bytes(b"x" + content[1:])
                        spec = replace(spec, deployment_operation_id=str(uuid4()))
                        before = len(handler.documents)
                        started = time.monotonic()
                        applied = _apply(client, spec)
                        elapsed = time.monotonic() - started
                        spec = replace(spec, prior_evidence=_prior(applied))
                        results.append(
                            {
                                "case": name,
                                "files": file_count,
                                "source_bytes": size_mib * 1024**2,
                                "sample": sample,
                                "change": change,
                                "elapsed_seconds": elapsed,
                                "transferred_payload_bytes": applied.transferred_bytes,
                                "measurement": handler.documents[-1]
                                if len(handler.documents) > before
                                else None,
                            }
                        )
        summary = []
        for name, _files, _size in cases:
            for change in ("initial", "unchanged", "one_file_edit"):
                selected = [
                    item
                    for item in results
                    if item["case"] == name and item["change"] == change
                ]
                durations = sorted(item["elapsed_seconds"] for item in selected)
                summary.append(
                    {
                        "case": name,
                        "change": change,
                        "p50_seconds": statistics.median(durations),
                        "p95_seconds": durations[math.ceil(len(durations) * 0.95) - 1],
                        "samples": samples,
                        "transferred_payload_bytes": selected[0][
                            "transferred_payload_bytes"
                        ],
                    }
                )
        return {
            "schema_version": 1,
            "transport": "simulated_docker_tar_in_memory",
            "native": False,
            "summary": summary,
            "results": results,
        }
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, choices=range(1, 11), default=5)
    parser.add_argument("--scratch-parent")
    parser.add_argument("--case", choices=[item[0] for item in CASES])
    args = parser.parse_args()
    print(
        json.dumps(
            benchmark(args.samples, args.scratch_parent, args.case),
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
