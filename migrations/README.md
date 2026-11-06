# Database migrations

`0001_initial_release` is Kodi Lab's canonical initial-release SQLite schema.
It replaces all 92 private prerelease revisions through
`0092_create_kodi_operations`. Existing prerelease installations require a fresh
installation; there is no conversion or compatibility path. Initialization and
installed startup reject unsupported, unversioned, malformed or ambiguous
revision state before migration writes. They never stamp, erase or reinterpret
an older database. The owner must explicitly retire the old installation before
installing fresh.

The baseline uses frozen explicit SQLite DDL and reference rows, independently
captured from main `e94f08764f94ec6b3b300c9a5be94fecb10f706a`. It preserves
135 application tables, 121 explicit indexes, 93 triggers, foreign keys, named
check/unique constraints, defaults, nullability and the seeded permission and
policy state. There are no views. SQLAlchemy `create_all()` alone is insufficient:
several authorization, audit, receipt and lifecycle guards exist only in SQL.
The production-control timestamp is generated at initialization. The packaged
initializer still applies the current first-install capacity policy only to a
truly empty database; repeated initialization preserves administrator choices.

The independent regression reference is `tests/fixtures/initial-schema.json`.
`tests/schema_manifest.py` captures byte-exact SQL hashes, `table_xinfo`,
`index_list`/`index_xinfo`, foreign keys, table properties and reference rows from
an initialized database. Only the generated policy timestamp is normalized;
its format is checked. Integrity and FK checks must pass. To reproduce a
manifest, initialize an isolated database at the desired source revision, then:

```console
uv run --locked python -m tests.schema_manifest /absolute/path/to/database.db
```

Alembic remains the forward migration framework. Future public schema changes
must add ordinary revisions with `down_revision = "0001_initial_release"`
(or the subsequent head). Add predecessor-to-new-head tests, populated fixtures
for any backfills, and appropriate downgrade safety checks. Never edit the frozen
baseline to introduce a later release's schema change. The baseline itself
refuses downgrade to `base` to protect authorization and audit history.
Offline SQL generation is supported for empty-database provisioning; applying
such SQL manually is not the supported initializer or a prerelease upgrade path.
Source and installed artifacts use this same migration tree.

`0003_managed_versions` follows `0002_managed_setup` and adds principal-bound
managed version Start/Stop receipts with immutable intent/child identity. It
changes no policies, grants, runtime registrations or profiles. Referenced
lifecycle children remain inspectable. Downgrade refuses once any receipt exists
to preserve replay safety and audit history. See
[the coordinator contract](../docs/MANAGED-VERSION-LIFECYCLE.md).
