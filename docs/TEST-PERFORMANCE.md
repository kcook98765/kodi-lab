# Test performance and database fixtures

`./scripts/check` remains the complete RELEASE gate. It runs the full pytest
suite, Ruff formatting and linting, strict mypy, package build, Alembic history,
and the package/configuration import. Its coverage remains intact, while ordinary
tasks may finish at FOCUSED or INTEGRATION without invoking the full gate.

The gate keeps ordinary pytest temporary files on `/dev/shm` when at least 1 GiB
is available. Snapshot publication tests use a separate private, run-owned
scratch directory under `/tmp` by default so the production 2 GiB snapshot
free-space reserve is evaluated against a suitable filesystem without moving
thousands of SQLite and fixture operations off tmpfs. Set
`KODI_LAB_TEST_SNAPSHOT_SCRATCH_PARENT` to select another suitable parent. The
gate removes only the exact roots it creates, including after ordinary failure
or interruption. Direct focused pytest runs use the same snapshot fixture and
create their own private scratch root.

An uncatchable termination can leave a scratch root behind. Run
`./scripts/clean-test-scratch` to list only same-user, marker-validated, dead
roots older than one hour. Add `--delete` to remove those validated candidates.
The command rejects symlinks, live matching PID/start-time identities, foreign
owners, permissive roots, malformed markers, and unrelated names.

Each gate stage reports elapsed time. The full pytest stage has a 900-second
watchdog by default; `KODI_LAB_CHECK_PYTEST_TIMEOUT_SECONDS` may raise that bound
for a known slower environment. A timeout is a harness failure to diagnose, not
permission to skip tests or reduce snapshot storage safety.

## Migration fixture architecture

Migration tests use a session-scoped `MigrationTemplateStore`. At session start,
one SQLite database advances through the Alembic graph in revision order. After
each successful transition, the closed database is integrity-checked, its exact
`alembic_version` is verified, and it is copied to a revision-specific pristine
template. No connection, engine, session, WAL file, or shared writable database
survives template construction.

Each test names its starting revision explicitly through `migration_setup`. The
store makes an ordinary file copy of the closed template into that test's private
temporary directory, then verifies integrity and revision again. Tests mutate
only their private copy. Regressions prove that independent paths do not share
mutations, the pristine template remains unchanged, a failed Alembic command
cannot poison a later clone, and copied version rows are exact.

Migration tests fall into three categories:

1. Global history tests start without a template and prove the complete
   base-to-head path, or start at head and prove the canonical full downgrade and
   re-upgrade invariant.
2. Migration-specific tests clone the immediate predecessor, apply only the
   migration under test, verify its schema or data transformation, and exercise
   its permitted downgrade or populated-data refusal directly.
3. Tests that only need the current schema may clone `head`; they must not use
   this category to stand in for a migration transition.

When adding a migration, add its direct predecessor-to-new-revision proof and
the appropriate downgrade proof. Use a populated predecessor fixture for data
backfills. Keep lossy-downgrade refusal assertions at the new revision and verify
that the version row and relevant data remain intact after refusal. Add another
full-history test only when the invariant genuinely spans multiple revisions.

## Other test-only acceleration

Web route tests inject reduced-cost Argon2id parameters by monkeypatching only
their test fixture before `WebAuthenticationService` construction. The algorithm
and authentication behavior remain real. Dedicated authentication and password
tests do not load the Web fixture and continue to exercise production-strength
Argon2id. Production configuration and application behavior are unchanged.

The suite contains short event/poll loops, but profiling did not identify them as
a dominant cost. No sleeps, timeout behavior, application setup, or production
database initialization was changed in this pass. A shared current-head template
for non-migration application tests remains a possible future optimization, but
the present improvement does not justify adding that extra fixture layer.

## Idle-host baseline (2026-09-28)

With the four legacy Kodi containers and two disposable current development
containers stopped, one pytest run collected **2,668 tests in 165 files** and
passed in **521.60 seconds** of pytest time (**542.31 seconds** end to end with
`uv run`, verbose output, and duration reporting). The 99 `slow` cases were all
in `tests/storage/test_migrations.py` and consumed **37.20 seconds** of reported
test phases. Excluding that marker alone would save only about seven percent of
the suite's time.

Reported test phases totaled 510.49 seconds: 335.84 call, 159.95 setup, and
14.70 teardown. The largest file groups were Web 174.95 seconds, application
166.64 seconds, storage 83.69 seconds, broker 32.05 seconds, Kodi adapters
25.57 seconds, and packaging 11.95 seconds. Migration tests were 37.20 seconds;
remote lifecycle storage tests were 41.34 seconds. Screenshot-named tests
accounted for 24.50 seconds, and the related live-display file for 27.04
seconds. These file/name sums overlap; they are selection guides, not additive
cost categories.

Available RAM stayed above 8.69 GB during the run. The observer recorded no
Docker events, new containers, DinD, live Kodi, image build, or pull. Pytest
used roughly one CPU core and peaked near 1.60 GB RSS. The isolated pytest
basetemp briefly used about 2.2 GB of `/dev/shm` and was removed afterward.
The prior long release-migration workflow also ran several cold acquisitions,
clean-host/recovery checks, repeated bundle staging, and five full-gate attempts;
its total duration should not be attributed to pytest alone.

The five gate attempts in that workflow took approximately **84 minutes**:
15m02s and 40m01s timed out while disposable Docker daemons remained active,
one near-complete run found a mypy error, then two clean passes took 9m44s and
9m39s. The last clean pass spent 9m34s in its pytest stage, leaving about five
seconds for Ruff, mypy, build, Alembic history, and import combined. On the
exact final bundle, cold Matrix, Nexus, and Omega acquisition took 175, 178,
and 201 seconds respectively (**9m14s** total). The prior workflow repeated
acquisition and clean-host acceptance on several candidates; those costs
overlap and are separate from the pytest suite.

## Development and future tuning policy

Use `./scripts/check-focused` by default with each new/materially changed test
file and directly affected subsystem paths. Use `./scripts/check-integration`
only for actual subsystem interaction. Run `./scripts/check` once on settled
bytes when RELEASE coverage is warranted; it is not a routine pre-commit
requirement. Keep live Kodi, DinD, cold acquisition, and other heavy acceptance
out of the full-gate window. The exact escalation and runtime selection policy
is in [CONTRIBUTING.md](../CONTRIBUTING.md).

Do not add `pytest-xdist` to any gate without a separate isolation review.
Likely blockers include process-global environment mutation, fixed socket and
filesystem names, in-process coordinators/rate limiters, thread pools, and tests
that monkeypatch shared module state. Database-template copies themselves are
compatible with later worker isolation because every test gets a distinct file.
Another performance pass is justified only when comparable profiling shows a
new stable bottleneck or suite growth pushes the authoritative gate materially
above this pass's result.
