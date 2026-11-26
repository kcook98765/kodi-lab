# Application database

`0001_foundation` initializes a new Kodi Lab application database. The installed
entrypoint is `kodi-lab-admin initialize`. The initializer rejects previous
Kodi Lab revisions and unversioned SQLite files before writing. Use a new
application state volume.

The foundation revision contains frozen SQLite DDL, indexes, foreign keys,
authorization and audit triggers, and the initial policy rows. This keeps the
database contract independent of later ORM changes. The initial policy timestamp
is generated during initialization. Re-running initialization on the same
schema preserves the data. Schema fixtures record the exact table, constraint,
index and trigger inventory.

`0002_worker_replay` adds an immutable UUID-only replay table for terminal
installer workers without a terminal installation row. Use the supported
quiesced application update to move from the foundation to the installed
head. Downgrade refuses to discard recorded replay history. The normal
verified application backup and restore commands carry the table.

The deployed foundation schema predates worker retirement and has no file-based
replay guards to migrate. Earlier unpublished candidates used private socket
volume markers. Retained terminal receipts can still supply a UUID before they
expire. A marker whose receipt has already expired belongs to the draft site's
retained evidence and is outside the supported application upgrade path.

The Web state volume stores credentials, external Kodi registrations and
policies, reviewed runtime acquisition metadata, and external development
state. The broker's separate managed volume stores the current profile,
private workspaces, snapshots, approved packages, the active lease and the last
operation. See the [managed-v2 install notes](../docs/MANAGED-V2-FRESH-INSTALL.md)
for the backup and recovery boundaries.
