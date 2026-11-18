# Kodi logs over MCP

## Search and count with `log_scan`

`log_scan` is a separate read-only `target.inspect` operation for granted managed,
external development and external production targets. It scans inside Narrow
Bridge, returning compact cumulative counts and bounded matching samples. Existing
`log_read` stats and tail requests keep their original contract below.

Scan both files for ERROR and FATAL records (the defaults):

```json
{"target_id":"<granted target UUID>","logs":"both","severities":["ERROR","FATAL"],"max_matches":10,"context_records":1}
```

When a response has a `cursor`, repeat **the same query**, adding that cursor:

```json
{"target_id":"<same UUID>","logs":"both","severities":["ERROR","FATAL"],"max_matches":10,"context_records":1,"cursor":"<returned cursor>"}
```

Results, counts, and samples are cumulative: replace the previous response;
do not sum pages or concatenate their samples. A cursor is reusable, including
after a lost continuation response. Work advances once under a Bridge lock;
retrying returns current cumulative progress. An initial request whose response
was lost must be restarted. A terminal continuation can be retried while its
state remains retained. Cursors expire **ten minutes after scan creation**, survive
neither a Bridge restart nor a pairing reset, and cannot move to another MCP
client, target, credential, Bridge, selection, or query. Expiry or mismatch returns
`log_cursor_invalid`; restart explicitly. Authorization, revocation, or identity
failures return the existing bounded error without publishing scan data or cursor
progress. They never promote earlier provisional counts to exact counts. The Bridge
retains at most 16 in-memory states, evicting terminal states first. Active-state capacity or concurrent scan
work returns `log_scan_busy`. No log bodies or cursor state are written to disk
or the replay cache; routine MCP audits contain no query text, cursors or bodies.

Filter warnings and errors by an inclusive **Kodi-local wall-clock** interval
and a case-sensitive literal add-on ID:

```json
{"target_id":"<UUID>","logs":"previous","severities":["WARNING","ERROR","FATAL"],"text":"script.channelsdvr.hub","start_time":"2026-11-15T09:00:00","end_time":"2026-11-15T10:30:00","context_records":1}
```

`text` is 1–256 Unicode characters, matched as literal UTF-8 bytes across the
entire logical record, even beyond the returned excerpt. There is no regex or
case-folding. Add-on text is evidence of a mention, **not proof of causality**.
Timestamp inputs require `YYYY-MM-DDTHH:MM:SS` with optional 1–6 fractional digits
and no timezone suffix. No UTC, offset, date for time-only logs, sorting, or
monotonic clock assumption is inferred. Every selected byte is scanned even if
earlier records are later than the end filter. Unknown timestamps are excluded
when either time filter is present and counted in `unknown_timestamp_records`.

### Records, counts and excerpts

A physical LF-delimited line starts a logical record when its first 512 bytes
match the fixed Kodi header grammar: optional full-date or time-only timestamp,
optional `T:<thread>`, severity, optional `<component>`, and `:`. Severity labels
are case-insensitive in logs and normalized to DEBUG, INFO, NOTICE, WARNING,
ERROR, FATAL or SEVERE. Query labels are uppercase; UNKNOWN selects a preamble
without a recognized header. Missing and malformed dates do not prevent a
recognized severity header from starting a record. Other lines, including
tracebacks and blank lines, continue the preceding record; leading unrecognized
lines form an UNKNOWN record. A header-shaped line inside a traceback follows
the same grammar. CRLF is accepted; invalid UTF-8 is replaced in excerpts.

Counts represent **logical records matching all filters**, grouped by severity;
context never increases counts. `records_scanned` counts all finalized records.
A record remains pending until its successor or the fixed boundary is reached.
Parsing and literal matching continue through oversized records in bounded memory.
The fixed boundary ends the final record, including a partial final physical line.

`max_matches` is 0–20 (default 10); zero requests counts only. Each sampled record
has at most 1,024 raw bytes decoded for `content`, plus up to two surrounding
records in each direction (`context_records`, default zero), each clipped to 256
raw bytes. Context may be filled in on a later continuation. `start_byte` and
`end_byte` cover the matching record, not context. Samples favor earlier matches;
they are diagnostic evidence, not a full export. A 32 KiB serialized scan-payload
limit can further reduce samples. `excerpts_truncated` signals omitted matches
or clipped matching-record content; `content_truncated` identifies a clipped
record. Clipping context to 256 bytes is unconditional. MCP carries the same
payload in text and structured content, so its complete wire envelope is larger
than the scan payload; the duplicated tool-result fields remain bounded.

Interpret the result explicitly:

| Result | Meaning |
| --- | --- |
| `complete=true`, `counts_exact=true` | All selected boundaries scanned consistently; counts are exact for those observations. |
| Above, with `excerpts_truncated=true` | Counts are exact; the sample list or record text is incomplete. |
| `complete=false`, `cursor` present | Counts are provisional. Resume, including when zero matches were found. |
| `complete=false`, no cursor | A file failed or changed; the selected scan is incomplete. Inspect per-file states and restart as needed. |

Each file reports opaque `file_id`, UTC metadata observation time, fixed range
`[0,boundary_bytes)`, `scanned_bytes`, `verified_bytes`, `append_observed`, state,
counts and `counts_exact`. Coverage includes bytes in the pending record; counts
include only finalized records. `bytes_read` is the current call's work, including
verification; it must not be summed as unique coverage. Files are independently
observed, **not an atomic two-file snapshot**. A missing, unsafe or unreadable file
does not prevent scanning the other file, but prevents whole-query exactness.

### Budgets and changing files

The Bridge reads at most 64 KiB at once, with independent **8 MiB per call** and
**0.75 second** scan-work budgets, one scan slice concurrently, and 32 KiB scan-payload
limits. This stays below the existing five-second Bridge response timeout under
ordinary local-file operation. OS file-open/read latency cannot be preempted;
transport failure is incomplete evidence, never a successful zero count. Roughly
72 MiB needs about ten byte-limited calls on a quiet local filesystem, rather than
thousands of tail reads. High match density does not stop scanning. `reason`
distinguishes `byte_budget`, `time_budget`, `aborted`, `file_changed`,
`file_unavailable`, and `complete`. Budget/abort responses preserve a cursor.

Each boundary is the size observed when the scan is created. Later appends are
excluded. Each active file retains one read-only identity descriptor to prevent
inode reuse after unlink/recreation. There are at most 32 such descriptors;
completion or failure closes them, and the existing HTTP service loop closes
expired states even while idle. Shutdown disposes the remaining states. These
handles can delay reclamation of an unlinked log until continuation or expiry;
no additional log copy is created. Same-file growth during the first pass is provisional: the Bridge
hashes exactly the scanned bytes and then verifies that entire fixed boundary
in a second bounded pass. Exactness requires matching SHA-256 digests and stable
file metadata throughout verification. It does not infer append-only continuity
from inode/size or small prefix samples. If appends continue during verification,
the scan reports `changed`; retry during a quieter interval. In-place edits,
truncation, replacement, rotation, changed links or unverifiable continuity never
silently mix generations. As with other file observations, changes after a file's
completed observation are outside that observation.

### Bridge compatibility and enabling

`log_scan` was introduced in **1.1.33** (exact-release preview **1.1.34**).
The current candidates **1.1.35/1.1.36** retain it. Protocol 1, the advertised
`log_scan` capability and exact approved trust are required; a numerically newer
version alone is insufficient. See the [version/capability matrix](BRIDGE.md#version-and-capability-matrix). The Lab schema migration admits this capability without altering
existing pairings, grants, reviewed rows or credentials. Historical identities,
including 1.1.31/1.1.32, remain trusted for their existing operations. An older
Bridge returns a capability-unavailable result before a scan is dispatched; use
`log_read` for bounded excerpts until the operator upgrades and reverifies it.
Installing Lab alone does not install or upgrade a Bridge.

The operator must install the exact reviewed ZIP through the existing supported
workflow and reverify its identity and capabilities; production additionally
requires exact reviewed-build admission and current HTTP transport acknowledgement.
Managed installation remains an explicit lifecycle operation. This feature does
not upgrade, restart, re-pair, change log levels, export logs, or modify profiles.
Native managed and external acceptance must be recorded separately from fixtures.

## Existing `log_read` stats and tail

`log_read` is a read-only `target.inspect` tool for granted managed instances,
external development targets and external production targets. It requires an
authenticated, trusted Bridge that advertises `log_read`. Bridge 1.1.19 introduced
this capability. Bridge 1.1.23 supports traversal-only Android storage ancestors;
the corresponding exact-release RC1 overlay is 1.1.24. Older Bridges retain
their existing operations and need the normal operator-controlled upgrade and
verification before log reading is available. Installing this Lab update alone
does not deploy a Bridge or change target trust.

Start with metadata without transporting log content:

```json
{"target_id":"<granted target UUID>","log":"current"}
```

The default `mode` is `stats`. The result identifies the selected log, its
logical Kodi location, size in bytes, modification time in nanoseconds since
the Unix epoch, UTC observation time and opaque snapshot identity. Stats do
not scan or count lines. `log` accepts only `current` (`kodi.log`) or `previous`
(`kodi.old.log`), including the previous session after a restart. The only
locations are `special://logpath/kodi.log` and `special://logpath/kodi.old.log`;
host paths, arbitrary filenames and URLs are never input parameters.

Request a bounded excerpt explicitly:

```json
{"target_id":"<granted target UUID>","log":"previous","mode":"tail","max_lines":200,"max_bytes":8192}
```

Defaults are 200 lines and 8192 raw bytes; hard maxima are 500 lines and 8192
raw bytes. Reads seek to the end of the file and use bounded memory even for
very large logs. UTF-8 decoding replaces invalid byte sequences. A single long
line may return a partial first line, marked by `partial_first_line`. The
result reports `start_byte`, `end_byte`, `bytes_read`, `lines_returned` and
`truncated`. An empty log returns an empty successful result.

To inspect older material, use `mode=tail`, `before` equal to the returned
`start_byte`, and the same `snapshot_id`. These are byte positions, not line
numbers or timestamps. Paging requires the snapshot identity and never silently
continues into a replaced file. Appends, truncation and rotation change the
identity; `log_changed` means refresh stats and restart the read. The response
also fails if a change is detected during the read. Time-range scanning is not
part of this bounded operation; local timestamps remain verbatim diagnostic
data, without an inferred timezone.

`log_missing` means the selected file does not exist. `log_unavailable` means
it cannot be read safely, such as permission denial, a link or a nonregular
file. `log_read_busy` and `rate_limited` are bounded admission failures. Invalid
selectors, cursor combinations and limits are rejected. Bridge reachability,
pairing, capability, identity and production transport-acknowledgement errors
retain their existing classifications. A stopped Kodi cannot serve its Bridge;
after restart request the previous log.

No visual, remote-control, deployment or production mutation permission is
required for an MCP client to call this tool. Target inspection grants,
target policy and verified native identity still apply. Production additionally
requires its current inspection scope, exact reviewed paired Bridge identity
and transport acknowledgement. This feature retains the existing operator
Bridge pairing ceremony and approved technical capabilities. It does not grant
visual scope, establish trust or change the production mutation policy. Managed
reads consume lifecycle-established trust and recheck the exact container and
runtime revision. All target types reauthorize before publishing a result.

Logs can contain secrets, personal information and untrusted instructions.
Treat excerpts as diagnostic data and review/redact them before sharing.
Routine MCP audits record target, client, tool, outcome and timestamps, without
arguments or log bodies. The Bridge does not persist excerpts or keep them in
its replay response cache. No log archive is created by the tool.

`log_read` is part of the supported `0001_initial_release` baseline.
`0072_kodi_log_capability` and D-0433 describe its historical prerelease
introduction, not a current upgrade step. Migration `0015_log_scan_capability`
adds `log_scan` to the closed pairing/reviewed-build vocabulary while preserving
rows and authority triggers. It grants no access or trust and refuses downgrade
while either stored set uses `log_scan`. Preserve provenance and use the
[supported backup/recovery path](LINUX-V1-UPGRADE-ROLLBACK.md), rather than deleting
records to force a downgrade. Current head `0017_system_snapshot_capability`
adds only the optional system-snapshot capability.

Historical managed Matrix 19.4 installed acceptance used exact reviewed Bridge
1.1.31 after explicit administrator installation and fresh target-specific
authentication. Current installation uses the [current candidate](BRIDGE.md#version-and-capability-matrix);
that older result does not qualify new bytes.
Native JSON-RPC readiness or readable log files cannot satisfy resident trust.
See [Matrix compatibility and installed acceptance](MATRIX-BRIDGE-COMPATIBILITY.md).

### Reviewed production Bridge updates

A production target still pinned to an older build can refuse `log_read` with
`bridge_identity_mismatch` or `log_scan` with `bridge_capability_unavailable` even
after the owner installs the new Bridge. With the explicit per-target
**Trust updates from Kodi Lab's reviewed Bridge releases** setting On, the next
authorized log observation may adopt an exact eligible reviewed successor while
preserving pairing and unchanged HTTP consent. Tool grants and production
protections remain independent. Otherwise use the existing manual verification
flow. See [production operations](PRODUCTION-OPERATIONS.md#trust-reviewed-updates-installed-by-the-owner).
