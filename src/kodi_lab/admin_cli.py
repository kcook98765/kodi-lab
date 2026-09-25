"""Explicit installed-host bootstrap and local administrator operations."""

from __future__ import annotations

import argparse
import getpass
import os
from collections.abc import Sequence

from alembic.util.exc import CommandError
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from kodi_lab.auth.passwords import InvalidPasswordError
from kodi_lab.auth.service import WebAuthenticationService
from kodi_lab.build_identity import installed_identity
from kodi_lab.config import Settings, safe_settings_error
from kodi_lab.host_bootstrap import (
    HostBootstrapError,
    explicit_data_root,
    generate_master_key,
    initialize_state,
)
from kodi_lab.remote_lifecycle import (
    backup as remote_backup,
)
from kodi_lab.remote_lifecycle import (
    initialize as remote_initialize,
)
from kodi_lab.remote_lifecycle import (
    lifecycle_lock,
)
from kodi_lab.remote_lifecycle import (
    recover_clean as remote_recover_clean,
)
from kodi_lab.remote_lifecycle import (
    restore as remote_restore,
)
from kodi_lab.remote_lifecycle import (
    upgrade as remote_upgrade,
)
from kodi_lab.remote_lifecycle import (
    validate as remote_validate,
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
    subcommands.add_parser(
        "initialize", help="initialize genuinely empty remote state once"
    )
    subcommands.add_parser(
        "backup", help="create a verified quiescent remote-state backup"
    )
    subcommands.add_parser("upgrade", help="back up and migrate stopped remote state")
    restore_parser = subcommands.add_parser(
        "restore", help="restore the matching pre-upgrade state"
    )
    restore_parser.add_argument("backup_id")
    restore_parser.add_argument("--confirm", required=True, metavar="BACKUP_ID")
    recover_parser = subcommands.add_parser(
        "recover-clean",
        help="restore a verified application backup into a fresh volume",
    )
    recover_parser.add_argument("backup_id")
    recover_parser.add_argument("--confirm", required=True, metavar="BACKUP_ID")
    subcommands.add_parser("serve", help=argparse.SUPPRESS)
    arguments = parser.parse_args(argv)
    if arguments.command == "version":
        print(installed_identity())
        return 0
    try:
        data_root = explicit_data_root()
        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        if settings.data_root != data_root:
            raise HostBootstrapError("configured data root does not match environment")
        if arguments.command == "serve":
            if not (data_root / ".state.json").is_file():
                raise HostBootstrapError(
                    "application state is not initialized; run "
                    "kodi-lab-admin initialize before starting Web"
                )
            with lifecycle_lock(data_root) as lock_fd:
                remote_validate(
                    data_root,
                    settings.database_url,
                    require_head=True,
                    require_marker=True,
                )
                os.environ["KODI_LAB_EXTERNAL_TARGET_MASTER_KEY"] = (
                    (data_root / "master-key").read_text(encoding="ascii").strip()
                )
                os.set_inheritable(lock_fd, True)
                os.execvp("kodi-lab-web", ["kodi-lab-web"])
        if arguments.command == "initialize":
            revision = remote_initialize(data_root, settings.database_url)
            print(f"Kodi Lab remote state initialized at revision {revision}.")
            return 0
        if arguments.command == "backup":
            with lifecycle_lock(data_root):
                identifier = remote_backup(data_root, settings.database_url)
            print(f"Kodi Lab remote backup complete: {identifier}")
            return 0
        if arguments.command == "upgrade":
            previous, current, upgrade_backup_id = remote_upgrade(
                data_root, settings.database_url
            )
            backup_label = upgrade_backup_id or "none (already at head)"
            print(f"Kodi Lab remote upgrade: {previous} -> {current}")
            print(f"Pre-upgrade backup: {backup_label}")
            return 0
        if arguments.command == "restore":
            revision, quarantine = remote_restore(
                data_root, settings.database_url, arguments.backup_id, arguments.confirm
            )
            print(f"Kodi Lab remote state restored to {revision}.")
            print(f"Newer state quarantined as {quarantine}.")
            return 0
        if arguments.command == "recover-clean":
            revision = remote_recover_clean(
                data_root, settings.database_url, arguments.backup_id, arguments.confirm
            )
            print(f"Kodi Lab clean-volume application state recovered at {revision}.")
            return 0
        if arguments.command == "generate-master-key":
            if os.environ.get("KODI_LAB_REMOTE_CONTAINER"):
                raise HostBootstrapError(
                    "remote container key creation is part of initialize"
                )
            generate_master_key(data_root)
            print("Kodi Lab master key created in the private data root.")
            return 0
        if arguments.command == "migrate":
            if os.environ.get("KODI_LAB_REMOTE_CONTAINER"):
                raise HostBootstrapError(
                    "remote container migration requires initialize or upgrade"
                )
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
    except ValidationError as error:
        parser.error(safe_settings_error(error))
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
