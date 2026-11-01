from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from scripts.browser_memory_qualification import (
    IncompleteCollection,
    Memory,
    Point,
    Process,
    TreeSampler,
    baseline,
    census,
    owned_processes,
    read_memory,
    summarize,
    trend,
)


def make_sampler(
    root: Process,
    scan: Callable[[], dict[int, Process]] = census,
    read: Callable[[Process], Memory] = read_memory,
) -> TreeSampler:
    return TreeSampler(root, scan, read, confirm_root=lambda: scan().get(root.pid))


def point(stage: str, at: float, rss: float, pss: float = 450) -> Point:
    return Point(stage, at, rss, rss, pss)


def measurements() -> list[Point]:
    return (
        [point("blank", i, 900) for i in range(15)]
        + [point("neutral", 15 + i, 1015) for i in range(15)]
        + [point("page", 30 + i, 1025) for i in range(15)]
        + [point("module", 45 + i, 1045) for i in range(15)]
        + [point("cycle1-active", 60 + i, 1125) for i in range(15)]
        + [point("cycle1-stopped", 75 + i, 1090) for i in range(15)]
        + [point("exited", 90 + i, 0, 0) for i in range(5)]
    )


def test_fixed_baseline_and_original_failed_gate_remain_visible() -> None:
    result = summarize(measurements())
    assert result["historical"] == {
        "metric": "process-tree /proc/status RSS over final5-of15 blank median",
        "blank_baseline_mib": 900,
        "peak_increment_mib": 225,
        "unchanged_limit_mib": 128,
        "result": "FAIL",
    }
    assert result["proposed"] == {
        "metric": "process-tree smaps RSS over fixed pre-import page median",
        "page_baselines_mib": {"rss_mib": 1025, "pss_mib": 450},
        "peak_increment_mib": 100,
        "provisional_limit_mib": 128,
        "within_provisional_budget": True,
        "formal_acceptance": False,
    }


def test_module_and_transient_peak_are_included_without_rebasing() -> None:
    rows = measurements()
    rows[46] = point("module", 46, 1200)
    result = summarize(rows)
    proposed = result["proposed"]
    assert isinstance(proposed, dict)
    assert proposed["peak_increment_mib"] == 175
    assert proposed["within_provisional_budget"] is False
    stages = result["stages"]
    assert isinstance(stages, dict)
    assert stages["cycle1-stopped"]["metrics"]["rss_mib"]["final5_median_mib"] == 1090


def test_historical_uses_status_rss_and_pss_never_overrides_rss() -> None:
    rows = [
        replace(p, rss_status_mib=p.rss_status_mib + (10 if p.at > 44 else 0))
        for p in measurements()
    ]
    result = summarize(rows)
    historical = result["historical"]
    assert isinstance(historical, dict)
    assert historical["peak_increment_mib"] == 235
    assert historical["result"] == "FAIL"


def test_baseline_final_five_preserves_original_convention() -> None:
    rows = [point("blank", i, 800 + i) for i in range(15)]
    assert baseline(rows, "blank", "rss_mib") == 812
    with pytest.raises(ValueError, match="15 baseline"):
        baseline(rows[:14], "blank", "rss_mib")
    rows += [point("blank", i, 1000) for i in range(15, 25)]
    assert baseline(rows, "blank", "rss_mib") == 812
    with pytest.raises(ValueError, match="14s"):
        baseline([replace(p, at=p.at / 10) for p in rows], "blank", "rss_mib")


def test_reject_missing_reordered_or_nonmonotonic_baseline() -> None:
    partial = summarize([p for p in measurements() if p.stage != "page"])
    assert isinstance(partial["historical"], dict)
    assert partial["historical"]["result"] == "FAIL"
    assert isinstance(partial["proposed"], dict)
    assert partial["proposed"]["status"] == "INCOMPLETE"
    rows = measurements()
    rows[44] = replace(rows[44], stage="module")
    rows[45] = replace(rows[45], stage="page")
    reordered = summarize(rows)
    assert isinstance(reordered["proposed"], dict)
    assert "follow" in str(reordered["proposed"]["reason"])
    with pytest.raises(ValueError, match="increasing"):
        summarize(list(reversed(measurements())))


def test_trend_reports_growth_and_reclamation_separately() -> None:
    rows = [point("motion", i, 1000 + 0.1 * i) for i in range(40)]
    result = trend(rows, "rss_mib")
    assert result["slope_mib_per_second"] == pytest.approx(0.1)
    assert result["median_change_mib"] == pytest.approx(2)
    with pytest.raises(ValueError, match="four"):
        trend(rows[:3], "pss_mib")


ROOT = Process(10, 100, 1, "S", 10)
CHILD = Process(11, 101, 10, "S", 10)
GRANDCHILD = Process(12, 102, 11, "S", 10)
OBSERVER = Process(20, 90, 1, "S")


def memory(process: Process) -> Memory:
    return Memory(process, 1024, 1000, 512, 4096, 1, 1.1)


def test_all_descendants_included_observer_excluded() -> None:
    current = {p.pid: p for p in (ROOT, CHILD, GRANDCHILD, OBSERVER)}
    sampler = make_sampler(ROOT, lambda: current, memory)
    row = sampler.sample("page")
    assert row["rss_mib"] == 3
    assert row["rss_status_mib"] == 3000 / 1024
    assert row["pss_mib"] == 1.5
    assert len(row["processes"]) == 3  # type: ignore[arg-type]
    assert "tree_hwm" not in row


def test_reparented_descendants_retained_after_root_exit_pid_reuse_excluded() -> None:
    known = {ROOT, CHILD, GRANDCHILD}
    orphan = replace(CHILD, ppid=1)
    reused = Process(13, 999, 1, "S", 99)
    known.add(Process(13, 103, 10, "S", 10))
    current = {p.pid: p for p in (orphan, reused, GRANDCHILD, OBSERVER)}
    owned = owned_processes(ROOT, known, current)
    assert {(p.pid, p.start) for p in owned} == {(11, 101), (12, 102)}


def test_reused_root_invalidates_session_ownership() -> None:
    with pytest.raises(RuntimeError, match="root PID reused"):
        owned_processes(ROOT, {ROOT}, {10: replace(ROOT, start=999)})


def test_child_created_during_read_is_included() -> None:
    current = {ROOT.pid: ROOT}

    def growing_read(process: Process) -> Memory:
        current[CHILD.pid] = CHILD
        return memory(process)

    row = make_sampler(ROOT, lambda: dict(current), growing_read).sample("module")
    assert row["rss_mib"] == 2


def test_terminated_process_recorded_and_cleanup_includes_orphans() -> None:
    current = {ROOT.pid: ROOT, CHILD.pid: CHILD}

    def retiring_read(process: Process) -> Memory:
        if process == CHILD:
            current.pop(CHILD.pid)
            raise ProcessLookupError
        return memory(process)

    sampler = make_sampler(ROOT, lambda: dict(current), retiring_read)
    row = sampler.sample("stopped")
    assert row["retired_during_read"] == [(11, 101)]
    current.clear()
    assert sampler.sample("exited", closing=True)["rss_mib"] == 0


def test_unreadable_live_process_fails_instead_of_undercounting() -> None:
    def refused(process: Process) -> Memory:
        raise PermissionError(process.pid)

    with pytest.raises(IncompleteCollection, match=r"11|10") as failure:
        make_sampler(ROOT, lambda: {10: ROOT}, refused).sample("page")
    assert failure.value.receipt["eligible_for_baseline_or_acceptance"] is False


def test_unstable_child_creation_fails_instead_of_undercounting() -> None:
    current = {10: ROOT}

    def growing_census() -> dict[int, Process]:
        pid = max(current) + 1
        current[pid] = Process(pid, 100 + pid, 10, "S", 10)
        return dict(current)

    with pytest.raises(RuntimeError, match="census"):
        make_sampler(ROOT, growing_census, memory).sample("active")


def test_proc_reader_records_both_rss_collectors(tmp_path: Path) -> None:
    folder = tmp_path / "10"
    folder.mkdir()
    fields = ["S", "1"] + ["0"] * 17 + ["100"]
    (folder / "stat").write_text("10 (browser name) " + " ".join(fields))
    (folder / "smaps_rollup").write_text("Rss: 2048 kB\nPss: 1024 kB\n")
    (folder / "status").write_text("VmRSS: 2000 kB\nVmHWM: 3000 kB\n")
    row = read_memory(ROOT, tmp_path)
    assert row.rss_kib == 2048
    assert row.rss_status_kib == 2000
    assert row.pss_kib == 1024
    assert row.hwm_kib == 3000


def test_new_orphan_is_included_through_dedicated_session() -> None:
    current = {ROOT.pid: ROOT, CHILD.pid: CHILD}

    def fork_then_exit(process: Process) -> Memory:
        if process == CHILD:
            current.pop(CHILD.pid)
            current[GRANDCHILD.pid] = replace(GRANDCHILD, ppid=1)
        return memory(process)

    row = make_sampler(ROOT, lambda: dict(current), fork_then_exit).sample("active")
    assert row["rss_mib"] == 3
    assert (12, 102) in row["live_identities"]  # type: ignore[operator]


def test_refuse_browser_without_dedicated_process_session() -> None:
    with pytest.raises(ValueError, match="dedicated"):
        make_sampler(replace(ROOT, session_id=1))


def test_later_blank_sample_cannot_rescue_compressed_first_fifteen() -> None:
    rows = [point("blank", i / 10, 900) for i in range(15)]
    rows.append(point("blank", 25, 900))
    with pytest.raises(ValueError, match="14s"):
        baseline(rows, "blank", "rss_mib")


def test_failed_post_exit_memory_is_in_proposed_peak() -> None:
    rows = measurements()
    rows[-1] = point("exited", rows[-1].at, 1300)
    proposed = summarize(rows)["proposed"]
    assert isinstance(proposed, dict)
    assert proposed["peak_increment_mib"] == 275
    assert proposed["within_provisional_budget"] is False


def test_stale_parent_pid_does_not_claim_unrelated_child() -> None:
    unrelated = Process(90, 1000, CHILD.pid, "S", 90)
    current = {p.pid: p for p in (ROOT, CHILD, unrelated)}
    row = make_sampler(ROOT, lambda: current, memory).sample("page")
    assert row["rss_mib"] == 2
    assert (90, 1000) not in row["live_identities"]  # type: ignore[operator]


def test_zombie_during_read_keeps_exit_evidence_without_live_memory() -> None:
    current = {ROOT.pid: ROOT, CHILD.pid: CHILD}

    def zombie_read(process: Process) -> Memory:
        if process == CHILD:
            current[CHILD.pid] = replace(CHILD, state="Z")
            raise ProcessLookupError
        return memory(process)

    sampler = make_sampler(ROOT, lambda: dict(current), zombie_read)
    row = sampler.sample("closing")
    assert row["rss_mib"] == 1
    assert row["terminal_identities"] == [(11, 101)]
    current.pop(ROOT.pid)
    row = sampler.sample("exited", closing=True)
    assert row["rss_mib"] == 0
    assert row["live_identities"] == []
    assert row["terminal_identities"] == [(11, 101)]


def test_real_reader_exit_between_smaps_and_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "10"
    folder.mkdir()
    fields = ["S", "1", "10", "10"] + ["0"] * 15 + ["100"]
    (folder / "stat").write_text("10 (browser) " + " ".join(fields))
    (folder / "smaps_rollup").write_text("Rss: 2048 kB\nPss: 1024 kB\n")
    (folder / "status").write_text("Name: browser\nState: Z (zombie)\n")
    original = Path.read_text

    def racing_read(path: Path, *args: object, **kwargs: object) -> str:
        if path == folder / "status":
            fields[0] = "Z"
            (folder / "stat").write_text("10 (browser) " + " ".join(fields))
        return original(path)  # all fixture reads use the default encoding

    monkeypatch.setattr(Path, "read_text", racing_read)
    row = read_memory(ROOT, tmp_path)
    assert row.process.state == "Z"
    assert row.rss_kib == 2048
    assert row.pss_kib == 1024
    assert row.rss_status_kib == row.hwm_kib == 0


@pytest.mark.parametrize("disappearing", ["status", "final-stat"])
def test_disappearance_after_smaps_invalidates_sample(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, disappearing: str
) -> None:
    folder = tmp_path / "10"
    folder.mkdir()
    fields = ["S", "1", "10", "10"] + ["0"] * 15 + ["100"]
    (folder / "stat").write_text("10 (browser) " + " ".join(fields))
    (folder / "smaps_rollup").write_text("Rss: 2048 kB\nPss: 1024 kB\n")
    (folder / "status").write_text("VmRSS: 2048 kB\nVmHWM: 4096 kB\n")
    original = Path.read_text

    def racing_read(path: Path, *args: object, **kwargs: object) -> str:
        text = original(path)
        if path == folder / "smaps_rollup":
            missing = "status" if disappearing == "status" else "stat"
            (folder / missing).unlink()
        return text

    monkeypatch.setattr(Path, "read_text", racing_read)
    with pytest.raises(IncompleteCollection, match="sample is incomplete") as failure:
        make_sampler(
            ROOT, lambda: {10: ROOT}, lambda p: read_memory(p, tmp_path)
        ).sample("closing")
    receipt = failure.value.receipt
    partial = receipt["unfenced_partial_process_read"]
    assert isinstance(partial, dict)
    fields_read = partial["fields_kib"]
    assert isinstance(fields_read, dict)
    assert fields_read["Rss"] == 2048 and fields_read["Pss"] == 1024
    if disappearing == "final-stat":
        assert fields_read["VmRSS"] == 2048 and fields_read["VmHWM"] == 4096
    assert partial["identity_validated_after_read"] is False


def test_unknown_numeric_session_after_root_exit_is_ambiguous() -> None:
    unrelated = Process(90, 1000, 1, "S", 10)
    with pytest.raises(RuntimeError, match="ambiguous"):
        owned_processes(ROOT, {ROOT}, {90: unrelated})


def test_root_disappearing_during_census_does_not_commit_new_ownership() -> None:
    current = {10: ROOT, 11: CHILD}
    checks = iter([ROOT, None])
    sampler = TreeSampler(
        ROOT, lambda: current, memory, confirm_root=lambda: next(checks)
    )
    with pytest.raises(RuntimeError, match="ownership unverified"):
        sampler.sample("page")
    assert sampler.known == {ROOT}


@pytest.mark.parametrize("root", [None, replace(ROOT, state="Z")])
def test_unexpected_root_exit_without_new_children_fails(root: Process | None) -> None:
    current = {} if root is None else {root.pid: root}
    sampler = TreeSampler(ROOT, lambda: current, memory, confirm_root=lambda: root)
    with pytest.raises(RuntimeError, match="unexpectedly exited"):
        sampler.sample("cycle1-idle")
    assert sampler.sample("exited", closing=True)["live_identities"] == []


def test_incomplete_page_preserves_computable_historical_result() -> None:
    rows = [point("blank", i, 900) for i in range(15)]
    rows += [point("page", 15 + i, 1040) for i in range(3)]
    result = summarize(rows)
    assert isinstance(result["historical"], dict)
    assert result["historical"]["peak_increment_mib"] == 140
    assert result["historical"]["result"] == "FAIL"
    assert isinstance(result["proposed"], dict)
    assert result["proposed"]["status"] == "INCOMPLETE"


def test_normal_sampling_jitter_preserves_original_blank_median() -> None:
    rows = [point("blank", i + (0.02 if i == 0 else 0), 900 + i) for i in range(15)]
    assert rows[-1].at - rows[0].at == pytest.approx(13.98)
    assert baseline(rows, "blank", "rss_mib") == 912


def test_unstable_census_preserves_successful_process_reads() -> None:
    current = {10: ROOT}

    def scan() -> dict[int, Process]:
        pid = max(current) + 1
        current[pid] = Process(pid, 100 + pid, 10, "S", 10)
        return dict(current)

    with pytest.raises(IncompleteCollection) as failure:
        make_sampler(ROOT, scan, memory).sample("active")
    receipt = failure.value.receipt
    assert receipt["complete"] is False
    assert receipt["eligible_for_baseline_or_acceptance"] is False
    assert receipt["processes"]
    assert float(str(receipt["observed_subset_rss_sum_mib"])) >= 1
