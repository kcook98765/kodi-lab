# Piers 22.0 RC2 managed runtime — 2026-11-12

**RC2 source, focused validation and isolated native core: PASS.**
Issue [#349](https://github.com/kcook98765/kodi-lab/issues/349) adds a separately
pinned Linux amd64 development preview. The initial isolated campaign preserved
Claw RC1; the later supported Claw adoption is recorded below. The earlier [RC1 qualification](VIEWER-PIERS-QUALIFICATION.md) remains historical.

## Verified Linux provenance

The [official RC2 release](https://github.com/xbmc/xbmc/releases/tag/22.0rc2-Piers)
resolves to `d686683a55d50e576dd282f53fb2bf9914931b38`; its `version.txt` declares
RC2. The release has no GitHub Linux binary asset. The supported source is the
published official Flathub beta build, with the existing reviewed OSTree assembly
path rather than a nightly or an unofficial binary.

[Flathub manifest commit](https://github.com/flathub/tv.kodi.Kodi/commit/0bae0f46fbdd6a0b2600b341c077f09dc58e4bcf)
`0bae0f46fbdd6a0b2600b341c077f09dc58e4bcf`, build 340397
([published status](https://hub.flathub.org/status/659036)), binds app commit
`83b803ab50c19471377943f35bed0d96247a425834580a420d5842912ce7cbdd`.
Its commit-object SHA256 equals that identity. Its detached signature verifies
with pinned Flathub primary key `6E5C05D979C76DAF93C081354184DD4D907A7CAE`
and subkey `54A6CDDD8919FB204200D8AC562702E9E3ED7EE8`. Signed metadata binds
the x86_64 beta ref and `org.flathub.Beta` collection. Embedded manifest and
metainfo bind the official source revision and `22.0~rc2-Piers`. The mutable beta
ref still returned RC1 during investigation; acquisition selects the independently
verified immutable published RC2 commit directly.

| Pinned input | SHA256 |
| --- | --- |
| App metadata | `c7ce238252232f77761e73142ef79aa3118ae564274f67b226e6159b6cd43c8f` |
| Native Kodi executable | `922eaf80a2885f3681a42f6acbb8d2032a4aa703edc4956294a1913735a5b672` |
| Embedded manifest | `05e96f0f7991045e5c4544a90634db1644dc4dd98abc32fc6da0e825bdb685ad` |
| Metainfo | `ead43aa15613d981f93295273eeff7c77e1f1adfd5871a39222755a0684028f6` |
| Acquisition definition | `4f5dd5942456563175f2f6bca1c0bfb192172792376d4c6c62d5ec633b7532bd` |
| Installation recipe, revision 4 | `16b275718524e6dafef27691677d07ac4bffb7af97e5960d227c0ae54bfa8527` |
| Assembly manifest | `fbbe31c3717ea65a9822bd030b539069a38d7ce982cc54e23715dc397b700869` |

Recipe `piers-22.0-rc2-r4` preserves the reviewed platform, GL, Ubuntu base and
116 support-package pins. RC2 provides Python API 3.1.1, ABI 3.0.0; admission and
validation use that independently verified identity. Recipe, source, ordinal,
consent, handoff, image and native validation bind the same release. Old RC1
artifacts, consent or validation cannot satisfy RC2. Historical r1/r2/r3 resources
remain available for exact retained evidence and replay.

Reviewed preview Narrow Bridge 1.1.32 already reports the actual native release
dynamically. Its source, ZIP and fingerprint
`54bd62b63902fad5ad3528039b8ce36d2ec4ef7ed1dad68aa2020761beea418a`
are unchanged. Actual RC2 resident authentication passed. Stable Bridge 1.1.31 and
all Matrix/Nexus/Omega runtime pins are unchanged.

## Independent native qualification

Claw Ubuntu 24.04, Linux amd64, Chromium 156.0.8078.4; candidate application
`fd2bfa1e194d99109cee1a742d5c4ba50fff0b32`, tree
`95891480c9bb9573171a68ef22b70e7935d989f5`. Subsequent qualification documentation and the standalone-asset mypy exclusion
do not change the tested application bytes. A separately owned Docker Engine
received no host Docker socket or original application/profile data. Its fresh
application had an independent database/master key, zero existing runtime/profile/
Create/Bridge/project/viewer records and no global selected project. One deliberate
Trusted Managed Lab grant had no per-target grants. Only one managed Kodi ran.

| Identity | Value |
| --- | --- |
| Successful Create parent | `7516847a-e5e1-4ab5-bbb1-314836d5e967` |
| Installation child | `16b6ecc9-cd7f-4612-b4d6-91473e347af1` |
| Runtime revision | `7bbbc37b-2eb9-4e10-b7e1-4736041c8962` |
| Locally assembled qualified image | `sha256:8aebc82f546494bcbc5e0e775641344a29d6474d73d2bb7c5b0a592898d8a0fd` |
| Initial QA native container | `b1c33657788bad3af18300b52daee18a553aa7e9831e4786b9d51f1209c3ac49` |
| Autonomous Reset | `2ff31c1e-2f22-4ee9-83b0-c2ada7f81c19` |
| Separate Reset Bridge preparation | `06d21727-64b7-460f-a476-e5cc77098cc5` |
| Final stopped QA container | `f147e88b085df9c5b76ad8f5658c3ec1bfaa0507eb4e2f134a9df24453553cdd` |

| Native check | Result |
| --- | --- |
| Cold acquisition, offline assembly, validation | PASS: 204.582 s, 366.243 s, 30.617 s respectively; native version 22.0 releasecandidate ordinal 2, revision `20261109-d686683a-dirty`; Python 3.14.7/API 3.1.1; ffmpeg 9.0.2; X11/llvmpipe |
| Guided Web Install and prepare | PASS: exact immutable preview consent initially unchecked; stopped completion; native readiness and authenticated reviewed Bridge; all ten Bridge files match |
| Original operation recovery | PASS: bounded UI polling paused during the slower build; a fresh browser read and explicitly resumed the same saved parent, with one installation child; original request replay returns that completed parent |
| First actual viewer | PASS: original first 375 px connection displayed native Home pixels before any MCP screenshot or deployment; normal Stop Viewing |
| Ordinary Start → Stop → Start | PASS: same profile/runtime/image retained; live inspection authenticated separately from stopped stored trust |
| Agent project/deploy/launch | PASS: private inert Program, nested Python/XML edits and validation; native custom window, screenshots and execution log; direction/focus, Select counter and Back to Home |
| Retained native identity drift | PASS: bounded QA-only stopped Bridge UUID change, unchanged reviewed code and secret; live inspection refused; explicit saved administrator repair restores authentication on the same profile |
| Retained invalidated trust | PASS: existing application invalidation helper retires the QA secret; ordinary Start preserves invalidation and refuses inspection; explicit administrator repair restores current-container authentication, finishes stopped and replays the same receipt |
| Autonomous Reset and redeployment | PASS: exact expected profile, idle stopped admission, clean replacement on the same RC2 image, separately completed Bridge receipt, stopped default; original UUID replay does not replace twice; agent Start/redeploy and native execution pass |
| Human Take Control | PASS at desktop and 375 px: real browser Enter causes native Select1, agent Select refused `human_control_active`, read-only inspection/screenshot allowed, release causes no replay, fresh agent Select2; mobile transition screenshots require actual pixels |
| Retained state and cleanup | PASS: original Create/installation rows identical after replay/reuse/Reset; source/project fingerprints unchanged; final stopped profile, exact ten-file Bridge, seven lease tables and active-use holds empty |

The first fixture launch was accepted but did not execute: default
`xbmcaddon.Addon()` lacked an inferred add-on context for path-based RunScript.
The fixture was corrected to pass its explicit add-on ID, then revalidated,
redeployed and observed running. Accepted dispatch was never counted as execution.
Ordinary container audio-sink/Avahi warnings remain; no playback/backend/audio
acceptance is claimed. Browser keyboard checks do not establish physical touch,
IME, Firefox, Windows or indefinite viewer reliability.

Two earlier isolated attempts remain preserved. The first cold acquisition passed,
then exact-release assembly context validation exposed a remaining RC1 comparison;
source fixes also corrected exact RC2 Start and image observation. The next cold
acquisition passed, but disk assembly exceeded the initial capacity estimate. Its
exact worker was cancelled and cleaned up; its failed parent was reconciled once,
never reissued. Both separate databases, terminal receipts and private project state
have independently verified backups and revoked temporary authority.

The successful configuration retains the existing 8 GiB admission check, 6 GiB
staging bound and OSTree default reserve. Its bounded tmpfs acquisition/socket
volume avoids disk duplication; application/profile/receipt data remains separately
persistent. The Engine was limited to 6 GiB, no swap and two CPUs. An independent
capacity monitor would pause only this owned Engine below 2.5 GiB free disk or
1 GiB available RAM. A proposed OSTree reserve override was withdrawn. Production
acquisition needs headroom for unpacked context, snapshots and compressed layers,
not only the downloaded payload. All QA services and the Engine are now stopped.

## Bridge, UI and viewer issue reconciliation

Fresh native Bridge evidence now spans Matrix 19.4 (original guided parent
`07434f7e-2612-4a46-9d15-e3641fcd79ce`, prepared/stopped at 2026-11-12 07:02:28 UTC),
[Nexus/Omega](AGENT-FIRST-QUALIFICATION.md), [Piers RC1](VIEWER-PIERS-QUALIFICATION.md)
and the independent RC2 qualification above. Four-generation focused fixtures cover
wrong selected Omega versus retained Nexus, invalidation, no-op/current trust,
interruption, replay, concurrent operations and authority/Origin/CSRF fences.
The RC2 native repair receipts are `2d614ae8-bd54-4ae0-ac8d-266088137833`
(identity drift) and `b526746c-14e9-494e-9e4c-41e6664c0590` (pre-invalidated trust).
Each was reopened and continued from its saved original request, authenticated the
resident Bridge and replayed without a second parent. No production Nexus repair
or selection change was needed. This completes #299/#300's preparation scope;
human Reset consent #302 remains separate.

[PR #353](https://github.com/kcook98765/kodi-lab/pull/353) adds compact readiness
text: Native Ready is a connection result, agent authority is separate, stopped
trust is stored evidence, recorded external checks require rechecking, and the
managed role grants no External Production authority. Its 31 focused presentation
cases and 108 Chromium fixture layouts pass; the candidate's 12 real console
layouts also pass accessibility/overflow/page-error checks. #281 retains its exact
missing live populated External Development acceptance; no target was manufactured.

Bounded native semantic input and human priority satisfy #307. Sustained viewer
reliability remains under #214: the prior Nexus 187.335 s `binding_check` outlier,
separate Omega pre-pixel helper exit 1 and historical first-viewer failure still
lack a proven shared cause. The bind-before-listen helper race is reproducible in
fixtures but is not attributed to those native failures. RC2 uses the same reviewed
helper digest `be90f0c00e36bcbe4e7d905c2a7c3d0ab6645cca42fa5b5ef830e67d338775db`.
A helper correction would require a separate immutable runtime/helper review and
native qualification; other runtime pins were not changed to expand this campaign.
No larger timeout, authorization bypass or reconnect loop was introduced.

The eight core integrated #310 goals have native/focused evidence across the
linked campaigns and this RC2 checkpoint: fresh Nexus/Omega Create, one managed
role, authored/imported inert projects, switching, real GUI and human priority,
Reset/replay/redeploy, preservation, and original-request recovery. Native original
ZIP/repository consumer limitations stay under #305/#339; they are distinct from
editable source-ZIP import and the qualified direct workspace workflow. #303 remains
the open product tracker for those actual outstanding limitations, #281 and #214.
No Windows, human Reset consent, navigation redesign or public release was added.

## Validation and installed transition boundary

Focused runtime/application/broker/Web lanes passed, including 16 RC2 regressions,
144 provenance integration cases (seven explicit opt-in skips), 136 Create/Bridge
integration cases, 100 image-operation cases and 60 dependency cases. These groups
overlap and are not a unique suite total. Ruff and mypy passed the final affected
25-path integration gate and the standalone RC2 healthcheck separately. Normal mypy
discovery also passed all 949 source files after adding the same archived-asset
exclusion used by other standalone runtime scripts. The exact diffs and native artifacts
were reviewed; there is no schema, external-authority or Bridge-code change.

No complete regression suite ran in this campaign. Focused validation exercises the
closed Piers identity change, historical RC1 admission and affected shared contracts,
with real native acceptance; a release/deployment gate was not undertaken. The most
recent separate accepted full gate remains `a92d9229ea61c7c24008e6db3757fe526c301f7a`,
tree `b86fc2d7829957ec1aea039ab90cc6a0fdd5ad69`: **8,799 passed, 49 skipped,
5 warnings, all eight stages, 48m44s**. It is a baseline, not a full-gate claim on RC2.

Fresh empty Piers slots select RC2 after this application update. Retained RC1 keeps
its exact runtime/image, label, trust and receipt history. Start/Stop and Reset use
that recorded runtime; Reset is not an upgrade. An occupied slot is not replaced.

At that isolated campaign's close, installed Claw remained
`a92d9229ea61c7c24008e6db3757fe526c301f7a`, schema
`0013_agent_packages`; the production application and four stopped profiles were
not deployed, reset or replaced. A future transition needs adequate disk headroom,
a separately reviewed application update/backup plan, a verified stopped native
RC1 archive and recovery rehearsal, exact reconciliation of occupied-slot and
uncertain historical receipts, and separate authorization for any supported
removal/recreation. No universal retained RC1 in-place upgrade was implemented or
qualified. Preserve private source and original uncertain receipts throughout.

Private recovery/evidence: `/home/kyle/kodi-lab-deployment/piers-rc2-core-20261112/`.
QA application and stopped-native backups were extracted/hash/SQLite verified;
temporary roles/credentials were revoked (actual MCP 401), administrator sessions
invalidated by supported hidden-input password rotation, and plaintext agent inputs
removed. All original production authority, credentials, external state, source,
profiles, containers, images, volumes, networks and NAS mount remain preserved.

## Later actual Claw adoption — 2026-11-13

[Supported Claw adoption](https://github.com/kcook98765/kodi-lab/issues/303#issuecomment-6863214072)
passed at application `a8ac577cc5b11209dd3447b41f48c1ebf5fd1682`, schema
`0014_human_reset_bridge`. Separately verified RC1 native preservation and supported
exact-profile retirement preceded independent RC2 Create on the unchanged
`piers-22.0-rc2-r4`. The original uncertain RC1 Create receipt was preserved;
retirement did not resolve its historical terminal outcome.

Actual Kodi 22.0 RC ordinal 2, reviewed Bridge 1.1.32 authentication, same-container
Start–Stop–Start, first narrow native pixels, direct inert-project execution,
Select/Back and bounded human Take Control/release passed. This adds no original-ZIP
GUI, repository installation, physical-input or indefinite viewer acceptance.
The owner later accepted #214's remaining historical viewer risk and deferred
#305/#339; the direct agent-owned source/deploy workflow remains supported.
