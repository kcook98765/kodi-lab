# Test performance and database fixtures

`./scripts/check` remains the complete RELEASE gate. It runs the full pytest
suite, Ruff formatting and linting, strict mypy, package build, Alembic history,
and the package/configuration import. Its coverage remains intact, while ordinary
tasks may finish at FOCUSED or INTEGRATION without invoking the full gate.

## Test prerequisites and isolation

Run `uv sync --dev` and ensure `node --version` succeeds in the gate's shell.
The Add Kodi JavaScript behavior harness invokes `node` directly; the Python
lockfile does not install it. A missing executable is an environment failure,
not a reason to omit the harness. The harness uses Node's built-in modules and
requires no npm installation. The Node 22 executable passed locally;
issue #4 also reports the harness passing with Node 24 on the workstation.

Web route fixtures must double every outbound transport, including the managed
resident Bridge preparation's native JSON-RPC transport. A fake broker and an
immediate readiness coordinator do not replace this separate HTTP path. Leaving
it real made lifecycle tests depend on routing to the synthetic `10.250.0.10`
address: a timeout kept the background readiness task checking longer than the
test's polling budget, with `jsonrpc_state=not_checked`. The transport fixture
now returns the fake native identity and reports no installed resident Bridge;
it does not bypass trust admission or establish credentials. Regression tests
reject real network calls and cover absent Bridge, transport failure and native
identity mismatch. Production timeouts, identity checks and trust pins are unchanged.

The gate selects `/dev/shm` when at least 3 GiB is available. Before running
checks it requires the selected pytest filesystem to have the unchanged production
2 GiB reserve plus 1 GiB of test headroom. This is a test-harness preflight;
application storage reserves are unchanged. For a full run whose temporary files exceed tmpfs capacity, set
`KODI_LAB_CHECK_PYTEST_TMP_PARENT=/tmp ./scripts/check` (or another absolute,
writable, non-symlink directory). An explicitly provided parent takes precedence
over the `/dev/shm` heuristic and is validated before temporary directories are
created. Focused/integration lanes honor the same explicit parent and use the same 3 GiB
tmpfs selection threshold. Keep the parent
path short: Unix socket tests retain the runtime 100-byte socket-path limit.
Snapshot publication tests use a separate private, run-owned
scratch directory under `/tmp` by default so the production 2 GiB snapshot
free-space reserve is evaluated against a suitable filesystem without moving
thousands of SQLite and fixture operations off tmpfs. Set
`KODI_LAB_TEST_SNAPSHOT_SCRATCH_PARENT` to select another suitable parent. The
gate removes only the exact roots it creates, including after ordinary failure
or interruption. Direct focused pytest runs use the same snapshot fixture and
create their own private scratch root.

An uncatchable termination can leave a scratch root behind. Run
`./scripts/clean-test-scratch` to list only same-user, marker-validated, dead
roots older than one hour. Run wrappers mark their own outer roots; pytest and
snapshot data live below those roots so pytest cannot erase the ownership marker. Add `--delete` to remove those validated candidates.
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

Closed persisted enum/check vocabularies need a migration when application
values are added. Test the durable coordinator against the migrated schema as
well as testing process-local observers or fresh ORM-created tables; the latter
do not prove that an existing deployment accepts the new values.

## Other test-only acceleration

Web route tests inject reduced-cost Argon2id parameters by monkeypatching only
their test fixture before `WebAuthenticationService` construction. The algorithm
and authentication behavior remain real. Dedicated authentication and password
tests do not load the Web fixture and continue to exercise production-strength
Argon2id. Production configuration and application behavior are unchanged.

The suite contains short event/poll loops, but profiling did not identify them as
a dominant cost. Production sleeps, timeout behavior, application setup and database initialization
remain unchanged. Ordinary Web and ZIP fixtures now use the limited ORM schema
template pilot described below; this does not replace the migration templates.

## Earlier failures and resource evidence (2026-10-21)

`./scripts/check` runs Ruff format/lint and strict mypy first, then exact broker
operation/capabilities and schema contracts. The complete unfiltered pytest suite
still runs after those guards, followed by package build, Alembic history and
package/config import. No test selection or parallel workers were added to the
full-suite stage. A failing guard stops the candidate before the expensive run.

Each full check prints a private evidence directory, created under `/tmp` or
`KODI_LAB_CHECK_EVIDENCE_PARENT` (an existing absolute, writable, non-symlink
parent). It retains `stages.tsv`, contract/full-suite JUnit XML and pytest JSON.
The JSON records case outcomes and durations for setup/call/teardown, cleanup
outcomes, the minimum sampled scratch-filesystem free space, Linux peak process
RSS in KiB and pytest exit status. Pytest prints its slowest 25 phases; focused
lanes print ten. Read complete summaries and the process exit status before
claiming a pass. An interrupted process can leave incomplete pytest artifacts.
The TSV records completed stages, including failures; a successful pytest JSON
alone does not establish that the later release stages passed.

## Case scratch cleanup

The test-only `tests.efficiency_plugin` recognizes only pytest's built-in
function-scoped `tmp_path`. It captures the private root's device/inode and
parent identity without adding files to that directory. After all fixture
teardown has completed, it reclaims a root only when setup, call and teardown
all passed, its identity/ownership/private permissions still match, and the
pytest process has no other live threads or child processes. Unknown worker
state retains scratch. Persistent workers can therefore limit reclamation;
the JSON/terminal summaries show these retained cases.

Descriptor-bound traversal handles ordinary read-only directories, unlinks
symlinks without following them and never chmods regular files or hardlink
targets. Replaced roots, custom `tmp_path` fixtures and session factory roots do
not confer deletion authority. Unavailable/unsafe paths remain for normal
run-owned cleanup. No worker is killed or joined by this plugin.

Direct pytest runs retain failed/skipped cases. `--keep-passed-tmp` retains all
successful cases; `@pytest.mark.keep_scratch` retains one case. Keep pytest's
built-in `tmp_path_retention_policy=all`: its `failed` option can remove
setup/teardown-failure evidence and miss read-only directory cleanup. The Lab
plugin provides the phase-aware policy instead. Wrapper scripts still remove
their own ephemeral run roots on exit; full checks retain failure traces in
JUnit XML and stage/results metadata in the separate private evidence directory.
Session data from `tmp_path_factory`, including migration templates, is preserved
for its session lifetime.

Full, focused, integration, smoke and fast wrappers use the same standard-library
`OwnedScratch` traversal for final cleanup. They capture root and parent identity
at creation, including snapshot fixtures, and never recursively chmod regular
files. A shared temporary parent is permitted only for wrapper-created roots;
the root must remain private and owned. Inaccessible directories or identity
changes fail closed and are reported as retained, without a permission-rewrite
fallback. Full-gate cleanup failures also make the command fail.

The abandoned-root cleaner is dry-run by default. New wrapper markers bind root
and parent identity, UID and live PID/start-time identity. Marker reads are bounded,
nonblocking and no-follow; malformed numbers, nonfinite timestamps, unknown owner
state, hardlinked or nonregular markers cannot authorize deletion. Legacy snapshot
markers remain supported with the same opened-file checks and descriptor-bound
removal. Run `--delete` only for the reviewed candidates. No production path,
profile or container is a cleanup candidate.


## Closed current-schema fixture pilot

`tests/web/conftest.py`, the shared uploaded-ZIP fixture, and nine explicitly
selected application fixtures use `CurrentSchemaTemplate`. The application
fixtures cover managed-instance selection/projection, runtime eligibility/matrix/
administration, source and uploaded-ZIP enablement, add-on inventory, and MCP
foundation services. Each pytest session builds one unseeded ORM schema,
closes every engine/SQLite handle, validates integrity and hashes the immutable
template. Each test receives an exclusive private file copy with an integrity
check, a fresh engine and the same per-case seeding/foreign-key behavior as
before. Existing destinations, changed templates, symlinks and journal sidecars
fail closed. No mutable database, engine, session, credentials or seed rows are
shared. Alembic revision fixtures and migration-transition proofs are unchanged.

A matched 51-case Web/ZIP pilot passed before and after: pytest time decreased
from 16.12s to 10.89s, measured wall time from 21.47s to 16.19s, and end-of-run
scratch from 102,155,361 bytes to 2,002,984 bytes. All 51 case roots were reclaimed;
the remaining scratch included the session schema template. Peak process RSS was
349,260 KiB before and 339,328 KiB after. This is one small pilot on the local
host, not a full-suite speedup or peak-memory qualification.

The 2026-10-24 #54 expansion retained every test body, per-case seed, engine,
session and foreign-key configuration. The matched lane passed 22 focused tests
plus 247 interaction tests before and after; four unchanged storage modules were
included as controls. The 247-case pytest portion decreased from 38.30s to
18.21s (52.5%), with setup phases decreasing from 29.24s to 8.22s. Complete lane
time decreased from 48.2s to 25.7s. Peak pytest process RSS for that portion was
372,272 KiB before and 271,916 KiB after. These are single local matched runs,
not a full-suite or native-host performance claim. Existing closed-template
integrity/mutation-isolation/handle/sidecar checks remain authoritative. No
Alembic transition, production initialization or authentication cost was changed.
See [the #54 ledger](PROGRAM-54-LEDGER.md) for the source pin and retained evidence.

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
