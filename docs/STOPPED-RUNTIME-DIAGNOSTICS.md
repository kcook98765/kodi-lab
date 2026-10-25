# Inspect stopped-runtime exit metadata

The authenticated Web instance page links to
`GET /api/instances/{id}/stopped-diagnostics`. This initial #50 slice reads only
fixed exit/OOM/start/finish metadata for an exact stopped disposable managed
container. It does not wake Kodi, require a resident Bridge, converge runtime
status, clear reconciliation or create evidence of successful deployment.

The application derives the exact current target/runtime/container binding. The
broker resolves the current immutable artifact independently, checks owner,
canonical name, container, revision and image, requires stable created/exited
state, and repeats inventory/inspection before returning. It rechecks canonical
storage after observation. The typed client also checks response identity. Normal
peer-UID broker authorization and Web authentication remain required; no MCP tool,
permission, preset or grant changes. Inputs accept no path, command or raw Docker
option. Audit fields contain only the target identifier, not diagnostic bodies.

`exit_code`, `oom_killed`, `started_at` and `finished_at` are observed Docker fields.
A never-started/unknown timestamp remains null; timestamps are normalized to UTC.
`observed_at` is the time of inspection. Exit 139 alone does not establish a native
stack trace, root cause, artwork fault or ordering relative to containment. Exit
143 alone does not prove a normal Lab-requested stop. `cause` deliberately remains
unknown. Changing/ambiguous ownership, pin, image, state or metadata refuses the
read; missing backend data is not treated as an empty or healthy runtime.

## Coordination and remaining evidence work

Reviewed the preserved local branch `feat/nexus-closed-log-retention-20261023`,
head `3dddbc2`, with commits `09d847e` (Nexus closed-file retention) and `3dddbc2`
(new-container Docker output rotation). It already bounds four fixed internal
logs at closed lifecycle boundaries and configures newly provisioned marked
containers. This feature neither modifies/publishes that branch nor duplicates
its recipe/helper/logging policy. Existing unmarked containers remain untouched.

This slice collects **no log bodies**, arbitrary artifacts, native crash reports
or stack traces. It does not yet retain evidence after restart/reset/deletion or
bind exit observations to a historical deployment operation. Those features need
bounded fixed-artifact reads, exact operation lineage, private retention and a
reviewed agent-access/redaction policy. Original native crash causality remains
unknown. Do not use this metadata read alone to justify profile disposal or claim
that #50, Fresh Test, version switching or native/platform acceptance is complete.
