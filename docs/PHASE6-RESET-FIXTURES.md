# Phase 6 disposable reset and predefined fixture

This document records the executable Phase 6 Step 1 contract. It does not define
a general fixture system.

## Reset contract

`DisposableInstanceResetService.reset(managed_instance_id)` accepts one semantic
UUID. It reloads ManagedInstance, the exact pinned RuntimeRevision execution
profile, and the optional fixture assignment from trusted persistence. It never
calls newest-approved or newest-eligible selection.

- Unprovisioned: provision the exact pin.
- Stopped: deprovision the exact owned stopped container, then provision the
  exact pin.
- Running: normal bounded stop, prove stopped, deprovision, then provision the
  exact pin.
- Transitional, unknown, or ambiguous: fail closed without mutation.
- Persistent: return `persistent_reset_unsupported`.

Success always means exactly one provisioned, stopped container. Prior running
state is not restored. With no fixture assignment, reset does not start Kodi.
With an assignment, reset automatically reconstructs the fixture baseline.
Target UUID, ManagedInstance UUID, RuntimeRevision UUID, fixture assignment,
support, approval/trust, validation evidence, notes, and other catalog state are
unchanged.

The process-local keyed lock rejects a concurrent reset of the same instance.
It is not a distributed lock or lease. No SQLite transaction spans Docker,
readiness, or fixture initialization. A multi-worker reset surface requires a
later coordination design.

## Failure contract

Reset is destructive reconciliation, not an atomic transaction:

- Stop failure makes no deprovision attempt.
- Deprovision failure makes no replacement provision attempt.
- Provision failure after successful removal leaves the durable ManagedInstance
  and exact pin intact and operational state unprovisioned.
- Archive failure retains the fresh stopped container when stopped state is
  proven.
- Start, readiness, scan, or semantic verification failure performs normal
  bounded stop. Proven stopped state is retained for diagnosis; inability to
  prove stopped returns `reset_reconciliation_required`.
- Final-stop failure also requires explicit reconciliation.

The workflow never falls back to another revision and never force-removes a
container to simulate rollback.

## small-mixed-v1

The finalized fixture is Small/Mixed version 1 and is compatible only with exact
Kodi 21.3 stable. Its fixed identity is:

- slug: `small-mixed-v1`
- UUID: `35d6854d-6cfc-5247-97df-7f2023c6ae9d`
- content digest:
  `sha256:5d3e523907983fffc3143c2c95a87cab8732d988954207bf17f5363d27b5c547`
- movie: `Kodi Lab Fixture Movie` (2024), synthetic ID
  `small-mixed-v1-movie`
- series: `Kodi Lab Fixture Series` (2024), synthetic ID
  `small-mixed-v1-series`
- episode: `Kodi Lab Fixture Episode`, season 1 episode 1, aired 2024-01-02,
  synthetic ID `small-mixed-v1-episode`

All metadata, PNG artwork, and one-second playable MP4 placeholders are generated
project assets. There is no private or copyrighted source media. The manifest
lists the complete sorted file set with size and SHA-256 for every member. Its
aggregate digest covers all semantic manifest fields and file identities.
Changing any byte or metadata requires a new fixture version and digest.

Live Phase 6 r1 validation preserves an important negative result: Kodi reached
normal readiness and accepted the scan, but returned zero movies, shows, and
episodes. `sources.xml` controls source visibility; Kodi's scanner separately
reads content and scraper assignment from the video database `path` table. v1
contains no database seed, remains immutable, and is not retroactively repaired.

## small-mixed-v2 and its configuration-only seed

`small-mixed-v2` retains every v1 synthetic media, NFO, artwork, and
`sources.xml` byte. It adds exactly one version-specific binary asset:

- slug: `small-mixed-v2`
- UUID: `f7bd24c7-bccc-44ad-b286-999979b59d9e`
- content digest:
  `sha256:624adef0e88de74d8c34d1176d0d306e7f4a2731db147eb26b526ab42b810d18`
- compatibility: exact Kodi 21.3 stable only
- database: `MyVideos131.db`, schema version 131
- database SHA-256:
  `f81fcaa98a192e6a93b9f36a659d9ce56546fdc96f4edfe74d3f1eb5094c3fd9`

The database originated from a clean start and clean stop of the exact validated
Kodi 21.3 image
`sha256:70e0166fd79ea731c3f81056c2f0516f8fe5c9c8ec729b0c0f1c3e6bac66f254`.
Kodi itself created the complete Omega database schema. Because Kodi exposes no
bounded noninteractive content-assignment API, the development-only
`scripts/materialize-small-mixed-v2-seed.py` helper copied that stopped fresh
database and inserted these fixed rows:

| path | content | scraper | recurse | folder names |
| --- | --- | --- | ---: | ---: |
| `/home/kodi/.kodi/fixture-media/movies/` | `movies` | `metadata.local` | 1 | 1 |
| `/home/kodi/.kodi/fixture-media/tvshows/` | `tvshows` | `metadata.local` | 0 | 0 |

All other content flags are fixed false, settings are empty, and hash/date/parent
fields are null. `metadata.local` is the Kodi-installed Local information only
scraper, so the workflow consumes the complete NFOs without external metadata
services.

The seed audit requires SQLite integrity, the exact `(131, 0)` version row, the
two exact path rows, Kodi's 387 built-in video-version type rows, and zero rows
in every other table. Thus the seed has no movie, TV-show, episode, file,
artwork, bookmark, rating, history, resume, credential, external-share, or user
state. The seed is source/content configuration, not fixture library truth.
Successful initialization still requires Kodi's normal scanner to create the
one expected movie, show, episode, paths, artwork, and relationships.

## Fixed container layout

The archive extracts only beneath `/home/kodi/.kodi`:

```text
fixture-media/
  movies/kodi-lab-fixture-movie/
    Kodi Lab Fixture Movie (2024).mp4
    Kodi Lab Fixture Movie (2024).nfo
    poster.png
    fanart.png
  tvshows/kodi-lab-fixture-series/
    tvshow.nfo
    poster.png
    fanart.png
    Season 01/
      Kodi Lab Fixture Series S01E01.mp4
      Kodi Lab Fixture Series S01E01.nfo
      Kodi Lab Fixture Series S01E01-thumb.png
userdata/sources.xml
userdata/Database/MyVideos131.db  # v2 only
```

Archive names must be relative, normalized, and within this allowlisted tree.
Absolute names, `..`, symlinks, hardlinks, unexpected members, missing members,
size/hash disagreement, or persisted/package digest disagreement fail closed.
Directories use mode 0755; files use 0644; every archive member has uid/gid 1000
for the fixed Kodi user. No post-copy exec or chown exists.

## Broker boundary

**Fixture copying is a narrow semantic broker capability, not a generic
file-copy primitive.**

`apply_assigned_fixture` accepts exactly:

- ManagedInstance UUID
- expected immutable container ID

The root broker resolves the assigned FixtureDefinition, exact RuntimeRevision,
compatible RuntimeDefinition release, packaged fixture, and content digest. It
then freshly proves disposable type, exactly one owned container, immutable ID,
deterministic name, exact revision label, and stopped state. The destination and
archive bytes are constants derived from the exact persisted approved package.
For v2 the database destination is the fixed
`/home/kodi/.kodi/userdata/Database/MyVideos131.db`; it is not a request field.
The Docker adapter
independently compares the complete resolved specification with that package
before archive extraction.

No broker caller can provide fixture identity, path, destination, archive,
content, URL, mount, command, entrypoint, shell, script, exec, or JSON-RPC. Root
does not contact Kodi.

## Unprivileged Kodi initialization

After archive application, the application starts through the normal broker
operation and waits for the existing four-layer Kodi readiness contract. The
Kodi-21.3-only adapter then issues only fixed allowlisted JSON-RPC calls:

- `VideoLibrary.Scan` for the fixed movie directory
- `VideoLibrary.Scan` for the fixed TV directory, only after movie scan progress
- `VideoLibrary.GetMovies`
- `VideoLibrary.GetTVShows`
- `VideoLibrary.GetEpisodes`

Kodi accepts a second scan request while a first scan is active but does not
perform it. The adapter therefore starts the movie scan and, within the same
60-second fixture deadline, issues the TV scan only after the expected movie is
visible. Semantic verification requires exactly the expected movie, series, and
episode, including their fixed synthetic paths, stable unique IDs, and episode
relationship; Kodi database row IDs are not part of the baseline. Reset then
uses normal broker stop and proves the final stopped state.

One validation-only method, `remove_fixture_movie_for_reset_test()`, finds only
the exact synthetic movie, internally consumes its Kodi row ID, invokes fixed
`VideoLibrary.RemoveMovie`, and proves the movie alone left the baseline. It has
no caller arguments and is not arbitrary JSON-RPC or deletion authority.

## Persistence and exposure

Migration `0007_fixture_definitions` adds immutable FixtureDefinition metadata
and a nullable restrictive ManagedInstance assignment. Existing instances
migrate with no fixture. Binary content is not stored in SQLite. There is no
update/delete API for finalized definitions and no reset history table.

Trusted application administration registers the exact packaged definition and
assigns or removes it explicitly. Assignment requires a disposable instance and
exact compatible release. Runtime reassignment rejects an incompatible retained
fixture rather than clearing it. Web reset and fixture administration are
deferred; existing authentication, lifecycle, readiness, and matrix behavior are
unchanged.

No migration is needed for v2. It is a second immutable FixtureDefinition
registered through the existing trusted administration service. v1 and its
failed-r1 evidence remain intact. There is no runtime SQL, broker SQL, generic
database API, generic archive copy, caller path, or added broker operation.

## Step 2 semantic status and read-only Web presentation

`ManagedInstanceResetStatus` is the stable application-owned view of reset and
fixture state. `ResetStatusService` derives it from the ManagedInstance, exact
pinned RuntimeRevision and release, optional FixtureDefinition, fresh bounded
broker inventory, and process-local activity. It reports:

- supported or unsupported reset eligibility with a bounded semantic reason;
- fixture none/assigned and compatible/incompatible state, slug, display name,
  version, Small/Mixed categories, and exact compatible Kodi release;
- provision/container state and the exact revision reset would preserve;
- current in-process reset phase, last ephemeral outcome, bounded failure phase
  and reason, and reconciliation-required state.

The fixed presentation statement is: “Reset recreates this exact runtime
revision; it does not upgrade Kodi.” `small-mixed-v1` remains accurately shown
as the historical version whose populated live baseline was not proven;
`small-mixed-v2` is not silently substituted. No fixture reassignment occurs
during status reads.

`DisposableInstanceResetService` publishes only phases it actually enters to an
injected `ResetActivityRegistry`: preflight, stop/deprovision/provision, fixture
apply/start/readiness/scan/verify, final stop/verify, then a bounded success,
failure, or reconciliation-required outcome. At most one active entry exists per
ManagedInstance. Completed entries use a fixed-capacity eviction policy; active
entries are never evicted. A new process starts with an empty registry, so the
UI never claims stale work is running. This remains observability for the
existing in-process lock, not durable history, a job system, or distributed
coordination.

The authenticated dashboard and
`GET /api/instances/{instance_id}/reset-status` are read-only consumers. The DTO
does not contain container IDs, image IDs, archive/database paths, evidence
paths, traceback or raw exception text. There is no reset button, reset POST,
fixture mutation route, schema change, or broker protocol/capability change.

## Step 3 durable coordination and confirmed Web reset

Migration `0008_reset_operations` adds reset-specific queued/running/succeeded/
failed/reconciliation-required state, latest phase, request/start/heartbeat/
completion times, bounded failure, and lease-token digest. A SQLite partial
unique index over queued/running rows is the cross-process one-active invariant.
Atomic conditional updates require the lease digest and unexpired ten-minute
lease. Meaningful phases renew it. The newest 20 terminal rows per instance are
retained without event or log payloads.

Each Web process may host an executor, but only the lease winner calls
`DisposableInstanceResetService`. Queued work is picked up after restart. An
expired running operation is not resumed; it becomes reconciliation-required
with its last phase. Durable status is authoritative and local activity only
supplemental. No generic jobs, payload, retry, cancellation, Redis, Celery, or
broker operation is added.

Authenticated `GET /instances/{instance_id}/reset` presents confirmation.
`POST /api/instances/{instance_id}/reset` requires existing same-origin,
session, and CSRF gates and exactly `{ "confirmed": true }`. It reloads reset
eligibility, queues or returns the existing operation, and returns 202 promptly.
The page warns that drift is discarded, fixture is reapplied, the exact revision
is not upgraded, and success ends stopped. Server services reject lifecycle and
trusted fixture/revision changes while active.

Stale acknowledgement remains read-only: clearing durable intervention state
needs an explicit audit/evidence contract. There is no destructive auto-resume
or generic Docker repair. The surface remains localhost-only through the
existing root broker boundary.

## Step 4 exact-release portability on Kodi 20.5

`small-mixed-v3` is a new immutable Small/Mixed version compatible only with
exact Kodi 20.5 stable. v1 and v2 remain byte-for-byte unchanged. v3 reuses the
same repository-owned movie, one-episode series, NFO metadata, artwork,
`sources.xml`, fixed paths, stable synthetic IDs, movie-then-TV scan sequence,
semantic verification, and bounded movie-removal validation method as v2.

The only payload difference from v2 is the exact-release configuration seed:

- slug: `small-mixed-v3`
- UUID: `1017f88f-105e-4baa-8719-4c721e864ab5`
- compatibility: exact Kodi 20.5 stable only
- database: `MyVideos121.db`, schema version 121
- database SHA-256:
  `bdcf6cb816751db076636bdd426ab4d0babdb32a44d2fe2552f59a2ac034335f`
- content digest:
  `sha256:08810a62f9c6679f9476a72cf00bbf7db40de455d71f8e7b6f146d178b5e16bc`

The fresh schema originated from the exact validated Kodi 20.5 image
`sha256:be35d7cb8899dfe474c6a1eca62da99c4ada1280f4325933d7b6193e83537638`.
Kodi initializes its video database lazily, so the isolated development-time
generation container used an internal-only network and one fixed read-only
`VideoLibrary.GetMovies` call before clean stop. The captured fresh database
had SQLite integrity `ok`, version `(121, 0)`, and zero rows outside `version`.

The argument-free `scripts/materialize-small-mixed-v3-seed.py` helper copies
that fixed fresh database and inserts only the same two path rows documented for
v2. The v3 audit requires those exact rows and zero rows in every other Nexus
table. No runtime SQL, broker SQL, generic database editor, caller path, or
network metadata dependency is introduced.

**Fixture semantic content may be shared, but Kodi database seed material is
exact-release/schema-specific.** The broker still accepts only ManagedInstance
UUID and expected container ID; the fixed database path is package-derived.
The reset service still provisions its unchanged pin, applies the assigned
fixture, starts, waits, scans, verifies, and stops. Only the fixed unprivileged
adapter's proven release allowlist expands from Kodi 21.3 to Kodi 20.5; its
method and parameter surface is unchanged.

## Step 5 exact-release portability on Kodi 19.5 Legacy

`small-mixed-v4` is a new immutable Small/Mixed version compatible only with
exact Kodi 19.5 stable. v1, v2, and v3 remain byte-for-byte unchanged. v4 reuses
the same repository-owned media, NFO metadata, artwork, `sources.xml`, fixed
paths, stable synthetic IDs, scan sequence, semantic verification, and bounded
movie-removal validation method as v2 and v3.

The exact-release configuration seed is the only payload difference:

- slug: `small-mixed-v4`
- UUID: `1b68a2ee-6217-4840-8582-536c1940f9bf`
- compatibility: exact Kodi 19.5 stable only
- database: `MyVideos119.db`, schema version 119
- fresh Kodi-created database SHA-256:
  `ec74c34d91d6f52a36529a3bfed8ce988321f802fb3012036b14c74cc3a0c9b8`
- finalized seed database SHA-256:
  `66946cf8e05c40d4950d484c0b5031eb5252eaad4bb5aaf50ac3f00178ad0f7c`
- content digest:
  `sha256:17a94df264c819f4704661774973d43850078b7c6a5e9e785d67713f13366ac9`

The schema came from exact validated image
`sha256:73c221f1fc1fc14cbfdfd82e62fd4b87752ef71e0521227cb6790451fa51d2bd`.
An isolated internal-only development container used one fixed read-only
`VideoLibrary.GetMovies` call to trigger lazy database creation, then stopped
cleanly. The fresh database had SQLite integrity `ok`, version `(119, 0)`, and
zero rows outside `version`. Its relevant `path` table column layout matches
Nexus schema 121; unlike Omega schema 131, Matrix has neither the later table
set nor the built-in 387-row `videoversiontype` catalog.

The argument-free `scripts/materialize-small-mixed-v4-seed.py` copies that one
fixed input and inserts only the two fixed `metadata.local` path/content rows.
The schema-aware v4 audit requires exactly those rows, the version row, and zero
rows in every other Matrix video-database table. It verifies integrity and
therefore covers library items, files, artwork, bookmarks, ratings, resume,
watched/history, credentials, and unrelated paths without adding runtime SQL.

Fixture compatibility is independent of runtime support tier. v4 may be
assigned to an already approved, validated, exact Kodi 19.5 disposable pin;
fixture availability does not make Legacy selectable unless trusted selection
explicitly enables Include Legacy. The broker operation, reset orchestration,
durable coordination, Web authority, adapter method set, and exact-pin behavior
remain unchanged.

## Step 6 Small/Movies taxonomy

Migration `0009_fixture_content_taxonomy` expands the persisted content
taxonomy from only Mixed to exactly Mixed and Movies. The column is bounded at
16 characters and retains an explicit check constraint; arbitrary strings and
the future TV category are not accepted. All existing fixture and reset data is
preserved. Downgrade to `0008` succeeds only when no Movies definition exists.

Canonical V1 defines Small and Movies but not a Small/Movies item count. The
Phase 6 executable definition is therefore three fictional, repository-owned
movies:

- `Kodi Lab Fixture Movie 1` (2024), ID `small-movies-v1-movie-1`;
- `Kodi Lab Fixture Movie 2` (2025), ID `small-movies-v1-movie-2`;
- `Kodi Lab Fixture Movie 3` (2026), ID `small-movies-v1-movie-3`.

`small-movies-v1` is UUID `9526870f-5fc2-4850-9fcf-0523aceb3cf5`, fixture
version 1, Small/Movies, and compatible only with exact Kodi 21.3 stable. Its
aggregate digest is
`sha256:3ffce70ae22cb3c3472a9dc46624cd4e4f85b539fce48b96634844ab3ea21da0`.
The exact validated Omega image generated fresh `MyVideos131.db` with SHA-256
`2bfba8f2e2b7f103bc1c0c7cdd7ac0489e57dbfecf355224578c64ea6f11b549`.
The fixed development helper adds only one `metadata.local` Movies path row;
the immutable seed hash is
`c0da622a88bc2169a877f336bae07a1949d198565f7853529033a5de7b40f600`.
Its audit requires schema 131, that exact row, Kodi's 387 built-in video-version
types, and zero movie/show/episode/file/art/bookmark or other user rows.

Trusted packaged semantic specifications now drive identity verification and
scan planning. Existing Small/Mixed definitions still verify exact 1/1/1 and
scan Movies then TV. Small/Movies verifies exact 3/0/0 and scans only the fixed
Movies directory. The bounded drift method resolves one package-owned movie ID
and accepts no caller target. This is a closed schema, not an assertion language
or caller-selectable scan API.

**Fixture size/content taxonomy describes semantic dataset shape;
exact-release fixture versions provide the Kodi-specific initialization seed.**
The existing reset flow, broker request and privilege, durable operation lease,
authenticated reset confirmation, and exact-pin behavior do not change.
