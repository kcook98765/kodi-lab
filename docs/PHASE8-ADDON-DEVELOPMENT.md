# Phase 8 add-on development contract

Phase 8 delivers the canonical add-on development workflows from
`V1-SPEC.md`: ZIP installation, bind-mounted local development source,
copy/sync local development source, repository URL testing, registered global
sources, manual non-deleting sync, protected repository replacement, and known
add-on inventory/provenance. Agent working copies remain Phase 9, and MCP
development workflows remain Phase 12.

## Step 1 boundary

Step 1 establishes only the create/read catalog for local source directories.
It does not attach a source to a ManagedInstance and cannot materialize any byte
inside Kodi. It adds no broker request, Docker access, Web mutation, ZIP or
repository workflow, Git interpretation, add-on inventory, watcher, operation
history, deployment reconciliation, or compatibility policy.

The only source kind is `local_directory`. ZIPs and repository URLs are separate
future input workflows. A Git checkout has no special meaning and may later be
registered only as an ordinary directory.

## Configuration and path authority

`KODI_LAB_DEVELOPMENT_ROOT` is optional bootstrap configuration with no default.
Registration is unavailable while it is unset. When supplied, it must be a
normalized non-root absolute path. Tests and validation use dedicated temporary
roots; the production data and runtime roots are unrelated.

Callers supply only a normalized root-relative locator. Absolute paths, empty,
dot, parent, NUL, backslash, and overlong components are rejected. The validator
opens the configured root from `/` one component at a time and opens every
locator component relative to the preceding descriptor with `O_NOFOLLOW` and
`O_DIRECTORY`. A linked root, linked source, intermediate link, escape,
non-directory, missing, or unreadable object fails closed. Neither the database
nor the conservative application DTO contains the derived absolute path.

There is no filesystem browser and no host copy authority.

## Registration identity

A DevelopmentSource records:

- server-generated UUID;
- unique trimmed NFC logical name, at most 120 code points;
- fixed `local_directory` source kind;
- unique canonical relative locator;
- unique stable add-on ID parsed from `addon.xml`;
- observed add-on name and version;
- sorted unique extension-point identifiers;
- UTC registration and validation timestamps.

The request never supplies add-on identity. V1 describes sources as globally
reusable and does not define alternate source trees for one add-on ID, so Step 1
permits one registration per parsed ID. No update or unregister method exists.

Name, version, and source content are mutable observations. `validated_at` means
only that the metadata passed validation during registration. A later read-only
validation sees current bytes without rewriting stored catalog data. Changing
the add-on ID changes identity and requires a future explicit workflow.

## addon.xml validation

Registration examines only exact root-level `addon.xml`; it does not recurse
into the source tree and therefore does not decide VCS or deployment-tree rules.
The file is opened without following links, must be regular with link count one,
and may contain at most 1 MiB. It must be UTF-8 XML. DTD and entity declarations
are rejected, and ElementTree performs no schema, network, or external-entity
fetch. Source code is never imported or executed.

The root element must be `addon`. ID, name, version, and at least one extension
point are required. Add-on IDs are 1 to 128 characters, begin with lowercase
letter or number, use only lowercase letters, numbers, dots, underscores, and
hyphens, and contain no consecutive dots. This is compatible with Kodi's
documented identifier character set and remains path-safe for later work.

Observed name and version are preserved exactly after bounded nonblank/control
validation, with limits of 256 and 128 code points. Extension points are bounded
to 128 characters each and 64 unique values, then sorted and persisted as
canonical compact JSON. Dependencies are deliberately deferred and create no
runtime compatibility gate.

## Persistence and service

Migration `0017_development_sources` creates only `development_sources`, with
closed kind and independent uniqueness constraints for logical name, locator,
and add-on ID. Populated downgrade is refused rather than silently dropping the
catalog.

`DevelopmentSourceRepository` provides create, lookup by UUID/name/locator/ID,
and deterministic logical-name/UUID listing. The transactional application
service validates, generates UUID and timestamps, rejects duplicates, and
returns bounded path-free DTOs. No history record is added in this foundation;
typed Phase 8 activity remains part of later attachment/deployment slices.

## Step 2 durable attachment

`ManagedInstanceDevelopmentSource` records only that one registered source is
associated with one disposable ManagedInstance. It is a many-to-many global
catalog relationship with a unique instance/source pair. The service also
enforces one attached registered add-on ID per instance, independently of the
Step 1 global uniqueness rule.

Attach holds the closed per-instance `development_source_attachment` mutation
lease and consults central reconciliation before change. It does not require a
container or any particular runtime state. Before insertion, the service runs
the same descriptor-relative root/addon.xml validation and rejects missing,
invalid, or changed add-on identity. Changed observed name/version is accepted;
the immutable registration-time observation is not updated. Detach removes only
the relationship and performs no filesystem, Kodi, Snapshot, or source-catalog
cleanup.

Reset and Snapshot create/restore preserve associations exactly and do not
materialize them. Successful instance deletion explicitly deletes association
rows while retaining DevelopmentSource records. `DevelopmentSourceAttachmentOperation`
stores typed synchronous attach/detach results with immutable safe source and
original-instance identity; its nullable live instance FK clears on deletion.
Unified activity uses fixed path-free summaries.

Application DTOs expose source UUID, logical name, add-on ID, observed
name/version, source kind, and attachment time. They expose neither absolute nor
relative source path. No attach/detach Web POST exists in this step.

## Step 3 read-only Web presentation

Authenticated global list and detail pages, plus matching metadata-only JSON
routes, present registered sources through path-free application DTOs. The list
shows stored logical/add-on identity, observed name/version/extensions,
validation time, and current live attachment count. Detail adds current live
instance identity, exact runtime revision, attachment time, and safe links.
Live instance detail lists its attached sources in deterministic order.

These surfaces explicitly describe catalog metadata as registration-time
observations and attachment as product metadata rather than deployment. An
ordinary GET never invokes `DevelopmentSourceValidator`, reads source files, or
updates catalog observations. Deleted instances disappear from current source
detail while typed attachment activity remains available in existing deleted
instance history.

Neither HTML nor JSON includes the configured development root, relative
locator, derived absolute path, source-tree filenames/content, or raw XML. No
source registration, attach/detach, deployment, download, browse, edit, or
unregister action exists. The existing authentication, no-store, same-origin,
escaping, and no-CORS security policy applies unchanged.

## Step 4 runtime-ownership prerequisite

Copy/sync cannot normalize add-on ownership from a convention or request.
Every usable RuntimeExecutionProfile therefore has required immutable
`runtime_uid` and `runtime_gid` catalog facts. They identify the numeric owner
required for Kodi-managed profile and add-on files inside that exact curated
runtime environment and permit values from zero through 2147483647.

The trusted resolution path is ManagedInstance to its pinned RuntimeRevision to
the one-to-one RuntimeExecutionProfile. No future browser, application, or
broker deployment request may provide a UID, GID, username, or group name.
Migration `0019_runtime_execution_ownership` explicitly recognizes and
backfills only the retained Kodi 19.5, 20.5, 21.3, and 22.0 Beta 2 curated
profile/image identities; an unknown or mismatched row aborts upgrade. Current
curated values happen to be 1000/1000, but that is matrix evidence rather than a
general invariant.

RuntimeExecutionProfile has no separate digest today: frozen domain equality
and the immutable one-to-one persisted row compare the complete profile,
including ownership. Runtime revision build-manifest digests remain distinct
build provenance and are not silently rewritten. Existing fixed Snapshot and
fixture archive owners remain their established archive-format semantics; any
future conversion of those versioned formats is separately gated.

## Step 4 stopped-only copy/sync

Step 4 accepts only an attached local-directory source and an exactly
provisioned, stopped, disposable instance. Its strict authenticated POST body is
empty. Every operation reserves the per-instance development-source deployment
lease and the global-heavy lease, then invokes one broker semantic operation
whose only inputs are instance, source, expected container, and operation UUIDs.

The application revalidates registered root `addon.xml`; the broker then
independently resolves persistence, runtime ownership, and the configured root.
It walks the source twice descriptor-relatively without following links and
requires identical captures. Only directories and single-link regular files
are allowed. Exact `.git`, `.hg`, and `.svn` directories are skipped after their
type is proven safe. Fixed limits are 25,000 files, 50,000 entries, 1 GiB total,
256 MiB per file, 512 UTF-8 bytes per path, 255 per component, 32 components,
and 16 MiB canonical manifest.

The immutable root-private stage normalizes directories to 0755 and files to
0644 plus source execute bits. It preserves no source ownership, ACL, xattr,
special bit, or mtime. Numeric ownership comes only through ManagedInstance to
RuntimeRevision to RuntimeExecutionProfile. Canonical manifests contain policy
versions and ordered path/type/mode/size/file-hash records, but ordinary
surfaces expose only aggregate counts.

The fixed destination is derived from validated add-on ID. Absence permits
initial materialization. An existing destination requires retained successful
same-context evidence and a fresh full bounded readback whose digest exactly
matches that retained full destination digest. A deployment-state row alone is
insufficient. External additions or alteration are replacement conflicts.

Sync transfers only new or byte/mode-changed files. Unchanged files are absent
from the delta. Nothing is deleted, so type replacement conflicts. A path from
the prior verified destination that has since been removed from source is
legitimate destination-only state: it remains unchanged and is included in the
new full destination evidence. This does not admit arbitrary extras. The source
and destination digests may differ, and the complete post-sync destination
digest becomes the next replacement baseline.

Final verification proves every current source path, preserved prior
destination-only path, normalized mode, revision-derived ownership, exact
container/revision/stopped identity, and absence of unexpected entries. Partial
or unverifiable application becomes reconciliation-required without deletion.
Reset, restore, and container/revision replacement mark evidence stale before
destruction; interruption makes it unknown. Snapshot create does not
invalidate. Instance deletion removes current evidence while history and the
global source survive.

Success means filesystem materialization only. Step 4 never starts, stops, or
restarts Kodi, calls JSON-RPC, enables or recognizes an add-on, binds a mount,
installs a ZIP or repository, watches files, or grants MCP authority.

## Step 5 live Kodi add-on inventory and recognition

Inventory means only the complete live installed-add-on result Kodi returns for
the exact managed instance. It is not the filesystem, Kodi database, attachment
catalog, or copy/sync evidence. Copied, recognized, enabled, and working are
separate facts; neither recognition nor enabled state proves function.

The application issues one fixed `Addons.GetAddons` request with type unknown,
installed true, enabled all, and properties name, version, and enabled. Kodi's
addonid and type base fields are required. Responses from Kodi 19.5, 20.5, 21.3,
and 22.0 Beta 2 share one adapter: strict complete-result parsing, 64-item pages,
2,048 total items, 64 KiB per exchange, three seconds per exchange, six seconds
overall, conservative string bounds, strict Boolean enablement, duplicate
rejection, deterministic ID sorting, and no redirects. No partial list is
returned when any record or page is untrusted.

The exact ManagedInstance and pinned RuntimeExecutionProfile supply authority.
Existing broker-owned container/runtime observation proves current owned
container identity, runtime revision, managed-network membership, running state,
healthy Kodi process, and the internal endpoint. The browser supplies only the
instance UUID. Unprivileged application HTTP performs JSON-RPC; no broker or MCP
inventory operation exists. A query never provisions, starts, waits through
readiness, restarts, stops, enables, installs, or changes Kodi.

`ManagedInstanceAddonInventory` contains instance, observation timestamp, Kodi
release, query status, bounded unavailable reason, completeness, and normalized
items. Each item contains ID, name, exact Kodi version string, strict enabled
Boolean, Kodi's single reported type, and conservative provenance. The API is
authenticated `GET /api/instances/{instance_id}/addons`; instance detail renders
all items or a bounded unavailable state. Both are no-store. Observations are
not persisted and create no activity history or migration.

The only non-unknown classification is
`development_source_at_last_verified_copy`. It requires a retained successful
copy/sync operation and verified evidence matching source, instance, add-on ID,
reported version, RuntimeRevision, and exact current trusted container. Its
wording is `Prior verified copy`, meaning only that Kodi recognizes the same
identity/version Kodi Lab last verified copying. It is not a current byte claim.
Stale or unknown confidence and every mismatch yield unknown provenance;
attachment, Snapshot, fixture, repository guess, and filesystem location do not
substitute for proof. Fresh destination readback remains deferred.

Normal Kodi 21.3 startup/recognition did not mutate the synthetic add-on tree,
so Step 4 replacement semantics required no amendment in that slice. Later
execution proof showed that Interpreter-default embedded Python creates `.pyc`
and `__pycache__` entries beneath an imported local package. The correction
below prevents that normal bytecode drift without amending Step 4.

## Runtime compatibility correction: Python bytecode policy

Every RuntimeExecutionProfile now records one required closed semantic policy:
`interpreter_default` or `suppress`. Interpreter default imposes no Kodi Lab
bytecode-write suppression. Suppress causes trusted provisioning to configure
exact `PYTHONDONTWRITEBYTECODE=1` before Kodi and embedded Python start. There is
no environment map, request field, Python option, bytecode flag, or caller
override.

The bytecode policy is immutable revision behavior. Retained Kodi 19.5 revision
1, Kodi 20.5 revision 1, Kodi 21.3 revisions 1 and 2, and Kodi 22.0 Beta 2
revision 1 remain Interpreter default. New suppression profiles belong to Kodi
19.5 revision 2, Kodi 20.5 revision 2, Kodi 21.3 revision 3, and Kodi 22.0 Beta 2
revision 2. Each new revision may reuse its predecessor's exact image while
remaining a distinct revision for selection, Snapshot compatibility, approval,
trust, and validation evidence. Existing instance and Snapshot pins never move
implicitly; validated suppression revisions become preferred for new pins only
through the existing highest-eligible-revision rule.

Image-owned fixed environment values are preserved in deterministic order. The
profile exclusively owns the reserved `PYTHONDONTWRITEBYTECODE` key and has
final precedence: Suppress produces exactly one value of `1`, while Interpreter
default requires absence. Duplicate environment keys or missing, wrong, or
injected policy state fail trusted container drift validation. Broker and Web
callers still provide only existing semantic identities.

This runtime change does not amend Step 4. `.pyc` files and `__pycache__`
directories receive no exclusion or exemption; exact complete destination
readback, no-delete synchronization, and replacement-conflict behavior are
unchanged. Suppression prevents normal CPython imports from generating those
new entries beneath a DevelopmentSource deployment. Pre-existing packaged or
system bytecode elsewhere is unaffected, and an add-on that intentionally writes
inside its installation directory remains incompatible with strict provenance.
Bind-mounted sources may benefit from suppression, but bind-mount architecture
has not yet been validated.

## Copied-add-on enablement

Enablement authority is the exact ManagedInstance, its exact attached
DevelopmentSource, and exact ID/version retained from successful Step 4 copy
evidence. The current instance must still use that exact RuntimeRevision and
container, evidence confidence must be `verified_at_last_operation`, and the
broker must freshly recapture the complete fixed destination and match retained
full destination evidence. The broker accepts only instance, source, and
expected-container identities; it derives the destination and returns no path
or digest.

Trusted source `addon.xml` parsing supplies extension behavior and normalized
`requires/import` declarations: dependency ID, exact version/minversion strings,
and strict optional Boolean. Duplicate IDs, malformed optional values, and
malformed descriptor relationships fail closed. Known service/auto-start
behavior is unsupported. Python execution-capable sources require the exact
current suppression revision; safe non-Python sources do not.

Live Kodi must recognize the copied ID and exact version. Source dependencies
must agree with fixed `Addons.GetAddonDetails` declarations. Current complete
inventory then classifies each dependency as present-enabled, missing,
disabled, optional-missing, version-unverified, or unknown. Required missing or
disabled dependencies block; optional missing is allowed. A required version
constraint blocks as `dependency_version_unverified`: the fixed API provides
no authoritative compatibility Boolean, and exact string equality does not
prove range compatibility. Kodi's exact `0.0.0` unset-bound sentinel is treated
as no declaration constraint during cross-checking while its reported string
remains preserved. Kodi Lab never orders version strings, installs a
dependency, or automatically enables one.

If all preconditions pass and the target is already enabled, the result is
successful `already_enabled` with `enable_performed=false`. Otherwise the fixed
adapter sends exactly one internally constructed `Addons.SetAddonEnabled`
request with server-derived ID and literal Boolean `true`, then immediately
queries the exact add-on and requires matching ID/version plus `enabled=true`.
It does not execute the plugin.

`DevelopmentSourceEnablementOperation` provides synchronous durable history.
Per-instance enablement conflicts with every relevant instance mutation and
uses no global-heavy lease. An interruption known to precede Kodi mutation is
failed; uncertainty after mutation becomes reconciliation-required, is never
automatically re-enabled, and is not rewritten to success based on a later
query. Only bounded identity/outcome/dependency fields persist. Current enabled
state and dependency inventory do not.

The authenticated same-origin CSRF-protected endpoint is
`POST /api/instances/{instance_id}/development-sources/{source_id}/enable` with
a strict empty body and synchronous HTTP 200 result. UI controls are advisory;
the POST revalidates all authority. The state ladder remains explicit:
attached, materialized, recognized, enabled, dependencies generally unproven,
working unproven, tested unproven.

## Deferred Phase 8 work

Bind-mounted sources, ZIP installation, repository URL testing, watchers,
broader execution authority, disable/toggle, generic add-on enablement,
dependency installation, and broader provenance remain separately gated.

## Copied-add-on execution and smoke testing

The first execution slice is limited to an exact copied Python video plugin:
one `xbmc.python.pluginsource` executable extension, exactly `video` provides,
a safe relative declared library present in the verified tree, and only passive
metadata companions. Kodi must report the same type for the exact recognized,
enabled ID/version, and the current revision must suppress bytecode. Unsupported
or mixed descriptors fail without dispatch; add-on ID prefixes are irrelevant.

The durable HTTP-202 operation takes only a per-instance mutation lease and
repeats attachment, retained successful copy, verified confidence, exact
revision/container, already-running healthy Kodi, recognition, enabled state,
no blocking reconciliation, trusted descriptor, D-0141/D-0142 dependency
preflight, and exact fresh destination verification. It never starts, enables,
installs, stops, retries, resets, or restarts anything.

The adapter constructs only `Files.GetDirectory` for the server-derived plugin
root, `video`, no properties, and at most one returned item. Callers cannot
supply execution parameters. The client deadline is 10 seconds, with existing
preflight/readback bounds and a 90-second durable ownership bound. Directory
items are checked in memory and discarded.

Operation state, overall result, execution result, and post provenance are
separate. Correlated success and correlated Kodi error both require exact
post-readback. A match preserves verified confidence; mismatch is
`source_tree_changed` and stale; verifier failure is unknown and not passed.
Profile add-on-data writes outside the deployment tree do not count as source
mutation.

Timeout or post-dispatch crash is ambiguous because execution may continue.
The operation remains reconciliation-required, confidence becomes unknown, no
immediate match restores it, and conflicting work is blocked while the exact
original container runs. Explicit ordinary stop is the recovery action. Once
that container is stopped or absent, the warning may become nonblocking, but
history remains unresolved and there is no mark-resolved mutation.

The strict Web action/status is authenticated, same-origin, CSRF-protected, and
content-free. The UI warns that the managed network is not egress-isolated and
plugin code may contact external services; no network or repository control is
introduced.

**Smoke passed is bounded historical evidence that one fixed plugin-directory
invocation completed and preserved deployed-source provenance. It is not a
general working/health guarantee.**

## Uploaded-ZIP installation

The implemented first slice accepts only independently uploaded ZIP artifacts;
it never infers DevelopmentSource, Git, repository, publisher, or trust origin.
Upload streams through a bounded authenticated multipart endpoint into
server-UUID private staging, and the browser filename is ignored. The strict
validator enforces D-0161 before canonicalization, requires one add-on-ID root,
and accepts only a passive-metadata Python video plugin on a suppression
runtime. Accepted originals and normalized payloads are immutable and retained.

**ZIP installation in this first slice is Lab-controlled validated
materialization of an uploaded immutable ZIP artifact. It is not Kodi's native
Install-from-ZIP GUI/installer path.** Kodi install APIs, repository lookup,
enablement, execution, caller paths, generic extraction, and generic JSON-RPC
remain absent.

Installation begins with an already-running healthy disposable instance because
complete live inventory must prove the ID absent. The operation owns exact stop,
stopped identity/absence recheck, normalized apply, stopped readback, restart,
readiness, disabled recognition, declared-dependency non-mutation, final
readback, and the running terminal state. The destination must also be absent;
there is no replacement, upgrade, overwrite, adoption, or reinstall.

Normalized modes follow Step 4 and ownership comes only from the immutable
RuntimeExecutionProfile. A complete canonical manifest drives both stopped and
post-recognition readbacks, including every path, digest, type, mode, UID/GID,
and absence of extras. Missing or disabled dependencies are allowed because the
target remains disabled, but their recorded state must not change.

Any uncertainty after application begins leaves the exact instance stopped,
current ZIP evidence unknown, and history reconciliation-required. No retry,
delete, adoption, overwrite, restart, or manual success exists. Snapshot restore
and reset stale current evidence without reconstructing artifact lineage.
Inventory may say only `Prior verified ZIP install` for an exact retained
identity match and does not claim fresh byte equality.

Raw direct bind remains rejected. ZIP replacement/upgrade/rollback,
ZIP-specific enablement/smoke, DevelopmentSource packaging, repository
integration, and accepted-artifact cleanup remain separate gates.
