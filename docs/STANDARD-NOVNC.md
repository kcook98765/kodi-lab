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

Viewing follows the explicitly selected [Web access mode](WEB-NETWORKING.md):
fresh local HTTP uses WS; optional HTTPS requires HTTPS/WSS and Secure cookies.
Cookies remain HttpOnly and SameSite=Strict in every mode. Private-LAN HTTP is
unencrypted and requires explicit confirmation. Existing HTTPS deployments retain
their trusted proxy and Secure-cookie policy on upgrade. Same-origin/CSRF checks, one-use tickets, bounded
workers, private Unix RFB transport and fail-closed cleanup remain mandatory.
There is no new public port, service, MCP viewer tool or permission.

The standard configuration is pinned noVNC 1.7.0, native 640×360, approximately
2 displayed FPS, compression 0, quality 6 and LP_NUM_THREADS=1. Audio is not
provided. These are the settings used for completed standard acceptance.

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
covers all four recipes. Standard implementation, production deployment and
installed acceptance are complete as recorded below; the earlier failed and
incomplete attempts retain their original verdicts.

## Older profiles and later production rollout

Installing a standard recipe preserves an existing registration's exact pin.
Older profiles remain ineligible for standard viewing and keep their eligible
screenshot/JPEG paths. Historical Omega r4 remains separately experimental.
Replacement requires an explicit supported lifecycle operation and fresh profile;
installing the recipe alone does not replace a profile. Advanced legacy Fresh
Test/replacement retains its existing restrictions, including refusal in retained
mode; it is not an ordinary retained-profile rebuild workflow. No automatic repin,
in-place migration, compatibility framework or profile reconstruction is provided.

## Completed production deployment (2026-11-01)

[PR #179](https://github.com/kcook98765/kodi-lab/pull/179) enabled standard viewing;
[PR #180](https://github.com/kcook98765/kodi-lab/pull/180) corrected a release test
fixture. The eight-stage release gate passed on `4c8bbec`: **6,927 tests passed,
38 expected skips**. [PR #181](https://github.com/kcook98765/kodi-lab/pull/181)
then corrected release bundle recipe packaging. Its packaging-only successor,
`e218daf92b8c30ad0535454257cb0bb55ba18092`, passed separate focused qualification
(66 tests plus static and real bundle checks); the complete suite was not repeated.

The November 1 Ubuntu/Claw production deployment of that exact successor and
four-version installed viewing acceptance are **PASS**. Production schema stayed
`0090_verified_noop_deployment`. A fresh supported backup and isolated recovery
rehearsal passed before deployment. The four obsolete profiles were explicitly
retired through supported lifecycle operations and replaced with fresh standard
profiles, one Kodi running at a time. Existing profiles were not migrated.

Installed Current Kodi acceptance covered ordinary Start Viewing without
experimental configuration, approximately 2 distinct displayed FPS at 640×360,
default view-only, independent existing-agent MCP navigation, Take/Release Control,
input ownership exclusion, Stop, cleanup and screenshot fallback on all four
versions. Installed Start verified the 600-second deadline across handoffs;
completed four-version real-time expiry evidence was reused from prior native
qualification, without a new installed ten-minute expiry trial. Acceptance used
headless Firefox and does not establish physical desktop, touch or IME acceptance,
or Windows/WSL2 fresh-host qualification.

[Issue #163](https://github.com/kcook98765/kodi-lab/issues/163) is closed with
[published final acceptance evidence](https://github.com/kcook98765/kodi-lab/issues/163#issuecomment-6562870677).
The full deployment report is retained on Claw at
`/home/kyle/kodi-lab-deployment/issue163-production-activation/REPORT.md`, with
component receipts and failed/incomplete trials preserved. Production deployment
and installed acceptance are distinct from the earlier source/native qualification;
completion does not waive runtime eligibility, permissions or lifecycle safeguards.
