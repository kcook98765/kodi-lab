# Matrix/Piers native agent-first qualification — 2026-11-12

Later on November 12, the [viewer/Piers follow-up](VIEWER-PIERS-QUALIFICATION.md)
accepted genuinely fresh guided Piers Create, its first native viewer and retained
reuse. It also records measured viewer corrections and five-minute Omega/Nexus
observations; sustained reliability remains PARTIAL for distinct residual failures.
The evidence and checkpoints below retain this earlier campaign's context.

**Matrix 19.4 native core: PASS. Piers 22.0-rc.1 native core: PASS. Integrated
release qualification: PARTIAL.** Campaign [#336](https://github.com/kcook98765/kodi-lab/issues/336)
extends [#303](https://github.com/kcook98765/kodi-lab/issues/303) and
[#310](https://github.com/kcook98765/kodi-lab/issues/310). Actual native execution
on Claw Ubuntu 24.04, Linux amd64, and Chromium 156.0.8078.4 supplies the evidence
below. The [preceding Nexus/Omega campaign](AGENT-FIRST-QUALIFICATION.md) remains
historical evidence and was not repeated.

Historical sustained-viewer failures and native GUI ZIP consumption remain
partial. Piers advanced administrator installation passed; fresh native acceptance
of the corrected guided Create path remains a separate gap. No release or tag was
created.

## Exact application and runtime checkpoints

Initial GitHub main was `81ce9bf4a85bd9090582d6a77d8eb84aea3564f8`; installed Claw
was `875adfb00c190cffa0d63c6d8ca927e0748370f6`. Qualified application, main before
this documentation PR, and installed Claw are
`91ff733550ab6cb7f5ac3054edd8f1e4a3bfed34`, tree
`46522b12076a0f1e7e1c6ff4226e1fcdfa38167d`, schema `0013_agent_packages`.
This report's integration changes documentation only.

Matrix's initial development/import and human-viewer slices ran on `875adfb`;
both versions' final Reset/replay, fresh deployment and switching slices ran on
`91ff733`. Piers native slices ran on `91ff733`. The exact source diff between
those applications changes only Create admission and pinned support retrieval;
the project, viewer, GUI and Reset implementations are unchanged.

| Identity | Matrix 19.4 | Piers 22.0-rc.1 preview |
| --- | --- | --- |
| Approved recipe / revision | `ubuntu-jammy-matrix-19.4` / 4 | `piers-22.0-rc1-r3` / 3 |
| Recipe SHA256 | `0e16005b14c643dc999309bb7ef116fc8c76bd525d0487338b810c15cf7b75ac` | `77d02aed048def17482f6b225fccf37670ca35d441bdf1e898c58acb46fdb7e8` |
| Runtime revision | `3b3221b0-5510-40fa-8f31-af9df35b95f1` | `8eac3f70-ca83-4205-9e76-81c6f7c3aed6` |
| Native image SHA256 | `c3d87a817ea422db3d8e0faa24ed03f48b6b19c21d46d4191184fead315d35db` | `514421c9050bfc3912773728f7b16825b708bead64dd58d6a44e610f048e5cc0` |
| Reviewed Narrow Bridge | Stable 1.1.31 | Separately reviewed preview 1.1.32 |
| Bridge build fingerprint | `e18b4176611506335316a41e4dee9ea2ed113f8b5e7a0b95f6c156597d1618be` | `54bd62b63902fad5ad3528039b8ce36d2ec4ef7ed1dad68aa2020761beea418a` |
| Final retained container | `6df9563fdc533a24d52ddb45cbe59d394f352cadef414f4f335bbe1d4400bc47` | `9e775f36ec8564a4026d386f5398e955099909dd9561b9f7389804518a231c3a` |

Each exact ten-file native Bridge inventory matches reviewed source, including
on the final Reset replacements. Matrix uses Kodi package `2:19.4+dfsg1-2`.
Piers retains approved Flathub beta app commit
`0a381e42781914ae76ed338d3aa11f9403c55c57f5c9c194e1f4fb437cf71630`, its reviewed
platform/GL closure, GPG checks and original support-package hashes. No newer
Kodi image, package version or Bridge was substituted.

Piers explicit administrator acquisition
`bf61a280-4c50-4eaa-8750-7a29d44fd2e9` completed in 445.868 seconds, including
179.064 seconds acquisition and 231.588 seconds assembly, followed by native
validation. Administrator Runtimes Create registered it; agent Start provisioned
its profile. Administrator Bridge preparation
`6b6387ad-b9c8-448c-aeec-c9d20c2bc756` completed with active stored trust and a
stopped profile. Agent runtime acquisition remains unavailable.

## Per-version results

| Area | Matrix | Piers |
| --- | --- | --- |
| Source/fixture qualification | PASS; RELEASE PASS source gate | PASS; RELEASE PASS source gate |
| Native installation, Start/Stop and retained reuse | PASS | PASS through administrator Runtimes; corrected guided Create native acceptance PARTIAL |
| Private project creation, nested editing, generation validation and deployment | PASS | PASS |
| Reviewed source-ZIP import, subsequent edit/deploy and native execution | PASS | PASS |
| Actual screenshots, native logs, focus, Select and Back | PASS | PASS |
| View-only, human input priority, denial/no replay and release | PASS within bounded viewing | PASS within bounded viewing |
| Autonomous Reset, reviewed Bridge, guards and original UUID replay | PASS | PASS |
| Security/authority isolation and safe closeout | PASS | PASS |
| Explicit switching, same project generation and capacity enforcement | PASS | PASS |
| Sustained viewer reliability / native GUI ZIP consumption | PARTIAL / PARTIAL | PARTIAL / PARTIAL |

One QA identity received Trusted Managed Lab before either new version profile
existed, with Read-only Inspection as its independent base preset and **no
per-version target grants**. It created its private project and workspace through
MCP, edited nested Python/XML/resource paths, validated exact generation 3 and
deployed that same generation on both versions. A reviewed inert source ZIP was
imported as a second private project, edited in its workspace, validated at
generation 1, then deployed and executed on both. No human Web project/workspace
selection was used.

The inert add-ons declare Python 3.0.0 and an explicit add-on identity. Native
custom-window pixels, focus 101/102, Select counters/log markers, Down/Up and Back
to Home prove execution beyond accepted launch dispatch. Stop/Start preserved
the profile and supported live Bridge inspection. Matrix's fresh-copy disabled
add-on prompt was screenshot-reviewed for the exact owned add-on and confirmed
through normal GUI input before a fresh launch. Piers presented no such modal;
its direct launch worked. This difference required no product change.

## GUI, viewing and human priority

Actual Chromium view-only observation allowed agent input. Take Control denied a
fresh agent Select with `human_control_active` before allocating a remote-action
receipt; read-only inspection remained available. A browser Enter held for
180 ms produced a real native Select counter/log marker. Exact-profile Reset
while viewing/control was refused with `active_viewer_or_input`. Release restored
agent input eligibility: native counters remained unchanged for the denied key,
and a fresh Select then incremented them. Denied requests had zero receipt rows.
Matrix successful Select UUID replay reused the receipt without another counter
change; both versions passed Reset and capacity-refusal replay below.

Each version completed a continuous 65-second view-only interval, normal Stop
Viewing, browser closure and helper exit 0. Mobile first-frame observations
included Piers at 375 px, and both versions at 320 px after Reset before screenshot
or deployment. These supplied actual nonuniform native framebuffer pixels; the
final Matrix 320 px Home image was visually reviewed. The current installed
Web/broker diagnostic capture has six ended sessions each, all helper exits 0,
with no watchdog or lease-expiry ending in those bounded runs.

The initial Matrix 375 px observer was interrupted by our deployment before its
planned interval completed. Its Home frame counts only as first-frame evidence;
its hidden Stop click timeout is not a spontaneous product disconnect. An
observer filename collision overwrote intermediate Matrix browser PNGs; those
images are excluded from Matrix claims. Version-prefixed original Matrix images,
unique MCP screenshots, JSONL events and native logs remain intact. Piers image
attribution is separately hashed; the private harness now uses exclusive unique
filenames. Earlier zero-duration browser key attempts are excluded from the
human-input claim.

Historical [#214](https://github.com/kcook98765/kodi-lab/issues/214) and
[#307](https://github.com/kcook98765/kodi-lab/issues/307) remain open. The preceding
26/41-second watchdog/lease failures did not recur during these intervals. No
unlimited viewer reliability, original fresh-Web-Create first-viewer repair, or
viewer source correction is claimed. Python windows can still return misleading
raw labels such as “System” or “Immediate HDD spindown”; pixels, classified Python
window, focus and native logs establish the tested interaction.

## Autonomous Reset and cross-version operation

Final focused Reset evidence (earlier successful and guarded attempts are also
retained):

| Evidence | Matrix | Piers |
| --- | --- | --- |
| Original request UUID | `c89021a9-7c85-4240-afe6-d4b153693b41` | `35d122c0-3757-4968-abd2-e994ba74b9dc` |
| Clean Reset receipt | `623225f9-5d98-41da-aa29-9380e0c0ac14` | `d6aeac4e-98b5-419d-8fb9-38a094ae080f` |
| Separate reviewed Bridge receipt | `9221bb29-b4ed-4a4d-8e12-daef9253260d` | `f54f7ee2-9510-4e06-b9bd-ae076795b669` |
| Fresh generation-3 deployment afterward | `f93087cd-8301-4c95-9b9b-e12eb38f3248` | `eac67535-65ed-424b-9f83-9314bc8124c9` |

Each idle Reset bound the original expected container/runtime/image, replaced
only that selected profile, retained the runtime/image, separately prepared its
compatible reviewed Bridge, and ended stopped with active stored trust. Seven
source/ownership/artifact tables and all 314 private file hashes/modes/owners
matched before/after the final Reset slices. Other managed profiles retained
exact identities. Replay with the original UUID and old expected profile reused
both receipts and left Docker State/StartedAt unchanged. Fresh deployment,
enablement, native window/Select/log inspection and Back passed on each final
replacement before explicit Stop.

Both versions refused Reset during actual paused native playback of an owned
temporary silent WAV with `playback_active`; original UUID refusal replay left
the profile unchanged. Supported Stop and the fixture's cleanup removed its own
media. During each final Reset's observed `removing/remove` mutation lease, a
concurrent Reset with the old expected profile was refused `target_busy` without
another destructive effect. Earlier probes that reached rate limiting or an
already changed identity are recorded separately and do not count as pending
operation acceptance. Human Web Reset consent and
[#302](https://github.com/kcook98765/kodi-lab/issues/302) semantics are unchanged.

The first Piers audio probe lacked music ListItem metadata, causing Kodi to route
its bare WAV path toward RetroPlayer; no playback occurred and no Reset was
sent. The private fixture was corrected to supply music/song metadata, audio WAV
MIME type and disabled content lookup, following the pinned
[Kodi Player](https://raw.githubusercontent.com/xbmc/xbmc/28ea2eac1eb7af8fdcbd2672d933ce87f594be79/xbmc/interfaces/legacy/Player.h)
and [ListItem](https://raw.githubusercontent.com/xbmc/xbmc/28ea2eac1eb7af8fdcbd2672d933ce87f594be79/xbmc/interfaces/legacy/ListItem.h)
interfaces. Corrected generation 3 passed on both versions. This was a fixture
defect, with no game add-on installation, backend access or product Reset fix.

Switching passed in both directions: Start of the alternate while one ran
returned `other_version_running` with no lifecycle/provision child; explicit Stop
of the running version; replay of the old refused UUID stayed refused; a fresh
Start reused the alternate's retained exact profile. The same authored generation
3 was deployed/launched/interacted with on that alternate. At most one managed
Kodi ran; there was no forced eviction or human workspace selection.

## Compatibility corrections and validation

| PR | Bounded correction | Normal merge |
| --- | --- | --- |
| [#337](https://github.com/kcook98765/kodi-lab/pull/337) | Persist exact Piers preview admission atomically with planned Create dispatch | `86449bfc355e38e1e1988b8e188f6455c75a2bfe` |
| [#338](https://github.com/kcook98765/kodi-lab/pull/338) | Recover four original pinned support artifacts from the official fixed snapshot only after HTTP 404 | `91ff733550ab6cb7f5ac3054edd8f1e4a3bfed34` |

The real broker admission check reproduced #337's omission; the former fake
broker had manufactured the missing record. Exact consent/UUID, transaction
rollback, resume and uncertain-response cases now use the real read-only check.
Broker checks and uncertain-request barriers remain intact. Original uncertain
Create `b8dfff02-172f-4d22-a49d-1229502498ae` remains fenced as
`installation_launch_unconfirmed`; its missing child was not manufactured,
erased or blindly retried. Advanced installation success does not repair that
receipt or establish fresh corrected guided-Create native acceptance.

For #338, 112/116 approved support URLs were available; four Perl 5.40.1-7ubuntu0.3
URLs returned 404. GET verified every original size/SHA256 at the already reviewed
Ubuntu `20261001T000000Z` snapshot without redirects. The correction applies only
to revision 3 and four exact URL/hash pairs after 404. Original recipe/support
manifest, package pins, GPG/OSTree identity, Bridge, deadline and redirect policy
are unchanged. Failed acquisition `146bad0b-bad1-49f6-8ccb-8b4c42749506` remains
retained; the separately requested supported retry succeeded.

Self-review used actual diffs and exact reviewed-head/merge tree identity. Focused
and integration counts overlap:

| Check | Result | Duration |
| --- | --- | --- |
| Baseline Reset/Bridge/role | 49 passed | 79.8 s |
| Admission reproduction before fix | 1 failed, cause reproduced | 6.50 s |
| Admission/installation correction | 53 passed, Ruff/mypy passed | 16.1 s |
| Ledger/concurrency | 54 passed, Ruff/mypy passed | 15.1 s |
| Interaction lane | 30 focused + 99 broker passed | 7.5 s |
| Pinned-support/worker/installation/assembly | 130 passed, Ruff/mypy passed | 12.0 s |
| Clean Reset guards | 42 passed, Ruff/mypy passed | 63.7 s |
| Resource/playback guards | 14 passed, Ruff/mypy passed | 7.3 s |
| Final settled-byte integrated source gate | **RELEASE PASS: 8,755 passed, 49 skipped, 5 warnings** | **48m41s** complete; 2,856.17 s pytest |

The prior 8,739 passed / 49 skipped / 5 warnings / 48m01s source gate was reused
as the starting baseline. The ONE final full gate ran 08:08:45.873–08:57:27.068 UTC
on clean frozen `91ff733`: **8,755 passed, 49 skipped, 5 warnings**, no failures
or errors; pytest 2,856.17 s, complete script 48m41s, wrapper 2,921.180 s.
Global Ruff, mypy (942 source files), six early guards, package build, Alembic
history and package/config import passed. All eight stages exited 0; the qualified
commit/tree and tracked source/test bytes stayed unchanged. Native Kodi, cold
acquisition and DinD acceptance were stopped before it. No repeat full run was
needed. Source RELEASE PASS and the bounded native acceptance remain separate.

The post-gate preservation check reconfirmed stopped profiles, healthy services,
zero leases, unchanged protected authority/source/secret inventories and exact
Nexus/Omega native Bridge files. Both concurrent Reset probe UUIDs have zero
Reset parent rows. A generated test-source `__pycache__` initially confused the
private verifier; comparison now uses the frozen Git tree's ten tracked Bridge
files. No native or product correction was required.

## Preservation, installation and final state

Both supported application transitions used stopped-service backups, repository
transition, same-schema upgrade and healthy start. Current bundle SHA256 is
`bb715bbc61988882529818f443713944f21370b895f31cecd025dece90805a6b`; latest backup
is `edbdeb44d9824f83ac5fdd9522ee6348`. Prior bundles/backups and recovery journals
are retained. Missing docker0 affected packaging; the documented build-only host
network override completed the candidate. Runtime Compose/host network settings
were unchanged.

Final proof: all three application services healthy; all four managed profiles
stopped/retained; zero viewer sessions, remote input guards, mutation/heavy leases,
start/provision reservations and promotion leases. Effective capacity remains
one; the legacy recorded policy is preserved for audit. Nexus/Omega container,
runtime/image, StartedAt/FinishedAt and exact stable Bridge inventories are
unchanged, with no starts or full native retest.

All 19 protected recordsets, all original 12 clients/16 credential authority
records, original external/master secret files and private project source remain
unchanged. The only new secret files belong exactly to Matrix/Piers managed
Bridge trust. Peer private-source reads and role access to the external-production
target were refused before external probing. No DVR backend was accessed.

Both temporary QA roles were withdrawn through Agents. A still-valid peer
credential was refused Start after role withdrawal; both credentials were then
revoked and verified HTTP 401. Clients remain enabled with no managed role to
retain private project source. The campaign-only password copy was removed after
comparison with the unchanged retained original. Supported Web Sign out ended the
campaign session; its authenticated API request changed from 200 to 401. Anonymous modern POST
`/mcp` returns 401 Bearer on loopback and 403 on the LAN listener.

All 22 baseline networks, 133 volumes, 223 unrelated baseline image records and
575 non-application container identities in the retained historical inventory are
present. The two authorized original application images were replaced; their
bundles remain for recovery. Actual NFS mount `/mnt/synology-claw` was verified.
No unrestricted cleanup, external Kodi changes, NAS writes, unrelated Docker
mutation, Codex/OpenCode changes or Git history rewriting occurred.

## Remaining limits, owner actions and release advice

- No owner action is required to keep the healthy stopped lab retained. Create a
  long-term identity through Agents and explicitly grant Trusted Managed Lab when
  desired; campaign credentials are revoked. The administrator password was not
  reset or requested in chat.
- Keep #214/#307 open and resolve/qualify sustained viewer watchdog/lease behavior
  before claiming reliable long GUI sessions. Bounded success is separate.
- Native GUI ZIP consumption remains partial under
  [#305](https://github.com/kcook98765/kodi-lab/issues/305). This campaign's source
  ZIP import/edit/deploy success is a different workflow. No fresh GUI consumer
  attempt was added; no temporary delivery origin was enabled. Program native ZIP
  installation remains unsupported.
- Qualify corrected guided Piers Create on a genuinely fresh supported target
  when available, preserving the original uncertain receipt. Retained Create
  recovery and general Create-after-Delete limitations are not repaired here.
- Piers remains the explicitly approved RC1 preview; no general stable-Piers or
  later-release claim follows. Linux/Chromium does not qualify Windows/WSL2.
  Native audio-output quality and custom-window raw-label semantics are unqualified.
- Keep #303/#310 open for these integrated gaps. No speculative modernization or
  public release campaign was started.

## Durable evidence and recovery

Ledger and current resume checkpoint:
`/home/kyle/kodi-lab-worktrees/matrix-piers-20261111/.campaign/matrix-piers/`.
Private request UUID journals, native screenshots/logs, viewer timings, exact
inventories, full gate output and recovery instructions:
`/home/kyle/kodi-lab-deployment/matrix-piers-20261111/`. Raw credentials, private
payloads and full private evidence must not be uploaded.

Native evidence manifest: `NATIVE-EVIDENCE-SHA256.json`, 440 completed files,
SHA256 `413db1971fcf6d8a2ef13c59f58fe95c5548b5d20352a373adcf4b05e44c3469`.
All 440 hashes and owner-read-only modes were reverified after the source gate.
Successful, failed, partial and observer-correction evidence is retained.
Completed source-gate and final closeout evidence is separately sealed in
`SOURCE-REGRESSION-SHA256.json`, 22 files, SHA256
`e08f4d19c30bf12385bb7e55d5848b6e04f08279834879ff0e6eca7d39ffae3b`.
Its actual counts/stages/timing are in `SOURCE-REGRESSION-SUMMARY.json`.
