"""Remote-Web context excludes local caches without dropping fixture resources."""

from __future__ import annotations

import tarfile
from pathlib import Path

from docker.utils.build import tar  # type: ignore[import-untyped]


def test_source_allowlist_does_not_reinclude_python_caches(tmp_path: Path) -> None:
    root = Path(__file__).parents[2]
    rules = (
        (root / "runtime/remote-web/Dockerfile.dockerignore").read_text().splitlines()
    )
    patterns = [line for line in rules if line and not line.startswith("#")]
    required = {
        "README.md",
        "LICENSE",
        "THIRD_PARTY_NOTICES.md",
        "bridge/service.kodi_lab_bridge/LICENSE",
        "bridge/service.kodi_lab_bridge/README.md",
        "bridge/service.kodi_lab_bridge/addon.xml",
        "bridge/service.kodi_lab_bridge/narrow_bridge.py",
        "bridge/service.kodi_lab_bridge/ui_observation.py",
        "bridge/service.kodi_lab_bridge/onboarding.py",
        "bridge/service.kodi_lab_bridge/pairing_ui.py",
        "bridge/service.kodi_lab_bridge/service.py",
        "bridge/service.kodi_lab_bridge/resources/settings.xml",
        "bridge/service.kodi_lab_bridge/resources/language/resource.language.en_gb/strings.po",
        "pyproject.toml",
        "uv.lock",
        "migrations/env.py",
        "migrations/versions/example.py",
        "src/kodi_lab/__init__.py",
        "src/kodi_lab/web/templates/login.html",
        "src/kodi_lab/web/static/style.css",
        "src/kodi_lab/runtime/recipes/example.json",
        "src/kodi_lab/fixtures/example/manifest.json",
        "src/kodi_lab/fixtures/example/payload/MyVideos131.db",
        "src/kodi_lab/fixtures/example/payload/poster.png",
        "src/kodi_lab/fixtures/example/payload/movie.mp4",
        "runtime/remote-web/launch.sh",
    }
    excluded = {
        ".env",
        ".env.local",
        ".git/config",
        ".venv/local.py",
        "build/output.tar",
        "bridge/unreviewed/kodi.bin",
        "bridge/other-service/addon.xml",
        "bridge/service.kodi_lab_bridge/kodi.bin",
        "bridge/service.kodi_lab_bridge/arbitrary.zip",
        "bridge/service.kodi_lab_bridge/resources/unreviewed.zip",
        "bridge/service.kodi_lab_bridge/resources/language/unreviewed.py",
        "bridge/service.kodi_lab_bridge/__pycache__/service.pyc",
        "migrations/__pycache__/env.cpython-312.pyc",
        "migrations/versions/__pycache__/example.cpython-312.pyc",
        "src/kodi_lab/__pycache__/__init__.cpython-312.pyc",
        "src/kodi_lab/application/__pycache__/service.cpython-312.pyc",
        "src/kodi_lab/standalone.pyc",
        "src/kodi_lab/standalone.pyo",
        "src/kodi_lab/standalone.pyd",
        "src/kodi_lab/.mypy_cache/cache.json",
        "src/kodi_lab/.pytest_cache/v/cache/nodeids",
        "src/kodi_lab/.ruff_cache/cache",
        "migrations/.pytest_cache/v/cache/nodeids",
    }
    for relative in required | excluded:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"source or local artifact")

    # Exercise Docker SDK context tar creation and ordered negation rules, rather
    # than merely checking that particular pattern strings occur in the file.
    with (
        tar(str(tmp_path), exclude=patterns) as context,
        tarfile.open(fileobj=context) as archive,
    ):
        members = set(archive.getnames())
    assert required <= members
    assert not excluded & members
