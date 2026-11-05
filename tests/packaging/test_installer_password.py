"""Installer stages consume the real interactive CLI outcome without restarting."""

from __future__ import annotations

import getpass
import json
import subprocess
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab import admin_cli
from kodi_lab.auth.service import WebAuthenticationService
from scripts import install
from tests.auth.test_admin_password import password_state as isolated_password_state
from tests.packaging.test_host_installer import Host
from tests.packaging.test_host_installer import host as simulated_host
from tests.packaging.test_host_installer import mcp_opener as simulated_mcp_opener

password_state = isolated_password_state
host = simulated_host
mcp_opener = simulated_mcp_opener


@pytest.mark.parametrize("kind", ["ubuntu", "windows-wsl2"])
@pytest.mark.parametrize("outcome", ["retry", "policy", "cancel", "exhausted"])
def test_password_recovery_and_failure_receipt(
    host: Host,
    password_state: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    outcome: str,
) -> None:
    host.kind = kind
    answers = iter(
        ["typo", "other", "accepted", "accepted"]
        if outcome == "retry"
        else ["", "", "accepted", "accepted"]
        if outcome == "policy"
        else ["typo", "other"] * 3
    )

    def prompt(_: str) -> str:
        if outcome == "cancel":
            raise EOFError
        return next(answers)

    monkeypatch.setattr(getpass, "getpass", prompt)
    run = host.run

    def invoke(command: list[str], cwd: Path, *, capture: bool = False) -> str:
        result = run(command, cwd, capture=capture)
        if "set-password" in command:
            assert (
                json.loads((host.root / "INSTALLATION.json").read_text())[
                    "completed_stage"
                ]
                == "database_initialized"
            )
            try:
                assert admin_cli.main(["set-password"]) == 0
            except SystemExit as error:
                assert isinstance(error.code, int)
                raise subprocess.CalledProcessError(error.code, command) from None
        return result

    monkeypatch.setattr(install, "run", invoke)
    if outcome in {"cancel", "exhausted"}:
        with pytest.raises(subprocess.CalledProcessError):
            install.install(host.args())
        expected = "database_initialized"
        assert not WebAuthenticationService(password_state).credentials_configured()
        assert all("up" not in command for command in host.commands)
    else:
        install.install(host.args())
        expected = "ready"
        assert WebAuthenticationService(password_state).login("accepted") is not None
    assert (
        json.loads((host.root / "INSTALLATION.json").read_text())["completed_stage"]
        == expected
    )
    assert sum("set-password" in command for command in host.commands) == 1
