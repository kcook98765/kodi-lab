# Fresh application database

`0001_foundation` initializes a new Kodi Lab application database. The installed
entrypoint is `kodi-lab-admin initialize`. The initializer rejects previous
Kodi Lab revisions and unversioned SQLite files before writing. Use a new
application state volume.

The single Alembic revision contains frozen SQLite DDL, indexes, foreign keys,
authorization and audit triggers, and the initial policy rows. This keeps the
database contract independent of later ORM changes. The initial policy timestamp
is generated during initialization. Re-running initialization on the same
schema preserves the data. The schema fixture records the exact table,
constraint, index and trigger inventory.

The Web state volume stores credentials, external Kodi registrations and
policies, reviewed runtime acquisition metadata, and external development
state. The broker's separate managed volume stores the current profile,
private workspaces, snapshots, approved packages, the active lease and the last
operation. See the [managed-v2 install notes](../docs/MANAGED-V2-FRESH-INSTALL.md)
for the backup and recovery boundaries.
