"""Qualification-only browser accounting; no runtime or admission policy changes.

Keep the blank-to-peak historical RSS gate alongside proposed page-relative
measurements. A sampled peak is not an operating-system allocation high-water mark.
"""

from __future__ import annotations

import statistics
import time
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import asdict, dataclass
from itertools import pairwise
from pathlib import Path

MIB = 1024
HISTORICAL_RSS_LIMIT_MIB = 128
BASELINE_MIN_SPAN_SECONDS = 13.5  # Nominal 14 s, allowing bounded 1 Hz jitter.


@dataclass(frozen=True)
class Process:
    pid: int
    start: int
    ppid: int
    state: str
    session_id: int = 0


@dataclass(frozen=True)
class Memory:
    process: Process
    rss_kib: int
    rss_status_kib: int
    pss_kib: int
    hwm_kib: int
    started: float
    ended: float


class IncompleteRead(RuntimeError):
    """Retain observed fields without claiming a successful identity fence."""

    def __init__(
        self, reason: str, process: Process, fields: dict[str, int], started: float
    ) -> None:
        super().__init__(reason)
        self.receipt: dict[str, object] = {
            "process": asdict(process),
            "fields_kib": fields,
            "started": started,
            "ended": time.monotonic(),
            "identity_validated_after_read": False,
        }


class IncompleteCollection(RuntimeError):
    """A failed interval's observations, ineligible for baseline or acceptance."""

    def __init__(self, reason: str, receipt: dict[str, object]) -> None:
        super().__init__(reason)
        self.receipt = receipt


def identity(pid: int, proc: Path = Path("/proc")) -> Process:
    raw = (proc / str(pid) / "stat").read_text()
    fields = raw[raw.rfind(")") + 2 :].split()
    return Process(pid, int(fields[19]), int(fields[1]), fields[0], int(fields[3]))


def census(proc: Path = Path("/proc")) -> dict[int, Process]:
    result = {}
    for path in proc.iterdir():
        if path.name.isdigit():
            with suppress(FileNotFoundError, ProcessLookupError):
                result[int(path.name)] = identity(int(path.name), proc)
    return result


def owned_processes(
    root: Process, known: set[Process], current: dict[int, Process]
) -> set[Process]:
    """Retain observed descendants after reparenting; fence every PID reuse."""
    if root.pid in current and current[root.pid].start != root.start:
        raise RuntimeError("Browser root PID reused; session ownership is ambiguous")
    owned = {
        current[p.pid]
        for p in known | {root}
        if p.pid in current and current[p.pid].start == p.start
    }
    # Launch Firefox as a dedicated session leader (Node spawn detached:true).
    # Session membership survives parent exit, including a child first observed
    # after reparenting. External Node/Python observers stay in another session.
    if root.session_id == root.pid:
        candidates = {
            p
            for p in current.values()
            if p.session_id == root.session_id and p.start >= root.start
        }
        known_keys = {(p.pid, p.start) for p in known | {root}}
        if root.pid not in current and any(
            (p.pid, p.start) not in known_keys for p in candidates
        ):
            raise RuntimeError("Unobserved session member after root exit is ambiguous")
        owned |= candidates
    # Do not establish new ownership from a bare parent PID: a non-atomic census
    # can span parent exit and reuse. Dedicated-session membership establishes
    # new ownership; previously observed identities remain fenced after setsid.
    return owned


def read_memory(process: Process, proc: Path = Path("/proc")) -> Memory:
    started = time.monotonic()
    path = proc / str(process.pid)
    if identity(process.pid, proc).start != process.start:
        raise ProcessLookupError("PID reused before memory read")
    smaps = (path / "smaps_rollup").read_text()
    partial = {
        line.split(":", 1)[0]: int(line.split()[1])
        for line in smaps.splitlines()
        if line.startswith(("Rss:", "Pss:"))
    }
    status = None
    try:
        status = (path / "status").read_text()
        after = identity(process.pid, proc)
    except (FileNotFoundError, ProcessLookupError) as error:
        if status is not None:
            partial.update(
                {
                    line.split(":", 1)[0]: int(line.split()[1])
                    for line in status.splitlines()
                    if line.startswith(("VmRSS:", "VmHWM:"))
                }
            )
        raise IncompleteRead(
            "Process retired after smaps read; sample is incomplete",
            process,
            partial,
            started,
        ) from error
    fields = {
        line.split(":", 1)[0]: int(line.split()[1])
        for line in (smaps + "\n" + status).splitlines()
        if line.startswith(("Rss:", "Pss:", "VmRSS:", "VmHWM:"))
    }
    if after.start != process.start:
        raise IncompleteRead(
            "PID reused after smaps read; sample is incomplete",
            process,
            fields,
            started,
        )
    required = {"Rss", "VmRSS", "Pss", "VmHWM"}
    if not required <= fields.keys():
        if after.state not in ("Z", "X"):
            raise RuntimeError("Memory fields missing from a live browser process")
        # Preserve any successful pre-exit smaps reads; absent status fields
        # belong to the now-verified terminal identity with no address space.
        fields = {key: fields.get(key, 0) for key in required}
    return Memory(
        after,
        fields["Rss"],
        fields["VmRSS"],
        fields["Pss"],
        fields["VmHWM"],
        started,
        time.monotonic(),
    )


class TreeSampler:
    """One serial sampler per browser, including cleanup after its root exits.

    Permission failures are fatal. If child creation prevents a complete bounded
    census, reject the sample instead of treating missing live memory as zero.
    Processes born and retired entirely between samples remain a sampling limit.
    """

    def __init__(
        self,
        root: Process,
        scan: Callable[[], dict[int, Process]] = census,
        read: Callable[[Process], Memory] = read_memory,
        confirm_root: Callable[[], Process | None] | None = None,
    ) -> None:
        if root.session_id != root.pid:
            raise ValueError("Firefox must own a dedicated process session")
        self.root = root
        self.known: set[Process] = {root}
        self.scan = scan
        self.read = read
        self.confirm_root = confirm_root or self._current_root
        self._partial_rows: dict[tuple[int, int], Memory] = {}

    def _current_root(self) -> Process | None:
        try:
            return identity(self.root.pid)
        except (FileNotFoundError, ProcessLookupError):
            return None

    def _remember(self, processes: set[Process]) -> None:
        known_keys = {(p.pid, p.start) for p in self.known}
        if any((p.pid, p.start) not in known_keys for p in processes):
            current = self.confirm_root()
            if current is None or current.start != self.root.start:
                raise RuntimeError("New process ownership unverified after root exit")
        self.known |= processes

    def _require_live_root(self) -> None:
        current = self.confirm_root()
        if (
            current is None
            or current.start != self.root.start
            or current.state in ("Z", "X")
        ):
            raise RuntimeError("Browser root unexpectedly exited during measurement")

    def sample(self, stage: str, *, closing: bool = False) -> dict[str, object]:
        started, wall = time.monotonic(), time.time()
        self._partial_rows = {}
        try:
            return self._collect(stage, closing=closing)
        except Exception as error:
            reads = list(self._partial_rows.values())
            receipt: dict[str, object] = {
                "stage": stage,
                "complete": False,
                "started": started,
                "ended": time.monotonic(),
                "wall_time": wall,
                "failure": repr(error),
                "processes": [asdict(row) for row in reads],
                "observed_subset_rss_sum_mib": sum(r.rss_kib for r in reads) / MIB,
                "observed_subset_rss_status_sum_mib": sum(
                    r.rss_status_kib for r in reads
                )
                / MIB,
                "observed_subset_pss_sum_mib": sum(r.pss_kib for r in reads) / MIB,
                "eligible_for_baseline_or_acceptance": False,
            }
            if isinstance(error, IncompleteRead):
                receipt["unfenced_partial_process_read"] = error.receipt
            raise IncompleteCollection(str(error), receipt) from error
        finally:
            self._partial_rows = {}

    def _collect(self, stage: str, *, closing: bool) -> dict[str, object]:
        started = time.monotonic()
        wall = time.time()
        if not closing:
            self._require_live_root()
        rows: dict[tuple[int, int], Memory] = {}
        self._partial_rows = rows
        retired: set[tuple[int, int]] = set()
        for _ in range(4):
            before = owned_processes(self.root, self.known, self.scan())
            self._remember(before)
            for process in before:
                key = process.pid, process.start
                if key in rows:
                    continue
                if process.state in ("Z", "X"):
                    now = time.monotonic()
                    rows[key] = Memory(process, 0, 0, 0, 0, now, now)
                    retired.add(key)
                    continue
                try:
                    rows[key] = self.read(process)
                except (FileNotFoundError, ProcessLookupError):
                    retired.add(key)
            after = owned_processes(self.root, self.known, self.scan())
            self._remember(after)
            terminal = {p for p in after if p.state in ("Z", "X")}
            for process in terminal:
                key = process.pid, process.start
                if key not in rows:
                    now = time.monotonic()
                    rows[key] = Memory(process, 0, 0, 0, 0, now, now)
                    retired.add(key)
            if all((p.pid, p.start) in rows for p in after):
                if not closing:
                    self._require_live_root()
                live = {(p.pid, p.start) for p in after - terminal}
                retired |= set(rows) - live
                # Rows read before a process retired still contribute to this
                # interval. Report the read interval, not an atomic-snapshot claim.
                return {
                    "stage": stage,
                    "wall_time": wall,
                    "started": started,
                    "ended": time.monotonic(),
                    "processes": [asdict(row) for row in rows.values()],
                    "live_identities": sorted(live),
                    "terminal_identities": sorted((p.pid, p.start) for p in terminal),
                    "retired_during_read": sorted(retired),
                    "rss_mib": sum(r.rss_kib for r in rows.values()) / MIB,
                    "rss_status_mib": sum(r.rss_status_kib for r in rows.values())
                    / MIB,
                    "pss_mib": sum(r.pss_kib for r in rows.values()) / MIB,
                }
        raise RuntimeError("Browser child census did not settle during sample")


@dataclass(frozen=True)
class Point:
    stage: str
    at: float
    rss_mib: float
    rss_status_mib: float
    pss_mib: float


def baseline(points: Sequence[Point], stage: str, metric: str) -> float:
    rows = [p for p in points if p.stage == stage]
    duration_window = rows[:15] if stage == "blank" else rows
    if (
        len(rows) < 15
        or duration_window[-1].at - duration_window[0].at < BASELINE_MIN_SPAN_SECONDS
    ):
        raise ValueError(
            f"{stage}: at least 15 baseline samples over nominal 14s "
            "(0.5s jitter tolerance) required"
        )
    window = rows[10:15] if stage == "blank" else rows[-5:]
    return statistics.median(float(getattr(p, metric)) for p in window)


def trend(points: Sequence[Point], metric: str) -> dict[str, float]:
    """OLS slope and half-window median change; no forced reclamation."""
    if len(points) < 4 or points[-1].at <= points[0].at:
        raise ValueError("Trend needs four ordered samples over nonzero time")
    xs = [p.at - points[0].at for p in points]
    ys = [float(getattr(p, metric)) for p in points]
    mx, my = statistics.mean(xs), statistics.mean(ys)
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / sum(
        (x - mx) ** 2 for x in xs
    )
    half = len(ys) // 2
    return {
        "slope_mib_per_second": slope,
        "median_change_mib": statistics.median(ys[-half:])
        - statistics.median(ys[:half]),
    }


def summarize(points: Sequence[Point]) -> dict[str, object]:
    """Partial/failed trials stay partial; no formal threshold approval implied."""
    if not points or any(b.at <= a.at for a, b in pairwise(points)):
        raise ValueError("Samples must have strictly increasing monotonic times")
    blank = baseline(points, "blank", "rss_status_mib")
    historical_peak = max(p.rss_status_mib for p in points) - blank
    historical = {
        "metric": "process-tree /proc/status RSS over final5-of15 blank median",
        "blank_baseline_mib": blank,
        "peak_increment_mib": historical_peak,
        "unchanged_limit_mib": HISTORICAL_RSS_LIMIT_MIB,
        "result": "FAIL" if historical_peak > HISTORICAL_RSS_LIMIT_MIB else "PASS",
    }
    try:
        page = {k: baseline(points, "page", k) for k in ("rss_mib", "pss_mib")}
        page_rows = [p for p in points if p.stage == "page"]
        page_end = page_rows[-1].at
        if any(p.stage == "blank" and p.at >= page_rows[0].at for p in points):
            raise ValueError("Blank baseline must precede the page baseline")
        if any(
            p.stage.startswith(("module", "cycle")) and p.at < page_end for p in points
        ):
            raise ValueError(
                "Module and viewer stages must follow the fixed page baseline"
            )
    except ValueError as error:
        return {
            "historical": historical,
            "proposed": {
                "status": "INCOMPLETE",
                "reason": str(error),
                "formal_acceptance": False,
            },
            "absolute_sampled_peaks_mib": {
                k: max(float(getattr(p, k)) for p in points)
                for k in ("rss_mib", "rss_status_mib", "pss_mib")
            },
        }
    stages: dict[str, object] = {}
    for stage in dict.fromkeys(p.stage for p in points):
        rows = [p for p in points if p.stage == stage]
        stages[stage] = {
            "samples": len(rows),
            "first_monotonic": rows[0].at,
            "last_monotonic": rows[-1].at,
            "metrics": {
                k: {
                    "final5_median_mib": statistics.median(
                        float(getattr(p, k)) for p in rows[-5:]
                    ),
                    "sampled_peak_mib": max(float(getattr(p, k)) for p in rows),
                    "peak_over_fixed_page_mib": max(float(getattr(p, k)) for p in rows)
                    - page[k],
                }
                for k in page
            },
        }
    feature = [p for p in points if p.at > page_end]
    proposed_peak = (
        max((p.rss_mib for p in feature), default=page["rss_mib"]) - page["rss_mib"]
    )
    return {
        "stages": stages,
        "historical": historical,
        "proposed": {
            "metric": "process-tree smaps RSS over fixed pre-import page median",
            "page_baselines_mib": page,
            "peak_increment_mib": proposed_peak,
            "provisional_limit_mib": HISTORICAL_RSS_LIMIT_MIB,
            "within_provisional_budget": proposed_peak <= HISTORICAL_RSS_LIMIT_MIB,
            "formal_acceptance": False,
        },
        "page_baseline_trends": {k: trend(page_rows[-10:], k) for k in page},
    }
