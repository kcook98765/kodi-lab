# Kodi Lab — v1 Project Specification

Status: implementation specification  
Working name: Kodi Lab  
Primary audience: Kodi add-on developers  
Primary platform: Linux/Docker  
Versioning: Semantic Versioning

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
- curated support beginning with Kodi 19.5, 20.5, 21.3, and a Kodi 22-series target;
- persistent and disposable instances;
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
- a public installation shape finalized during Phase 16 release preparation;
  the original Compose requirement remains the preferred release candidate but
  is not a Phase 14 requirement.

### 2.1 Current normative V1 MCP and production contract

This section supersedes the broader speculative MCP catalog and production
capabilities retained later in this document as design history.

The authoritative V1 MCP catalog contains exactly fifteen tools:

- `target_list`
- `target_status`
- `addon_list`
- `screenshot_capture`
- `remote_action`
- `development_source_list`
- `workspace_create`
- `workspace_status`
- `workspace_tree`
- `workspace_read`
- `workspace_edit`
- `workspace_validate`
- `workspace_deploy`
- `workspace_execute`
- `promotion_request`

Exactly five are production-eligible: `target_list`, `target_status`,
`addon_list`, `screenshot_capture`, and `remote_action`. Production has no
deployment tool. Raw JSON-RPC, production logs/diagnostics/library queries,
generic add-on installation/update, production lifecycle, host lifecycle,
shell, SSH, generic filesystem access, user-development workspace deployment
to production, and broad persistent Kodi-settings mutation are outside the V1
production authority. The seven standing production actions are `up`, `down`,
`left`, `right`, `back`, `home`, and `info`; the seven transient actions require
one-shot Web approval; `context_menu` and `select` are always prohibited.

Production bridge bootstrap is manual installation of an exact reviewed ZIP
whose published checksum and provenance are registered and confirmed through
Web administration. A hosted Kodi repository is optional post-V1 convenience,
not a production bootstrap dependency. Kodi Lab does not automatically install
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
- Windows/macOS host support;
- official ARM64 support;
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
- optional hosted Kodi repository (POST-V1 distribution convenience, not a
  production bridge requirement).

## 6. Deployment

Phase 16 owns the canonical public V1 installation contract. The original
release target remains versioned GHCR images plus a versioned Docker Compose
file, but no Compose definition exists at the Phase 14 checkpoint. Therefore
Compose is a Phase 16 release-packaging acceptance item, not a Phase 14
closeout blocker and not a supported install mechanism that may be claimed
today.

Phase 16 should also provide:
- Git clone + Compose for contributors/development;
- GitHub release archives;
- checksums;
- bundled runtime catalog/manifests;
- example configuration.

Host policy:
- Docker-capable Linux generally;
- Ubuntu is the CI-validated host for v1;
- x86_64 official initially;
- ARM64 architecture anticipated but roadmap only.

## 7. Releases

Use SemVer for public releases. The repository's static `0.0.0` package version
is an unreleased-development sentinel, not a published compatibility promise.
Phase 16 release preparation owns selecting `1.0.0`, synchronizing package and
artifact versions, and performing the release checklist; Phase 14 does not
change the public version.
Stable images: exact version plus `latest`.
Prerelease images: exact prerelease plus `edge`.
`latest` must never point to beta/RC.

Release bundle:
- Compose file;
- checksums;
- source archive;
- runtime catalog/manifests;
- example configuration;
- release notes;
- standard GitHub provenance/signing where practical.

## 8. Persistent data

One configurable data root, recommended:
`/var/lib/kodi-lab`

Suggested subdirectories:
- database/
- secrets/
- instances/
- snapshots/
- artifacts/
- screenshots/
- logs/
- runtime-builds/
- cache/

Dedicated Kodi Lab service user/group owns the persistent data root.

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

One Kodi instance per container.
Types:
- Disposable
- Persistent

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

Default:
- sensible generated name;
- starts after creation;
- host reboot default is stay stopped;
- latest stable Kodi;
- Small Mixed synthetic library;
- Add-on Development MCP preset.

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

Initial curated matrix:
- Kodi 19.5
- Kodi 20.5
- Kodi 21.3
- Kodi 22-series target

Policy:
- Current = latest stable major + previous major;
- Legacy = older best-effort, hidden behind Include Legacy;
- beta/RC/nightly hidden behind Include Pre-release.

Official runtime catalog ships with Kodi Lab releases in v1.

Runtime sources:
- official Kodi packages/repos by default;
- advanced alternate repo/package URL;
- local package/build;
- custom container image.

Common Linux runtime foundation where practical.

## 21. Runtime revisions/custom builds

Same Kodi version rebuilt from different base/source/build = distinct runtime revision.
Existing instances pin exact revision until explicit upgrade.
New instances use newest approved revision by default.

Custom manifests allowed without code changes.

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

First-class v1 feature.
Central storage outside active instance filesystem.

Creation:
1. record prior running/stopped state;
2. stop Kodi;
3. snapshot;
4. basic validate;
5. restore prior state.

One heavy job globally at a time.
Default timestamp/state name + optional custom name.
No automatic retention limit initially.
Per-instance browsing sufficient initially.

Restore:
- warn;
- stop;
- restore;
- validate;
- restart if previously running.

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
does not add tools to the authoritative fifteen-tool catalog. Native production
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

The implemented flat catalog is the exact fifteen-tool list in Section 2.1.
Earlier candidate names such as `player_state`, `library_search`,
`addon_install`, `log_search`, and `jsonrpc_call` were never V1 commitments and
are not implied missing tools. Raw JSON-RPC, lifecycle, production deployment,
production logs/diagnostics and production library queries are rejected V1
authority, not discoverable hidden methods.

Phase 11 originally exposed five tools. Phases 12 through 14 expanded behavior
under the same schema and finalized the fifteen-tool V1 catalog. Add-on
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

Users duplicate an active built-in into a custom preset with a new UUID and
bounded unique name. V1 custom permissions are an immutable creation-time
snapshot: there is no arbitrary permission editor, later built-in permissions
are not inherited automatically, and the custom preset may be assigned at the
client and internal-target policy layers. Custom rename and deletion are
deferred.

Assign preset at client/token and target.
Effective access is intersection.

## 33. Managed development policy

V1 managed development is the least-authority fifteen-tool contract. It
supports bounded workspace inspection/create/edit/validate, exact approved
deployment, fixed execution, Web-reviewed promotion requests, bounded target
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

Production bridge bootstrap is manual operator installation of an exact
reviewed release ZIP after checksum verification. Kodi Lab verifies native
identity separately, registers the reviewed build in Web administration, pairs
with a show-once token and requires explicit provenance confirmation. There is
no repository prerequisite, automatic refresh, bridge update or production
support-addon deployment. Future bridge deployment/update capability requires
a new post-V1 architecture and security gate.

## 36. Hosted Kodi repository

**POST-V1 optional distribution convenience.** A hosted Kodi repository is not
required for Phase 14, V1 production validation, or bridge functionality.
Canonical V1 bridge distribution is a release ZIP with published checksum and
provenance. Generic repository-driven production installation/update and
automatic bridge upgrade remain outside Kodi Lab product authority.

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
without changing the fifteen-tool V1 catalog; any MCP expansion requires a
separate explicit catalog decision.

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

Stable releases validate curated matrix:
- 19.5
- 20.5
- 21.3
- 22-series target

MCP public-v1 gate:
- managed dev target;
- remote target;
- Production and Development policy;
- allowed/blocked/approval behavior;
- the exact fifteen-tool contract in Section 2.1; parity with an older Kodi MCP
  tool set is explicitly not required.

Manual capability checklist acceptable initially; automate later.

## 46. Core public-v1 acceptance

The following list is reconciled to the implemented V1 contract. Phase 16 owns
public packaging/installation acceptance; Phase 15 owns the remaining
operational diagnostics/hardening. Neither is a Phase 14 implementation slice.

Must include at least by public release:
- one documented clean public installation path; the planned Compose path is a
  Phase 16 deliverable and is not yet present;
- first-run setup;
- curated four-version creation/readiness;
- stop/restart/reset/delete;
- synthetic libraries;
- screenshots all curated runtimes;
- at least one interactive-display path where host permits;
- reliable snapshots;
- ZIP install;
- dev source attach/sync;
- agent working copy and patch-back;
- health;
- Phase 15 operational diagnostics/hardening evidence; no MCP diagnostic tool
  is implied;
- required web workflows;
- the exact fifteen-tool MCP catalog in Section 2.1;
- approved bounded development add-on workflows;
- screenshots and closed semantic actions according to policy; MCP logs,
  diagnostics and raw JSON-RPC are not V1 requirements;
- remote registration with fixed native semantic JSON-RPC calls;
- exact reviewed release-ZIP bridge bootstrap with checksum/provenance;
- production restrictions and approvals;
- token lifecycle;
- audit;
- migration tests;
- no unrestricted Docker socket;
- no secret leakage;
- manual install/upgrade/rollback docs.

## 47. Implementation phases

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

## 49. First internal milestone

Before full MCP parity:
- all four Kodi versions launch/stop/reset;
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

## 56. Phase 14 validation and closeout boundary

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
source/package review, and a newly registered reviewed-build record. This
specification does not authorize that version change or publication.

Phase 14 may close only after:

1. this V1 boundary reconciliation and the production/manual bridge operator
   documentation are accepted;
2. the exact production bridge artifact decision is resolved through its
   separate review gate;
3. Stages A through D are executed later under explicit live-validation
   authorization and produce bounded redacted evidence;
4. an independent Phase 14 closeout audit reproduces the required invariants;
5. one authoritative final repository gate passes after the final content
   change.

Until then Phase 14 is **PARTIAL**. Stage A-D execution, live production/LAN
contact, bridge publication, support-addon deployment and Phase 15 work are not
authorized by this preparation slice.

Phase 15 remains Diagnostics/hardening: bounded operational diagnostics,
performance, security hardening and remaining operational validation as its
canonical phase gate determines. It must not silently expand the V1 MCP or
production catalog. Phase 16 remains Public release preparation: public
installation/Compose ownership, release packaging, the first public SemVer,
artifact/checksum/provenance publication, public documentation and release
checklist. The static project version stays `0.0.0` until that release gate
selects and validates `1.0.0`.

## 57. Web-only production screenshot maximum amendment

An authenticated Web administrator may explicitly add or remove only the exact
`screenshot` member of an existing verified, confirmed and enabled production
target's configured capability maximum. The request carries the complete
expected configured set, expected policy revision and an exact human
acknowledgement. It is not a generic capability editor, cannot change native
verified evidence and has no MCP equivalent.

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
