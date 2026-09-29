"""Run explicitly selected development tests before broader release checks."""

from __future__ import annotations

import argparse
import os
import subprocess
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parent.parent
MIN_SHM_FREE_BYTES = 1_073_741_824


def _selection(value: str, *, tests_only: bool) -> str:
    path = Path(value)
    if path.is_absolute() or value.startswith("-"):
        raise argparse.ArgumentTypeError("use a relative repository path")
    try:
        resolved = (REPOSITORY / path).resolve(strict=True)
    except OSError as exc:
        raise argparse.ArgumentTypeError(f"path does not exist: {value}") from exc
    boundary = REPOSITORY / "tests" if tests_only else REPOSITORY
    if not resolved.is_relative_to(boundary) or not (
        resolved.is_file() or resolved.is_dir()
    ):
        raise argparse.ArgumentTypeError(f"path is outside the allowed tree: {value}")
    if not tests_only and resolved.is_file() and resolved.suffix != ".py":
        raise argparse.ArgumentTypeError(f"source path must be Python: {value}")
    return value


def _test_path(value: str) -> str:
    return _selection(value, tests_only=True)


def _source_path(value: str) -> str:
    return _selection(value, tests_only=False)


def _temp_parent() -> str | None:
    shm = Path("/dev/shm")
    if shm.is_dir() and os.access(shm, os.W_OK):
        stats = os.statvfs(shm)
        if stats.f_bavail * stats.f_frsize >= MIN_SHM_FREE_BYTES:
            return str(shm)
    return None


@contextmanager
def _pytest_temp() -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="klp.", dir=_temp_parent()) as root:
        try:
            yield Path(root) / "pytest"
        finally:
            # Some safety tests deliberately make their private directories read-only.
            subprocess.run(["chmod", "-R", "u+rwX", "--", root], check=False)


def _run(label: str, command: list[str], *, env: dict[str, str] | None = None) -> None:
    print(f"\n==> {label}", flush=True)
    started = time.monotonic()
    result = subprocess.run(command, cwd=REPOSITORY, env=env, check=False)
    print(f"<== {label}: {time.monotonic() - started:.1f}s ({result.returncode})")
    if result.returncode:
        raise SystemExit(result.returncode)


def _focused(tests: list[str], sources: list[str]) -> None:
    with _pytest_temp() as basetemp:
        for selected in tests:
            _run(
                f"pytest {selected}",
                [
                    "uv",
                    "run",
                    "--locked",
                    "pytest",
                    "--basetemp",
                    str(basetemp),
                    selected,
                ],
            )
    paths = list(dict.fromkeys([*tests, *sources]))
    _run("Ruff format", ["uv", "run", "--locked", "ruff", "format", "--check", *paths])
    _run("Ruff lint", ["uv", "run", "--locked", "ruff", "check", *paths])
    mypy_env = os.environ.copy()
    mypy_paths = [str(REPOSITORY / "src"), mypy_env.get("MYPYPATH", "")]
    mypy_env["MYPYPATH"] = os.pathsep.join(path for path in mypy_paths if path)
    _run("mypy", ["uv", "run", "--locked", "mypy", *paths], env=mypy_env)


def main() -> None:
    if os.geteuid() == 0:
        raise SystemExit("Kodi Lab development checks must run as a non-root user.")
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="tier", required=True)
    focused = commands.add_parser(
        "focused", help="run each selected test path separately"
    )
    focused.add_argument("tests", type=_test_path, nargs="+")
    focused.add_argument("--source", type=_source_path, nargs="*", default=[])
    integration = commands.add_parser(
        "integration", help="run focused checks, then selected interaction tests"
    )
    integration.add_argument("--focused", type=_test_path, nargs="+", required=True)
    integration.add_argument("--integration", type=_test_path, nargs="+", required=True)
    integration.add_argument("--source", type=_source_path, nargs="*", default=[])
    args = parser.parse_args()
    os.chdir(REPOSITORY)
    started = time.monotonic()
    if args.tier == "focused":
        _focused(args.tests, args.source)
    else:
        _focused(args.focused, args.source)
        with _pytest_temp() as basetemp:
            _run(
                "integration pytest",
                [
                    "uv",
                    "run",
                    "--locked",
                    "pytest",
                    "--basetemp",
                    str(basetemp),
                    *args.integration,
                ],
            )
    print(f"\n{args.tier.upper()} PASS in {time.monotonic() - started:.1f}s")


if __name__ == "__main__":
    main()
