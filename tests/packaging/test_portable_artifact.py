"""Installed-resource checks independent of the source checkout."""

from __future__ import annotations

import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path
from zipfile import ZipFile

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory


@pytest.fixture(scope="module")
def built_wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = Path(__file__).parents[2]
    output = tmp_path_factory.mktemp("portable-wheel")
    subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--wheel",
            "--no-isolation",
            "--outdir",
            str(output),
        ],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return next(output.glob("kodi_lab-*.whl"))


def test_wheel_contains_current_web_migrations_and_cli(built_wheel: Path) -> None:
    root = Path(__file__).parents[2]
    with ZipFile(built_wheel) as archive:
        names = set(archive.namelist())
        metadata = archive.read("kodi_lab-1.0.0rc1.dist-info/METADATA")
        assert b"License-Expression: MIT" in metadata
        assert b"License-File: LICENSE" in metadata
        assert "kodi_lab-1.0.0rc1.dist-info/licenses/LICENSE" in names
        for path in (
            "kodi_lab/migrations/env.py",
            "kodi_lab/migrations/README.md",
            "kodi_lab/migrations/script.py.mako",
            "kodi_lab/web/templates/login.html",
            "kodi_lab/web/templates/pair-kodi.html",
            "kodi_lab/web/static/pair-kodi.js",
            "kodi_lab/web/static/style.css",
            "kodi_lab/admin_cli.py",
            "kodi_lab/host_bootstrap.py",
            "kodi_lab/remote_lifecycle.py",
            "kodi_lab/storage/packaged_migrations.py",
            "kodi_lab-1.0.0rc1.dist-info/entry_points.txt",
        ):
            assert path in names
        source_revisions = {
            path.name for path in (root / "migrations/versions").glob("*.py")
        }
        wheel_revisions = {
            Path(name).name
            for name in names
            if name.startswith("kodi_lab/migrations/versions/") and name.endswith(".py")
        }
        assert wheel_revisions == source_revisions
        for kind in ("templates", "static"):
            source_assets = {
                path.relative_to(root / "src/kodi_lab/web").as_posix()
                for path in (root / "src/kodi_lab/web" / kind).rglob("*")
                if path.is_file()
            }
            wheel_assets = {
                name.removeprefix("kodi_lab/web/")
                for name in names
                if name.startswith(f"kodi_lab/web/{kind}/")
            }
            assert wheel_assets == source_assets
        assert (
            archive.read("kodi_lab/web/static/pair-kodi.js")
            == (root / "src/kodi_lab/web/static/pair-kodi.js").read_bytes()
        )
        assert (
            archive.read("kodi_lab/web/templates/pair-kodi.html")
            == (root / "src/kodi_lab/web/templates/pair-kodi.html").read_bytes()
        )
        entry_points = archive.read(
            "kodi_lab-1.0.0rc1.dist-info/entry_points.txt"
        ).decode()
        assert "kodi-lab-admin = kodi_lab.admin_cli:main" in entry_points
        assert "kodi-lab-web = kodi_lab.web.run:main" in entry_points


def test_packaged_migration_from_other_cwd_is_fresh_and_idempotent(
    built_wheel: Path, tmp_path: Path
) -> None:
    config = Config()
    config.set_main_option(
        "script_location", str(Path(__file__).parents[2] / "migrations")
    )
    head = ScriptDirectory.from_config(config).get_current_head()
    assert head is not None
    extracted = tmp_path / "installed"
    extracted.mkdir()
    with ZipFile(built_wheel) as archive:
        archive.extractall(extracted)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / ".env").write_text("KODI_LAB_DATA_ROOT=/wrong/cwd\n")
    state = tmp_path / "state"
    environment = os.environ.copy()
    environment.update(
        PYTHONPATH=str(extracted),
        KODI_LAB_DATA_ROOT=str(state),
    )
    for _ in range(2):
        result = subprocess.run(
            [sys.executable, "-m", "kodi_lab.admin_cli", "migrate"],
            cwd=outside,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
        assert f"current={head}" in result.stdout
        assert f"head={head}" in result.stdout
    database = state / "database" / "kodi-lab.db"
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == (head,)
    assert (state / "secrets" / "external-targets").is_dir()
    assert (state / "secrets" / "external-bridges").is_dir()

    older = tmp_path / "older-state"
    older_environment = {**environment, "KODI_LAB_DATA_ROOT": str(older)}
    earlier_revision_script = "\n".join(
        (
            "from alembic import command",
            "from kodi_lab.config import Settings",
            "from kodi_lab.host_bootstrap import initialize_state",
            "from kodi_lab.storage.packaged_migrations import packaged_config",
            "settings = Settings(_env_file=None)",
            "initialize_state(settings.data_root)",
            "with packaged_config(settings.database_url) as config:",
            "    command.upgrade(config, '0052_production_policy_revision_binding')",
        )
    )
    subprocess.run(
        [
            sys.executable,
            "-c",
            earlier_revision_script,
        ],
        cwd=outside,
        env=older_environment,
        check=True,
        capture_output=True,
        text=True,
    )
    upgraded = subprocess.run(
        [sys.executable, "-m", "kodi_lab.admin_cli", "migrate"],
        cwd=outside,
        env=older_environment,
        check=True,
        capture_output=True,
        text=True,
    )
    assert f"current={head}" in upgraded.stdout

    unversioned = tmp_path / "unversioned-state"
    unversioned.mkdir(mode=0o700)
    (unversioned / "database").mkdir(mode=0o700)
    with sqlite3.connect(unversioned / "database" / "kodi-lab.db") as connection:
        connection.execute("CREATE TABLE unrelated (id INTEGER)")
    rejected = subprocess.run(
        [sys.executable, "-m", "kodi_lab.admin_cli", "migrate"],
        cwd=outside,
        env={**environment, "KODI_LAB_DATA_ROOT": str(unversioned)},
        capture_output=True,
        text=True,
    )
    assert rejected.returncode != 0
    assert "existing database has no Alembic revision" in rejected.stderr
    with sqlite3.connect(unversioned / "database" / "kodi-lab.db") as connection:
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall() == [("unrelated",)]

    identity = subprocess.run(
        [sys.executable, "-m", "kodi_lab.admin_cli", "version"],
        cwd=outside,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    assert re.fullmatch(
        r"Kodi Lab 1\.0\.0rc1 package-sha256=[0-9a-f]{64}\n", identity.stdout
    )
