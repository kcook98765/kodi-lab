# Database migrations

Alembic manages forward SQLite schema migrations. The initial revision creates
the core target and managed-instance tables. Create later revisions only
alongside the corresponding domain/storage change.

Revision `0002_runtime_catalog` adds runtime definitions/revisions and replaces
the free-form managed-instance revision field with a restrictive revision UUID
foreign key. It intentionally refuses populated `0001` development databases
because their free-form values cannot supply trustworthy catalog provenance.
