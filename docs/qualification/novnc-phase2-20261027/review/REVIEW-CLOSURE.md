**No remaining P1/P2 findings identified within the requested scope.**

Static review of the current source confirms:

- **Native Stop recovery:** `runtime_lifecycle.py:285–330` holds the shared dispatch flock around the lifecycle operation, checks the returned instance/container IDs and `STOPPED` enum, then retires both remote and human-viewer ownership. `novnc_control.py:191–203` conditionally deletes only ownership matching that container. The default-app orphan-ownership regression is present at `tests/web/test_novnc_control.py:316–341`.
- **No stale input-mutating proof:** Both the canonical helper and r4 copy use `check_input(release=False)` for `--probe-control`, bounded by default-action `SIGALRM` after one second. The only `release=True` call is in armed-helper cleanup, after child termination and before closing the inherited flock. A disarmed helper with `child=None` skips it.
- **Fail-closed finalization:** Qualification proof requires a successful read-only probe for a running interactive container. Broker finalization/reaping retains unproven ownership and capacity rather than treating lease expiry or disconnect as death proof. Held input after helper SIGKILL therefore remains blocked; exact native Stop is the independent recovery.
- **Human-only boundary:** No new MCP VNC capability, grant, credentials, input, or capture exposure was found in the scoped paths. Qualification remains separate from ordinary launchers; nothing reviewed broadens normal-agent authority.

**Limitations:** Read-only source review only. The regression test was inspected, not executed. No validation-gate PASS level, native Firefox qualification result, or release acceptance is claimed.