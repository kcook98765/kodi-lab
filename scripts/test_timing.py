"""Small, value-free, crash-resilient pytest timing journal (test tooling only)."""

from __future__ import annotations

import hashlib
import json
import os
import re
import resource
import subprocess
import time
from contextlib import suppress
from pathlib import Path
from typing import Any
from uuid import uuid4

MAX_BYTES = 64 * 1024 * 1024


def safe_id(value: str) -> str:
    """Never copy parameter values or arbitrary absolute paths into the journal."""
    value = value.split("[", 1)[0]
    if (
        len(value) > 512
        or value.startswith("/")
        or ".." in value.split("/")
        or re.fullmatch(r"[a-zA-Z0-9_./:-]+", value) is None
    ):
        return "redacted"
    return value


def source_identity(root: Path) -> dict[str, str | bool | None]:
    """Persist source identity, never source contents, paths or credentials."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD", "HEAD^{tree}"],
            cwd=root,
            capture_output=True,
            timeout=5,
            check=True,
        )
        commit, tree = result.stdout.decode().splitlines()
        diff = subprocess.run(
            ["git", "diff", "HEAD", "--", "."],
            cwd=root,
            capture_output=True,
            timeout=5,
            check=True,
        ).stdout
        untracked = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard", "-z"],
            cwd=root,
            capture_output=True,
            timeout=5,
            check=True,
        ).stdout
        return {
            "commit": commit,
            "tree": tree,
            "tracked_diff_sha256": hashlib.sha256(diff).hexdigest(),
            "untracked_files_present": bool(untracked),
            "identity_complete": not diff and not untracked,
        }
    except (OSError, ValueError, subprocess.SubprocessError):
        return {
            "commit": None,
            "tree": None,
            "tracked_diff_sha256": None,
            "untracked_files_present": None,
            "identity_complete": False,
        }


def resources(scratch: Path) -> dict[str, int | str | bool]:
    result: dict[str, int | str | bool] = {
        "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
    }
    with suppress(OSError):
        for line in Path("/proc/self/status").read_text().splitlines():
            key, _, value = line.partition(":")
            if key in {"VmRSS", "Threads"}:
                result[{"VmRSS": "rss_kib", "Threads": "threads"}[key]] = int(
                    value.split()[0]
                )
        result["fds"] = len(os.listdir("/proc/self/fd"))
    with suppress(OSError):
        for line in Path("/proc/self/io").read_text().splitlines():
            key, _, value = line.partition(":")
            if key in {"read_bytes", "write_bytes"}:
                result[key] = int(value)
    with suppress(OSError):
        info = os.statvfs(scratch)
        result["scratch_free_bytes"] = info.f_bavail * info.f_frsize
    return result


class TimingJournal:
    def __init__(self, path: Path, root: Path, scratch: Path) -> None:
        self.started = time.monotonic()
        self.scratch = scratch
        self.size = 0
        self.ordinal = 0
        self.run_id = str(uuid4())
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
        self.stream = os.fdopen(fd, "w", buffering=1)
        self.emit("session_start", source=source_identity(root))

    def emit(self, event: str, **values: Any) -> None:
        body = (
            json.dumps(
                {
                    "schema_version": 1,
                    "run_id": self.run_id,
                    "event": event,
                    "elapsed_seconds": time.monotonic() - self.started,
                    **values,
                },
                allow_nan=False,
                separators=(",", ":"),
            )
            + "\n"
        )
        self.size += len(body.encode())
        if len(body) > 16384 or self.size > MAX_BYTES:
            raise RuntimeError("pytest timing journal exceeded its bounded size")
        self.stream.write(body)

    def start_case(self, nodeid: str) -> None:
        self.ordinal += 1
        self.emit("case_start", ordinal=self.ordinal, nodeid=safe_id(nodeid))

    def complete_case(self, nodeid: str, case: dict[str, Any]) -> None:
        phases = case["phases"]
        outcomes = {detail["outcome"] for detail in phases.values()}
        outcome = (
            "failed"
            if "failed" in outcomes
            else "skipped"
            if "skipped" in outcomes
            else "passed"
        )
        self.emit(
            "case_complete",
            ordinal=self.ordinal,
            nodeid=safe_id(nodeid),
            outcome=outcome,
            phases=phases,
            cleanup=case.get("cleanup", "no_scratch"),
            orm_cache=case.get("orm_cache"),
            resources=resources(self.scratch),
        )

    def finish(self, exitstatus: int) -> None:
        self.emit("session_finish", exit_status=int(exitstatus))
        self.stream.close()
