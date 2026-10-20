"""Host listener diagnosis checks the configured bind without rewriting it."""

import errno
import runpy
import socket
from pathlib import Path

import pytest


def test_listener_preflight_accepts_actual_loopback() -> None:
    tool = runpy.run_path(str(Path(__file__).parents[2] / "scripts/linux-v1-bundle"))
    tool["preflight_repository_listener"]("127.0.0.1")


def test_missing_private_bind_reports_recovery_without_inventing_an_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool = runpy.run_path(str(Path(__file__).parents[2] / "scripts/linux-v1-bundle"))
    calls = []

    class Missing:
        def __enter__(self) -> "Missing":
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def bind(self, address: tuple[str, int]) -> None:
            calls.append(address)
            raise OSError(errno.EADDRNOTAVAIL, "unavailable")

    monkeypatch.setattr(socket, "socket", lambda *args: Missing())
    with pytest.raises(
        ValueError,
        match=r"Repository listener address is unavailable.*DHCP/reservation",
    ):
        tool["preflight_repository_listener"]("192.168.50.10")
    assert calls == [("192.168.50.10", 0)]


@pytest.mark.parametrize("address", ["0.0.0.0", "8.8.8.8", "::1"])
def test_listener_preflight_rejects_a_broader_or_unrelated_bind(address: str) -> None:
    tool = runpy.run_path(str(Path(__file__).parents[2] / "scripts/linux-v1-bundle"))
    with pytest.raises(ValueError, match="explicit private or loopback"):
        tool["preflight_repository_listener"](address)
