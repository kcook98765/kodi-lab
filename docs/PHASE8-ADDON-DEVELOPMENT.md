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
so Step 4 replacement semantics require no amendment in this slice. Executing a
Python add-on may later create bytecode or other files beneath its directory;
that execution-time mutation question remains deferred and unproven.

## Deferred Phase 8 work

Bind-mounted sources, ZIP installation, repository URL testing, watchers,
fresh byte-level provenance, execution-time tree mutation, enable/disable,
dependency installation, and broader provenance remain separately gated.
