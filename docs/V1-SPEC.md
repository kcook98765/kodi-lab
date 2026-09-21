# Kodi Lab — v1 Project Specification

Status: implementation specification  
Working name: Kodi Lab  
Primary audience: Kodi add-on developers  
Primary platform: Linux/Docker  
Versioning: Semantic Versioning

## 1. Product definition

Kodi Lab is a self-hosted development and testing environment for creating, running, inspecting, debugging, and comparing isolated Kodi installations.

It provides managed Kodi instances, synthetic libraries, snapshots, add-on deployment workflows, visual access, diagnostics, and integrated MCP access suitable for human developers and AI coding agents.

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
- production/development policies and approvals;
- logging, history, diagnostics;
- restricted container-management broker;
- SQLite metadata;
- Docker Compose deployment.

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

MCP owns target interaction and agent-development workflows such as:
- JSON-RPC;
- playback/navigation;
- library query/mutation;
- logs/screenshots/diagnostics;
- Kodi/add-on settings according to policy;
- approved add-on install/update;
- managed development working copies;
- bug reproduction and cross-version comparison prompts.

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
- hosted Kodi repository.

## 6. Deployment

Canonical public v1 install:
- versioned GHCR images;
- versioned Docker Compose file.

Also provide:
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

Use SemVer.
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

Core API/service methods should be domain-oriented:
- create_instance
- start_instance
- stop_instance
- capture_screenshot
- install_addon
- sync_dev_source
- restore_snapshot
- query_library

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

Example capabilities:
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

One-click diagnostic bundle in v1:
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

Flat explicit names preferred:
- target_list
- player_state
- playback_pause
- library_search
- addon_install
- log_search
- screenshot_capture
- jsonrpc_call

MCP tool/API schema has separate explicit version.

Phase 11 retains Kodi Lab schema version `1` and exposes exactly `addon_list`,
`remote_action`, `screenshot_capture`, `target_list`, and `target_status`.
Add-on inventory is bounded and path-free. Screenshot capture returns native
`image/png` MCP content plus compact semantic metadata while updating the
existing durable latest-screenshot cache. Remote action is the existing closed
semantic control surface with durable client/request replay protection. Raw
JSON-RPC, lifecycle, diagnostics, external targets, and the richer semantic
catalog remain later phases or separately scheduled expansion.

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

Optimize for low-friction agents.
May allow:
- broad Kodi JSON-RPC;
- library mutations;
- addon install/uninstall;
- broad addon settings;
- broad ordinary Kodi settings;
- raw JSON-RPC;
- working-copy edits;
- logs/screenshots;
- navigation/playback;
- approved dev filesystem writes.

Avoid unnecessary confirmations for normal dev-target operations.

## 34. Production remote policy

Conservative defaults:
Allowed:
- read-only inspection;
- logs;
- playback/navigation;
- safe library queries;
- Kodi Lab repo refresh;
- approved support-addon updates.

Blocked by default:
- arbitrary addon install/remove;
- broad settings;
- destructive library mutation;
- raw JSON-RPC;
- arbitrary filesystem writes.

Users may explicitly broaden.

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

Initial production repo/bridge bootstrap manual.
Kodi Lab shows local repo URL/instructions.

After bootstrap, may automate:
- own repo refresh;
- Kodi Lab/MCP support-addon updates.

Verify:
- JSON-RPC;
- repo presence;
- bridge presence/version.

## 36. Hosted Kodi repository

Kodi Lab hosts canonical local repository endpoint containing Kodi Lab/MCP support add-ons required by bridge functionality.

Reuse concepts proven by existing Kodi MCP deployment, but no exact API compatibility requirement.

## 37. MCP add-on sources

Managed Dev may install/update only from sources explicitly configured for target:
- Lab repo;
- uploaded ZIP;
- registered dev source;
- configured external repo.

Arbitrary unregistered URLs/files blocked.

Managed Dev may uninstall approved dev/test add-ons.
Production remote cannot arbitrarily uninstall by default.

## 38. Kodi settings/library/raw JSON-RPC

Managed Dev:
- broad ordinary Kodi settings read/write;
- broad library mutations;
- unrestricted raw Kodi JSON-RPC.

Production:
- editable writable settings allowlist;
- high-impact settings may require approval;
- raw JSON-RPC disabled by default.

Record prior setting values where practical and support one-level restore.

Library coverage:
- movies;
- TV;
- seasons;
- episodes;
- music videos;
- files;
- playlists;
- metadata.

Search supports compound filters, sort, limit/offset; default page size 25; one primary sort key initially.

Mutations may include scan/clean, watched/resume, playlist and reliable item edits, policy-controlled.

Audit raw JSON-RPC method/params/result/error; redact secrets; audit result may truncate while caller gets full result where practical.

## 39. GUI/playback/logs/diagnostics/events

GUI:
- active window/dialog/focused control normalized;
- optional raw details;
- direct GUI/window actions where supported;
- production defaults to basic navigation.

Playback:
- play/pause/stop;
- seek;
- speed;
- next/prev;
- stream/subtitle select;
- volume/mute;
- repeat/shuffle;
- normalized fresh player state.

Logs:
- current and previous Kodi log;
- substring and regex search;
- default ~50 matches, 2 context lines;
- small inline, large authenticated file reference;
- `[REDACTED]` for obvious secrets;
- production log access enabled by default and audited.

One-call MCP diagnostics:
- Kodi version/health;
- bridge/repo;
- policy/preset;
- addon summary;
- recent errors;
- screenshot metadata;
- relevant history.

Screenshots:
- fresh capture request;
- authenticated retrievable image reference;
- stale cached fallback with age if unavailable.

Events v1:
- playback start/stop/pause;
- library scan start/complete;
- addon install/enable/disable;
- GUI window/dialog changes.
Streaming preferred, polling fallback with ~15-minute retention.
No event-triggered automation engine.

Resources:
- target status/capabilities;
- current/previous logs;
- current screenshot;
- instance config summary;
- installed addon inventory.

## 40. MCP prompts

Built-in immutable prompts:
1. Inspect/health selected target.
2. Diagnose addon install/launch failure.
3. Compare behavior across versions.
4. Reproduce bug with logs/screenshots/GUI/library/addon state.

Users duplicate/customize.
Typed parameters.
Clients only see executable prompts.
Prompt run history parent + child tool calls.

Cross-version prompt guides agent loop; no hidden matrix orchestrator.

Bug repro inputs:
- description;
- optional addon ID;
- expected/actual;
- preferred targets.

No separate repro-artifact subsystem required v1.

## 41. Approvals

Production blocked-but-eligible action creates pending approval in web UI.
Client retries after approval.
No MCP inline bypass.

Approval types:
- one-time;
- time-limited;
- permanent.

Pending expiry default 15m configurable.
One-time binds exact params.
Timed/permanent capability-level.
Target-specific and initially client/session-specific.
Exact duplicate pending requests reuse same request.

Approval card:
- target;
- client/session;
- capability/action;
- summary;
- parameters;
- scope;
- expandable raw params.

Client preset prohibition cannot be bypassed by approval.
Revocation affects new calls immediately; running call may finish.
Persistent audit includes approver, timestamps, expiry/revocation/outcome.

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
- meaningful parity with existing Kodi MCP capabilities.

Manual capability checklist acceptable initially; automate later.

## 46. Core public-v1 acceptance

Must include at least:
- clean Compose deployment;
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
- logs/diagnostic bundle;
- required web workflows;
- MCP list/select and normal Kodi operations;
- approved dev add-on install/update;
- logs/screenshots/diagnostics/raw JSON-RPC according to policy;
- remote registration/plain JSON-RPC;
- bridge-enhanced remote;
- production restrictions and approvals;
- token lifecycle;
- audit;
- migration tests;
- no unrestricted Docker socket;
- no secret leakage;
- install/upgrade docs.

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

Phase 13 starts with a constrained Slice 1: authenticated Web administrators
may register only `external_development` Kodi targets, run bounded native
read-only verification, explicitly confirm observed identity, and manage
enable/disable/deactivation. The external subtype reuses the common target UUID;
credentials are protected outside SQLite. External-production type, MCP use of
external targets, bridge pairing, screenshots, deployment, execution and
lifecycle control remain unavailable until later accepted slices.

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
