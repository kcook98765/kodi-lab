"""Downloaded installers obtain one exact source checkout before initialization."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import install


@pytest.fixture
def prerequisites(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "applications"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setattr(install, "host_kind", lambda: "ubuntu")
    monkeypatch.setattr(install, "engine", lambda *_: "engine-one")
    monkeypatch.setattr(install, "check_networks", lambda *_: None)
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/" + name)


def arguments(tmp_path: Path, revision: str) -> argparse.Namespace:
    return argparse.Namespace(
        directory=str(tmp_path / "installation"),
        revision=revision,
        web_port=18100,
        repository_port=18120,
        repository_local_port=18121,
        repository_bind="192.168.50.20",
        repository_base_url="http://lab.example:18120",
    )


@pytest.mark.usefixtures("prerequisites")
def test_pinned_local_git_checkout_runs_the_selected_installer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "fixture-source"
    (source / "scripts").mkdir(parents=True)
    result_path = tmp_path / "installer-result.json"
    monkeypatch.setenv("BOOTSTRAP_TEST_RESULT", str(result_path))
    (source / "scripts/install.py").write_text(
        "import json,os,pathlib,sys\n"
        "pathlib.Path(os.environ['BOOTSTRAP_TEST_RESULT']).write_text("
        "json.dumps({'argv':sys.argv,'cwd':os.getcwd()}))\n"
    )
    subprocess.run(["git", "init", "--quiet", str(source)], check=True)
    subprocess.run(["git", "add", "."], cwd=source, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "--quiet",
            "-m",
            "Fixture installer",
        ],
        cwd=source,
        check=True,
    )
    revision = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=source, text=True
    ).strip()
    monkeypatch.setattr(install, "SOURCE_REPOSITORY", source.as_uri())
    install.bootstrap(arguments(tmp_path, revision))
    result = json.loads(result_path.read_text())
    checkout = Path(result["cwd"])
    assert checkout.is_relative_to(tmp_path / "applications/kodi-lab/sources")
    assert (
        subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=checkout, text=True
        ).strip()
        == revision
    )
    assert result["argv"][1:4] == [
        "install",
        "--directory",
        str(tmp_path / "installation"),
    ]
    assert result["argv"][-4:] == [
        "--repository-bind",
        "192.168.50.20",
        "--repository-base-url",
        "http://lab.example:18120",
    ]
    assert not (tmp_path / "installation").exists()


@pytest.mark.usefixtures("prerequisites")
@pytest.mark.parametrize("revision", ["main", "a" * 40])
def test_fetch_failure_preserves_source_without_running_an_installer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, revision: str
) -> None:
    commands: list[list[str]] = []

    def run(command: list[str], cwd: Path, *, capture: bool = False) -> str:
        commands.append(command)
        if "fetch" in command:
            raise subprocess.CalledProcessError(1, command)
        return ""

    monkeypatch.setattr(install, "run", run)
    with pytest.raises(subprocess.CalledProcessError):
        install.bootstrap(arguments(tmp_path, revision))
    assert len(list((tmp_path / "applications/kodi-lab/sources").iterdir())) == 1
    assert all(command[0] == "git" for command in commands)
    assert not (tmp_path / "installation").exists()


@pytest.mark.usefixtures("prerequisites")
@pytest.mark.parametrize(
    "revision", ["latest", "../main", "-upload-pack=other", "A" * 40, "abc;command"]
)
def test_invalid_revision_has_no_filesystem_or_process_effects(
    tmp_path: Path, revision: str
) -> None:
    with pytest.raises(ValueError, match="revision"):
        install.bootstrap(arguments(tmp_path, revision))
    assert not (tmp_path / "applications").exists()


@pytest.mark.usefixtures("prerequisites")
def test_existing_installation_is_never_replaced(tmp_path: Path) -> None:
    destination = tmp_path / "installation"
    destination.mkdir()
    marker = destination / "private-state"
    marker.write_bytes(b"keep")
    with pytest.raises(ValueError, match="new installation directory"):
        install.bootstrap(arguments(tmp_path, "main"))
    assert marker.read_bytes() == b"keep"
    assert not (tmp_path / "applications").exists()


@pytest.mark.usefixtures("prerequisites")
def test_wrong_commit_never_executes_downloaded_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands: list[list[str]] = []

    def run(command: list[str], cwd: Path, *, capture: bool = False) -> str:
        commands.append(command)
        return "b" * 40 if capture else ""

    monkeypatch.setattr(install, "run", run)
    with pytest.raises(ValueError, match="source commit differs"):
        install.bootstrap(arguments(tmp_path, "a" * 40))
    assert all(command[0] == "git" for command in commands)


def test_downloaded_file_is_standalone_and_help_has_no_project_dependency(
    tmp_path: Path,
) -> None:
    script = tmp_path / "downloaded-installer.py"
    script.write_bytes((Path(__file__).parents[2] / "scripts/install.py").read_bytes())
    result = subprocess.run(
        [sys.executable, str(script), "--help"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "bootstrap" in result.stdout
    assert list(tmp_path.iterdir()) == [script]
