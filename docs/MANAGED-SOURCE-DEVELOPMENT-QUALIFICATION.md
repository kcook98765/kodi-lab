# Managed source development qualification

Qualification date: 2026-11-12 on Claw, Ubuntu 24.04, linux/amd64.
Application candidate: `a92d9229ea61c7c24008e6db3757fe526c301f7a`, tree
`b86fc2d7829957ec1aea039ab90cc6a0fdd5ad69`, schema `0013_agent_packages`.
This follows the separate [repository installer investigation](MANAGED-ADDON-INSTALLATION-QUALIFICATION.md).
It qualifies [owned source deployment](MANAGED-ADDON-INSTALLATION.md), not native GUI ZIP installation.

## Native results

Native feasibility used fresh campaign-owned profiles on exact cached approved
images and an internal network. Managed MCP qualification used a separate Docker
Engine with its own fresh profiles, key, catalog, credentials and state.
The four original Claw profiles remained stopped and unchanged. Public runtime
records were verified and enrolled under the separate QA key. This was cached
runtime qualification, not cold acquisition or clean-host installation testing.
MCP acceptance used the QA Web service's authenticated loopback HTTP endpoint.
Remote-client connectivity and browser interaction were outside this test.

| Runtime | Larger source, module import, enablement, consumer execution and both updates | Extra first-copy activation cycle | Reviewed GitHub example |
| --- | --- | --- | --- |
| Matrix 19.4 | PASS | Required | PASS expected refusal: requires Python 3.0.1; Matrix provides 3.0.0 |
| Nexus 20.5 | PASS | Required | PASS native genre listing |
| Omega 21.3 | PASS | Required | PASS original listing and updated source execution |
| Approved Piers 22.0 RC1 | PASS | Unnecessary | PASS native genre listing |

The constructed video source ZIP was 189,748 bytes and included a deterministic
nested catalog, translations, license and Python entry point. A separate
`script.module` project was deployed and enabled before its consumer. Native logs
proved the consumer imported that module from its installed library, executed
its token-returning code and read the catalog's expected SHA-256. Both source-only
module changes at the same manifest version and consumer changes then executed.
All eight installed module/consumer trees were independently reconstructed and
verified against complete deployment manifests, including file modes and uid/gid.
Those observed trees had no extra files. This campaign adds no obsolete-file
deletion or general exact-mirror policy; existing destination-retention rules
still apply outside the qualified edits.

The real reviewed source was
[`romanvm/plugin.video.example` at 8c7ac5e](https://github.com/romanvm/plugin.video.example/tree/8c7ac5e1a77f2559c6bcfc36f941f1fbb89d4a80),
version 3.0.2, requiring `xbmc.python` 3.0.1. The client prepared a 2,136,615-byte ZIP
containing 15 tracked source/artwork/license files, excluding only `.gitignore`.
SHA-256: `26ca93c005332ed1a6b6bc35462e63f6adadbf73803db970f5f30068bfd1bfd7`.
Actual MCP chunk transfer, private project import, capture, validation, deployment
and enablement succeeded without Web source registration. Omega displayed the
unmodified source's Comedy/Drama/Horror listing. A benign log-marker edit then
executed on Omega, and the same validated generation displayed the listing on
Nexus and Piers. Complete installed-tree readback matched all three receipts.
No video playback or remote content fetch was requested. Matrix correctly refused
this upstream API requirement with no add-on tree and all mutation flags false;
the separate Matrix-compatible constructed consumer executed successfully.

The initial module/consumer application was `1417290`; the final candidate above
qualified the GitHub workflow and early standalone API guard. Exact managed
runtime image IDs:

| Runtime | Image SHA-256 |
| --- | --- |
| Matrix | `c3d87a817ea422db3d8e0faa24ed03f48b6b19c21d46d4191184fead315d35db` |
| Nexus | `617719c43df6277f9051074b81fdc8a0facbdb54b229971c8052daa0dd04f172` |
| Omega | `7f779626cd56fb3bf86e14e39657fee5e8c5299a8f6f7abf59449a1163e7aa8e` |
| Piers | `514421c9050bfc3912773728f7b16825b708bead64dd58d6a44e610f048e5cc0` |

## Supported intake and dependencies

| Bound | Staged editable source |
| --- | --- |
| Compressed ZIP | 4 MiB |
| Expanded source | 16 MiB |
| ZIP entries | 1,024 |
| One file | 2 MiB |
| Decoded chunk | 32 KiB, with a shorter final chunk |
| MCP request body | Unchanged 64 KiB |
| Transfer lifetime | 24 hours, expiry observed lazily |
| Retained transfer receipts | 4 per client, 32 globally, including terminal tombstones |
| Storage admission | At creation: existing 2 GiB reserve plus the bounded archive allocation |

Inline editable intake remains 45 KiB compressed with its original bounds. The
single `project_transfer` helper feeds existing `project_import`; it does not
clone repositories, execute shell commands, fetch arbitrary URLs or accept host
paths. Private chunks use ordered offsets, SHA-256, atomic publication, durable
request identity, ownership checks, cancellation and replay. ZIP validation retains
path, duplicate, symlink, special-file, CRC and compression-bomb protections.
Expansion is serialized. Backup recovery includes transfers and immutable seeds.

Reviewed passive `script.module.*` sources may declare only `xbmc.python`.
A consumer may require at most three separately imported owned modules. Deploy
modules before the consumer, preserving each exact validated generation/version,
runtime identity and verified installed tree. Missing, disabled, incompatible,
unowned, changed or uncertain dependencies refuse. Recursive module graphs,
cycles, optional dependencies and unsupported version-range declarations are
rejected rather than partially installed. Editing a shared module requires
redeploying its current generation on every target before launching a consumer
there. No arbitrary repository downloads or generic binary/service/skin/native
dependency installer was added.

Actual MCP denials covered peer-owned projects/workspaces/transfers and add-on ID
collision. Missing modules were refused on all four targets. Matrix additionally
refused an incompatible required module before any native mutation. Deliberate drift of
one known QA module file refused consumer launch with `dispatch_accepted:false`
and `launch_preflight_failed`; deployment returned `deployment_conflict`. Those
existing generic error mappings do not report the helper's more specific internal
reason. The changed QA bytes and failed receipts were retained, with Kodi stopped.
The final candidate also received a separate four-version API/ownership/ID-collision
denial pass after its complete source gate; all incompatible API deployment
receipts were checked for zero copy and lifecycle mutation flags.
Unit tests additionally exercise disabled dependencies, version/type/declaration
changes, stale generations, wrong runtime/owner and failed/uncertain receipts.

## Discovery, activation and updates

Phase A preceded production-code changes. Native `UpdateLocalAddons` recognized
new live copies and changed manifests on all four versions, preserved enabled
state and did not reload an active Python interpreter. Existing stopped-copy/start
deployment already discovered these changes, so no new rescan or Bridge operation
was needed. Production uses its existing fixed managed deployment/lifecycle path.
Live copying remains unqualified; the existing stop/copy/start transaction
provides the supported predictable update path even when a fresh invocation
could observe a code edit without a native restart.

The approved Matrix/Nexus/Omega distro builds can leave a first-copy **Disabled
add-ons** question open after JSON-RPC enablement. One explicit existing
`managed_stop`/`managed_start` cycle, with separate UUIDs and the same profile,
cleared it. No confirmation input was sent. Piers required no extra cycle; passive
modules did not cause the question. Subsequent updates used only the deployment
path's existing stopped-copy/start operation. New plugin invocations saw changed
Python code; an already running interpreter retained its imported module. No
hot-reload engine or automatic disable/enable policy was introduced.

A test harness mistakenly replaced a file using a truncated 200-line
`workspace_read` result. Kodi's log correctly showed the resulting SyntaxError;
that accepted/not_observed launch was retained. The complete reviewed client
source was restored in a new workspace generation and executed successfully.
Agents must follow `next_line`, `truncated`, observed generation and file SHA-256,
or use their complete reviewed client copy before replacement. Structural
workspace validation does not establish Python execution success.

Repository automatic-update settings were preserved. No arbitrary builtins API,
new Narrow Bridge command, second installation engine or orchestration wrapper was
justified. Existing deploy already composes exact copy, lifecycle, discovery,
enablement, dependency checks and verification; reviewed modules are explicit
separate deployments with separate receipts.

## Source qualification and installation

RELEASE PASS on the candidate above: **8,799 passed, 49 skipped, 5 warnings**;
all eight stages passed in **48m44s**. One bounded complete gate ran with the QA
Engine stopped. Its log SHA-256 is
`7b1f79df972cd19eac246a3b4787b54be701c756cd61f06e58075c35826b7c95`.
Focused source lanes passed before their merges: PR347 95 cases; PR348 190 and
61 overlapping cases; PR350 36 cases; PR351 138 cases, with the corresponding
Ruff/mypy checks. GitHub check runs were absent; these are actual local results.

| Source PR | Normal merge commit |
| --- | --- |
| [#347](https://github.com/kcook98765/kodi-lab/pull/347) | `aefaf08485db15c673adfb6fcf57a332a2df90aa` |
| [#348](https://github.com/kcook98765/kodi-lab/pull/348) | `1417290ac17d5eeaf9a58cee80fa1256eed2656f` |
| [#350](https://github.com/kcook98765/kodi-lab/pull/350) | `b3ecef8b3695b51f4c9de2b08d8ebb7c372f9f8e` |
| [#351](https://github.com/kcook98765/kodi-lab/pull/351) | `a92d9229ea61c7c24008e6db3757fe526c301f7a` |

Claw was upgraded from `89fd044fdddc3db5206f908ee4017c0a2bcd1f7c` to the candidate
above using supported same-schema backup, repository transition and upgrade.
Pre-upgrade and qualified-candidate backups were independently extracted and
checked against every manifest hash and SQLite integrity before services started.
The QA application upgrade had already rehearsed the same path with private source
projects and staged-transfer state. Both installed roles match all **1,342** expected
package files; Web, broker and repository are healthy. Schema remains
`0013_agent_packages`. Final documentation-only integration reuses this gate.

The four original native profiles remain stopped and identical in recorded content,
modes, ownership and timestamps. Existing private source/project storage, credentials,
external Kodi authority/secrets, managed policy, unrelated Docker resources and
NAS mount remain unchanged. All seven operation-lease categories are empty.
Both QA client roles were withdrawn; all four temporary MCP credentials are revoked,
with real HTTP 401 checks, and no Web sessions remain. QA administrator passwords
were rotated through hidden-input CLI to unretained random values. The isolated
Engine is stopped; verified native/application backups and successful/failed receipts
are retained privately. No external Kodi installation authority or public release/tag.
## Limits and follow-up

PASS applies to the bounded owned pure-Python development workflow above. It is
not a claim that every GitHub add-on fits the intake policy, runs on every Kodi
version or has supported dependencies. Transfer tombstones count toward a finite
quota; cancellation does not free a receipt or erase request identity. Automatic
quota reclamation needs a separately reviewed retention policy.
Larger source intake also leaves the existing MCP edit-text limit unchanged:
48 KiB aggregate UTF-8 text per edit and 64 KiB per request. `replace` requires
complete resulting text; `exact_patch` requires both complete old and new text.
The reviewed example's Python entry point fits. A bounded update method for
larger text files is separate follow-up work.

Native GUI ZIP and repository installation remain separate unresolved/unsupported
workflows under #339/#305. `InstallAddon(id)` was not repeated, and no confirmation
dialog automation was used. Sustained viewer, browser interaction, cold runtime
acquisition and external Kodi installation were outside this campaign's acceptance.
No external installation authority, profile settings change, release or tag was
introduced. The original issue remains open for those distinct installer questions.
