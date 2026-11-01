# Standard managed Kodi viewing

Current Kodi offers **Start Viewing** for an exact installed, validated display
runtime. No experimental UUID or Web/broker flag is needed. Fresh installations
select these reviewed recipes:

| Kodi | Recipe | Revision |
| --- | --- | --- |
| Matrix 19.4 | ubuntu-jammy-matrix-19.4 | 3 |
| Nexus 20.5 | ubuntu-noble-nexus-20.5 | 3 |
| Omega 21.3 | ubuntu-resolute-omega-21.3 | 5 |
| Piers 22 RC1 | piers-22.0-rc1-r3 | 3 |

Piers remains Preview and requires exact prerelease consent. Version names,
package presence and screenshots alone never confer viewer eligibility. Admission
checks the exact packaged recipe and helper/package integrity, sealed installation
and native display validation, current image/container incarnation, selected
session identity and existing ownership/reconciliation fences. Unknown, altered,
unvalidated and older images do not inherit standard viewing.

## Normal workflow

Choose and prepare a fresh Kodi in Current Kodi, start it, then click Start
Viewing. noVNC loads only on that request. The initial session is view-only;
agent navigation remains independent. Take Control explicitly acquires human
ownership and excludes conflicting agent/Web input. Release Control proves native
input neutralization and restores agent input. Stop Viewing, disconnect, hidden
tab, logout or identity change ends viewing. The absolute session maximum is ten
minutes across handoffs; start a new session explicitly after expiry.

Viewing and authentication writes require HTTPS; session and administrator-view
cookies are Secure, HttpOnly and SameSite=Strict. Deployments must retain the
existing trusted HTTPS proxy. Same-origin/CSRF checks, one-use tickets, bounded
workers, private Unix RFB transport and fail-closed cleanup remain mandatory.
There is no new public port, service, MCP viewer tool or permission.

The standard configuration is pinned noVNC 1.7.0, native 640×360, approximately
2 displayed FPS, compression 0, quality 6 and LP_NUM_THREADS=1. Audio is not
required. Do not tune these values as part of standard activation.

## Resource qualification contract

The owner-approved standard Firefox process-tree RSS budget is **512 MiB above
the stable authenticated Current Kodi page before viewer import**. Freeze the
baseline at the last five stable page samples. Import, connection, motion,
handoffs and stopped-open retention count against the same baseline; do not
reset it between cycles. Preserve the **2 GiB absolute browser process-tree
emergency ceiling**. Record absolute RSS and PSS independently; PSS is diagnostic
and cannot substitute for RSS.

Use the PID/start/session-fenced collector and measurement method in
[the browser memory method](qualification/BROWSER-MEMORY-METHOD.md).
Require bounded session behavior, coverage of native workers, held-input and
ownership cleanup, and independent verification that all owned browser processes
exit. Retain existing host available-memory (2 GiB), CPU, local disk (20 GiB),
application/native increment (160 MiB) and worker safety guards. These are
qualification safeguards, not a new runtime memory-control system.

Ordinary transient caching or finite residual growth is not a demonstrated leak.
Record slopes, peaks and cycle residuals without extrapolating finite windows;
continuous dangerous accumulation or host exhaustion fails qualification. No
forced GC, cache/sandbox override or higher resource limit may manufacture PASS.

The original 128 MiB comparisons, their failures and incomplete campaigns remain
historical evidence in [the implementation ledger](STANDARD-NOVNC-IMPLEMENTATION.md)
and the issue #163 deployment reports. This owner decision changes the standard
acceptance budget, not their original verdicts. Prior exact-image native evidence
covers all four recipes; activation still requires focused admission acceptance
and the complete release gate on final merged source.

## Older profiles and later production rollout

Installing a standard recipe preserves an existing registration's exact pin.
Older profiles remain ineligible for standard viewing and keep their eligible
screenshot/JPEG paths. Historical Omega r4 remains separately experimental.
The owner accepts fresh profiles: use existing explicit Fresh Test/replacement
and supported lifecycle operations when replacing an old profile. No automatic
repin, in-place migration, compatibility framework or profile reconstruction is
provided or needed.

Production deployment is a separate owner-authorized phase. Take a fresh supported
backup and rehearse supported recovery, deploy the exact qualified application
commit, then create/setup the four standard profiles sequentially with one Kodi
running at a time. Retire obsolete profiles only by explicit supported lifecycle
operations. Confirm standard admission with no experimental configuration and
preserve external registrations, credentials, grants, workspaces, add-on sources,
backups and unrelated workloads. Source qualification does not establish installed
production acceptance or close issue #163.
