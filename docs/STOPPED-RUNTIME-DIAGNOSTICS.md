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

The historical implementation branch `feat/nexus-closed-log-retention-20261023`,
head `3dddbc2`, is preserved unchanged. Its Nexus closed-file retention helper
and runtime recipe remain unmerged. The bounded Docker output-rotation slice
from its second commit is now integrated independently for **new** managed
containers, as described below; this does not change or repin an existing runtime.

The metadata-only endpoint collects **no log bodies**, arbitrary artifacts,
native crash reports or stack traces, and does not create a retained capture.
The explicit capture workflow below adds bounded retention and optional exact
deployment association. Those captures are Web-administrator-only; ordinary
target inspection grants do not confer agent access to retained bodies.
Original native crash causality remains unknown. Do not use the metadata read
alone to justify profile disposal or claim that #50, Fresh Test, version switching
or native/platform acceptance is complete.

## Select and retain stopped log excerpts

The Web administrator can open `/instances/{id}/diagnostics` to save an exact
stopped test, and `/diagnostics` to review retained captures. Ordinary forms
carry a durable request ID: repeating the same form returns that capture and
does not repeat the read. The JSON equivalent is
`POST /api/instances/{id}/diagnostic-captures`, with a request UUID, displayed
expected runtime/container, optional exact deployment operation UUID, and
`retain_redacted_logs: true`. Authentication, same-origin and CSRF checks apply.
No MCP tool, preset or grant is added.

The broker reads the four fixed internal files already covered by the
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

The fifth artifact is a fixed Docker stdout/stderr window: at most 200 lines,
timestamps included, without following the stream. The encoded response is
bounded to 64 KiB before strict non-TTY multiplex decoding (or a bounded raw
TTY response), full-window redaction and a 4 KiB retained tail. Oversized,
malformed or unavailable responses return an explicit artifact state without
retaining an unsafe partial body. Docker's `source_size_bytes` counts the
decoded returned window, not all historical output. A bounded window does not
establish rotation, full-history coverage or crash cause. Capture itself changes no logging driver or
rotation setting. Schema-2 captures include all five fixed artifact
states; schema-1 captures remain readable with their Docker output explicitly
uncollected. A reader that supports schema 2 is required for those new bodies.

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
transaction. The unchanged 2 GiB storage reserve applies before admission to
both the configured data root and the actual SQLite database filesystem. These
are logical admission limits, not a physical database-file quota.
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
Known broker failures use fixed reason codes; raw exception messages never
become saved diagnostic error text. Retention and discard acknowledgements
require actual JSON booleans, not coercible numbers or strings.

General native crash-report discovery and report bodies remain uncollected.
Their availability must not be inferred from empty/missing excerpts. Fixed
bounded capture is not native crash
causality, symbolication, complete #50 acceptance or permission for Fresh/Switch.

## Metadata for explicitly referenced crash reports

Schema-3 snapshots can retain file metadata for at most two approved report names
explicitly quoted by complete retained log lines. The official
[Omega 21.3 launcher](https://github.com/xbmc/xbmc/blob/21.3-Omega/tools/Linux/kodi.sh.in)
and [Nexus 20.5 launcher](https://github.com/xbmc/xbmc/blob/20.5-Nexus/tools/Linux/kodi.sh.in)
use a timestamped report filename under `CRASHLOG_DIR`, defaulting to home, and
print its location. That upstream behavior motivates this narrow allowlist; it
does not prove an installed wrapper, an existing report, native fault ownership
or stack availability on a particular runtime/platform.

Recognize only the exact launcher phrase and
`/home/kodi/kodi_crashlog-YYYYMMDD_HHMMSS.log`, with a valid calendar timestamp.
Optional Docker timestamp prefixes are accepted. Other paths, names, suffixes,
invalid dates, partial first lines and references outside the retained excerpts
do not cause reads. The parser prefers the end of the bounded Docker/internal
stdout windows, then other approved logs, deduplicating sources. This selection
does not establish cross-file event ordering or a filename timestamp timezone.
More than two references sets an explicit discovery-truncated flag.

Only fixed HEAD path-stat requests check the private home ancestors and exact
derived leaf twice. Unsafe link/type/mode, missing, unavailable and changed
states remain distinct. Available metadata includes observed byte size and
modification time where available. A large file can have metadata even though
its body is not read. The same current disposable identity, runtime/image pin,
private layout, time budget and final unchanged-container check still apply.
No directory listing/archive, caller-selected path, report-body download,
symbolication, helper execution or installed-byte change is introduced.

The relationship is **log-reported reference**. The log can be wrong or forged;
metadata does not authenticate native crash origin or prove that a usable stack
exists. Report bodies remain `not_collected`; unreferenced reports remain
`not_observed`. An empty result means no approved reference in these excerpts,
not absence of reports. Independent file observations are not a consistent
diagnostic bundle. Selected metadata survives profile removal with its original
capture identity/digest under the same Web-admin retention/access policy.

Schema-1 and schema-2 captures remain readable and explicitly state that report
metadata was not collected. Schema 3 requires a matching reader/broker; older
readers must not be used to interpret new payloads. The payload byte/count/history
limits and reviewed discard/evidence holds remain unchanged. No MCP authority,
agent body access, rotation policy, runtime recipe or existing Kodi resource is
changed. Source/HTTP/socket fixtures remain distinct from native acceptance.

## Native crash triage and missing stack evidence

Start with the exact runtime revision, immutable image/container identity and
an explicitly retained capture. Preserve its payload digest, observation time,
artifact availability/truncation states and any selected deployment association.
Use the retained terminal deployment and lifecycle receipts to establish which
operations were requested and which outcomes were actually observed. Do not
assign crash ownership from a nearby warning, the add-on's version, or timing
alone. A same-container association is not an operation-specific log window.

Keep these conclusions separate:

| Evidence | Supported conclusion | Unestablished conclusion |
| --- | --- | --- |
| Exact Docker `oom_killed` observation | Docker reported that OOM flag for this container observation | Native stack or faulty component |
| Exit 139 | Observed numeric exit status | Authenticated native crash report, stack, or root cause |
| Exit 143 | Observed numeric exit status | A normal Lab-requested stop without matching operation evidence |
| Referenced report metadata is available | An approved log reference resolved to a safely inspected file | Report body, origin, usable stack, or available debug symbols |
| Report body is `not_collected` | This capture contains no report body | The report is empty or the runtime lacks a debugger |
| Missing/oversized/unavailable artifact | The stated capture limit or availability condition | Absence of an earlier crash or complete historical coverage |

The existing capture interface does not export report bodies, cores or arbitrary
files. For a separately authorized investigation, first scope and preserve the
specific debug artifact through an approved evidence export. Do not turn the
metadata endpoint into a caller-selected file reader. Raw reports/cores can
contain private paths, process memory and secrets; their authority, redaction,
byte limits, retention and sharing require separate review.

Prefer an isolated analysis environment for symbolication. Record artifact
hashes, architecture, exact Kodi/package build and matching executable/shared
library identities before selecting debugger/symbol packages. Review the
provider's symbol availability, package trust and redistribution/licensing terms
for that exact build. If matching artifacts or symbols are unavailable, record
the missing prerequisite; a symbolized trace from another build is not evidence
about this runtime. This is a proposed investigation strategy, not an implemented
debug-artifact acquisition or symbolication service.

Do not install a permanent debugger into ordinary runtimes, repin an active
image, manufacture a crash, restart a failed instance or discard its profile to
obtain a stronger diagnosis. Any debugger execution, raw export, disposable
debug runtime or native reproduction needs its own justified scope. Existing
fixed capture and retention remain available without those actions.

## Remaining agent and platform acceptance

The current retained-body policy deliberately admits authenticated Web
administrators. A target's MCP inspection grant and live `log_read` capability
do not authorize stopped-runtime retained-body access. Adding agent access needs
an explicit decision about metadata versus body permissions, retained-record
ownership after target retirement, current/revoked grants, privacy and output
bounds. No new MCP tool, inherited grant or automatic body export follows from
the capture feature or this runbook.

Native crash-report availability, a usable original stack, Windows/WSL2 behavior
and agent-facing stopped diagnostics remain separate acceptance/design gaps.
The preserved log-rotation/closed-retention branch is also separate work; this
runbook neither merges it nor changes existing runtime recipes or logging policy.

## Docker output retention for new managed containers

New managed container creation uses the existing Docker `json-file` driver with
fixed `max-size=1m` and `max-file=2` options. The broker chooses these settings;
callers cannot provide logging options or policy labels. It verifies the actual
Engine configuration after creation before reporting success. This bounds normal
rotated Docker output; it does not bound Kodi's internal profile logs, crash
reports, disk allocation or an individual oversized log record. See Docker's
[JSON-file logging options](https://docs.docker.com/engine/logging/drivers/json-file/).

A versioned container label preserves the verification requirement on retries,
including after uncertain rollback. An absent or different configuration on a
marked container refuses provisioning; it never silently adopts an unbounded
new object. Failed creation retains the existing exact-ID ownership checks for
rollback, preserving uncertain objects rather than removing an unproven identity.

Existing unmarked containers keep their logging configuration and ordinary
idempotent identity checks. No daemon-wide settings, running services, existing
profiles or images are changed. Older output can rotate away; explicitly capture
needed bounded diagnostics before profile disposal. A retained excerpt remains
a partial historical window, not a full startup record or crash diagnosis.

The Nexus internal-file helper remains separate work requiring exact-runtime
qualification; it is not supplied by Docker output rotation. Native Linux Engine
verification of the new settings and bounded synthetic output is separate from
Kodi application behavior and from untested Windows/Docker Desktop acceptance.

### Isolated Linux Engine evidence (2026-10-29)

A new stopped managed container on cached approved Omega r4 image
`sha256:8df4cbf1988514ac9713fab7702b0d5f04483f2a167ea95507d9ade66032403b`
proved the actual Engine logging configuration and idempotent same-container
readback, then was retired without starting Kodi. A separate sequential,
network-none, read-only helper on that same image wrote 4,194,304 bytes of
synthetic records. Docker retained 1,383,466 decoded stream bytes: the oldest
marker was absent and recent stdout/stderr markers were present. The exact
helper was stopped and retired after preserving generated output and receipts.
This is local Engine rotation evidence, not a disk-allocation ceiling, Kodi
functional test or Windows/Desktop qualification. An earlier harness incorrectly
rejected inherited image labels before helper startup; its failure and exact-ID
retirement evidence remain separate from the corrected successful procedure.
Host-local evidence is retained under
`/home/kyle/kodi-lab-deployment/open-issue-campaign-20261029/native/50-engine-final/`.
