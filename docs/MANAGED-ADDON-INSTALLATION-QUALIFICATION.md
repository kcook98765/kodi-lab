# Managed add-on installation feasibility qualification — 2026-11-12

**Decision: use existing controlled source deployment.** Repository
`InstallAddon(id)` fails #339's noninteractive exact-package/update contract on
all four approved versions. The campaign completed its unsupported-path closeout;
[#339](https://github.com/kcook98765/kodi-lab/issues/339) remains open for a future
supported installer architecture. The [agent workflow and technical decision](MANAGED-ADDON-INSTALLATION.md)
explain the supported alternative, publication/dependency limits and rejected
shortcuts. No runtime feature, Bridge API, schema or installed Claw update was
introduced. The independently useful change is a current unattended agent guide,
architecture decision and reproducible source/native evidence summary.

## Baseline and scope

- GitHub main: `7cf516137f7d4a60b35545569bf5088657d49094`.
- Installed Claw: source `89fd044fdddc3db5206f908ee4017c0a2bcd1f7c`, tree
  `4e33e7889b56e01803834f87e26c8317c3810ceb`, schema `0013_agent_packages`.
- Live Compose labels/mounts/images identify the active bundle. Both Web and
  broker matched all 1,340 expected installed package/resource files; eight
  documented legacy catalog/evidence files were absent as expected.
- Three healthy services, four stopped retained profiles, seven empty
  lease/reservation categories and unchanged one-running policy were verified.
  Source work used an isolated main-based worktree. About 21 GiB free at start
  favored cached approved images; no runtime acquisition or image rebuild ran.

The exact tagged source investigation captured 31 files, including all four
builtin implementations, installers, repository selectors/updaters, databases
and JSON-RPC method schemas. The [source manifest](qualification/issue339-20261112/upstream-sources.json)
records each URL, SHA-256, size and resolved tag commit. Source inspection was
followed by actual native execution; fixtures alone did not establish the result.

## Real native probes

Each probe used a fresh independent writable profile on the **exact approved
managed runtime image**, a dedicated internal Docker network, fixed inert owned
QA add-ons and no production database, credentials, profile mounts or Docker
socket. One native Kodi ran at a time. These are standalone native-runtime
feasibility probes, **not end-to-end Lab managed Create/Reset, agent publication
or native installation acceptance**.

The primary QA catalog was generated, admitted and approved through existing
`DevelopmentRepositoryService` in isolated QA storage/database. A temporary
read-only static QA server served it. This did not change the installed feed or
add a product publication service. A separate synthetic peer catalog advertised
the same private add-on ID at a higher version solely to test ambiguous selection.
Piers's ability to read this catalog does not add Piers to production publication's
Matrix/Nexus/Omega allowlist.

| Actual native version | Primary catalog refresh | New ID, primary 1.0.0 | Same ID, enabled peer 2.0.0 | Installed 0.1.0 / catalog 0.2.0 | Disabled installed ID |
| --- | --- | --- | --- | --- | --- |
| Matrix 19.4 | Native DB and HTTP confirmed | Yes/No prompt; cancelled | Prompt named peer candidate | Version and files unchanged | Remained disabled |
| Nexus 20.5 | Native DB and HTTP confirmed | Yes/No prompt; cancelled | Prompt named peer candidate | Version and files unchanged | Remained disabled |
| Omega 21.3 | Native DB and HTTP confirmed | Yes/No prompt; cancelled | Prompt named peer candidate | Version and files unchanged | Remained disabled |
| Approved Piers 22.0 RC1 | Native DB and HTTP confirmed | Yes/No prompt; cancelled | Prompt named peer candidate | Version and files unchanged | Remained disabled |

All four dialogs had native ID `10100`, title **Add-on required**, and asked to
download the named add-on. Blocking `xbmc.executebuiltin(..., True)` remained
pending until cancellation. The Python result was `None`; source inspection
shows that the builtin discards the native installer result. With both private
repositories enabled, the prompt switched from **Issue339 Primary 1** to
**Issue339 Peer 2**. Native catalog database copies independently contained the
respective ID/version records. This demonstrates why an ID does not pin the
reviewed primary ZIP, even before considering dependencies.

No install confirmation was accepted, no test-plugin ZIP or ZIP hash sidecar was
requested, and no candidate add-on files were installed. Installed-version and
enabled-state readbacks confirmed the no-ops. Independent stopped-profile
inventories verified both original seed files byte-for-byte and candidate-tree
absence. All four successful native containers stopped cleanly with exit 0.
Exact image/container identities, observed versions, assertions, original seed
file hashes and raw-evidence hashes are in the [native result summary](qualification/issue339-20261112/native-results.json).

Piers refused disabling its protected `repository.xbmc.org` through JSON-RPC.
The harness retained that refusal, left the protection intact and continued in
the internal network with no Internet route. No runtime recipe/helper, system
repository package or security policy was patched. Other harness corrections
were limited to QA settings-path selection, preserving the baked X11 directory,
using the private container address, and rejecting an unrelated startup dialog
unless the expected candidate label was observed. Failed attempts and original
logs remain retained alongside successful evidence.

## What is and is not qualified

- **Confirmed native:** catalog refresh, mandatory initial confirmation,
  higher-version private-repository collision, installed-ID upgrade no-op,
  disabled-ID enablement no-op, exact original seed files and clean shutdown.
- **Source/fixture evidence:** dependency selection and hash checking in Kodi;
  Lab package/manifest admission, ownership, cross-agent refusal, role revocation,
  generation/replay protection and controlled source deployment.
- **Not implemented/qualified:** managed automatic repository provisioning on
  Create/Reset or its Stop/Start restoration; trusted-agent feed publication;
  successful unattended native repository installation, exact installed package
  or dependency closure; native upgrades/replay through a new installer; new
  native cross-agent installation authority. The initial consumer contract failed,
  so downstream acceptance is unavailable rather than a fixture-based PASS.
- **Existing alternative:** owned-project source deployment and launch already
  have separate prior managed-native qualification on all four versions. Those
  reports are linked from the workflow; this campaign did not reopen that core
  implementation, repeat original-profile testing or relabel it native repository
  installation. No native GUI ZIP success was required or claimed.

## Validation

**FOCUSED PASS: 225 tests across eight individually invoked existing files.**

| Lane | Cases | Complete lane |
| --- | --- | --- |
| Owned projects, generation/target selection, original packages, repository admission and authenticated MCP ownership/revocation | 62 passed | 60.1 s |
| Required dependency evaluation and direct workspace deployment/replay/recovery | 163 passed | 45.5 s |

Both lanes passed Ruff formatting/lint and mypy. The first lane reported three
existing Alembic configuration deprecation warnings; no tests failed or skipped.
Native evidence above is separate from these fixtures. Documentation links,
input examples and changed-path scope were reviewed against current code; Git
whitespace checks passed.

No new full suite was justified by documentation-only changes. The verified
installed application baseline retains its prior **RELEASE PASS: 8,761 passed,
50 skipped, 5 warnings; all eight stages passed in 48m31s** on frozen `89fd044`.
At campaign start, main differed from that installed source only in four Markdown paths.
This campaign changes only documentation/evidence and does not claim a new
full-suite run. See the [baseline qualification](VIEWER-PIERS-QUALIFICATION.md).

## Preservation and closeout

Before/after comparisons proved unchanged protected external/preset/grant/client/
credential records, secret fingerprints, source/workspace fingerprints, managed
policy/state, deployment pointer and every original stopped profile's file content,
metadata, container/runtime/image and recorded stop state. Only observation UTC
fields were excluded from otherwise exact record comparisons. All seven lease
categories remain empty. Web/broker/repository retain their original IDs, images,
mounts/labels, healthy status and zero restarts.

All 587 baseline containers, 190 distinct images, 24 networks and 137 volumes
remain present. Nine campaign-owned QA containers are stopped; one isolated QA
network and retained private evidence are the only new Docker resources. No new
images or volumes were acquired. No production QA clients, credentials, grants or
sessions were created, so there is no temporary production authority to revoke.
The stopped QA RPC and static server expose no running listener. No original
profile writes, Reset/Delete, credential rotation, external Kodi changes, NAS
writes, Codex/OpenCode changes, unrelated Docker cleanup, release or tag occurred.

Private durable ledger, recovery checkpoint, raw evidence and scripts:
`/home/kyle/kodi-lab-deployment/issue339-20261112/`. Do not upload private database,
profile or full log copies. Public summaries contain bounded findings and hashes.
No application transition or rollback is necessary; the installed baseline and
its previously verified recovery inventory remain intact.

No owner action is needed for supported source iteration. An owner who requires
native repository installation must choose a separately reviewed supported
installer boundary that can bind package/dependency identity before execution,
or await a suitable upstream API. Automating confirmation, adding broad builtins,
calling internal C++ symbols or checking bytes only after installation does not
resolve that architectural requirement. Keep #339 open for that decision.
