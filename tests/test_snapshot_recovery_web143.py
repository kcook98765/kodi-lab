from __future__ import annotations

import hashlib
import io
import os
import tarfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from kodi_lab import snapshot_recovery_host as host
from kodi_lab.application import snapshot_recovery_maintenance as maintenance
from kodi_lab.application.snapshot_recovery_evidence import RecoveryRefused
from tests.snapshot_recovery_support import Case, ConsumerEngine
from tests.snapshot_recovery_support import factory as factory
from tests.snapshot_recovery_support import recovery_case as recovery_case

WEB, BROKER = "c" * 64, "d" * 64
BOOT = "2026-01-02T00:00:00Z"
BEFORE = (
    b"2026-01-02T00:00:01Z INFO:     Started server process [7]\n"
    b"2026-01-02T00:00:01.1Z INFO:     Waiting for application startup.\n"
    b"2026-01-02T00:00:01.2Z INFO:     Application startup complete.\n"
    b'2026-01-02T00:00:02Z INFO:     127.0.0.1:1234 - "GET /login HTTP/1.1" 200 OK\n'
)
AFTER = (
    b"2026-01-03T00:00:00.1Z INFO:     Shutting down\n"
    b"2026-01-03T00:00:00.12Z INFO:     Waiting for application shutdown.\n"
    b"2026-01-03T00:00:00.15Z INFO:     Application shutdown complete.\n"
    b"2026-01-03T00:00:00.2Z INFO:     Finished server process [7]\n"
)
START = host._utc_ns("2026-01-03T00:00:00.05Z")


class EventStream(Iterator[dict[str, Any]]):
    def __init__(self, entries: list[dict[str, Any]]) -> None:
        self.entries = iter(entries)
        self.closed = False

    def __next__(self) -> dict[str, Any]:
        return next(self.entries)

    def close(self) -> None:
        self.closed = True


class Web143Engine(ConsumerEngine):
    def __init__(self, root: Path, fault: str = "none") -> None:
        super().__init__(root)
        self.fault = fault
        self.records = {
            WEB: self.record(WEB, "web", running=True),
            BROKER: self.record(BROKER, "broker", running=True),
        }
        web = self.records[WEB]
        web.update(Path="/usr/local/bin/kodi-lab-remote-web", Args=[])
        web["Config"].update(
            Entrypoint=[web["Path"]],
            WorkingDir="",
            Env=[
                "PATH=/usr/local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            ],
        )
        web["HostConfig"].update(ReadonlyRootfs=True, Init=True)
        for cid, row in self.records.items():
            row["State"].update(
                Pid=100 if cid == WEB else 300,
                StartedAt=BOOT,
                OOMKilled=False,
                Error="",
                Dead=False,
                Paused=False,
            )
        package = Path(__file__).parents[1] / "src/kodi_lab"
        self.files = {
            "/usr/local/bin/kodi-lab-remote-web": (
                b"#!/bin/sh\nset -eu\numask 077\nexec kodi-lab-admin serve\n"
            ),
            **{
                f"/usr/local/bin/kodi-lab-{name}": (
                    f"#!/usr/local/bin/python\nimport sys\nfrom {module} import main\n"
                    "if __name__ == '__main__':\n    sys.exit(main())\n"
                ).encode()
                for name, module in (
                    ("admin", "kodi_lab.admin_cli"),
                    ("web", "kodi_lab.web.run"),
                )
            },
            "/usr/local/lib/python3.12/site-packages/uvicorn/__init__.py": (
                b"# synthetic reviewed init\n"
            ),
            "/usr/local/lib/python3.12/site-packages/uvicorn/server.py": (
                b"# synthetic reviewed graceful signal-reraise server\n"
            ),
            **{
                f"/usr/local/lib/python3.12/site-packages/kodi_lab/{name}": (
                    package / name
                ).read_bytes()
                for name in ("admin_cli.py", "web/run.py")
            },
        }
        if fault in {"source", "uvicorn_source"}:
            name = "kodi_lab/web/run.py" if fault == "source" else "uvicorn/server.py"
            self.files["/usr/local/lib/python3.12/site-packages/" + name] += (
                b"# changed\n"
            )
        if fault == "console":
            self.files["/usr/local/bin/kodi-lab-web"] += b"print('extra code')\n"
        if fault == "launcher":
            self.files["/usr/local/bin/kodi-lab-remote-web"] += b"exit 143\n"
        if fault == "entrypoint":
            web["Config"]["Entrypoint"] = ["custom-server"]
        if fault == "command":
            web["Config"]["Cmd"] = ["--custom"]
        if fault == "signal":
            web["Config"]["StopSignal"] = "SIGKILL"
        if fault == "environment":
            web["Config"]["Env"].append("PYTHONPATH=/tmp/custom")
        if fault == "code_mount":
            web["Mounts"].append(
                {
                    "Type": "bind",
                    "Source": str(root),
                    "Destination": "/usr/local",
                    "RW": False,
                }
            )
        self.event_stream: EventStream | None = None

    def get_archive(self, _cid: str, path: str) -> tuple[list[bytes], dict[str, Any]]:
        if self.fault in {"metadata_read", "zero_unknown_stack"}:
            raise OSError("synthetic unavailable metadata")
        raw = self.files[path]
        if (
            self.fault == "post_source"
            and WEB in self.stops
            and path.endswith("uvicorn/server.py")
        ):
            raw += b"# changed after Stop\n"
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w") as archive:
            member = tarfile.TarInfo(path.rsplit("/", 1)[1])
            member.size = len(raw)
            archive.addfile(member, io.BytesIO(raw))
        return [output.getvalue()], {}

    def logs(self, cid: str, **kwargs: Any) -> bytes:
        if cid != WEB:
            if self.fault == "broker_final_audit" and BROKER in self.stops:
                return (
                    b'{"event":"broker request completed",'
                    b'"broker_result_code":"response_transport_error"}'
                )
            return super().logs(cid, **kwargs)
        assert kwargs["timestamps"] is True
        before = BEFORE
        if self.fault == "duplicate_start":
            before += b"2026-01-02T00:00:03Z INFO:     Started server process [7]\n"
        if WEB not in self.stops:
            return before
        suffix = AFTER
        if self.fault == "missing_finish":
            suffix = b"\n".join(suffix.splitlines()[:-1]) + b"\n"
        if self.fault == "wrong_finish_pid":
            suffix = suffix.replace(b"[7]", b"[8]")
        if self.fault == "wrong_sequence":
            suffix = b"\n".join(reversed(suffix.splitlines())) + b"\n"
        if self.fault == "shutdown_error":
            suffix = suffix.replace(
                b"INFO:     Application shutdown complete.",
                b"ERROR:    Application shutdown failed.",
            )
        if self.fault == "truncated_log":
            return before + suffix[:-1]
        if self.fault == "changed_prefix":
            before = before.replace(b"GET /login", b"GET /other")
        return before + suffix

    def stop(self, cid: str, *, timeout: int) -> None:
        super().stop(cid, timeout=timeout)
        state = self.records[cid]["State"]
        state["FinishedAt"] = (
            "2026-01-03T00:00:00.3Z" if cid == WEB else "2026-01-03T00:00:05Z"
        )
        state["ExitCode"] = (
            (0 if self.fault in {"zero", "zero_unknown_stack"} else 143)
            if cid == WEB
            else (143 if self.fault == "broker143" else 0)
        )
        if cid == WEB:
            if self.fault == "exit137":
                state["ExitCode"] = 137
            if self.fault == "exit1":
                state["ExitCode"] = 1
            if self.fault in {"oom", "error", "pid", "restart", "dead", "paused"}:
                if self.fault == "restart":
                    self.records[cid]["RestartCount"] += 1
                else:
                    key, value = {
                        "oom": ("OOMKilled", True),
                        "error": ("Error", "uncertain"),
                        "pid": ("Pid", 101),
                        "dead": ("Dead", True),
                        "paused": ("Paused", True),
                    }[self.fault]
                    state[key] = value
            if self.fault == "interrupted":
                raise RuntimeError("synthetic helper interrupted after Web Stop")

    def events(self, **kwargs: Any) -> EventStream:
        assert (
            kwargs["filters"] == {"container": WEB, "type": "container"}
            and kwargs["decode"]
        )
        entries: list[dict[str, Any]] = []
        for offset, action, attributes in (
            (10_000_000, "kill", {"signal": "15"}),
            (350_000_000, "stop", {}),
            (400_000_000, "die", {"exitCode": "143"}),
        ):
            entries.append(
                {
                    "Type": "container",
                    "Action": action,
                    "timeNano": START + offset,
                    "Actor": {"ID": WEB, "Attributes": attributes},
                }
            )
        if self.fault == "forced":
            entries[0]["Actor"]["Attributes"]["signal"] = "9"
        if self.fault == "custom_signal":
            entries[0]["Actor"]["Attributes"]["signal"] = "2"
        if self.fault == "duplicate_signal":
            entries.insert(1, entries[0])
        if self.fault == "missing_event":
            entries.pop()
        if self.fault == "wrong_cid":
            entries[0]["Actor"]["ID"] = "a" * 64
        if self.fault == "event_restart":
            entries[1]["Action"] = "restart"
        if self.fault == "event_exit":
            entries[-1]["Actor"]["Attributes"]["exitCode"] = "137"
        if self.fault == "stale_event":
            entries[0]["timeNano"] = START - 1
        if self.fault == "coarse_noise":
            entries.insert(
                0,
                {
                    "Type": "container",
                    "Action": "exec_die",
                    "timeNano": START - 1,
                    "Actor": {"ID": WEB, "Attributes": {"exitCode": "0"}},
                },
            )
            entries.append(
                {
                    "Type": "container",
                    "Action": "health_status: unhealthy",
                    "timeNano": START + 2_500_000_001,
                    "Actor": {"ID": WEB},
                }
            )
        if self.fault == "inside_healthcheck":
            for index, action in enumerate(
                (
                    "exec_create: python -c readonly",
                    "exec_start: python -c readonly",
                    "exec_die",
                )
            ):
                entries.append(
                    {
                        "Type": "container",
                        "Action": action,
                        "timeNano": START + 20_000_000 + index,
                        "Actor": {"ID": WEB, "Attributes": {"exitCode": "0"}},
                    }
                )
        if self.fault == "die_before_stop":
            entries[1]["timeNano"], entries[2]["timeNano"] = (
                entries[2]["timeNano"],
                entries[1]["timeNano"],
            )
        if self.fault == "post_return_events":
            entries[1]["timeNano"] = START + 750_000_000
            entries[2]["timeNano"] = START + 900_000_000
        self.event_stream = EventStream(entries)
        return self.event_stream


def install_inputs(sdk: Web143Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    sources = {
        name: hashlib.sha256(
            sdk.files["/usr/local/lib/python3.12/site-packages/" + name]
        ).hexdigest()
        for name in maintenance.UVICORN_0530_SOURCES
    }
    # Known synthetic source pin, not a patched rejecting guard. Corrupt-source
    # negatives retain the expected original pin.
    if sdk.fault == "uvicorn_source":
        sources["uvicorn/server.py"] = hashlib.sha256(
            b"# synthetic reviewed graceful signal-reraise server\n"
        ).hexdigest()
    monkeypatch.setattr(maintenance, "UVICORN_0530_SOURCES", sources)
    monkeypatch.setattr(host, "UVICORN_0530_SOURCES", sources)
    monkeypatch.setattr(
        host,
        "_cgroup",
        lambda pid, cid: ("/docker/" + cid, "11" if pid == 101 else "10"),
    )
    monkeypatch.setattr(host, "_empty_cgroup", lambda path: sdk.fault != "kernel_live")
    actual_read, actual_bytes, actual_link = (
        Path.read_text,
        Path.read_bytes,
        os.readlink,
    )

    def read(path: Path, *args: Any, **kwargs: Any) -> str:
        value = str(path)
        if value == "/proc/100/status":
            return "NSpid:\t1\n"
        if value == "/proc/101/status":
            return "NSpid:\t8\n" if sdk.fault == "pid_namespace" else "NSpid:\t7\n"
        if value == "/proc/100/task/100/children":
            return "101"
        if value == "/proc/101/task/101/children":
            return ""
        return actual_read(path, *args, **kwargs)

    def read_bytes(path: Path) -> bytes:
        if str(path) == "/proc/101/cmdline":
            return (
                b"other\0"
                if sdk.fault == "process_command"
                else b"/usr/local/bin/python\0/usr/local/bin/kodi-lab-web\0"
            )
        return actual_bytes(path)

    def link(path: Any, *args: Any, **kwargs: Any) -> str:
        if str(path) == "/proc/101/exe":
            return "/usr/local/bin/python3.12"
        return str(actual_link(path, *args, **kwargs))

    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    monkeypatch.setattr(os, "readlink", link)
    completed_observation = START + 3_000_000_000
    if sdk.fault == "early_observation":
        completed_observation = START + 1_000_000_000
    if sdk.fault == "late_observation":
        completed_observation = START + 11_000_000_000
    observation = (
        [] if sdk.fault in {"zero", "zero_unknown_stack"} else [completed_observation]
    )
    ticks = iter(
        (
            START,
            START + 500_000_000,
            *observation,
            START + 4_000_000_000,
            START + 6_000_000_000,
        )
    )
    monkeypatch.setattr("time.time_ns", lambda: next(ticks))


def plan(case: Case) -> host.InstallationPlan:
    return host.InstallationPlan(
        installation="owned-qa",
        bundle_root=str(case.root),
        compose_file=str(case.root / "compose.yml"),
        engine_id="separate-synthetic-engine",
        state_volume="qa-state",
        web_container_id=WEB,
        broker_container_id=BROKER,
        candidate_package_digest=case.seal.candidate_package_digest,
        review=case.review,
    )


@pytest.mark.parametrize(
    "fault",
    [
        "none",
        "zero",
        "zero_unknown_stack",
        "source",
        "uvicorn_source",
        "console",
        "launcher",
        "entrypoint",
        "command",
        "signal",
        "environment",
        "code_mount",
        "metadata_read",
        "duplicate_start",
        "missing_finish",
        "wrong_finish_pid",
        "wrong_sequence",
        "shutdown_error",
        "truncated_log",
        "changed_prefix",
        "exit137",
        "exit1",
        "oom",
        "error",
        "pid",
        "restart",
        "dead",
        "paused",
        "interrupted",
        "kernel_live",
        "forced",
        "custom_signal",
        "duplicate_signal",
        "missing_event",
        "wrong_cid",
        "event_restart",
        "event_exit",
        "stale_event",
        "pid_namespace",
        "process_command",
        "post_source",
        "broker143",
        "broker_final_audit",
        "coarse_noise",
        "inside_healthcheck",
        "die_before_stop",
        "post_return_events",
        "early_observation",
        "late_observation",
    ],
)
def test_actual_web143_helper_flow_retains_raw_exit_and_refuses_near_misses(
    recovery_case: Case,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    case = recovery_case
    sdk = Web143Engine(case.root, fault)
    install_inputs(sdk, monkeypatch)
    positives = {
        "none",
        "zero",
        "zero_unknown_stack",
        "coarse_noise",
        "inside_healthcheck",
        "die_before_stop",
        "post_return_events",
    }
    if fault in positives:
        seal = host.drain(plan(case), sdk)
        seal.validate_chain()
        assert seal.broker.exit_code == 0 and seal.broker.web_graceful_sigterm is None
        assert sdk.stops == [WEB, BROKER]
        if fault not in {"zero", "zero_unknown_stack"}:
            assert seal.web.exit_code == sdk.records[WEB]["State"]["ExitCode"] == 143
            proof = seal.web.web_graceful_sigterm
            assert (
                proof is not None
                and proof.server_host_pid == 101
                and proof.server_namespace_pid == 7
            )
            assert proof.server_start == "11" and proof.init_start == "10"
            assert proof.stop_completed_ns == START + 500_000_000
            assert proof.events_observation_until_ns == START + 2_500_000_000
            assert proof.events_observation_completed_ns == START + 3_000_000_000
            assert proof.event_proof_scope == "lifecycle_controls_only"
            assert proof.out_of_window_event_records == (
                2 if fault == "coarse_noise" else 0
            )
            assert proof.noncontrol_event_records == (
                3 if fault == "inside_healthcheck" else 0
            )
            if fault == "post_return_events":
                assert proof.stop_event_ns > proof.stop_completed_ns
                assert proof.die_event_ns > proof.stop_completed_ns
            assert (
                type(seal).model_validate_json(seal.model_dump_json()).web.exit_code
                == 143
            )
            assert sdk.event_stream is not None and sdk.event_stream.closed
            malformed = seal.model_dump()
            malformed["broker"] = malformed["web"]
            with pytest.raises(RecoveryRefused):
                type(seal).model_validate(malformed).validate_chain()
            malformed = seal.web.model_dump()
            malformed["web_graceful_sigterm"] = None
            with pytest.raises(ValueError):
                maintenance.ServiceRetirement.model_validate(malformed)
        else:
            assert seal.web.exit_code == 0 and seal.web.web_graceful_sigterm is None
    else:
        with pytest.raises((RecoveryRefused, ValueError, OSError, RuntimeError)):
            host.drain(plan(case), sdk)
        assert BROKER not in sdk.stops or fault in {"broker143", "broker_final_audit"}
        calls = list(sdk.stops)
        # A failed/partial helper has no seal and cannot be resumed by re-entry.
        with pytest.raises(RecoveryRefused):
            host.drain(plan(case), sdk)
        assert sdk.stops == calls
    assert case.provider.calls == []


@pytest.mark.parametrize(
    "change", ["missing", "wrong_pid", "error", "duplicate", "truncated", "before_stop"]
)
def test_web_completion_text_alone_does_not_establish_graceful_retirement(
    change: str,
) -> None:
    final = BEFORE + AFTER
    if change == "missing":
        final = BEFORE
    elif change == "wrong_pid":
        final = final.replace(
            b"Finished server process [7]", b"Finished server process [99]"
        )
    elif change == "error":
        final = BEFORE + b"2026-01-03T00:00:00.01Z ERROR:    interrupted\n" + AFTER
    elif change == "duplicate":
        final += AFTER
    elif change == "truncated":
        final = final[:-1]
    with pytest.raises(RecoveryRefused):
        host._web_shutdown_suffix(
            BEFORE,
            final,
            BOOT,
            7,
            START + (1_000_000_000 if change == "before_stop" else 0),
            START + 300_000_000,
        )


@pytest.mark.parametrize(
    "change",
    [
        "preceding",
        "following",
        "inside_exec",
        "exec_nonzero_not_classified",
        "inside_health",
        "delivery_order",
        "die_before_stop",
        "post_return",
        "inclusive_bounds",
        "duplicate_stop",
        "duplicate_die",
        "duplicate_signal",
        "forced",
        "custom_signal",
        "missing",
        "stale_signal",
        "late_die",
        "wrong_cid",
        "wrong_type",
        "unknown_control",
        "before_signal",
        "malformed_time",
        "boolean_time",
    ],
)
def test_exact_event_window_is_causal_control_only_not_exec_quiescence(
    change: str,
) -> None:
    def event(
        action: str, offset: int, attributes: dict[str, str] | None = None
    ) -> dict[str, Any]:
        return {
            "Type": "container",
            "Action": action,
            "timeNano": START + offset,
            "Actor": {"ID": WEB, "Attributes": attributes or {}},
        }

    events = [
        event("kill", 10, {"signal": "15"}),
        event("stop", 100),
        event("die", 200, {"exitCode": "143"}),
    ]
    outside = noncontrol = 0
    valid = {
        "preceding",
        "following",
        "inside_exec",
        "exec_nonzero_not_classified",
        "inside_health",
        "delivery_order",
        "die_before_stop",
        "post_return",
        "inclusive_bounds",
    }
    if change == "preceding":
        events.insert(0, event("exec_die", -1, {"exitCode": "0"}))
        outside = 1
    elif change == "following":
        events.append(event("exec_die", 1001, {"exitCode": "0"}))
        outside = 1
    elif change in {"inside_exec", "exec_nonzero_not_classified", "inside_health"}:
        events.append(
            event(
                "health_status: healthy" if change == "inside_health" else "exec_die",
                150,
                {"exitCode": "137" if change == "exec_nonzero_not_classified" else "0"},
            )
        )
        noncontrol = 1
    elif change == "delivery_order":
        events.reverse()
    elif change == "die_before_stop":
        events[1]["timeNano"], events[2]["timeNano"] = (
            events[2]["timeNano"],
            events[1]["timeNano"],
        )
    elif change == "post_return":
        # An analytic SDK return at +300 is separate; emissions at +700/+800
        # stay later. The observation ends at +1000, never rewritten to +300.
        events[1]["timeNano"], events[2]["timeNano"] = START + 700, START + 800
    elif change == "inclusive_bounds":
        events[0]["timeNano"], events[2]["timeNano"] = START, START + 1000
    elif change.startswith("duplicate_"):
        events.append(
            events[
                {"duplicate_signal": 0, "duplicate_stop": 1, "duplicate_die": 2}[change]
            ]
        )
    elif change in {"forced", "custom_signal"}:
        events[0]["Actor"]["Attributes"]["signal"] = "9" if change == "forced" else "2"
    elif change == "missing":
        events.pop()
    elif change == "stale_signal":
        events[0]["timeNano"] = START - 1
    elif change == "late_die":
        events[2]["timeNano"] = START + 1001
    elif change == "wrong_cid":
        events[1]["Actor"]["ID"] = BROKER
    elif change == "wrong_type":
        events[1]["Type"] = "network"
    elif change == "unknown_control":
        events.append(event("restart", 160))
    elif change == "before_signal":
        events[1]["timeNano"] = START + 1
    elif change == "malformed_time":
        events[0]["timeNano"] = "unknown"
    elif change == "boolean_time":
        events[0]["timeNano"] = True
    if change in valid:
        result = host._web_stop_events(events, WEB, START, START + 1000)
        assert (
            result.out_of_window_records == outside
            and result.noncontrol_records == noncontrol
        )
        assert result.signal_ns <= min(result.stop_ns, result.die_ns)
        if change == "post_return":
            assert result.stop_ns > START + 300 and result.die_ns > START + 300
    else:
        with pytest.raises(RecoveryRefused, match="recovery_web_stop_events_unproven"):
            host._web_stop_events(events, WEB, START, START + 1000)
