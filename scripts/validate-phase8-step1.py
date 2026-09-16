#!/usr/bin/env python3
"""Run the isolated Phase 8 Step 1 local-source catalog validation."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import text

from kodi_lab.application import DevelopmentSourceAdministrationService
from kodi_lab.config import Settings
from kodi_lab.development import (
    MAX_ADDON_XML_BYTES,
    DevelopmentSourceValidationError,
    DevelopmentSourceValidator,
)
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.storage.repositories import DevelopmentSourceRepository


def addon_xml(*, addon_id: str, name: str, version: str) -> str:
    return (
        f'<addon id="{addon_id}" name="{name}" version="{version}" '
        'provider-name="Kodi Lab Validation">'
        '<extension point="xbmc.python.pluginsource" />'
        '<extension point="xbmc.service" />'
        "</addon>"
    )


def write_source(
    root: Path, locator: str, *, addon_id: str, name: str, version: str
) -> Path:
    source = root.joinpath(*locator.split("/"))
    source.mkdir(parents=True, exist_ok=True)
    (source / "addon.xml").write_text(
        addon_xml(addon_id=addon_id, name=name, version=version), encoding="utf-8"
    )
    return source


def rejection(validator: DevelopmentSourceValidator, locator: str) -> str:
    try:
        validator.validate(locator)
    except DevelopmentSourceValidationError as error:
        return error.code.value
    raise RuntimeError(f"validation unexpectedly accepted {locator!r}")


def main() -> None:
    project_root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(
        prefix="kodi-lab-phase8-step1-", dir="/var/tmp"
    ) as temporary:
        validation_root = Path(temporary)
        development_root = validation_root / "development"
        development_root.mkdir(mode=0o700)
        data_root = validation_root / "data"
        (data_root / "database").mkdir(parents=True)
        os.environ["KODI_LAB_DATA_ROOT"] = str(data_root)
        os.environ["KODI_LAB_DEVELOPMENT_ROOT"] = str(development_root)

        settings = Settings()
        if settings.development_root != development_root:
            raise RuntimeError("configured development-root identity mismatch")
        config = Config(str(project_root / "alembic.ini"))
        command.upgrade(config, "head")
        engine = create_database_engine(settings.database_url)
        factory = create_session_factory(engine)
        validator = DevelopmentSourceValidator(settings.development_root)
        catalog = DevelopmentSourceAdministrationService(factory, validator)

        source_a = write_source(
            development_root,
            "video/source-a",
            addon_id="plugin.video.kodi-lab-step1",
            name="Kodi Lab Step 1",
            version="1.0.0",
        )
        registered = catalog.register_source(
            logical_name="Source A", relative_locator="video/source-a"
        )
        with factory() as session:
            persisted = DevelopmentSourceRepository(session).get(registered.id)
            migration_head = session.scalar(
                text("SELECT version_num FROM alembic_version")
            )
        if persisted is None:
            raise RuntimeError("registered source was not persisted")
        if (
            catalog.list_sources() != [registered]
            or catalog.get_source(registered.id) != registered
        ):
            raise RuntimeError("catalog list/get proof failed")

        outside = validation_root / "outside-source"
        write_source(
            validation_root,
            "outside-source",
            addon_id="plugin.video.outside",
            name="Outside",
            version="1.0.0",
        )
        (development_root / "outside-link").symlink_to(
            outside, target_is_directory=True
        )
        (development_root / "source-root-link").symlink_to(
            source_a, target_is_directory=True
        )
        confinement = {
            "absolute": rejection(validator, str(source_a)),
            "parent_escape": rejection(validator, "../outside-source"),
            "outside_symlink": rejection(validator, "outside-link"),
            "source_root_symlink": rejection(validator, "source-root-link"),
        }

        malformed = development_root / "metadata-malformed"
        malformed.mkdir()
        (malformed / "addon.xml").write_text("<addon", encoding="utf-8")
        unsafe = development_root / "metadata-unsafe"
        unsafe.mkdir()
        (unsafe / "addon.xml").write_text(
            '<!DOCTYPE addon [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
            '<addon id="plugin.video.unsafe" name="&xxe;" version="1">'
            '<extension point="xbmc.service" /></addon>',
            encoding="utf-8",
        )
        invalid_id = development_root / "metadata-invalid-id"
        invalid_id.mkdir()
        (invalid_id / "addon.xml").write_text(
            addon_xml(addon_id="../escape", name="Invalid", version="1"),
            encoding="utf-8",
        )
        oversized = development_root / "metadata-oversized"
        oversized.mkdir()
        (oversized / "addon.xml").write_bytes(b"x" * (MAX_ADDON_XML_BYTES + 1))
        metadata_safety = {
            "malformed": rejection(validator, "metadata-malformed"),
            "unsafe_construct": rejection(validator, "metadata-unsafe"),
            "invalid_addon_id": rejection(validator, "metadata-invalid-id"),
            "oversized": rejection(validator, "metadata-oversized"),
        }

        (source_a / "addon.xml").write_text(
            addon_xml(
                addon_id="plugin.video.kodi-lab-step1",
                name="Kodi Lab Step 1 Changed",
                version="2.0.0",
            ),
            encoding="utf-8",
        )
        current = validator.validate("video/source-a")
        retained = catalog.get_source(registered.id)
        if (
            retained.observed_name != "Kodi Lab Step 1"
            or retained.observed_version != "1.0.0"
        ):
            raise RuntimeError("mutable source silently changed catalog observation")
        if (
            current.observed_name != "Kodi Lab Step 1 Changed"
            or current.observed_version != "2.0.0"
        ):
            raise RuntimeError(
                "read-only revalidation did not observe current metadata"
            )

        root_metadata = development_root.stat()
        evidence = {
            "schema": "kodi-lab-phase8-step1-live-v1",
            "validation_root_identity": {
                "description": "dedicated temporary development root",
                "device": root_metadata.st_dev,
                "inode": root_metadata.st_ino,
                "uid": root_metadata.st_uid,
                "gid": root_metadata.st_gid,
                "mode": oct(stat.S_IMODE(root_metadata.st_mode)),
            },
            "source_a": {
                "registration_uuid": str(registered.id),
                "relative_locator": persisted.relative_locator,
                "addon_id": registered.addon_id,
                "observed_name": registered.observed_name,
                "observed_version": registered.observed_version,
                "extension_types": list(registered.extension_types),
                "deterministic_list_get": True,
            },
            "confinement_rejections": confinement,
            "metadata_rejections": metadata_safety,
            "mutable_source": {
                "stored_name": retained.observed_name,
                "stored_version": retained.observed_version,
                "revalidated_name": current.observed_name,
                "revalidated_version": current.observed_version,
                "catalog_unchanged": True,
            },
            "migration_head": migration_head,
            "temporary_root_removed_after_validation": True,
        }
        print(json.dumps(evidence, indent=2, sort_keys=True))
        engine.dispose()


if __name__ == "__main__":
    main()
