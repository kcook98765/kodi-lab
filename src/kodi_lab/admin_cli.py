"""Explicit installed-host bootstrap and local administrator operations."""

from __future__ import annotations

import argparse
import getpass
import os
from collections.abc import Sequence

from alembic.util.exc import CommandError
from sqlalchemy.exc import SQLAlchemyError

from kodi_lab.auth.passwords import InvalidPasswordError
from kodi_lab.auth.service import WebAuthenticationService
from kodi_lab.build_identity import installed_identity
from kodi_lab.config import Settings
from kodi_lab.host_bootstrap import (
    HostBootstrapError,
    explicit_data_root,
    generate_master_key,
    initialize_state,
)
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.storage.packaged_migrations import migrate_database


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kodi-lab-admin")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser(
        "set-password", help="set or replace the single local administrator password"
    )
    subcommands.add_parser("migrate", help="initialize or advance the database")
    subcommands.add_parser(
        "generate-master-key", help="write a new private master-key file once"
    )
    subcommands.add_parser("version", help="show installed version and package digest")
    arguments = parser.parse_args(argv)
    if arguments.command == "version":
        print(installed_identity())
        return 0
    try:
        data_root = explicit_data_root()
        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        if settings.data_root != data_root:
            raise HostBootstrapError("configured data root does not match environment")
        if arguments.command == "generate-master-key":
            generate_master_key(data_root)
            print("Kodi Lab master key created in the private data root.")
            return 0
        if arguments.command == "migrate":
            previous_umask = os.umask(0o077)
            try:
                initialize_state(data_root)
                current, head = migrate_database(
                    data_root / "database" / "kodi-lab.db", settings.database_url
                )
            finally:
                os.umask(previous_umask)
            print(f"Kodi Lab database revision: current={current} head={head}")
            return 0
    except (CommandError, SQLAlchemyError, OSError, RuntimeError, ValueError) as error:
        parser.error(str(error))

    if arguments.command != "set-password":
        parser.error("unsupported command")
    if not (data_root / "database" / "kodi-lab.db").is_file():
        parser.error("database is missing; run kodi-lab-admin migrate first")

    password = getpass.getpass("New Kodi Lab administrator password: ")
    confirmation = getpass.getpass("Confirm password: ")
    if not secrets_match(password, confirmation):
        parser.error("passwords do not match")

    engine = create_database_engine(settings.database_url)
    try:
        WebAuthenticationService(create_session_factory(engine)).set_password(password)
    except InvalidPasswordError as error:
        parser.error(str(error))
    finally:
        engine.dispose()
    print("Kodi Lab administrator password updated; existing sessions invalidated.")
    return 0


def secrets_match(left: str, right: str) -> bool:
    import secrets

    return secrets.compare_digest(left, right)


if __name__ == "__main__":
    raise SystemExit(main())
