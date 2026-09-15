"""Narrow local administrator password bootstrap command."""

from __future__ import annotations

import argparse
import getpass
from collections.abc import Sequence

from kodi_lab.auth.passwords import InvalidPasswordError
from kodi_lab.auth.service import WebAuthenticationService
from kodi_lab.config import Settings
from kodi_lab.storage.engine import create_database_engine, create_session_factory


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kodi-lab-admin")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser(
        "set-password", help="set or replace the single local administrator password"
    )
    arguments = parser.parse_args(argv)
    if arguments.command != "set-password":
        parser.error("unsupported command")

    password = getpass.getpass("New Kodi Lab administrator password: ")
    confirmation = getpass.getpass("Confirm password: ")
    if not secrets_match(password, confirmation):
        parser.error("passwords do not match")

    settings = Settings()
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
