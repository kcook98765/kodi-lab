# Database migrations

Alembic manages forward SQLite schema migrations. Phase 0 intentionally has no
schema revision: domain tables begin in Phase 1. Create revisions only alongside
the corresponding domain/storage change.
