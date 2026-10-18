# Kodi logs over MCP

`log_read` is a read-only `target.inspect` tool for granted managed instances,
external development targets and external production targets. It requires an
authenticated, trusted Bridge that advertises `log_read`. Bridge 1.1.19 adds
this capability; the exact-release RC1 overlay is 1.1.20. Older Bridges retain
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

Schema migration `0072_kodi_log_capability` adds the closed Bridge capability,
preserves existing rows and authority triggers, and grants no permissions or
trust. Downgrade refuses while any paired or reviewed record contains
`log_read`; use normal credential retirement and record removal rather than
discarding provenance. See D-0433.
