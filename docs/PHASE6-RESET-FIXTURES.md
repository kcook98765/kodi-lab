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
