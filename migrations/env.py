import sqlite3
from logging.config import fileConfig
from typing import cast

from alembic import context
from alembic.script import ScriptDirectory
from sqlalchemy import engine_from_config, pool

from kodi_lab.config import Settings
from kodi_lab.storage import metadata
from kodi_lab.storage.schema_compatibility import check_database

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

if not config.get_main_option("sqlalchemy.url"):
    config.set_main_option("sqlalchemy.url", Settings().database_url)
target_metadata = metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.begin() as connection:
        # sqlite3's legacy transaction mode does not begin a transaction for
        # DDL. Keep all revision steps and version writes in one transaction.
        connection.exec_driver_sql("BEGIN")
        check_database(
            cast(sqlite3.Connection, connection.connection.driver_connection),
            {
                revision.revision
                for revision in ScriptDirectory.from_config(config).walk_revisions()
            },
        )
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
