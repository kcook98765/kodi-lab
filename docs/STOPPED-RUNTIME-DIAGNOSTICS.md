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

## Select and retain internal log excerpts

The Web administrator can open `/instances/{id}/diagnostics` to save an exact
stopped test, and `/diagnostics` to review retained captures. Ordinary forms
carry a durable request ID: repeating the same form returns that capture and
does not repeat the read. The JSON equivalent is
`POST /api/instances/{id}/diagnostic-captures`, with a request UUID, displayed
expected runtime/container, optional exact deployment operation UUID, and
`retain_redacted_logs: true`. Authentication, same-origin and CSRF checks apply.
No MCP tool, preset or grant is added.

The broker reads only the four fixed internal files already covered by the
preserved log-retention branch: current/previous Kodi logs, internal
`kodi-stdout.log` and `xvfb.log`. It requires the current disposable target,
immutable image pin, standard UID/GID 1000 layout, an unchanged stopped identity,
and a private profile without configured shared mounts/privileged/host-PID mode.
Every fixed ancestor uses HEAD path-stat; symlink/type/mode/name checks precede
a single-file TAR read. Source files are bounded to 1 MiB each; archives have
an additional fixed 64 KiB header budget. Each returned excerpt is at most 4 KiB
of UTF-8 after redaction of the complete bounded input. Oversized logs return
`too_large` without downloading or trimming them. Missing, unsafe, changed and
unavailable are separate states. No recipe, rotation configuration or existing
file is modified. Archive reads can make the Engine mount its own stopped
filesystem view internally; this is not a new container, caller-selected mount,
process launch or installed-byte mutation.

The installed SDK's generic archive iterator disables socket timeouts. This
reader instead uses fixed raw HTTP endpoints and bounded `read1` calls, per-call
timeouts, an overall observation budget and explicit response closure. The
installed SDK is exercised against a local HTTP fixture as well as the real
privileged broker socket protocol. Those are source/transport fixtures, not
native Docker/Kodi or Windows acceptance. The reviewed
[Moby path-stat implementation](https://github.com/moby/moby/blob/v28.5.1/daemon/containerfs_linux.go)
uses `Lstat` and reports symlink metadata; the
[archive implementation](https://github.com/moby/moby/blob/v28.5.1/daemon/archive_unix.go)
retains its container lock while streaming. No raw Docker option or path is
accepted from a caller.

A capture can name an existing terminal direct managed workspace deployment
only when its target, runtime and original container match exactly. The saved
context includes workspace generation and add-on identity. This establishes
**same-container association after that operation**, not causality or a complete
per-operation log window. An unbound capture remains explicitly unbound; no
latest/nearby deployment is guessed. Retained reconciliation fences remain and
are never cleared or borrowed as mutation authority. Independent file reads do
not establish a transactionally consistent bundle. Exit/OOM remain observations;
cause and unobserved rotation remain unknown.

### Private retention policy

Migration 0081 adds only `managed_diagnostic_captures`, initially empty, after
0080. Historical IDs have no live-resource foreign key, so a selected capture
survives target/runtime/profile removal. Each redacted JSON payload is bounded
to 49,152 UTF-8 bytes in application admission and the actual SQLite constraint.
At most 64 retained/pending bodies and 256 total history rows are admitted;
one global capture can be pending. Claims and capacity share a SQLite writer
transaction. The unchanged 2 GiB storage reserve applies before admission.
Capacity failure never prunes or overwrites previous evidence.

Captures are marked for review after 30 days and remain until explicitly
discarded. There is no automatic expiry, sweep, idle deletion or scheduler.
Body reads are Web-admin-only, non-cacheable and escaped in HTML; bodies never
enter ordinary audit fields. Recognized token/password/userinfo patterns are
redacted before truncation, but arbitrary sensitive values cannot be universally
identified. Review saved excerpts before sharing them. No agent access is
inferred from existing deployment or inspection grants.

Discard requires the exact displayed state and payload digest plus explicit
confirmation/CSRF. The body is removed while identity, digest and terminal
history remain. Abandoning a pending **read** prevents its late result from
publishing. It does not settle an uncertain Docker create, remove a Kodi
resource, clear a lease/fence or authorize profile disposal. Cancellation after
a committed publication cannot erase that receipt. Lost/failed requests remain
inspectable by their durable identity; retries cannot bind the same request ID
to another target/container/operation. Populated capture history refuses a
lossy downgrade; no production migration is performed by this source work.

Native crash-report discovery and Docker's separate stdout/stderr stream are
not collected in this internal-file increment. Their availability must not be
inferred from empty/missing excerpts. Fixed bounded capture is not native crash
causality, symbolication, complete #50 acceptance or permission for Fresh/Switch.
