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
archive bytes are constants derived from the sole package. The Docker adapter
independently compares the complete resolved specification with that package
before archive extraction.

No broker caller can provide fixture identity, path, destination, archive,
content, URL, mount, command, entrypoint, shell, script, exec, or JSON-RPC. Root
does not contact Kodi.

## Unprivileged Kodi initialization

After archive application, the application starts through the normal broker
operation and waits for the existing four-layer Kodi readiness contract. The
Kodi-21.3-only adapter then issues only fixed allowlisted JSON-RPC calls:

- `VideoLibrary.Scan` for the fixed fixture media directory
- `VideoLibrary.GetMovies`
- `VideoLibrary.GetTVShows`
- `VideoLibrary.GetEpisodes`

The 60-second fixture deadline is separate from readiness. Semantic verification
requires exactly the expected movie, series, and episode, including stable
synthetic unique IDs and episode relationship; Kodi database row IDs are not
part of the baseline. Reset then uses normal broker stop and proves the final
stopped state.

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
