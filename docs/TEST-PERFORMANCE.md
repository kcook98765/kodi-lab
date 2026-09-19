# Test performance and database fixtures

`./scripts/check` remains the authoritative gate. It runs the complete pytest
suite, Ruff formatting and linting, strict mypy, package build, Alembic history,
and the package/configuration import. Performance work must not remove, skip, or
make any part of that gate optional.

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

## Development and future tuning policy

Use the narrow affected pytest module while editing, `./scripts/check-smoke` for
the inner loop, and `./scripts/check-fast` for a broader sub-slice gate. Run
`./scripts/check` after the final content change and before committing.

Do not add `pytest-xdist` to any gate without a separate isolation review.
Likely blockers include process-global environment mutation, fixed socket and
filesystem names, in-process coordinators/rate limiters, thread pools, and tests
that monkeypatch shared module state. Database-template copies themselves are
compatible with later worker isolation because every test gets a distinct file.
Another performance pass is justified only when comparable profiling shows a
new stable bottleneck or suite growth pushes the authoritative gate materially
above this pass's result.
