# Database migrations

Alembic manages forward SQLite schema migrations. The initial revision creates
the core target and managed-instance tables. Create later revisions only
alongside the corresponding domain/storage change.
