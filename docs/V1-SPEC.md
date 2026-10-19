# Kodi Lab — v1 Project Specification

Status: implementation specification  
Working name: Kodi Lab  
Primary audience: Kodi add-on developers  
Primary platform: Linux/Docker  
Versioning: Semantic Versioning

V1 scope decision (D-0381): Kodi Lab-created managed instances are disposable.
The stable Linux amd64 V1 release set is Matrix 19.4, Nexus 20.5, and Omega
21.3. Their reviewed signed-package recipes are assembled locally and validated
by Kodi Lab. Kodi 22/Piers is outside the installable stable V1 set. Historical
four-runtime/private-archive acceptance remains compatibility evidence, not a
public release requirement. `LINUX-V1-RELEASE.md` defines the current local,
unpublished, binary-free release candidate. External/production Kodi targets
are separate from Docker-managed instances and retain their own contracts.

## 1. Product definition

Kodi Lab is a self-hosted development and testing environment for creating, running, inspecting, debugging, and comparing isolated Kodi installations.

It provides managed Kodi instances, synthetic libraries, snapshots, bounded
add-on development workflows, visual access, Web administration, and an
integrated least-authority MCP surface suitable for human developers and AI
coding agents. General diagnostics remain Phase 15 work rather than an implied
MCP capability.

The web interface is the primary administration surface.

MCP primarily operates Kodi targets and performs add-on development/testing workflows. MCP is not a duplicate Kodi Lab administration interface.

## 2. v1 goals

v1 must provide:
- reliable isolated Kodi instances;
- curated stable Linux amd64 runtime support for Matrix 19.4, Nexus 20.5, and
  Omega 21.3;
- disposable Kodi Lab-created managed instances with durable application state
  and bounded Snapshot preservation;
- synthetic libraries;
- snapshots and reset;
- browser administration;
- screenshot visual inspection;
- optional noVNC/interactive display where reliable;
- browser remote controls;
- uploaded-ZIP, repository URL testing, and copied/synced add-on development
  workflows;
- protected agent working copies and reviewable patch-back;
- integrated MCP;
- remote LAN Kodi targets;
- production/development policies and one-shot transient approvals;
- bounded history and audit; broader logs/diagnostics are Phase 15;
- restricted container-management broker;
- SQLite metadata;
- a reviewed public installation path; the local Linux amd64 Compose and
  binary-free bundle are implemented release-candidate foundations, with
  exact-candidate acceptance and publication still separate gates.

### 2.1 Current normative V1 MCP and production contract

This section supersedes the broader speculative MCP catalog and production
capabilities retained later in this document as design history.

The authoritative V1 MCP registry contains twenty tools (D-0433). Client discovery is
filtered by current preset permissions:

- `target_list`
- `target_status`
- `addon_list`
- `inspect_ui_state`
- `log_read`
- `screenshot_capture`
- `remote_action`
- `development_source_list`
- `workspace_source_scan`
- `workspace_create`
- `workspace_status`
- `workspace_tree`
- `workspace_read`
- `workspace_edit`
- `workspace_capture`
- `workspace_validate`
- `workspace_deploy`
- `workspace_execute`
- `workspace_execution_recover`
- `promotion_request`

`inspect_ui_state` is a read-only `target.inspect` operation with a compact,
provider-independent schema, actual focus and explicitly validated container/item
association. Bounded non-atomic snapshots invalidate associations after detected
GUI changes. See [UI observation](UI-OBSERVATION.md) for provider and fallback semantics.

`workspace_execution_recover` requires both `target.control` and
`development.execute`. It provides one bounded containment/recovery operation
for an exact timeout-classified ambiguous smoke, or D-0427's independently
stopped completed non-timeout ambiguity, on an internal disposable target,
under D-0405's identity, ownership, grant, and state gates. It never
rewrites or redispatches the original execution and grants no generic lifecycle
or execution authority.

`log_read` provides metadata-first stats and bounded tail excerpts of only
`kodi.log` and `kodi.old.log` through a trusted authenticated Bridge. It uses
`target.inspect`, exact target grants and current production inspection scope,
without visual, remote-control, deployment or production mutation authority.
Snapshot-bound byte paging detects log changes; routine audits omit content.
See [Kodi log inspection](KODI-LOGS.md) and D-0433 for limits and trust checks.

Exactly seven are production-eligible: `target_list`, `target_status`,
`addon_list`, `inspect_ui_state`, `log_read`, `screenshot_capture`, and `remote_action`. Production has no
deployment tool. Raw JSON-RPC, broader production diagnostics/library queries,
generic add-on installation/update, production lifecycle, host lifecycle,
shell, SSH, generic filesystem access, user-development workspace deployment
to production, and broad persistent Kodi-settings mutation are outside the V1
production authority. The seven standing production actions are `up`, `down`,
`left`, `right`, `back`, `home`, and `info`; the seven transient actions require
one-shot Web approval; `context_menu` and `select` are always prohibited.

Production bridge bootstrap is manual installation of an exact reviewed ZIP
whose published checksum and provenance are registered and confirmed through
Web administration. The hosted stable Kodi repository is a V1 installation
convenience, not a production bootstrap dependency. Kodi Lab does not automatically install
or update its bridge on production. Any future updater/deployer requires a new
architecture and security gate.

## 3. Important product boundary

**Web UI administers Kodi Lab. MCP primarily operates and develops against Kodi targets.**

Web UI owns infrastructure administration such as:
- create/delete managed instances;
- runtime definitions/catalog;
- fixtures;
- snapshot creation/deletion;
- global configuration;
- development-source registration;
- token/preset/approval management;
- ports/network topology;
- trusted custom build controls.

MCP owns the implemented bounded target interaction and agent-development
workflows such as:
- server-owned semantic target inspection and control;
- playback/navigation;
- bounded add-on inventory and screenshots;
- approved development-target add-on workflows through the closed workspace
  services;
- managed development working copies; and
- client-orchestrated bug reproduction/cross-version comparison using only the
  authorized tools; the server supplies no V1 prompt catalog.

Do not expand MCP into a second lab-admin API merely because an operation exists in the web UI.

## 4. Non-goals for v1

Deferred:
- native Windows/macOS server execution and formal Windows host qualification;
- ARM64/Apple Silicon host support;
- persistent Kodi Lab-created managed instances, unless a concrete post-V1
  use case justifies a separate lifecycle and recovery design;
- GPU/audio passthrough management;
- full real-media library management;
- multi-user/RBAC;
- distributed workers/Kubernetes;
- full automated matrix runner;
- plugin architecture for Kodi Lab itself;
- self-managed TLS;
- dynamic fixture engine beyond predefined fixtures;
- general container shell/filesystem administration through MCP;
- automatic heavy-job resume;
- advanced snapshot dedupe/compression;
- built-in scheduled backups.

## 5. Architecture

Preferred stack:
- Python;
- FastAPI;
- server-rendered HTML with minimal JavaScript initially;
- SQLite;
- filesystem artifact storage;
- Nginx front door;
- restricted container-management broker;
- Docker;
- one Kodi instance per container;
- common application/service layer shared by web/API/MCP adapters;
- generic Target abstraction for managed and remote Kodi.

Conceptual flow:

Browser / API / MCP
        |
 Application/service layer
        |
 Target / Job services
        |
 Restricted broker
        |
      Docker
        |
 per-instance Kodi containers

Supporting storage:
- SQLite;
- restricted secrets store;
- runtime catalog;
- snapshots;
- artifacts;
- logs;
- screenshot cache;
- hosted Kodi repository (V1 distribution convenience, not a
  production bridge requirement).

## 6. Deployment

The historical Phase 3 Linux broker decision gate accepted an optional
Compose-managed broker from the same installed wheel as Web. At that checkpoint
it was a deployment foundation, before the managed-lab install; see D-0369
and `BROKER-CONTAINER.md`.

The local, unpublished Linux amd64 release candidate already has source-free
Compose, application images, three reviewed runtime recipes, checksums, and
installation/recovery guidance. See `LINUX-V1-RELEASE.md` and
`LINUX-V1-INSTALL.md`. Public publication and exact-candidate RC acceptance
remain separate. The older Phase 14 statement that no Compose definition
existed described that checkpoint only.

Host policy:
- Docker-capable Linux generally;
- Ubuntu is the CI-validated host for v1;
- x86_64 official initially;
- ARM64/Apple Silicon and native Windows/macOS server execution are post-V1 host work;
- D-0434/D-0436 define the maintained Windows/WSL2 Ubuntu amd64 installation path
  using Docker Desktop's Linux engine and the existing container broker. This is
  a working host configuration; new installer acceptance and formal clean-host
  qualification remain separately recorded evidence.

## 7. Releases

Use SemVer for public releases. The historical static `0.0.0` package version
was an unreleased-development sentinel, not a published compatibility promise.
Linux amd64 release preparation selected `1.0.0rc1` for the local, unpublished
candidate. The final public version and publication checklist remain separate
decisions.
No mutable image tag establishes release identity. The current local bundle
uses exact application image IDs, Compose files, three reviewed recipe
descriptions, `BUNDLE.json`, `SHA256SUMS`, and installation/recovery guidance.
`LINUX-V1-RELEASE.md` defines its complete inventory and authenticity limits.
Public image tags, archives, provenance, and signing require their own
publication decision; they are not claims about this unpublished candidate.

## 8. Persistent data

One configurable data root, recommended:
`/var/lib/kodi-lab`

Suggested subdirectories:
- database/
- secrets/
- snapshots/
- artifacts/
- screenshots/
- logs/
- runtime-builds/
- cache/

This is an early conceptual layout, not a claim that every directory exists.
V1 has no durable persistent managed-instance directory; the actual ownership
and recovery inventory is in `MANAGED-STATE-RECOVERY.md`.

The Web service UID/GID `10001:10001` owns the private data root and
application state. The Linux broker publishes managed snapshots as
`0:10001` with non-Web-writable modes inside that root. Application rollback
and full installation recovery use separate sets; see
`MANAGED-STATE-RECOVERY.md`.

Runtime sockets:
`/run/kodi-lab`

Source repository:
`<project-root>` on development host during development.

## 9. SQLite and files

SQLite stores structured metadata:
- instances;
- targets;
- runtime definitions/revisions;
- snapshots;
- artifact metadata;
- development-source registrations;
- jobs;
- history;
- MCP clients/tokens/presets;
- approvals;
- settings;
- schema version/migrations.

Large objects stay on filesystem:
- snapshots;
- ZIP files;
- screenshots;
- logs;
- build output.

## 10. Secrets/authentication

Admin password: hash only.
MCP tokens: show once, hash only; default 24-hour expiration with configurable/non-expiring option.
Remote Kodi credentials: recoverable restricted secrets store, SQLite stores references/metadata.

Web:
- one local administrator in v1;
- server-side sessions;
- secure HTTP-only cookies;
- SameSite;
- CSRF tokens on mutating requests;
- all normal UI authenticated.

Nginx handles routing/transport; application owns auth/authz.

TLS:
- not managed by Kodi Lab v1;
- support external TLS/reverse proxy;
- LAN-local HTTP allowed.

## 11. Configuration

Normal settings: web UI + persisted settings.
Environment variables: bootstrap/deployment overrides.

Precedence:
environment variable > persisted setting > built-in default.

First-run setup:
- admin account;
- data-root confirmation;
- localhost vs LAN exposure;
- essential defaults;
- host checks (container access, disk/RAM, port conflicts, prerequisites);
- only fundamental failures block setup;
- optional end-to-end disposable smoke-test instance.

## 12. Nginx/front door

One coherent external front door.
Typical routes:
- `/` web UI
- `/api/`
- `/mcp`
- `/repo/`
- `/screenshots/`
- `/display/`

Do not require users to manage many service ports.

## 13. Docker security

Main web/application process must not have unrestricted Docker-socket access.
Use a narrow broker exposing only explicitly required operations.

Core API/service methods should be domain-oriented. This historical list is
illustrative architecture, not a list of implemented public or MCP authority:
- create_instance
- start_instance
- stop_instance
- capture_screenshot
- install_addon
- sync_dev_source
- restore_snapshot
- query_library (POST-V1 candidate)

Do not expose Docker primitives as public business API.

## 14. Networking

Managed Kodi endpoints remain internal to Kodi Lab Docker networking by default.
Kodi Lab mediates normal access.
Advanced per-instance direct host/LAN exposure allowed for debugging.

New development subnet candidate on development host: `10.250.0.0/24`, subject to revalidation.

## 15. Target model

Generic Target abstraction with at least:
- ManagedTarget
- RemoteKodiTarget

Normalized fields include:
- id;
- name;
- type;
- Kodi version;
- availability;
- health;
- capabilities;
- policy mode;
- MCP preset;
- connection state.

Capabilities describe technical support.
Policy describes permission.
Unsupported controls are hidden/disabled with explanation.

Example capabilities below are historical design vocabulary, not current V1
authority. Implemented registries and Section 2.1 are normative; in particular
`raw_jsonrpc`, generic log access, generic filesystem reads, and generic add-on
installation are rejected for production V1:
- jsonrpc
- screenshots
- interactive_display
- addon_install
- addon_uninstall
- addon_settings
- kodi_settings
- log_access
- library_mutation
- raw_jsonrpc
- bridge_features
- filesystem_read
- developer_working_copy

## 16. Managed instance model

V1 Kodi Lab-created managed instances are disposable: one Kodi instance per
broker-owned container, pinned to an exact reviewed RuntimeRevision. Provision
creates or recreates the stopped container from that revision and its managed
state contract. Stop retains the current provisioned container and writable
layer. Deprovision removes that container and layer while retaining the
ManagedInstance record, exact revision pin, and durable Kodi Lab state. Reset
recreates the runtime in its deterministic assigned fixture state. Snapshots
provide bounded preservation outside the active container; the confirmed
instance-deletion workflow applies the existing preserve-or-destroy Snapshot
choice. See `PHASE7-SNAPSHOTS.md` and D-0381 for the accepted boundaries.

SQLite/application state, exact revision records, fixtures, Snapshots,
preserved Snapshots, and applicable backup/recovery contracts persist according
to their own rules. A stopped provisioned container keeps its writable layer
until deprovision or deletion. These do not constitute a persistent managed
instance mode. Dormant domain terminology for a persistent value does not make
that mode a V1 capability; any future implementation needs a concrete use case
and a separate lifecycle/storage/recovery decision.

External development and production Kodi targets may be long-lived
installations. They are registered and governed by their existing onboarding,
bridge, and policy contracts, not by this disposable Docker lifecycle.

Instance metadata includes:
- stable internal ID;
- name;
- Kodi version/runtime revision;
- type;
- runtime state;
- health;
- creation settings;
- startup behavior;
- fixture;
- MCP preset;
- dev-source attachments;
- notes;
- history.

Provisioning creates a stopped container; starting Kodi is an explicit
lifecycle operation. Exact revision and fixture selection remain under the
existing catalog and assignment policies.

## 17. State/readiness

Distinguish:
- container running;
- Kodi process running;
- JSON-RPC responsive;
- usable.

Ready requires Kodi JSON-RPC response.

Startup failure:
- temporary warning during bounded readiness;
- error after timeout;
- preserve logs;
- manual retry;
- no indefinite auto-retry.

Restart initially restarts whole container.

## 18. Reset/delete

Reset returns Kodi-side state to creation baseline while preserving Lab metadata:
- user snapshots;
- notes;
- history;
- dev-source associations.

Baseline may use hidden snapshot or deterministic recreation, whichever simplest/reliable.

Delete requires confirmation and option to preserve user-created snapshots.

## 19. Health

Overall:
- healthy
- warning
- error

Worst active check wins.

Minimum managed checks:
- container;
- Kodi/readiness;
- JSON-RPC.

Modular optional checks:
- repo;
- bridge;
- dev source;
- screenshot/display;
- add-on state.

Resolved issues disappear from current health but remain in history.
Recheck on startup/restart/major operations; periodic polling later.

## 20. Runtime catalog/version policy

Catalog-driven, not hard-coded.

Stable V1 reviewed release set on Linux amd64:
- Matrix 19.4
- Nexus 20.5
- Omega 21.3

Kodi 22/Piers is outside the installable stable V1 set. The private 19.5 and
four-runtime compatibility catalog and acceptance remain historical evidence;
they do not expand the current release set.

Policy:
- Current = latest stable major + previous major;
- Legacy = older best-effort, hidden behind Include Legacy;
- beta/RC/nightly hidden behind Include Pre-release.

The release ships reviewed recipe descriptions and exact catalog authority;
Kodi binaries are acquired from reviewed signed packages and assembled
locally. See `LINUX-V1-RELEASE.md`.

The current V1 installation path accepts only its three reviewed fixed
recipes. Earlier alternate-source and custom-build vocabulary describes
private/advanced catalog concepts; it does not grant a release installer an
arbitrary package URL, source build, or custom image input.

Common Linux runtime foundation where practical.

## 21. Runtime revisions/custom builds

Same Kodi version rebuilt from different base/source/build = distinct runtime revision.
Existing instances pin exact revision until explicit upgrade.
New instances use newest approved revision by default.

The historical/advanced catalog allows custom manifests without code changes;
this does not make them reviewed V1 installation recipes.

Normal manifests are declarative.
Arbitrary build execution only through **Trusted Custom Build**:
1. global opt-in;
2. per-runtime confirmation.

Record provenance:
- manifest;
- build timestamp;
- resulting image digest/ID;
- build logs.

Official runtime definitions live as reviewable repository files and may be contributed via PR.

## 22. Synthetic fixtures

New instances default to synthetic library.

Sizes:
- None
- Small
- Medium
- Large

Types:
- Movies
- TV
- Mixed

v1:
- predefined deterministic fixtures;
- fake metadata/artwork;
- lightweight placeholders as necessary;
- no large real media requirement.

Targeted fixture engine and legal sample playback packs later.
Fine-grained fixture reset later; full reset/snapshot sufficient initially.

## 23. Snapshots

Snapshots are the accepted bounded V1 preservation mechanism for disposable
managed instances. Their validated artifacts live outside the active container
writable layer. Creation, source-bound restore, explicit deletion, preserved
Snapshot browsing after confirmed instance deletion, and recovery follow
`PHASE7-SNAPSHOTS.md` and `MANAGED-STATE-RECOVERY.md`. A preserved Snapshot of
a deleted source is archive-only and cannot be restored to another instance.

## 24. Add-on development

v1 must support:
- uploaded-ZIP installation;
- safe explicit copy/sync from a registered local DevelopmentSource;
- repository URL testing.

Registered dev sources:
- global/reusable;
- logical name + host path;
- constrained to configured global dev root;
- original source read-only to Kodi Lab agent workflows;
- validate exists/readable; addon.xml validation where straightforward;
- explicit source/instance attachment and detachment.

Registration supplies read trust only. Any DevelopmentSource directory
separately provisioned writable for promotion becomes part of the Kodi Lab
application trusted-computing base. Kodi Lab does not chmod or chown source
trees; administrators must grant least-privilege service-account ownership or
ACLs only for sources intentionally enabled for promotion.

The supported v1 local-development materialization workflow is explicit
copy/sync from an attached registered DevelopmentSource.

Manual sync initially:
- changed/new files;
- no deletes initially;
- no automatic Kodi restart;
- exact source/instance provenance and replacement-conflict protection.

Protect an attached DevelopmentSource from repository replacement by default.
Allow an explicit approved replacement/update override for upgrade testing.

Inventory where possible:
- ID;
- name;
- version;
- enabled state;
- provenance only when Kodi Lab knows it.

After v1, live-source development may be revisited only through a
containment-safe projection or equivalent policy-enforcing mechanism. **Raw
direct Docker bind of mutable DevelopmentSource host paths is not an approved
Kodi Lab mechanism.**

## 25. Agent working copy

Original registered host source remains protected/read-only from MCP.

The registered `local_directory` DevelopmentSource is the user's normal
externally editable working tree. Human editors, Git and other host tools may
change it without Kodi Lab. Registration records identity and an initial
observation; it does not freeze the directory or automatically update an
existing workspace. Git metadata is optional and advisory. Captured bytes and
their normalized tree digest are authoritative.

An explicit fresh scan observes the current source without mutating a workspace.
An explicit whole-source capture into an **existing**, ready, clean workspace
stages and verifies the complete source tree, including additions and removals,
then switches the active protected baseline and tree together under a durable
capture operation. A changed capture advances exactly one workspace generation
regardless of file count. An identical capture is a no-op. The original creation
baseline and each superseded capture baseline remain distinct and recoverable.
Unincorporated private workspace edits or an unresolved promotion/deployment
block capture; Kodi Lab never silently overwrites them. A source race during
staging fails before the switch. An interrupted switch leaves the workspace
unavailable until deterministic reconciliation proves old/new bytes and receipt.

Validation, deployment, publication, installed-tree verification and execution
continue to bind exact workspace generations and bytes, never the live source.
External source edits do not retroactively invalidate historical receipts. A
source scan and workspace status must distinguish current working source,
active captured generation, latest validation, deployed generation, and verified
target generation; differing digests/generations are shown as stale.

MCP source inspection and explicit capture require source-specific and exact
workspace authorization when surfaced. Direct MCP/agent writes to the host
source are deferred. `workspace_edit` remains a bounded private-workspace tool;
no automatic reverse synchronization overwrites the external source.

The workspace detail page offers separate authenticated **Scan current source**
and CSRF-protected **Capture current source** actions. A fresh scan returns the
observed add-on ID/version, normalized tree digest and scan time without changing
workspace bytes or generation. Its result is evidence from that instant only;
an unscanned page always says the live source has not been freshly scanned.
Capture re-reads and stages current source bytes using the same internal service,
requires an expected generation and a clean workspace, and never trusts a prior
scan as a lock on externally edited source. An interrupted capture is shown with
a bounded receipt-based reconciliation action; inconsistent evidence remains
unavailable for ordinary capture.

MCP `workspace_source_scan` requires the opt-in `development_source.inspect`
permission, an exact source grant and an exact workspace binding. The distinct
mutating `workspace_capture` requires opt-in `workspace.capture` with those same
grants and an expected generation. Neither grants source write access. Existing
built-in presets and pre-existing custom clients gain no new permission. The
status page distinguishes the most recently scanned source, active capture,
private workspace edits, current or stale validation, latest repository
deployment and latest exactly verified target generation. The latest validation
retains its exact tree digest; migration 0059 backfills it only when the old
validation generation still matches the current workspace.

Captured baseline bytes named by durable validation, deployment, publication,
verification, execution or recovery evidence are retained. V1 does not
automatically prune capture history. Existing whole-workspace cleanup refuses a
workspace with capture history until a separately reviewed reference-aware
cleanup exists. Application backup/restore includes capture bytes and receipts.

MCP agents modify a Kodi Lab-managed working copy.

Working-copy tools:
- read;
- write/create;
- directory inspect;
- patch-based edit.

Patch-back through web UI:
1. compute diff;
2. show preview;
3. detect conflicts;
4. one explicit confirmation;
5. apply clean changes;
6. sync/reload;
7. verify add-on present/enabled;
8. inspect Kodi log for immediate errors.

The one patch-back confirmation covers both source apply and its bound selected
target verification. Source apply success is immutable: target sync, reload,
inventory, or bounded-log failure is reported separately and never triggers an
automatic source rollback. Verification uses the same exact container,
preserves enabled state, and has no generic command or filesystem-read surface.

Do not require globally clean source tree, but actual conflicts block apply.

No general container shell.
Read-only selected Kodi paths may be exposed.
Writes limited to working copy and approved dev/test add-on data directories.

## 26. Web UI

Primary administration surface.
Server-rendered/minimal JS initially.
Desktop-first responsive.

Dashboard:
- name;
- Kodi version;
- running/stopped;
- health;
- uptime where practical;
- IP/ports;
- display availability;
- search by name/version.

No grouping/tags/bulk actions initially.

Instance detail:
- state;
- version/revision;
- health checks;
- notes;
- screenshot/display;
- actions;
- inventory;
- dev sources;
- snapshots;
- recent history (~10) + full-history link;
- relevant MCP status.

Errors:
- concise summary;
- expandable technical detail/log excerpt.

## 27. Visual access

Every managed runtime must support screenshots.
Common screenshot abstraction chooses reliable backend.

Keep latest cached screenshot with timestamp/age.
Manual refresh default; optional auto-refresh around 3s and configurable.

Optional interactive noVNC/direct display where reliable; not required everywhere.

Browser remote:
- D-pad;
- select;
- back;
- home;
- context/menu;
- info;
- play/pause;
- stop;
- prev/next;
- volume;
- mute.

Navigation should trigger immediate screenshot refresh.
Browser communicates through backend, never directly to Kodi.
Optional keyboard capture off by default; send-full-text preferred over detailed key-by-key initially.

## 28. History/logging/diagnostics

The durable semantic history already implemented remains V1. The broader
logging and diagnostic-bundle design below is **POST-V1 / Phase 15 work** and
does not add tools to the current catalog in Section 2.1. Native production
log access and one-call MCP diagnostics are not V1 capabilities.

Persistent activity history:
- lifecycle;
- snapshots;
- installs;
- config changes;
- MCP actions;
- approvals;
- errors;
- runtime builds;
- migrations.

Filter by target/instance and type.
JSON export.
Configurable age/count pruning; critical audit may retain longer.

Logging:
- one structured Kodi Lab application log with subsystem/category;
- native per-instance Kodi logs.

Candidate Phase 15 one-click diagnostic bundle:
- application/runtime versions;
- relevant logs;
- target metadata;
- health;
- recent history;
- selected Kodi logs;
- bridge/repo status;
- screenshot metadata if useful;
- redact/exclude secrets.

## 29. Background jobs

In-process job queue/worker in v1.
No Redis/Celery required.

Heavy jobs:
- snapshot/restore;
- runtime build;
- large deploy/install/export;
- large diagnostic bundle as needed.

One heavy job globally at a time, FIFO.
Light/read-only actions continue when safe.

Persist job metadata.
On restart:
- queued may be discarded;
- running -> interrupted/failed with guidance;
- no automatic resume.

## 30. MCP purpose and transport

MCP is first-class v1, but focused on target use/development.

Transports:
- modern Streamable HTTP, with primary protocol `2026-07-28`;
- canonical stdio through a thin authenticated loopback proxy to that same
  HTTP authority.

Client:
- authenticate every independent HTTP request with a durable MCP bearer
  credential;
- list authorized targets;
- inspect type/capabilities;
- pass an explicit target UUID to every target-scoped tool.

The modern HTTP path has no initialize/initialized handshake, application
transport session, `Mcp-Session-Id`, selected/default-target state, or GET
stream. Phase 11 Slice 5 also supports exact handshake-era protocol
`2025-11-25` over that same stateless authenticated HTTP route. The initial
headerless request must be an exact `2025-11-25` initialize offer; subsequent
requests carry that exact version. There is no permissive fallback between
eras, and canonical stdio remains modern `2026-07-28` only.

Cross-target loops are client/agent side.

## 31. MCP authentication/tool exposure

Separate durable MCP clients and verifier-only bearer credentials. MCP
`clientInfo` is diagnostic metadata at most and never establishes identity.
There is no MCP application session ID.
Default token expiry 24h; configurable/non-expiring.
Show token once, hash only.

Tool availability = intersection of:
- valid credential and enabled client;
- client permission/preset;
- exact client/target grant;
- target policy;
- server-owned tool permission and target-class policy;
- technical capability and current target state.

Discovery is target-independent and reflects only current client-preset
permission. Exact grants, target policy, capability, and current state are
recomputed for every call; discovery is never authorization.

The implemented flat catalog is the exact nineteen-tool list in Section 2.1.
Earlier candidate names such as `player_state`, `library_search`,
`addon_install`, `log_search`, and `jsonrpc_call` were never V1 commitments and
are not implied missing tools. Raw JSON-RPC, generic lifecycle, production deployment,
production logs/diagnostics and production library queries are rejected V1
authority, not discoverable hidden methods.

Phase 11 originally exposed five tools. Phases 12 through 14 expanded behavior
under the same schema to a fifteen-tool checkpoint. Later registered-source
scan/capture and exact ambiguous-smoke recovery add three tools. Add-on
inventory remains bounded and path-free. Screenshot capture returns native
`image/png` MCP content plus compact semantic metadata. Remote action remains
the closed semantic control surface with durable client/request replay
protection.

## 32. MCP baseline/presets

Existing Kodi MCP project is a capability reference, not a tool-count compatibility contract.
Audit old tools after core Lab works:
- retain useful;
- merge redundant;
- redesign weak;
- remove only with documented replacement/rationale.

Built-in immutable presets:
- Add-on Development
- Read-only Inspection
- Full Lab Control (target-use maximum, not infrastructure admin)
- Production Remote Kodi

Users select an explicit nonempty subset of an active non-production built-in's
permissions for a custom preset with a new UUID and bounded unique name. V1
custom permissions are an immutable creation-time snapshot: there is no later
permission editor, later built-in permissions are not inherited automatically,
and the custom preset may be assigned at client and non-production target policy
layers. Custom rename and deletion are deferred. `workspace_validate` uses the
existing `workspace.inspect` permission, so that one permission also admits
read/status tools for the exact bound workspace. The narrow physical development
client uses only `workspace.inspect`, `workspace.deploy`, and
`development.execute`; it has no promotion, control, or visual permission.

Assign preset at client/token and target.
Effective access is intersection.

## 33. Managed development policy

V1 managed development is the least-authority nineteen-tool contract. It
supports bounded workspace inspection/create/edit/validate, registered-source
scan/capture, exact approved deployment, fixed execution, exact ambiguous-smoke
recovery, Web-reviewed promotion requests, bounded target
inspection/screenshots and closed semantic remote actions according to exact
preset, grant and target policy. It exposes no shell, Git, Docker, arbitrary
execution, arbitrary filesystem path, arbitrary add-on install/uninstall, raw
JSON-RPC, general library mutation, general log retrieval or broad settings
mutation. The older broader list is retained in history only as a POST-V1
candidate set requiring separate architecture gates.

## 34. Production remote policy

Production V1 is the exact five-tool subset in Section 2.1. Read-only status
and bounded add-on inventory use native semantic calls; screenshots require the
reviewed bridge and visual scope; navigation and the seven transient actions
use bounded native semantic calls. The transient actions require exact one-shot
Web approval.

Production logs, library queries, repository refresh, support-addon updates,
deployment, arbitrary add-on install/remove, settings mutation, library
mutation, raw JSON-RPC and filesystem writes are unavailable. Presets, grants,
scopes or approvals cannot broaden this closed server-owned vocabulary.

## 35. Remote Kodi

Manual registration v1:
- name;
- host/IP;
- port;
- username/password;
- policy/preset.

Generic credential model internally; username/password implementation initially.

Capability levels:
- plain JSON-RPC;
- bridge-enhanced.

Production Bridge bootstrap uses operator installation of the compatible official
Bridge through Kodi Lab's built-in stable repository. At Web startup, the
deployment-pinned, locally verified repository generation and the application's
exact official ZIP digest establish an immutable build record automatically.
Ordinary administrators do not separately approve official builds. Custom builds
retain the existing advanced manual reviewed-build mechanism and compatibility
checks. Kodi Lab verifies native identity separately, pairs Bridge 1.1.15 through
the shared four-character `otp-1` protocol and checks authenticated identity and
target-bound provenance. Household Connect coordinates the existing provenance
confirmation. No automatic refresh, Bridge update or production support-addon
deployment is introduced. Future Bridge deployment/update capability requires
a new post-V1 architecture and security gate. Managed lifecycle trust is separate.

## 36. Hosted Kodi repository

V1 has two distinct repository origins: reviewed infrastructure
`repository.kodi-lab` and a separate opt-in development feed
`repository.kodi-lab-dev`. The stable feed contains its
per-install bootstrap add-on and the exact reviewed bridge ZIP. Its public-to-LAN
distribution server is unauthenticated, read-only HTTP on an explicitly chosen
private-LAN address. It has no administrative Web, API, MCP, database, secrets,
or Docker/broker socket. HTTP gives no transport authenticity against a malicious
LAN actor; a later TLS reverse proxy does not change repository content.

The installer selects the durable base URL and deterministically generates both
repository add-ons for that URL. Installation provenance records generated hashes
separately from immutable bundle inputs. Published ID/version/ZIP bytes are
immutable and metadata changes atomically. The stable feed accepts only reviewed
infrastructure. The development feed accepts only explicitly Web-approved,
bounded Python video-plugin ZIPs and advertises the latest approved version per
add-on. Both repository IDs and the bridge are reserved. A source-free backup
binds publication records to exact development artifact and feed bytes; a
mismatch blocks development serving while stable serving remains available.
Workspace deployment integration and the first manual Kodi development-update
proof belong to Phase 3. Repository availability grants no production mutation
authority. Managed Kodi retains its direct controlled install path.

Repository-mode `workspace_deploy` is available only to a paired, verified
external-development target whose separately installed development repository
has been observed and whose LAN reachability/manual-update setting the operator
confirmed. Preparation reads target inventory and the paired bridge's installed
development-repository tree as preconditions. It packages an explicit source
version from one validated workspace generation and creates a target-bound
deployment plus a prepared, unpublished publication before stopping at
`awaiting_approval`. Exact human Web approval makes that publication live in the
development feed and advances deployment to `awaiting_kodi_update`. The user
then installs or updates the add-on in Kodi and selects **Recheck installation**.
Kodi Lab observes the native add-on ID/version/enabled state and compares the
authenticated bridge's complete installed-tree digest, file count and byte
count with the predicted package. Only that match completes deployment. A
matching version with different tree bytes fails closed. Direct deployment
remains available; production deployment and automatic repository
refresh/install remain prohibited.

## 37. MCP add-on sources

Managed development add-on workflows may use only closed, explicitly
configured sources:
- Lab repo;
- uploaded ZIP;
- registered dev source;
- configured external repo.

Arbitrary unregistered URLs/files blocked.

The current implementation supports the bounded Phase 8 and Phase 12/13 flows;
this list does not create a generic MCP installer. Production cannot install,
update or uninstall add-ons at all through Kodi Lab V1.

## 38. Kodi settings/library/raw JSON-RPC

The earlier broad settings, library search/mutation and raw JSON-RPC proposal is
**rejected for production V1** and is not part of the implemented MCP catalog
for managed or external-development targets either. Kodi Lab uses fixed native
JSON-RPC methods internally behind semantic services; callers cannot supply a
method, player ID, raw parameters or raw protocol payload. Broad persistent
Kodi-settings mutation is likewise outside V1. Any future library/settings
surface is POST-V1 work requiring its own bounded vocabulary, privacy model,
audit design and architecture/security gate.

## 39. GUI/playback/logs/diagnostics/events

V1 GUI/playback MCP authority is only the closed `remote_action` vocabulary.
Production standing actions and one-shot transient actions are enumerated in
Section 2.1; seek, speed, stream/subtitle selection, repeat/shuffle, raw window
selection and normalized player-state tools are not V1. Screenshot behavior is
the implemented `screenshot_capture` contract, not a general authenticated
file/reference service.

MCP log search, native log download, one-call diagnostics, event streaming and
diagnostic resources are **POST-V1 / Phase 15 candidates**. Production log
access is not enabled. Phase 15 may add internal/Web operational diagnostics
without changing the current V1 catalog in Section 2.1; any MCP expansion
requires a separate explicit catalog decision.

## 40. MCP prompts

The original built-in prompt proposal is **POST-V1**. The V1 MCP server exposes
the exact tools in Section 2.1 and makes no prompt, log, library or hidden matrix
orchestration commitment.

## 41. Approvals

Only the seven transient production actions use V1 approvals. MCP creates or
replays one exact pending request and cannot approve it. Authenticated Web
administration may approve once, deny or cancel; the client then retries the
same request UUID and exact action. Pending lifetime is at most fifteen minutes
and an approval is dispatchable for at most five minutes. There are no timed or
permanent capability approvals, bearer approval tokens, MCP approval methods or
raw-parameter cards. Consumption and receipt claim are atomic; ambiguity after
claim means stop, never retry with a new request.

## 42. Instance presets/cloning

Built-in immutable instance presets:
- Developer: latest stable, Small Mixed, Add-on Development;
- Clean Kodi: latest stable, no fixture, minimal extras;
- Compatibility Test: user-selected Kodi version, deterministic fixture, reproducibility emphasis.

Users duplicate into local presets.

v1 supports configuration-only clone.
No multi-version create action in v1.
Cross-version testing is manual UI + MCP-guided workflow; no full matrix runner.

## 43. Export/backup/low disk

Individual instance configuration export first:
- versioned YAML manifest;
- schema version required;
- exact supported schema initially acceptable.

Dev sources referenced by logical name, not host absolute path.
Missing dependencies on import create degraded/unresolved instance rather than hard block where practical.

Portable bundles declarative first, self-contained later.

Whole-lab backup later; architecture should support:
- SQLite;
- non-secret config;
- instance definitions;
- snapshots;
- relevant artifacts;
- exclude rebuildable caches/images.

Low disk:
- warn;
- block new snapshots/heavy storage operations below safety threshold.
Choose default after real measurements.

Reliability before snapshot compression/dedupe.

## 44. Data migrations/upgrades

Automatic forward migrations on startup with backup of SQLite/config metadata first.
v1 rollback = restore pre-migration backup and old app version.

Within same major version, any earlier stable should upgrade directly to latest stable.

Kodi Lab upgrades leave existing runtime revision untouched unless explicit migration.
No silent Kodi version upgrade.
Explicit in-place Kodi upgrade may exist; recommend snapshot.
New instance on newer runtime remains preferred reproducible workflow.

## 45. CI/release gate

Early CI:
- unit/service/domain tests;
- config tests;
- broker security tests;
- Docker lifecycle;
- one current stable Kodi integration.

Later full Kodi E2E.

The stable V1 release validates its reviewed Matrix 19.4, Nexus 20.5, and
Omega 21.3 recipes and their exact candidate behavior. Historical/private
Kodi 19.5 and Piers compatibility runs do not create release obligations.

MCP public-v1 gate:
- managed dev target;
- remote target;
- Production and Development policy;
- allowed/blocked/approval behavior;
- the exact nineteen-tool contract in Section 2.1; parity with an older Kodi MCP
  tool set is explicitly not required.

Manual capability checklist acceptable initially; automate later.

## 46. Core public-v1 acceptance

The following list states V1 release acceptance. The local binary-free Linux
amd64 installation foundation is implemented; public publication and
exact-candidate acceptance remain separate. Historical phase checkpoints
below do not reopen completed implementation slices.

Must include at least by public release:
- one documented, accepted clean public installation path; local Compose and
  installation guidance exist, while exact-candidate RC acceptance remains;
- first-run setup;
- reviewed Matrix 19.4, Nexus 20.5, and Omega 21.3
  creation/readiness from the current binary-free release recipes;
- stop/restart/reset/delete;
- synthetic libraries;
- screenshots all curated runtimes;
- at least one interactive-display path where host permits;
- reliable snapshots;
- ZIP install;
- dev source attach/sync through an accepted release development-source path
  (REQUIRED BEFORE RC; the local development flow alone does not prove this);
- agent working copy and patch-back;
- health;
- Phase 15 operational diagnostics/hardening evidence; no MCP diagnostic tool
  is implied;
- required web workflows;
- the exact nineteen-tool MCP catalog in Section 2.1;
- approved bounded development add-on workflows;
- screenshots and closed semantic actions according to policy; MCP logs,
  diagnostics and raw JSON-RPC are not V1 requirements;
- remote registration with fixed native semantic JSON-RPC calls;
- exact current reviewed release-ZIP bridge bootstrap with published
  checksum/provenance (REQUIRED BEFORE RC);
- production restrictions and approvals;
- token lifecycle;
- audit;
- migration tests;
- no unrestricted Docker socket;
- no secret leakage;
- manual install/upgrade/rollback docs.

## 47. Implementation phases

The sequence below records project phase ownership and historical checkpoints.
Completed phase text, including four-runtime private compatibility acceptance,
does not change the current three-runtime stable V1 release contract.

The Phase 8 uploaded-ZIP first slice interprets “ZIP install” narrowly:
authenticated upload produces an immutable, bounded, validated private artifact;
installation is Lab-controlled normalized materialization into an absent managed
destination with operation-owned stop/restart and exact readback. It is not
Kodi's native Install-from-ZIP installer and grants no replacement, upgrade,
enablement, execution, dependency/repository, bind, caller-path, or generic
filesystem/JSON-RPC authority. Those capabilities require later explicit gates.

Phase 0 — Repository foundation  
Phase 1 — Core domain/storage  
Phase 2 — Restricted container broker  
Phase 3 — Single Kodi runtime end-to-end  
Phase 4 — Web UI vertical slice  
Phase 5 — Curated Kodi matrix  
Phase 6 — Fixtures/reset  
Phase 7 — Snapshots  
Phase 8 — Add-on development workflows  
Phase 9 — Agent working copy  
Phase 10 — Browser remote/display  
Phase 11 — MCP core  
Phase 12 — MCP development workflows  
Phase 13 — Remote targets  
Phase 14 — Production policy/approvals  
Phase 15 — Diagnostics/hardening  
Phase 16 — Public release preparation

Phase 13 starts with constrained external-development slices. Slice 1 lets
authenticated Web administrators register an `external_development` Kodi,
perform bounded native read-only verification, explicitly confirm observed
identity, and manage enable/disable/deactivation. Slice 2 lets the existing
`target_list`, `target_status`, `addon_list` and closed `remote_action` tools
use an exactly granted, enabled, verified, identity-matching target through a
fixed native adapter. Configured capability is only a maximum and must
intersect read-only technical proof, target policy, client permission and exact
grant. Slice 3 adds an optional Web-paired, persistent-UUID bridge with a closed
authenticated identity/capability/screenshot protocol. It admits eligible
external development targets to the unchanged `screenshot_capture` schema and
returns bounded in-memory PNG bytes without a remote path. Mutual HMAC,
request/nonce replay bounds and pinned bridge/add-on/Kodi/build identity protect
the LAN exchange. Slice 4 additively gives that bridge a pathless content-
addressed workspace-deployment capability, retains fixed execution on the
native closed video-plugin smoke, and accepts exact external deployment/
execution evidence for the unchanged Web-reviewed promotion request. Web alone
establishes the exact DevelopmentSource baseline and reconciles ambiguous
terminal bridge receipts; drift blocks mutation. The common target UUID,
fifteen-tool catalog and tool schemas remain unchanged; credentials and bridge
secrets remain protected outside SQLite. External-production type, arbitrary
file/add-on management, host lifecycle and production control remain
unavailable.

**Phase 13 — Remote targets: COMPLETE.** Independent formal review accepted
the external-development boundary after D-0302 closed bridge replay-capacity
and native total-deadline defects. Phase 14 remains the first possible scope
for external-production policy, approval or activation.

## 48. Recommended module boundaries

Example:

kodi_lab/
  app/
    services/
    models/
    policies/
    jobs/
  adapters/
    web/
    api/
    mcp/
  runtime/
    catalog/
    managed/
    remote/
  kodi/
    jsonrpc/
    bridge/
    screenshots/
    logs/
    addons/
  storage/
    sqlite/
    filesystem/
    secrets/
  broker/
    client/
  dev/
    sources/
    working_copy/
    patches/
  diagnostics/
  fixtures/

Version-specific behavior should live in runtime/Kodi adapters rather than service-layer conditionals.

## 49. First internal milestone (historical)

Before full MCP parity:
- the then-curated four private Kodi versions launch/stop/reset;
- screenshots;
- optional noVNC;
- snapshots;
- ZIP install;
- minimal web UI;
- create/delete;
- health;
- clean broker lifecycle.

## 50. Defining product rule

**Kodi Lab administers environments through the web UI; MCP operates and develops against Kodi targets.**

Primary developer flow:
create isolated Kodi instance → attach/install add-on → inspect/manipulate via UI or MCP → reproduce behavior → modify protected working copy → review/apply patch → retest → repeat on another Kodi version.

## 51. Phase 14 production foundation boundary

External production is a separate immutable target class layered on the shared
external endpoint and native-trust representation. Completed registrations
require a production policy subtype and separate Web-only native-identity and
production-designation confirmations. Registration starts disabled/unverified;
probe alone establishes no trust or designation. Material configuration or
identity drift advances a monotonic generation and fails closed.

Production mutations are globally blocked by default with a monotonic policy
epoch and may also be suspended per target. Removing a suspension grants no
authority. Slice 1 adds no production MCP, bridge, screenshot, remote control,
approval, workspace, deployment, execution, promotion, lifecycle, DNS/WAN/SSH,
shell, filesystem or raw JSON-RPC capability. The MCP catalog remains exactly
fifteen tools and one shared authorization veto denies production through both
direct target and workspace-target paths.

## 52. Phase 14 read-only production inspection boundary

Slice 2 permits only the existing `target_list`, `target_status` and
`addon_list` tools against `external_production`. Authority requires a current
credential/enabled client, the exact immutable Production Inspection client
and target preset, `target.inspect`, exact generic grant, active current-
generation inspect scope, confirmed production policy/native identity/
designation, enabled verified coherent target state, required configured and
verified capabilities, registry eligibility and post-network reauthorization.

Listing uses stored trust and reveals no ineligible production target or
network/credential/observation detail. Direct status and inventory perform a
fixed fresh native identity preamble with bounded deadlines; inventory retains
64-record native pages, 2,048-record total, 100-record MCP page and path-free
sub-64-KiB semantic output. Identity drift revokes local trust and advances the
generation once without changing Kodi.

Production read rates and concurrency are separate and bounded, with no server
retry. Global and per-target mutation switches do not block inspection. Scope,
preset and grant administration is Web-only. The other twelve tools,
workspace-target paths, bridge/screenshot/control, approvals, deployment and
lifecycle remain unavailable. Phase 14 remains partial.

## 53. Phase 14 production visual inspection boundary

Slice 3 reuses bridge protocol v1 and final Phase 13 GUI screenshot capture.
Production pairing is Web-only and requires coherent native production trust,
an active explicitly reviewed package record, one-time possession proof,
configuration-bound HTTP acknowledgement when applicable, and separate exact
installed-target/provenance confirmation. Bridge trust has a positive monotonic
revision independent of native identity generation; pairing alone grants no MCP
authority.

An immutable `visual` scope binds exact client, production target, native
generation and bridge revision and is effective only beside a current active
`inspect` scope. The production-only, assignable, non-clonable `Production
Visual Inspection` preset contains exactly `target.inspect` and
`target.visual`; no other preset can publish production screenshot pixels.

The existing `screenshot_capture` is the fourth of exactly fifteen
production-eligible tools. Its optional UUID `request_id` is required for
production and backward-compatible elsewhere. Durable client/target/parameter
identity and bridge replay protection prevent silent recapture. Replay resolves
before one-per-30-second client-target and one-global-capture admission. Pixels
are memory-only and are withheld unless the entire native/bridge/scope/registry
intersection remains valid after capture.

Only `screenshot` is accepted as production bridge capability even when a
reviewed bridge advertises development deployment features. The other eleven
tools, especially `remote_action`, workspace/deployment/execution, approvals and
lifecycle, remain denied. Phase 14 remains partial.

## 54. Phase 14 constrained production navigation boundary

Slice 4A makes the existing `remote_action` the fifth of exactly fifteen
production-eligible tools while retaining centralized server-owned action
policy. Production permits only `up`, `down`, `left`, `right`, `back`, `home`
and `info`. `context_menu` and `select` are prohibited, while playback, track
and volume actions remain hard denied pending separately reviewed Slice 4B.
External development retains the complete sixteen-action vocabulary.

New dispatch requires the exact non-clonable Production Remote Kodi maximum on
client and target, generic grant, active current inspect and navigate scopes,
current native generation and reviewed bridge revision, verified semantic-
remote capability, clear global and target mutation vetoes, durable receipt/
audit capacity and fresh bridge/native identity followed by immediate
reauthorization. The bridge authenticates identity only; the fixed semantic
command is sent once over the native adapter and never retried.

Native HTTP control requires separate immutable Web acknowledgement of the
unprotected native transport; HTTPS retains normal certificate validation.
Phase 11 replay remains client/request scoped and cross-transport, with an
immutable production authority binding and protected pending/ambiguous history.
Only coherent completed base, binding and audit evidence expires atomically
after the Phase 11 30-day replay window; malformed relationships fail closed.
Navigation is two actions/second/client-target with burst three, one in flight
per target and four globally beneath the external ceiling. No approval queue,
transient control, deployment, execution, lifecycle, shell, filesystem, SSH,
arbitrary JSON-RPC or support-add-on deployment is added. Phase 14 remains
partial.

## 55. Phase 14 one-shot transient production control boundary

Slice 4B preserves the seven standing navigation actions and admits exactly
seven approval-required actions: `play_pause`, `stop`, `previous`, `next`,
`volume_up`, `volume_down` and `mute_toggle`. `context_menu` and `select` remain
unconditionally prohibited. External-development retains all sixteen actions.
The MCP catalog remains exactly fifteen tools and gains no approval tool.

New transient requests require the exact Production Remote Kodi client and
target maximum, current generic grant UUID, active current inspect and
`request_transient_control` scope UUIDs, initiating credential, client authority
revision, native generation, bridge revision, target policy revision, mutation
epoch, reviewed build, semantic capability and applicable native-HTTP
acknowledgement. Navigate and visual scopes are not required. MCP creates only a
pending exact request record; authenticated Web administration alone may
approve, deny or cancel it under Host, Origin and CSRF protection.

Pending lifetime is at most fifteen minutes and the approved dispatch window is
at most five minutes from approval. Neither can be extended. Before claim, the
adapter performs fresh authenticated bridge identity and read-only native
identity/version/method preflight. Temporary unreachability leaves approval
approved; durable authority, policy, capability or identity change cancels it
with a closed reason.

Consumption, Phase 11 base receipt insertion, production transient binding,
semantic dispatch audit, consumed event and durable admission state are one
SQLite transaction. Only after commit may one fixed server-derived native
mapping be sent. Crash after commit leaves a protected pending receipt and
never redispatches, intentionally permitting an approved action not to occur.
Exact duplicates replay approval state before consumption and the Phase 11
receipt afterward; changed semantics conflict across modern HTTP, stdio and
compatibility HTTP.

New approvals are limited to one per thirty seconds/client-target, eight live
per client and 128 globally. Transient claim is one per five seconds/client-
target with burst one, one in flight per target and one globally. Terminal
history is bounded to 1,000/client and 10,000 globally with approximately
30-day retention. Live, pending, ambiguous, malformed or incoherent evidence is
never evicted. Slice 4B adds no deployment, execution, lifecycle, support-addon,
shell, filesystem, SSH or arbitrary JSON-RPC authority. Phase 14 remains partial.
The independent completion audit closes Slice 4B with permanent one-use grant
identity, monotonic direct-state revision, atomic policy-cancellation and
coherent approval/receipt retention guards; it does not authorize a later
Phase 14 slice.

## 56. Phase 14 validation and closeout boundary (historical checkpoint)

Production support-addon deployment is formally deferred post-V1. It is not a
Phase 14 completion blocker. Production V1 bootstrap is ordinary operator-
owned manual installation of an exact reviewed bridge release ZIP after
checksum verification. Kodi Lab then establishes only its own trust: Web
reviewed-build registration, one-time pairing, explicit installed provenance
confirmation and revision-bound scopes. Separating installation authority from
product trust avoids turning the bridge into a generic deployment or lifecycle
channel. Any future bridge updater/deployer requires a new architecture and
security gate; automatic upgrades and generic production deployment remain
outside authority.

The Phase 13 reviewed 1.1.7 archive has a deterministic exact ZIP hash and a
separate embedded semantic fingerprint. Its packaged README says never to use
the add-on with production and its `addon.xml` summary says external-
development-only. Correcting either packaged file changes the exact ZIP hash;
wording alone does not change the hard-coded semantic fingerprint. Therefore
1.1.7 remains historical Phase 13 evidence but cannot silently be represented
as the corrected production release artifact. The minimal recommendation is a
1.1.8 packaging-only successor with a synchronized version constant, newly
computed deterministic ZIP hash and semantic fingerprint, fresh independent
source/package review, and a newly registered reviewed-build record. That
successor was subsequently built as a local candidate; its independent
exact-artifact review, registration, and publication remain separate.

At this preparation checkpoint, Phase 14 closeout required:

1. this V1 boundary reconciliation and the production/manual bridge operator
   documentation are accepted;
2. the exact production bridge artifact decision is resolved through its
   separate review gate;
3. Stages A through D are executed later under explicit live-validation
   authorization and produce bounded redacted evidence;
4. an independent Phase 14 closeout audit reproduces the required invariants;
5. one authoritative final repository gate passes after the final content
   change.

At that checkpoint Phase 14 was **PARTIAL**. The list is historical phase
evidence, not a fresh requirement to repeat completed implementation work.
The current reviewed bridge ZIP/provenance gap remains REQUIRED BEFORE RC.

Phase 15 remains Diagnostics/hardening: bounded operational diagnostics,
performance, security hardening and remaining operational validation as its
canonical phase gate determines. It must not silently expand the V1 MCP or
production catalog. Phase 16 owns public release preparation. The local
`1.0.0rc1` candidate already has Compose, packaging, and installation guidance;
exact-candidate acceptance and publication remain open gates.

## 57. Web-only production screenshot maximum amendment

An authenticated Web administrator may explicitly add or remove only the exact
`screenshot` member of an existing verified, confirmed and enabled production
target's configured capability maximum. The request carries the complete
expected configured set, expected policy revision and an exact human
acknowledgement. It is not a generic capability editor, cannot change native
verified evidence and has no MCP equivalent. The database rejects any material
production configured-set change that lacks a matching valid amendment row.

The configured set is only the operator-approved semantic maximum. Adding
`screenshot` advances and binds the production policy revision once but does
not change identity generation, native verification, designation, enablement,
inspection scope, bridge trust, bridge verification, visual scope, transport
acknowledgement or receipt state. Screenshot authority remains the full
configured maximum, reviewed bridge, verified bridge capability, preset,
grant, scope, generation, revision and transport intersection.

Removing `screenshot` advances that same revision once, writes an immutable
exact audit, creates or advances a monotonic bridge tombstone, retires pairing
material, revokes bridge trust and all current visual/navigate/transient-request
scopes, and cancels pending or approved transient operations. Native inspection,
the generic grant, current target preset, target enablement, reviewed-build
registry, receipts, acknowledgements and immutable history remain. Retained
history is permanently stale and remove/re-add cannot reactivate it.

Future V1 onboarding may suggest role-based configured maxima, but it must
never silently approve or enlarge one. Phase 16 onboarding is not implemented
by this amendment.
