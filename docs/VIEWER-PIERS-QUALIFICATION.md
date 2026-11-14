# Sustained viewing and fresh Piers guided Create — 2026-11-12

> **Historical checkpoint.** Later [RC2 qualification](PIERS-RC2-QUALIFICATION.md)
> and [Windows acceptance](WINDOWS-WSL2-INSTALL.md#accepted-windows-workflow--2026-11-13)
> supersede pending claims below. The owner closed #214 with accepted viewer risk
> and deferred #305/#339; their original evidence and limits remain recorded.

Campaign [#307](https://github.com/kcook98765/kodi-lab/issues/307) and
[#336](https://github.com/kcook98765/kodi-lab/issues/336) follow the
[Nexus/Omega](AGENT-FIRST-QUALIFICATION.md) and
[Matrix/Piers](MATRIX-PIERS-AGENT-QUALIFICATION.md) native campaigns.
Native work, safe closeout and the single bounded integrated source gate are
complete within the bounds below.

## Independent verdicts

| Capability | Verdict | Evidence boundary |
| --- | --- | --- |
| Sustained native viewing | PARTIAL | Omega 305.026 s and Nexus 305.028 s actual continuous intervals passed; corrected-source binding timeout and separate startup failure remain unresolved |
| Human Take Control | PASS | All four versions: real input, agent refusal, release, unchanged native counter and fresh authorized key; both widths exercised |
| Fresh Piers guided Create | PASS | Independently empty supported installation, exact preview admission/artifacts/Bridge, stopped completion, actual Start and retained reuse |
| First viewer after Create | PASS for fresh Piers | Its original first 375 px connection displayed native Home pixels; historical #214 remains open |
| Regression | PASS | Focused gates plus one integrated gate: 8,761 passed, 50 skipped, 5 warnings; all eight stages passed on unchanged installed source |
| Security and preservation | PASS | Original authority/source/native pins preserved; all temporary QA authority revoked, stopped profiles and zero leases |

The environment is Claw Ubuntu 24.04, Linux amd64, Chromium 156.0.8078.4.
Real browser keyboard input reached native RFB/Kodi. Desktop and narrow browser
viewports do not establish physical touch, IME, Firefox or Windows acceptance.
No public release or tag was created. Native ZIP GUI installation, #339 and
human Reset consent changes were outside this campaign.

## Baseline and installed identities

GitHub main began at `22c7859980c9c45cda187500687dbb8aadec2384`; installed Claw
began at `91ff733550ab6cb7f5ac3054edd8f1e4a3bfed34`, tree
`46522b12076a0f1e7e1c6ff4226e1fcdfa38167d`. Schema was and remains
`0013_agent_packages`. The baseline had three healthy application services,
four stopped retained managed profiles, no outstanding viewer/input/mutation
or other lease/reservation rows, and SQLite integrity `ok`.

Viewer corrections were qualified and installed at
`89fd044fdddc3db5206f908ee4017c0a2bcd1f7c`, tree
`4e33e7889b56e01803834f87e26c8317c3810ceb`. Every installed Web and broker package
file matches that committed source plus the documented wheel includes:
1,340 exact files per role. The eight legacy runtime catalog/evidence JSONs
removed by the existing Dockerfile are recorded separately. This report's
integration changes documentation only; GitHub main at the application source checkpoint is
`89fd044`. This documentation merge advances main without changing those application bytes. Claw has three healthy services and four stopped profiles.

Each update used the supported stopped-service backup, repository transition,
same-schema upgrade and service start. Protected authority, external/master
secret fingerprints and private source fingerprints matched across every update.
The final pre-update backup is `5443bef9a7bc43c6ad4c3d35901ec13a`.

## Causes and bounded corrections

Two independent failure paths were reproduced. On unchanged `91ff733`, Omega
375 px completed 305 s view-only, then its interactive successor ended after 84.878 s
at the broker with `web_lease_expired`; the Web watchdog was still pending.
A subsequent Omega session on diagnostic `5f76f2c` ended after 130.232 s at the
Web watchdog during ownership renewal; the broker ended by disconnect, with a
trusted-renewal gap below expiry. This proves the watchdog and broker expiry can
arise independently. It does not identify every historical 26/41 s cause or the
immediate first-viewer incident in [#214](https://github.com/kcook98765/kodi-lab/issues/214).

A separate renewal race was reproduced before correction: a delayed ORM flush
could update the instance key after a successor replaced the attempt, shortening
its admission lease from about 10 s to 2 s. [PR #342](https://github.com/kcook98765/kodi-lab/pull/342)
uses one conditional update against exact instance, attempt, owner, active state
and unexpired relative/absolute deadlines. It obtains the SQLite writer lock
before sampling time, so a lock wait cannot revive an expired lease. Same-owner,
different-owner successor and expiry-while-waiting regressions cover the fix.
This race was real but did not explain subsequent native watchdog closures:
post-fix Omega still failed after 113.220 s.

Payload-free timing in [#341](https://github.com/kcook98765/kodi-lab/pull/341) and
[#343](https://github.com/kcook98765/kodi-lab/pull/343) separated authentication,
binding, control, renewal, lock, update and commit. Untraced Nexus ended after
94.021 s with watchdog 1001 ms and a renewal completing after the deadline;
commit accounted for 438 ms, while broker validation reached 690 ms. A bounded
synchronization/lock-only syscall trace then captured a 924 ms renewal, 922 ms in
commit, including 450 ms and 322 ms `fdatasync` calls. Its check exceeded 1 s after
binding work while the broker's 2 s lease was still current. The isolated tracer
had no CPU throttling, used under 5 MiB, recorded no SQL/payload/credentials and
was stopped/detached/removed. The traced trial is diagnostic evidence only.

[PR #344](https://github.com/kcook98765/kodi-lab/pull/344) gives only the periodic
check 1.5 s for authorization plus durable renewal. The 250 ms tick leaves 250 ms
scheduling margin within the independent 2 s broker lease. Admission remains 1 s;
DB and broker leases remain 2 s, foreground/helper leases 3 s, and the absolute
session deadline 600 s. No heartbeat precedes a completed authorized commit.
The regression delays a real SQLite commit 1.1 s: it fails before the correction
and renews afterward. A 1.8 s commit still closes without a trusted heartbeat.
No automatic reconnect, denied-input retry, weaker owner check, disabled
watchdog or relaxed native cleanup was introduced.

This correction is not a sustained reliability PASS. On installed `89fd044`,
Nexus subsequently ended after 187.335 s at a different phase, `binding_check`,
with watchdog 1573 ms and trusted-renewal gap 1824 ms. The broker ended by disconnect
after 186.330 s, helper exit 0/proven and all leases zero. A 1301 ms native renewal
had survived earlier with the revised budget. This remaining binding outlier's
underlying cause is unproven; commit timing must not be assigned to it by analogy.
A 20-check read-only capability profile included cold imports and did not justify
skipping authorization or caching current admission. Further timeout increases
were not used to conceal the residual failure.

## Native viewing and control

| Installed `89fd044` native check | Observation | Disposition |
| --- | --- | --- |
| Omega 1280 px | 305.026 s continuous view-only, then 120.014 s interactive | Native duration/input PASS; controller's old log-tail assertion failed after release; independent native Selected 1/Selected 2 proof retained |
| Omega 375 px | 65 s view-only and 10 s interactive | PASS, unchanged released frame, fresh key, normal Stop/cleanup |
| Nexus 1280 px | 187.335 s before binding watchdog timeout | FAIL; helper exit 0/proven, zero leases |
| Nexus 375 px | 65.016 s initial view, 10.030 s interactive, then 305.028 s continuous post-release view-only | PASS, actual frame changes, normal Stop/cleanup; the long worker was untraced |
| Nexus natural headed tab visibility | Real document.hidden/blur, automatic release, foreground return remains view-only | PASS; no synthetic DOM event |
| Browser closure and fresh Nexus viewer | Close while connected, zero leases, fresh 5 s native pixels, normal Stop | PASS |
| Matrix 1280 px/375 px | Each 65 s view-only and 10 s interactive | Both controllers PASS, normal Stop and proven cleanup |
| Piers 1280 px | 65.014 s view-only and 10.013 s interactive | Counter/input/cleanup proved; strict whole-frame comparison failed despite Selected 1 before/after release; separate fresh Select 2 proof retained |
| Piers 375 px |65 s view-only and 10 s interactive | Controller PASS using unchanged counter/frame evidence, fresh key, normal Stop and cleanup |

Trials used the original retained approved runtime revisions/images. The same
four native container identities were retained; no Reset, Delete, repin or
replacement was needed. View-only allowed bounded journaled agent Down/Up to
prove updated native framebuffer hashes. Take Control used the ordinary Web
control and real held browser Enter, producing a native fixture Select counter.
Agent Select was refused `human_control_active` before a remote-action receipt;
read-only inspection remained allowed. Release did not replay that key; a fresh
agent Select incremented the counter. Normal Stop Viewing, browser closure,
helper exit/proof and zero ownership/input/mutation rows are distinct checks.
The native headed tab check used a fresh exclusively owned Chrome process and
CDP `noDefaults`, which avoids Playwright's ordinary focus override. A blank-page
probe first demonstrated actual visible/focused to hidden/unfocused transitions;
no visibility event was synthesized. The real Nexus tab then automatically
released control, kept its native counter at 1, returned in View Only and accepted
a fresh agent Select 2. Browser closure while connected cleared its worker and
leases; a fresh 375 px viewer displayed native pixels and stopped normally.

The initial headed Playwright attempt could not make the page hidden and stays
inconclusive. Its actual browser closure while interactive cleaned up, but one
fresh Omega recovery viewer failed before pixels: helper stderr/exit 1, zero RFB
bytes, 856 ms broker interval and 484 ms trusted-renewal gap; watchdog pending with
max check 242 ms. That startup failure is distinct from timeout/lease expiry.
The native exception was not retained, so its underlying cause is unproven.

The approved Omega/Nexus helper copies also have a fixture-reproducible Unix
socket readiness race: bind creates the path before listen accepts connections,
while the helper checks path existence then immediately connects. Both unmodified
run functions raise ConnectionRefusedError in a bound-before-listen fixture.
A bounded native helper trace during the next planned Nexus session connected
successfully, so the fixture race is not assigned to the earlier native stderr
or historical #214. The reviewed helper digests and all runtime pins were preserved;
a helper change would need a separately reviewed runtime identity, incompatible
with this campaign's explicit Piers no-repin constraint.

All failed and successful sessions are retained. Browser close codes alone do
not identify the initiator: server teardown can produce a clean browser 1000
close after a watchdog or broker failure. Terminal Web/broker records, exact
attempt IDs, native helper proof and independently sampled ownership rows supply
the classification. No unlimited reconnect or indefinite reliability is claimed.

## Genuinely fresh Piers guided Create

Fresh Create and first-viewer acceptance ran on unchanged corrected `91ff733`,
separately from viewer changes. A separate database on the host Engine would
collide with Piers' deterministic native target identity. Instead, one explicitly
owned isolated Docker 29 Engine received its own data volume, private socket and
network, 4 GiB/2 CPU limits and loopback-only published ports. It received no host
Docker socket. The four retained Claw profiles were preserved and only one
managed Kodi ran at a time.

An initial QA socket-owner mistake prevented the capability-dropped installer
from opening that private socket. A capability-identical read-only preflight
reproduced the error. Correcting only the owned socket to root/private-QA-group,
mode 0660, made the same probe pass; no installer permission changed. Failed
parent `0c9c9bb1-143f-419d-baa0-bec59714ef12` and child
`de93646d-0700-4055-956b-d3845df21b2b` remain in a separate stopped QA database.
Ordinary Resume resolved their known failure to `needs_attention/create_failed`;
no database editing or uncertain redispatch manufactured success.

The successful supported installation was independently empty: zero runtime
revisions/installations, native instances, Create/Reset receipts and viewers,
with an independent master key. Ordinary **Managed Kodi → Create** accepted
exact `piers-22.0-rc1-r3` revision 3, recipe SHA256
`77d02aed048def17482f6b225fccf37670ca35d441bdf1e898c58acb46fdb7e8`, immutable
explicit preview approval and `start_after_setup=false`.

| Receipt | Identity |
| --- | --- |
| Original request | `9a73ebdd-92ed-44d6-926d-69f8f83293f6` |
| Create parent | `7810aa37-08f7-4205-a780-e87d5f2f197d` |
| Installation child | `3960a384-f2b7-41c6-86fe-9a103f28eb12` |
| Runtime revision | `bbb30e8c-e389-4886-876a-0fa7686d1932` |
| Locally assembled approved image | `sha256:3a522632c20902682b275e387310eead7a5e60403698391fce2624d4a1020001` |
| Retained native container | `1c102bcc0cf0becc1dca206a9c8113b013bbfb7b0fae36e45e65e2aafa0acd23` |

The browser closed after known dispatch. A new browser first read the same
parent, then explicitly chose **Resume setup**; there was one original acceptance
POST and one child. Real acquisition took 194.745 s, assembly 147.947 s and native
validation 30.809 s; the runtime became ready at 18:55:35.900 UTC. Approved pinned
artifacts, preview admission and #338's exact official-snapshot fallback remained
unchanged. No package, runtime, Bridge or support identity was substituted.

Guided setup completed stopped with stored active trust. All ten native Bridge
files exactly matched reviewed preview 1.1.32, fingerprint
`54bd62b63902fad5ad3528039b8ce36d2ec4ef7ed1dad68aa2020761beea418a`.
Ordinary Web Start reached actual readiness. Its **first** 375 px viewer displayed
native Piers Home pixels before any MCP screenshot, development deployment, other viewer or reconnect.
Normal Stop Viewing and Web Stop passed. Original-request replay returned 200,
`replayed=true`, the same complete parent and no second child. Another ordinary
Start/Stop reused the same container/runtime/image/Bridge. Parent and installation
rows were byte-identical before and after replay/reuse; final state stopped,
one parent/child/instance and zero leases. Original Claw uncertain Piers Create
`b8dfff02` remains untouched and fenced.

## Tests, preservation and closeout

Focused gates ran per changed file and affected subsystem, with Ruff/mypy.
Notable overlapping groups: baseline viewer 43 cases, Piers 163, diagnostics 43,
ownership/authority/same-instance 64, phase diagnostics 21, periodic budget 81 passed
plus one expected skip. The atomic successor regression and the 1.1 s durable
commit regression failed before their respective fixes. Counts overlap and must
not be summed into a unique suite total.

One integrated `./scripts/check` ran on frozen installed commit `89fd044`, tree
`4e33e7889b56e01803834f87e26c8317c3810ceb`, from 20:16:27 to 21:04:58 UTC.
All eight stages passed: Ruff format/lint, mypy, contract guards, pytest, package
build, Alembic history and package/config import. Pytest collected 8,811 cases:
**8,761 passed, 50 skipped, 5 warnings in 47m41s**. The entire gate took 48m31s,
exit 0, with the source commit/tree/clean state unchanged. Skips were explicit
opt-ins or unavailable isolated tooling/privileged fixtures; they do not establish
native coverage. No full-gate retry occurred.

Enforced limits were 8 GiB memory, no swap, 2.5 CPUs, 1,024 tasks and 65,536 file
descriptors, with pytest 3,600 s, outer 4,200 s and unit 4,500 s timeouts. Actual
peak memory was 1,625,579,520 bytes (1.51 GiB). All managed Kodi and isolated QA
services were stopped before the gate; cold acquisition and native testing did
not run alongside it. Documentation-only integration does not require a second
full application gate.

All four original stopped native profiles were archived before writes and
safely extracted; 29 SQLite checks passed. Fresh QA native recovery and all three
QA application backups were exported, extracted and checked separately. QA
administrator credentials/sessions were invalidated through supported hidden-input
rotation, their password inputs removed, and every owned QA service/new Engine
stopped. Failed/successful/uncertain receipts and recovery keys remain private.
All 536 original native add-on source files retain their hashes, ownership and
modes. Original enabled/origin records match. Four Estuary settings files were
serialized differently, but every setting ID/value/attribute is identical; other
original add-on settings remain byte-identical. No original native database file
is missing and all 29 final SQLite checks pass.

The sole Claw QA client keeps its Read-only Inspection base preset, remains
enabled to preserve its private project, has no managed role or target grants,
and its sole credential is revoked. Managed status was refused after role removal;
the revoked bearer returned 401 and its plaintext private input was removed.
Supported Web signout changed authenticated API 200 to 401. All seven lease and
reservation tables are zero, capacity policy is unchanged and all four original
native profiles are stopped. Every original Create/runtime-installation row,
including uncertain Piers `b8dfff02`, matches the pre-update backup; no receipt was
rewritten or discarded.

All 19 original authority recordsets, seven protected secret files, seven source
tables and 314 private source files match. The original Web administrator credential
is unchanged. Docker preservation retains 580 of 583 baseline containers; only the
three owned application services were replaced. All 22 networks, 133 volumes and
every unrelated image remain; two old owned application-image metadata identities
were replaced. The NAS remains actually mounted as NFS.

Final supported stopped-service backup `e6647ae82cef46e3906a2bac1225155a` was exported,
safely extracted, file-hash/SQLite checked and verified to contain the revoked QA
authority. It matches this retained stopped native inventory; three services were
started healthy afterward. Source/native recovery artifacts remain private.

Only verified ignored mypy caches and exact immutable private Kodi Lab build-cache
records were reclaimed for disk headroom. No protected source/history/private project, original
credential or native profile was discarded. Unrelated Docker resources, external
production/development Kodi, NAS mounts/data and Codex/OpenCode were preserved.

| PR | Normal merge commit |
| --- | --- |
| [#341](https://github.com/kcook98765/kodi-lab/pull/341) | `5f76f2c18e8674f0f75d63fda733557a8bf11100` |
| [#342](https://github.com/kcook98765/kodi-lab/pull/342) | `ade5bb078b0e143acf773ccdb57c1d3a3c91eb93` |
| [#343](https://github.com/kcook98765/kodi-lab/pull/343) | `7c27eabb6fdb9d54c6cd3a1761fa9da98c0f265d` |
| [#344](https://github.com/kcook98765/kodi-lab/pull/344) | `89fd044fdddc3db5206f908ee4017c0a2bcd1f7c` |

Durable private evidence and recovery: `/home/kyle/kodi-lab-deployment/viewer-piers-20261112/`;
`LEDGER.md`, `RECOVERY.md`, original UUID journals, payload-free diagnostics,
native PNGs/logs, ownership/resource samples and sealed manifests.
Completed native/focused/preservation seal: 662 files, SHA256
`54560bd2b74ed98a516e681f0c33d613a585ae2cd5aecb1f5647529243761c6d`;
13 private recovery archives are owner-read-only. Completed full-gate logs are
excluded from that native seal and receive a separate final receipt. Credentials,
private source and framebuffer/input payloads are excluded from public reports.
No immediate owner action is needed to keep the stopped lab healthy. Keep #307
open for the measured residual binding outlier and #214 for the native startup
failure and historical first-viewer question. The fixture socket-readiness race
needs a separately reviewed runtime/helper identity before a native correction;
no Piers repin or unsupported in-place patch was attempted. These gaps prevent a
sustained reliability PASS. Native GUI ZIP installation is a separate capability,
not a prerequisite for completing this focused campaign. No #339/#302 redesign,
Windows/WSL 2 qualification, model integration, public release or tag occurred.
